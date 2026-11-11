"""Private-only admin console. All actions re-check ADMIN_IDS server-side."""
import time
import logging
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.error import BadRequest
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
from app.config import Config
from app.database import get_conn, get_allowed_chats
from app.audit import audit
log=logging.getLogger(__name__)

def allowed(update):
    return bool(update.effective_user and update.effective_user.id in Config.ADMIN_IDS and update.effective_chat and update.effective_chat.type=='private')

def menu():
    return K([[B('📊 运行统计',callback_data='guard:stats'),B('👥 用户查询',callback_data='guard:help')],
              [B('🛡️ 待验证',callback_data='guard:pending'),B('⚠️ 最近违规',callback_data='guard:violations')],
              [B('📋 管理日志',callback_data='guard:logs'),B('🏠 刷新首页',callback_data='guard:home')]])

async def panel(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not allowed(update):return
    await update.effective_message.reply_text('🛡️ 群组守护 · 管理面板\n仅允许指定管理员在私聊操作。\n用户查询：/guarduser 123456789\n群组列表：/guardchats',reply_markup=menu())

def _user_card(uid):
    c=get_conn();u=c.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone()
    o=c.execute('SELECT chat_id,until_ts FROM member_observation WHERE user_id=? AND until_ts>?',(uid,int(time.time()))).fetchall()
    v=c.execute('SELECT COUNT(*) FROM violations WHERE user_id=?',(uid,)).fetchone()[0]
    d=c.execute('SELECT chat_id,status FROM direct_join_verifications WHERE user_id=?',(uid,)).fetchall()
    c.close()
    if not u and not o and not v and not d:return None
    name=(u['username'] if u else None) or '未记录'
    return f'👤 用户 {uid}\n用户名：{name}\n警告次数：{u["warning_count"] if u else 0}\n历史违规：{v}\n验证通过：{"是" if u and u["passed_verification"] else "否/未知"}\n观察期群数：{len(o)}\n最近验证：{d[-1]["status"] if d else "无"}'

def user_menu(uid):
    return K([[B('🔄 重置警告',callback_data=f'guard:reset:{uid}'),B('⏩ 结束观察期',callback_data=f'guard:end:{uid}')],
              [B('🔎 刷新用户',callback_data=f'guard:user:{uid}')],[B('🏠 返回首页',callback_data='guard:home')]])

async def user_query(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not allowed(update):return
    if len(ctx.args)!=1 or not ctx.args[0].isdigit():
        await update.message.reply_text('用法：/guarduser 123456789');return
    uid=int(ctx.args[0]);txt=_user_card(uid)
    if txt is None:
        lines=[]
        for cid in get_allowed_chats()[:10]:
            try:
                m=await ctx.bot.get_chat_member(cid,uid)
                lines.append(f'群 {cid}：{m.status}')
            except Exception:
                continue
        if lines:
            txt=f'👤 用户 {uid}（尚无本地记录）\n'+'\n'.join(lines)
        else:
            await update.message.reply_text(f'未找到用户 {uid} 的本地记录。\n若已配置 ALLOWED_CHAT_IDS，可查询白名单群内成员。\n请核对数字 ID；未入群的用户无法通过 Telegram API 任意查询。');return
    await update.message.reply_text(txt,reply_markup=user_menu(uid))
    audit('tg_admin',update.effective_user.id,'query_user',uid)

async def chats(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not allowed(update):return
    ids=get_allowed_chats()
    await update.message.reply_text('🗂️ 白名单群组\n'+('\n'.join(map(str,ids)) if ids else '未设置群组白名单（允许所有群）'))

async def callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q:return
    if not allowed(update):await q.answer('无权操作',show_alert=True);return
    data=q.data or ''
    if data.startswith('guard:user:'):
        uid=int(data.split(':')[-1]);await q.answer();await q.edit_message_text(_user_card(uid) or '没有找到该用户',reply_markup=user_menu(uid));return
    if data.startswith(('guard:reset:','guard:end:')):
        _,act,raw=data.split(':');uid=int(raw)
        c=get_conn()
        if act=='reset':
            c.execute('UPDATE users SET warning_count=0 WHERE user_id=?',(uid,))
        else:c.execute('DELETE FROM member_observation WHERE user_id=?',(uid,))
        changed=c.total_changes;c.commit();c.close()
        audit('tg_admin',update.effective_user.id,'reset_warnings' if act=='reset' else 'end_observation',uid,f'rows={changed}')
        await q.answer('已执行' if changed else '无可修改记录',show_alert=True)
        await q.edit_message_text((_user_card(uid) or f'用户 {uid} 暂无本地记录')+'\n\n注意：本操作不解除 Telegram 已存在的禁言。',reply_markup=user_menu(uid));return
    c=get_conn()
    if data=='guard:stats':
        users=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        violations=c.execute('SELECT COUNT(*) FROM violations').fetchone()[0]
        obs=c.execute('SELECT COUNT(*) FROM member_observation WHERE until_ts>?',(int(time.time()),)).fetchone()[0]
        pending=c.execute("SELECT COUNT(*) FROM direct_join_verifications WHERE status='pending' AND expires_at>?",(int(time.time()),)).fetchone()[0]
        txt=f'📊 运行统计\n已记录用户：{users}\n历史违规：{violations}\n观察期人数（群记录）：{obs}\n待验证：{pending}'
    elif data=='guard:pending':
        rows=c.execute("SELECT user_id,chat_id,expires_at FROM direct_join_verifications WHERE status='pending' AND expires_at>? ORDER BY expires_at LIMIT 8",(int(time.time()),)).fetchall()
        txt='🛡️ 待验证用户\n'+('\n'.join(f'{r["user_id"]} / 群 {r["chat_id"]} / 剩余 {max(0,r["expires_at"]-int(time.time()))} 秒' for r in rows) if rows else '暂无')
    elif data=='guard:violations':
        rows=c.execute('SELECT user_id,reason,created_at FROM violations ORDER BY id DESC LIMIT 8').fetchall()
        txt='⚠️ 最近违规\n'+('\n'.join(f'{r["user_id"]}: {str(r["reason"])[:50]}' for r in rows) if rows else '暂无')
    elif data=='guard:logs':
        rows=c.execute('SELECT category,action,target FROM audit_logs ORDER BY id DESC LIMIT 8').fetchall()
        txt='📋 最近管理操作\n'+('\n'.join(f'{r["category"]} {r["action"]} {r["target"]}' for r in rows) if rows else '暂无')
    elif data=='guard:help':txt='👥 查询用户\n发送 /guarduser 123456789\n可重置警告、解除观察期。\n发送 /guardchats 查看群组白名单。'
    else:txt='🛡️ 群组守护 · 管理面板'
    c.close();await q.answer()
    try:
        await q.edit_message_text(txt,reply_markup=menu())
    except BadRequest as exc:
        if 'Message is not modified' not in str(exc):
            raise

handlers=[CommandHandler('panel',panel),CommandHandler('guarduser',user_query),CommandHandler('guardchats',chats),CallbackQueryHandler(callback,pattern=r'^guard:(home|stats|help|pending|violations|logs|user:[0-9]+|reset:[0-9]+|end:[0-9]+)$')]
