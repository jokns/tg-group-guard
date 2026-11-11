"""轻量图片审核：缓存、感知哈希、RapidOCR；按群隔离。"""
import asyncio
import io
import logging
import time
from app.database import get_conn,get_setting
log=logging.getLogger(__name__)
_SEM=asyncio.Semaphore(2)
_OCR=None

def flag(key,default='1'):return get_setting(key,default)=='1'
def mark_observation(chat_id,user_id):
    days=max(0,min(30,int(get_setting('observation_hours','24'))))
    conn=get_conn();conn.execute('INSERT OR REPLACE INTO member_observation VALUES(?,?,?)',(chat_id,user_id,int(time.time())+days*3600));conn.commit();conn.close()
def observing(chat_id,user_id):
    conn=get_conn();r=conn.execute('SELECT until_ts FROM member_observation WHERE chat_id=? AND user_id=?',(chat_id,user_id)).fetchone();conn.close()
    return bool(r and r['until_ts']>time.time())

def _ocr_sync(data, smart=True):
    global _OCR
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image, ImageOps
    from app.handlers.smart_ocr import candidates, crops
    if _OCR is None:_OCR=RapidOCR()
    image=Image.open(io.BytesIO(data))
    image=ImageOps.exif_transpose(image)
    # 解压炸弹与异常大图：限制像素和实际处理尺寸
    if image.width * image.height > 20_000_000:
        raise ValueError('图片像素超过安全上限')
    image=image.convert('RGB')
    image.thumbnail((1400,1400))
    if max(image.size)<800:
        scale=min(4,1400/max(image.size))
        image=image.resize((max(1,int(image.width*scale)),max(1,int(image.height*scale))),Image.Resampling.LANCZOS)
    result,_=_OCR(np.asarray(image))
    original=' '.join(str(item[1]) for item in (result or []) if len(item)>1)
    if not smart:return [original] if original else []
    found=candidates(result)
    if original and original not in found:found.insert(0,original)
    return found[:48],image


def _hash_sync(data):
    from PIL import Image
    import imagehash
    im=Image.open(io.BytesIO(data));
    if im.width*im.height>20_000_000:raise ValueError('图片像素超过安全上限')
    im=im.convert('RGB');im.thumbnail((1200,1200))
    return str(imagehash.phash(im))

def _similar(chat_id,hashstr):
    import imagehash
    now=int(time.time());threshold=max(0,min(16,int(get_setting('image_hash_distance','6'))))
    conn=get_conn()
    rows=conn.execute('SELECT image_hash,is_spam FROM image_fingerprints WHERE chat_id=? AND created_at>? ORDER BY created_at DESC LIMIT 300',(chat_id,now-7*86400)).fetchall()
    conn.close()
    return any(r['is_spam'] and imagehash.hex_to_hash(r['image_hash'])-imagehash.hex_to_hash(hashstr)<=threshold for r in rows)

def _remember(chat_id,hashstr,spam):
    conn=get_conn();conn.execute('INSERT INTO image_fingerprints VALUES(?,?,?,?)',(chat_id,hashstr,int(spam),int(time.time())));conn.commit();conn.close()

async def check_media(update,ctx,chat_id,user_id):
    msg=update.message
    static_sticker=bool(msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video)
    has_image=bool(msg.photo or static_sticker or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('image/')))
    if flag('observation_enabled') and observing(chat_id,user_id):
        if (has_image and flag('observation_block_images')) or (flag('observation_block_links') and ('http://' in (msg.text or msg.caption or '').lower() or 'https://' in (msg.text or msg.caption or '').lower() or 't.me/' in (msg.text or msg.caption or '').lower())):
            return '新成员观察期：禁止图片或链接'
    if not has_image or not (flag('ocr_enabled') or flag('image_hash_enabled')):return ''
    try:
        file_obj=msg.photo[-1] if msg.photo else (msg.sticker if static_sticker else msg.document)
        if file_obj.file_size and file_obj.file_size>5*1024*1024:return ''
        async with _SEM:
            tgfile=await ctx.bot.get_file(file_obj.file_id)
            raw=bytes(await tgfile.download_as_bytearray())
            if len(raw)>5*1024*1024:return ''
            h=await asyncio.to_thread(_hash_sync,raw) if flag('image_hash_enabled') else None
            if h and await asyncio.to_thread(_similar,chat_id,h):return '重复或相似的已知广告图片'
            spam=False;reason=''
            if flag('ocr_enabled'):
                from app.handlers.group_messages import is_spam
                from app.handlers.smart_ocr import normalize,crops,candidates
                smart=flag('smart_ocr_enabled','1')
                if smart:
                    recognized,image=await asyncio.to_thread(_ocr_sync,raw,True)
                else:
                    recognized=await asyncio.to_thread(_ocr_sync,raw,False)
                # 原文优先，随后尝试按坐标拼接的候选；归一化仅对明确的自定义关键词生效
                from app.handlers.group_messages import get_blocked_keywords,record_keyword_hit
                keywords=get_blocked_keywords() if smart else []
                for text in recognized:
                    spam,reason=await asyncio.to_thread(is_spam,text)
                    if not spam and smart:
                        clean=normalize(text)
                        for kw in keywords:
                            if len(normalize(kw))>=3 and normalize(kw) in clean:
                                spam=True;reason='智能拼接关键词：'+kw
                                await asyncio.to_thread(record_keyword_hit,kw)
                                break
                    if spam:break
                if smart and not spam:
                    # 仅在首次未命中时进行最多 4 次分块 OCR，避免常规图片的额外开销
                    def _crop_ocr():
                        out=[]
                        for part in crops(image):
                            try:
                                result,_=_OCR(np.asarray(part))
                                out.extend(candidates(result)[:12])
                            except Exception:
                                log.exception('智能 OCR 分块识别失败')
                        return out[:48]
                    for text in await asyncio.to_thread(_crop_ocr):
                        spam,reason=await asyncio.to_thread(is_spam,text)
                        if not spam:
                            clean=normalize(text)
                            for kw in keywords:
                                if len(normalize(kw))>=3 and normalize(kw) in clean:
                                    spam=True;reason='智能拼接关键词：'+kw
                                    await asyncio.to_thread(record_keyword_hit,kw)
                                    break
                        if spam:break
                if spam:reason='图片 OCR：'+reason
            if h:await asyncio.to_thread(_remember,chat_id,h,spam)
            return reason if spam else ''
    except Exception:
        log.exception('图片审核失败 chat=%s user=%s',chat_id,user_id)
        return ''
