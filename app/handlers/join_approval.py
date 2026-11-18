"""先申请、私聊验证、后批准；SQLite 持久化，支持容器重启。"""
import hashlib
import hmac
import logging
import secrets
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ChatJoinRequestHandler, CommandHandler, CallbackQueryHandler, ContextTypes
from app.database import get_conn, get_setting, is_chat_allowed
from app.math_verification import new_question, cooldown_remaining, start_cooldown
from app.verification_web import verification_mode, verification_url, turnstile_ready, create_challenge

log = logging.getLogger(__name__)
def enabled(): return get_setting('join_approval_enabled','1') == '1'
def timeout(): return max(60,min(900,int(get_setting('join_approval_timeout','300'))))
def _digest(token, answer): return hashlib.sha256(f'{token}:{answer}'.encode()).hexdigest()

def _challenge(chat_id,user_id, preserve_attempts=False):
    question, answer, opts = new_question()
    token=secrets.token_urlsafe(12)
    conn=get_conn()
    conn.execute("""INSERT INTO join_requests(chat_id,user_id,token,answer_hash,expires_at,status,attempts)
      VALUES(?,?,?,?,?,'pending',0) ON CONFLICT(chat_id,user_id) DO UPDATE SET
      token=excluded.token,answer_hash=excluded.answer_hash,expires_at=CASE WHEN ? THEN join_requests.expires_at ELSE excluded.expires_at END,status='pending',attempts=CASE WHEN ? THEN join_requests.attempts ELSE 0 END,verify_mode='math'""", 
      (chat_id,user_id,token,_digest(token,str(answer)),int(time.time())+timeout(),int(preserve_attempts),int(preserve_attempts)))
    conn.commit();conn.close()
    markup=InlineKeyboardMarkup([[InlineKeyboardButton(str(n),callback_data=f'ja:{chat_id}:{token}:{n}')] for n in opts])
    return f'🛡️ 入群申请验证\n请在 {timeout()} 秒内回答：{question} = ?\n验证通过后自动批准入群。',markup

async def on_request(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    req=update.chat_join_request
    if not req or not enabled() or not is_chat_allowed(req.chat.id):return
    remaining = cooldown_remaining(req.chat.id, req.from_user.id)
    if remaining:
        log.warning("Join request in cooldown chat=%s user=%s remaining=%s", req.chat.id, req.from_user.id, remaining)
        try: await ctx.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
        except Exception: log.exception("Could not decline cooldown request")
        return
    # New join request starts a new welcome lifecycle; do not reset on /start.
    conn = get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM welcome_delivery WHERE chat_id=? AND user_id=?",
                         (req.chat.id, req.from_user.id))
    finally:
        conn.close()
    mode = verification_mode()
    if mode == 'cloudflare':
        if not turnstile_ready():
            log.error("Cloudflare mode configured but keys/URL missing; request kept unapproved")
            return
        token = secrets.token_urlsafe(12)
        conn=get_conn()
        with conn:
            conn.execute("""INSERT INTO join_requests(chat_id,user_id,token,answer_hash,expires_at,status,attempts,verify_mode)
                VALUES(?,?,?,'',?,'pending',0,'cloudflare')
                ON CONFLICT(chat_id,user_id) DO UPDATE SET token=excluded.token,answer_hash='',expires_at=excluded.expires_at,status='pending',attempts=0,verify_mode='cloudflare'""",
                (req.chat.id,req.from_user.id,token,int(time.time())+timeout()))
        conn.close()
        challenge=create_challenge(req.chat.id,req.from_user.id,'request',expires_at=int(time.time())+timeout())
        text='🛡️ 入群安全验证\n请点击下方按钮，在 Telegram 内完成 Cloudflare 人机验证。'
        markup=InlineKeyboardMarkup([[InlineKeyboardButton('🌐 打开验证网页',web_app=WebAppInfo(url=f'{verification_url()}/verify?challenge={challenge}'))]])
    else:
        text,markup=_challenge(req.chat.id,req.from_user.id)
    try:
        await ctx.bot.send_message(chat_id=req.user_chat_id,text=text,reply_markup=markup)
    except Exception as exc:
        log.warning('无法主动私聊申请人 %s: %s',req.from_user.id,exc)
        # 备用深度链接：通过群管理告知申请人使用 /start；不自动批准

