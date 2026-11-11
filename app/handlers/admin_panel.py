"""Private-only management panel. ADMIN_IDS from environment is the sole authority."""
import logging
from telegram import Update,InlineKeyboardButton,InlineKeyboardMarkup
from telegram.ext import CommandHandler,CallbackQueryHandler,ContextTypes
from app.config import Config
from app.database import get_conn
log=logging.getLogger(__name__)

def authorized(update):
    return bool(update.effective_user and update.effective_user.id in Config.ADMIN_IDS and update.effective_chat and update.effective_chat.type=='private')

def menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton('📊 查看统计',callback_data='guard:stats')],[InlineKeyboardButton('👥 查询用户',callback_data='guard:help')],[InlineKeyboardButton('🔄 刷新面板',callback_data='guard:home')]])

async def panel(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not authorized(update):return
    await update.effective_message.reply_text('🛡️ 群组守护 · 管理面板\n仅 ADMIN_IDS 指定的管理员可访问。',reply_markup=menu())

async def callback(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not authorized(update):
        if q:await q.answer('无权操作',show_alert=True)
        return
    await q.answer()
    if q.data=='guard:stats':
        c=get_conn()
        u=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        v=c.execute('SELECT COUNT(*) FROM violations').fetchone()[0]
        o=c.execute("SELECT COUNT(*) FROM member_observation WHERE until_ts > strftime('%s','now')").fetchone()[0]
        c.close()
        txt=f'📊 管理统计\n用户数：{u}\n违规记录：{v}\n观察期记录：{o}'
    elif q.data=='guard:help':
        txt='👥 用户管理\n发送 /guarduser 用户ID 查询用户并操作。\n操作仅在管理员私聊中有效。'
    else:txt='🛡️ 群组守护 · 管理面板'
    await q.edit_message_text(txt,reply_markup=menu())

async def user_query(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not authorized(update):return
    if len(ctx.args)!=1 or not ctx.args[0].isdigit():
        await update.message.reply_text('用法：/guarduser 123456789');return
    uid=int(ctx.args[0]);c=get_conn()
    u=c.execute('SELECT warning_count,passed_verification FROM users WHERE user_id=?',(uid,)).fetchone()
    obs=c.execute("SELECT chat_id,until_ts FROM member_observation WHERE user_id=? AND until_ts > strftime('%s','now')",(uid,)).fetchall();c.close()
    if not u:
        await update.message.reply_text('没有找到该用户的记录');return
    markup=InlineKeyboardMarkup([[InlineKeyboardButton('重置警告',callback_data=f'guard:reset:{uid}')],[InlineKeyboardButton('解除所有观察期',callback_data=f'guard:end:{uid}')]])
    await update.message.reply_text(f'用户 {uid}\n警告：{u["warning_count"]}\n验证：{"已通过" if u["passed_verification"] else "未通过"}\n观察期群数：{len(obs)}',reply_markup=markup)

async def user_action(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not authorized(update):
        if q:await q.answer('无权操作',show_alert=True)
        return
    try:_,action,raw=q.data.split(':');uid=int(raw)
    except (ValueError,AttributeError):await q.answer('操作无效');return
    if action not in ('reset','end'):
        await q.answer('操作无效');return
    c=get_conn()
    if action=='reset':c.execute('UPDATE users SET warning_count=0 WHERE user_id=?',(uid,))
    else:c.execute('DELETE FROM member_observation WHERE user_id=?',(uid,))
    c.commit();c.close()
    log.warning('Admin %s performed %s on user %s',update.effective_user.id,action,uid)
    await q.answer('操作完成')
    await q.edit_message_text(f'已执行：{"重置警告" if action=="reset" else "解除所有观察期"}\n用户 ID：{uid}\n注意：不会自动解除 Telegram 禁言/封禁。')

handlers=[CommandHandler('panel',panel),CommandHandler('guarduser',user_query),CallbackQueryHandler(callback,pattern=r'^guard:(home|stats|help)$'),CallbackQueryHandler(user_action,pattern=r'^guard:(reset|end):[0-9]+$')]
