import logging
import threading
import asyncio
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from telegram.ext import ApplicationBuilder, CommandHandler
from app.config import Config
from app.database import init_db
from app.handlers.join_request import chat_member_handler, verification_callback_handler
from app.handlers.group_messages import message_handler
from app.handlers.join_approval import request_handler, start_handler, answer_handler, cleanup
from app.handlers.admin_commands import get_admin_handlers
from app.handlers.admin_panel import handlers as admin_panel_handlers
from app.audit import cleanup as cleanup_audit

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def run_web():
    from app.web.server import run_web as _run_web
    _run_web(host="0.0.0.0", port=8080)

async def audit_cleanup_job(ctx):
    await asyncio.to_thread(cleanup_audit)

async def register_command_menu(application):
    """Use Telegram native command menu, with per-admin private-chat scope."""
    public = [BotCommand('start', '开始使用 / 入群验证'), BotCommand('help', '查看使用说明')]
    private_admin = [
        BotCommand('start', '打开管理面板 / 入群验证'),
        BotCommand('help', '查看管理帮助'),
        BotCommand('panel', '打开管理员控制台'),
        BotCommand('guarduser', '查询用户及处罚记录'),
        BotCommand('guardchats', '查看管理群组'),
        BotCommand('stats', '查看运行统计'),
    ]
    try:
        await application.bot.set_my_commands(public, scope=BotCommandScopeDefault())
        for admin_id in Config.ADMIN_IDS:
            try:
                await application.bot.set_my_commands(private_admin, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                logger.exception('Cannot register private command menu for admin %s; start bot chat first', admin_id)
        logger.info('Telegram native command menus registered for %d admins', len(Config.ADMIN_IDS))
    except Exception:
        logger.exception('Telegram command menu registration failed')

async def help_command(update, context):
    if not update.effective_chat or update.effective_chat.type != 'private':
        return
    if update.effective_user and update.effective_user.id in Config.ADMIN_IDS:
        await update.effective_message.reply_text(
            '🛡️ 管理员帮助\n/start 或 /panel 打开管理面板\n'
            '/guarduser 用户ID 查询用户\n/guardchats 查看管理群组\n'
            '/stats 查看运行统计\n\n管理操作仅在管理员私聊有效。'
        )
    else:
        await update.effective_message.reply_text('🛡️ 群组守护机器人\n如需完成入群验证，请点击群内验证按钮，再按提示操作。')

def main():
    init_db()

    # 启动 Web 管理后台（守护线程）
    web_thread = threading.Thread(target=run_web, name="web", daemon=True)
    web_thread.start()
    logger.info("Web admin panel started on http://0.0.0.0:8080")

    # 在主线程启动 Telegram Bot（需要信号处理器）
    application = ApplicationBuilder().token(Config.BOT_TOKEN).post_init(register_command_menu).build()
    application.add_handler(request_handler)
    application.add_handler(start_handler)
    application.add_handler(answer_handler)
    application.add_handler(chat_member_handler)
    application.job_queue.run_repeating(cleanup, interval=60, first=30)
    application.add_handler(verification_callback_handler)
    for handler in admin_panel_handlers:
        application.add_handler(handler)
    for handler in get_admin_handlers():
        application.add_handler(handler)

    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(message_handler)
    application.job_queue.run_repeating(audit_cleanup_job, interval=3600, first=20)
    logger.info("Bot started. Polling for updates...")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
