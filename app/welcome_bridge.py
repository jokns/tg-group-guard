"""Thread-safe bridge from Flask's verification worker to Telegram's asyncio loop.

All welcome sends and scheduled deletions run through the existing bot application.
"""
import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeout

log = logging.getLogger(__name__)
_lock = threading.Lock()
_app = None
_loop = None


def register(application):
    global _app, _loop
    with _lock:
        _app = application
        _loop = asyncio.get_running_loop()


async def _send(chat_id, user_id):
    from app.handlers.join_request import send_welcome
    with _lock:
        application = _app
    member = await application.bot.get_chat_member(chat_id, user_id)
    await send_welcome(chat_id, member.user, application)


def notify_verified(chat_id, user_id):
    """Schedule welcome from Flask; a failed welcome never undoes a successful join."""
    with _lock:
        loop = _loop
    if not loop or not loop.is_running():
        log.error("Cannot send welcome: Telegram event loop unavailable chat=%s user=%s", chat_id, user_id)
        return
    future = asyncio.run_coroutine_threadsafe(_send(chat_id, user_id), loop)
    def on_done(completed):
        try:
            completed.result()
        except Exception:
            log.exception("Web verification welcome failed chat=%s user=%s", chat_id, user_id)
    future.add_done_callback(on_done)