async def on_start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type!='private':return
    user_id=update.effective_user.id
    # 直接入群：通过群内一次性深度链接进入私聊，才发送答题按钮。
    if ctx.args and ctx.args[0].startswith('d_'):
        try:
            _, sid, token = ctx.args[0].split('_',2)
            sid = int(sid)
        except (ValueError,TypeError):
            await update.message.reply_text('验证链接格式无效。');return
        conn=get_conn()
        row=conn.execute("""SELECT * FROM direct_join_verifications WHERE session_id=? AND user_id=?
            AND token=? AND status='pending' AND expires_at>?""",(sid,user_id,token,int(time.time()))).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text('验证链接已失效，请重新申请入群。');return
        if cooldown_remaining(row['chat_id'],user_id):
            await update.message.reply_text('答题次数已用完，请 10 分钟后重新验证。')
            return
        if row['verify_mode'] == 'cloudflare':
            if not turnstile_ready():
                await update.message.reply_text('网页验证暂不可用，请联系管理员。')
                return
            challenge=create_challenge(row['chat_id'],user_id,'direct',session_id=sid,expires_at=row['expires_at'])
            markup=InlineKeyboardMarkup([[InlineKeyboardButton('🌐 打开验证网页',web_app=WebAppInfo(url=f'{verification_url()}/verify?challenge={challenge}'))]])
            await update.message.reply_text('🛡️ 请打开验证网页完成 Cloudflare 人机验证，验证成功后自动解除禁言。',reply_markup=markup)
        else:
            from app.handlers.join_request import generate_question
            text,_,keyboard=generate_question(sid)
            await update.message.reply_text(text.replace('请在群内直接回答','请在私聊中点击按钮回答'),reply_markup=InlineKeyboardMarkup(keyboard))
        return
    conn=get_conn()
    rows=conn.execute("SELECT chat_id,verify_mode,expires_at FROM join_requests WHERE user_id=? AND status='pending' AND expires_at>?",(user_id,int(time.time()))).fetchall()
    conn.close()
    if not rows:
        from app.config import Config
        if user_id in Config.ADMIN_IDS:
            from app.handlers.admin_panel import panel
            await panel(update,ctx)
        return
    for row in rows:
        if cooldown_remaining(row['chat_id'],user_id):
            await update.message.reply_text('答题次数已用完，请稍后再试。')
            continue
        if row['verify_mode'] == 'cloudflare':
            if not turnstile_ready():
                await update.message.reply_text('网页验证暂不可用，请联系管理员。')
                continue
            challenge=create_challenge(row['chat_id'],user_id,'request',expires_at=row['expires_at'])
            markup=InlineKeyboardMarkup([[InlineKeyboardButton('🌐 打开验证网页',web_app=WebAppInfo(url=f'{verification_url()}/verify?challenge={challenge}'))]])
            await update.message.reply_text('🛡️ 请打开验证网页完成人机验证。',reply_markup=markup)
        else:
            text,markup=_challenge(row['chat_id'],user_id,preserve_attempts=True)
            await update.message.reply_text(text,reply_markup=markup)

