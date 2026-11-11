"""先申请、私聊验证、后批准；SQLite 持久化，支持容器重启。"""
import hashlib
import hmac
import logging
import secrets
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ChatJoinRequestHandler, CommandHandler, CallbackQueryHandler, ContextTypes
from app.database import get_conn, get_setting, is_chat_allowed

log = logging.getLogger(__name__)
def enabled(): return get_setting('join_approval_enabled','1') == '1'
def timeout(): return max(60,min(900,int(get_setting('join_approval_timeout','300'))))
def _digest(token, answer): return hashlib.sha256(f'{token}:{answer}'.encode()).hexdigest()

def _challenge(chat_id,user_id):
    # 题目随机、选项随机，单次 token 不可预测；不能代替专业 CAPTCHA
    import random
    a,b = secrets.randbelow(18)+2,secrets.randbelow(18)+2
    answer = a+b
    wrong = set()
    while len(wrong)<3:
        n = answer + secrets.randbelow(17)-8
        if n!=answer and n>0: wrong.add(n)
    opts = list(wrong)+[answer]
    random.SystemRandom().shuffle(opts)
    token=secrets.token_urlsafe(12)
    conn=get_conn()
    conn.execute("""INSERT INTO join_requests(chat_id,user_id,token,answer_hash,expires_at,status,attempts)
      VALUES(?,?,?,?,?,'pending',0) ON CONFLICT(chat_id,user_id) DO UPDATE SET
      token=excluded.token,answer_hash=excluded.answer_hash,expires_at=excluded.expires_at,status='pending',attempts=0""",
      (chat_id,user_id,token,_digest(token,str(answer)),int(time.time())+timeout()))
    conn.commit();conn.close()
    markup=InlineKeyboardMarkup([[InlineKeyboardButton(str(n),callback_data=f'ja:{chat_id}:{token}:{n}')] for n in opts])
    return f'🛡️ 入群申请验证\n请在 {timeout()//60} 分钟内回答：{a} + {b} = ?\n验证通过后自动批准入群。',markup

async def on_request(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    req=update.chat_join_request
    if not req or not enabled() or not is_chat_allowed(req.chat.id):return
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
        from app.handlers.join_request import generate_question
        text,_,keyboard=generate_question(sid)
        await update.message.reply_text(text.replace('请在群内直接回答','请在私聊中点击按钮回答'),reply_markup=InlineKeyboardMarkup(keyboard))
        return
    conn=get_conn()
    rows=conn.execute("SELECT chat_id FROM join_requests WHERE user_id=? AND status='pending' AND expires_at>?",(user_id,int(time.time()))).fetchall()
    conn.close()
    if not rows:return
    for row in rows:
        text,markup=_challenge(row['chat_id'],user_id)
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
    if not row or row['status']!='pending' or not hmac.compare_digest(row['token'],token):
        conn.close();await q.answer('验证已失效，请重新申请',show_alert=True);return
    if row['expires_at']<int(time.time()):
        conn.execute("UPDATE join_requests SET status='timeout' WHERE chat_id=? AND user_id=?",(chat_id,user_id));conn.commit();conn.close()
        try:await ctx.bot.decline_chat_join_request(chat_id,user_id)
        except Exception:log.exception('拒绝超时申请失败')
        await q.answer('验证超时',show_alert=True);return
    if not hmac.compare_digest(row['answer_hash'],_digest(token,answer)):
        conn.execute("UPDATE join_requests SET status='failed',attempts=attempts+1 WHERE chat_id=? AND user_id=?",(chat_id,user_id));conn.commit();conn.close()
        try:await ctx.bot.decline_chat_join_request(chat_id,user_id)
        except Exception:log.exception('拒绝错误答案申请失败')
        await q.answer('答案错误，申请已拒绝',show_alert=True)
        try:await q.edit_message_text('❌ 验证失败，请重新申请入群。')
        except Exception:pass
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