async def on_answer(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not q.data:return
    try:
        _,chat_id,token,answer=q.data.split(':',3);chat_id=int(chat_id)
    except (ValueError,TypeError):return
    user_id=q.from_user.id
    conn=get_conn()
    row=conn.execute("SELECT * FROM join_requests WHERE chat_id=? AND user_id=?",(chat_id,user_id)).fetchone()
    if not row or row['status']!='pending' or row['verify_mode']!='math' or not hmac.compare_digest(row['token'],token):
        conn.close();await q.answer('验证已失效，请重新申请',show_alert=True);return
    if row['expires_at']<int(time.time()):
        conn.execute("UPDATE join_requests SET status='timeout' WHERE chat_id=? AND user_id=?",(chat_id,user_id));conn.commit();conn.close()
        try:await ctx.bot.decline_chat_join_request(chat_id,user_id)
        except Exception:log.exception('拒绝超时申请失败')
        await q.answer('验证超时',show_alert=True);return
    if not hmac.compare_digest(row['answer_hash'],_digest(token,answer)):
        attempts=row['attempts']+1
        if attempts>=2:
            conn.execute("UPDATE join_requests SET status='failed',attempts=? WHERE chat_id=? AND user_id=?",(attempts,chat_id,user_id))
            conn.commit();conn.close()
            start_cooldown(chat_id,user_id)
            try:await ctx.bot.decline_chat_join_request(chat_id,user_id)
            except Exception:log.exception('拒绝错误答案申请失败')
            await q.answer('两次回答错误，请 10 分钟后重试',show_alert=True)
            try:await q.edit_message_text('❌ 两次回答错误，本次验证失败。10 分钟后可重新申请。')
            except Exception:pass
            return
        conn.execute("UPDATE join_requests SET attempts=? WHERE chat_id=? AND user_id=?",(attempts,chat_id,user_id))
        conn.commit();conn.close()
        await q.answer('答案错误，还剩 1 次机会',show_alert=True)
        return
    # 先标记批准中，避免 Telegram 的 chat_member 事件抢先到达并误踢用户。
    conn.execute("UPDATE join_requests SET status='approving' WHERE chat_id=? AND user_id=? AND token=?",(chat_id,user_id,token))
    conn.commit();conn.close()
    try:await ctx.bot.approve_chat_join_request(chat_id,user_id)
    except Exception:
        conn=get_conn()
        conn.execute("UPDATE join_requests SET status='pending' WHERE chat_id=? AND user_id=? AND token=? AND status='approving'",(chat_id,user_id,token))
        conn.commit();conn.close()
        log.exception('批准入群失败 chat=%s user=%s',chat_id,user_id)
        await q.answer('批准失败，请稍后重试',show_alert=True);return
    conn=get_conn()
    conn.execute("UPDATE join_requests SET status='passed' WHERE chat_id=? AND user_id=? AND token=?",(chat_id,user_id,token))
    conn.execute("INSERT OR REPLACE INTO users(user_id,username,passed_verification) VALUES(?,?,1)",(user_id,q.from_user.username))
    conn.commit();conn.close()
    await q.answer('验证成功')
    try:await q.edit_message_text('✅ 验证成功，已批准入群。')
    except Exception:pass

async def cleanup(ctx:ContextTypes.DEFAULT_TYPE):
    conn=get_conn()
    expired=conn.execute("SELECT chat_id,user_id,session_id,message_id FROM direct_join_verifications WHERE status='pending' AND expires_at<?",(int(time.time()),)).fetchall()
    conn.close()
    from app.handlers.join_request import kick_member
    for item in expired:
        await kick_member(item['chat_id'],item['user_id'],ctx)
        conn=get_conn()
        conn.execute("UPDATE direct_join_verifications SET status='timeout' WHERE session_id=? AND status='pending'",(item['session_id'],))
        conn.execute("UPDATE verification_sessions SET status='timeout' WHERE id=? AND status='pending'",(item['session_id'],))
        conn.commit();conn.close()
        if item['message_id']:
            try: await ctx.bot.delete_message(item['chat_id'],item['message_id'])
            except Exception: pass
    conn=get_conn()
    rows=conn.execute("SELECT chat_id,user_id FROM join_requests WHERE status='pending' AND expires_at<?",(int(time.time()),)).fetchall()
    conn.execute("UPDATE join_requests SET status='timeout' WHERE status='pending' AND expires_at<?",(int(time.time()),))
    conn.execute("DELETE FROM image_fingerprints WHERE created_at<?",(int(time.time())-7*86400,))
    conn.commit();conn.close()
    for r in rows:
        try:await ctx.bot.decline_chat_join_request(r['chat_id'],r['user_id'])
        except Exception:log.warning('拒绝过期申请失败 %s %s',r['chat_id'],r['user_id'])

request_handler=ChatJoinRequestHandler(on_request)
start_handler=CommandHandler('start',on_start)
answer_handler=CallbackQueryHandler(on_answer,pattern=r'^ja:')
