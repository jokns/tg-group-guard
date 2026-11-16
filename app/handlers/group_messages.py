import re
import logging
from datetime import datetime, timezone, timedelta
from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes, MessageHandler, filters
from app.database import get_conn
from app.config import Config

def _get_max_warnings():
    from app.database import get_setting
    val = get_setting("max_warnings", "")
    return int(val) if val and val.isdigit() else Config.MAX_WARNINGS

def _get_warning_limit_action():
    from app.database import get_setting
    action = get_setting('warning_limit_action', 'ban')
    return action if action in ('ban', 'kick', 'ban_and_kick') else 'ban'

def _save_warning_message(chat_id: int, user_id: int, message_id: int):
    conn = get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO warning_messages (chat_id, user_id, message_id) VALUES (?, ?, ?)",
                     (chat_id, user_id, message_id))
        conn.commit()
    finally:
        conn.close()

async def _clear_warning_messages(context, chat_id: int, user_id: int):
    """仅清理本机器人记录的、该用户在当前群的警告通知；失败的 ID 留待下次重试。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT message_id FROM warning_messages WHERE chat_id=? AND user_id=? ORDER BY message_id",
                            (chat_id, user_id)).fetchall()
    finally:
        conn.close()
    for row in rows:
        message_id = row["message_id"]
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.warning("Could not remove old warning chat=%s user=%s msg=%s: %s",
                           chat_id, user_id, message_id, exc)
            continue
        conn = get_conn()
        try:
            conn.execute("DELETE FROM warning_messages WHERE chat_id=? AND user_id=? AND message_id=?",
                         (chat_id, user_id, message_id))
            conn.commit()
        finally:
            conn.close()

def _get_mute_duration():
    from app.database import get_setting
    val = get_setting("mute_duration", "")
    return int(val) if val and val.isdigit() else Config.MUTE_DURATION

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+', re.IGNORECASE)
TG_INVITE_PATTERN = re.compile(r't\.me/\+?[a-zA-Z0-9_]+|telegram\.me/\+?[a-zA-Z0-9_]+', re.IGNORECASE)
SHORT_URLS = ["bit.ly", "t.cn", "goo.gl", "tinyurl.com", "short.link", "dlvr.it"]

def get_link_filter_settings():
    """读取链接过滤配置"""
    from app.database import get_setting
    return {
        "enabled": get_setting("link_filter_enabled", "1") == "1",
        "tg_invite": get_setting("link_filter_tg_invite", "1") == "1",
        "short_url": get_setting("link_filter_short_url", "1") == "1",
        "all_url": get_setting("link_filter_all_url", "0") == "1",
    }

WECHAT_PATTERN = re.compile(r'(微信|vx|wechat|薇信|威信)[:\s]*[a-zA-Z0-9_\-]+', re.IGNORECASE)
PHONE_PATTERN = re.compile(r'[\+]?\d{11,}')
QQ_PATTERN = re.compile(r'[Qq][Qq][:\s]*\d{5,}')

def get_blocked_keywords():
    """从数据库读取启用的违禁词"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT keyword FROM blocked_keywords WHERE enabled = 1")
    keywords = [row["keyword"] for row in c.fetchall()]
    conn.close()
    return keywords

def record_keyword_hit(keyword: str):
    """记录关键词命中次数"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE blocked_keywords SET hit_count = hit_count + 1 WHERE keyword = ?", (keyword,))
    conn.commit()
    conn.close()

def sanitize_reason(reason: str) -> str:
    """脱敏违规原因中的链接，防止广告链接在通知中被完整展示或预览"""
    def _mask_url(m):
        url = m.group(0)
        clean = re.sub(r'^https?://', '', url, flags=re.IGNORECASE)
        clean = re.sub(r'^www\.', '', clean, flags=re.IGNORECASE)
        if len(clean) > 20:
            return clean[:17] + "..."
        return clean
    return URL_PATTERN.sub(_mask_url, reason)

def is_spam(text: str) -> tuple[bool, str]:
    if not text:
        return False, ""
    lower = text.lower()
    
    # 动态读取数据库中的违禁词
    blocked_keywords = get_blocked_keywords()
    for kw in blocked_keywords:
        if kw.lower() in lower:
            record_keyword_hit(kw)
            return True, f"关键词拦截: {kw}"
    
    # 链接过滤
    link_settings = get_link_filter_settings()
    if link_settings["enabled"]:
        urls = URL_PATTERN.findall(text)
        if urls:
            for url in urls:
                # TG 邀请链接
                if link_settings["tg_invite"] and TG_INVITE_PATTERN.search(url):
                    return True, f"TG邀请链接: {url}"
                # 短链接
                if link_settings["short_url"]:
                    for short in SHORT_URLS:
                        if short in url.lower():
                            return True, f"短链接: {url}"
            # 过滤所有链接
            if link_settings["all_url"]:
                return True, f"包含链接: {urls[0]}"
            if len(urls) >= 3:
                return True, "消息包含过多链接"
    
    if WECHAT_PATTERN.search(text):
        return True, "包含微信号"
    if PHONE_PATTERN.search(text):
        return True, "包含手机号"
    if QQ_PATTERN.search(text):
        return True, "包含QQ号"
    
    return False, ""

# ========== 防刷屏 ==========
# 内存中记录用户最近消息时间 { (chat_id, user_id): [timestamp1, timestamp2, ...] }
_USER_MESSAGE_LOGS = {}

def get_antiflood_settings():
    """读取防刷屏配置"""
    from app.database import get_setting
    return {
        "enabled": get_setting("antiflood_enabled", "1") == "1",
        "seconds": int(get_setting("antiflood_seconds", "10")),
        "count": int(get_setting("antiflood_count", "5")),
        "action": get_setting("antiflood_action", "mute"),
    }

def check_antiflood(chat_id: int, user_id: int) -> tuple[bool, str]:
    """检查用户是否刷屏，返回 (是否刷屏, 原因)"""
    settings = get_antiflood_settings()
    if not settings["enabled"]:
        return False, ""
    import time
    key = (chat_id, user_id)
    now = time.time()
    window = settings["seconds"]
    limit = settings["count"]
    if key not in _USER_MESSAGE_LOGS:
        _USER_MESSAGE_LOGS[key] = []
    _USER_MESSAGE_LOGS[key] = [t for t in _USER_MESSAGE_LOGS[key] if now - t < window]
    _USER_MESSAGE_LOGS[key].append(now)
    if len(_USER_MESSAGE_LOGS[key]) >= limit:
        return True, f"防刷屏: {settings['seconds']}秒内发送{len(_USER_MESSAGE_LOGS[key])}条消息"
    return False, ""

def get_auto_replies():
    """从数据库读取启用的自动回复"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT keyword, reply_text FROM auto_replies WHERE enabled = 1")
    rows = c.fetchall()
    conn.close()
    return [(row["keyword"], row["reply_text"]) for row in rows]

def match_auto_reply(text: str) -> str:
    """匹配自动回复，返回回复内容或空字符串"""
    if not text:
        return ""
    replies = get_auto_replies()
    for keyword, reply in replies:
        if keyword.lower() in text.lower():
            return reply
    return ""

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    
    from app.database import is_chat_allowed
    if not is_chat_allowed(update.effective_chat.id):
        return
    
    chat_id = update.effective_chat.id
    user = update.effective_user
    if not user:
        return

    # 频道消息转发(777000=Channel_Bot)和管理员豁免内容过滤
    is_admin = False
    # 777000 是 Telegram 频道转发消息的系统账号，视为管理员
    if user.id == 777000:
        is_admin = True
    else:
        try:
            member = await update.effective_chat.get_member(user.id)
            if member.status in ("administrator", "creator"):
                is_admin = True
        except Exception:
            pass

    # 用户白名单：豁免违禁词、链接等内容过滤
    from app.database import get_setting
    user_whitelist_str = get_setting("user_whitelist", "")
    if user_whitelist_str:
        whitelist_ids = {int(x.strip()) for x in user_whitelist_str.split(",") if x.strip().lstrip("-").isdigit()}
        if user.id in whitelist_ids:
            is_admin = True
    
    text = update.message.text or update.message.caption or ""
    
    # 图片审核与观察期限制（管理员/白名单豁免）
    if not is_admin:
        from app.handlers.media_guard import check_media
        media_reason = await check_media(update, context, chat_id, user.id)
    else:
        media_reason = ''

    # 防刷屏检查
    flood_flag, flood_reason = check_antiflood(chat_id, user.id)
    if flood_flag:
        await handle_flood_violation(update, context, chat_id, user, flood_reason, is_admin)
        return
    
    # 管理员豁免内容过滤（消息不删、不记录违规）
    if is_admin:
        is_spam_flag, reason = False, ""
    else:
        is_spam_flag, reason = is_spam(text)
        if media_reason:
            is_spam_flag, reason = True, media_reason
    if not is_spam_flag:
        # 检查自动回复
        reply = match_auto_reply(text)
        if reply and update.message:
            try:
                await context.bot.send_message(chat_id=chat_id, text=reply, reply_to_message_id=update.message.message_id)
            except Exception as e:
                logger.warning(f"Auto reply failed: {e}")
        return
    
    logger.info(f"Spam from {user.id}: {reason}")
    
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete msg failed: {e}")
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO violations (user_id, chat_id, message_text, reason) VALUES (?, ?, ?, ?)",
              (user.id, chat_id, text[:500], reason))
    c.execute("SELECT warning_count FROM users WHERE user_id = ?", (user.id,))
    row = c.fetchone()
    warnings = (row["warning_count"] if row else 0) + 1
    c.execute("INSERT OR REPLACE INTO users (user_id, username, warning_count, passed_verification) VALUES (?, ?, ?, COALESCE((SELECT passed_verification FROM users WHERE user_id=?), 0))",
              (user.id, user.username, warnings, user.id))
    conn.commit()
    conn.close()
    
    mention = user.mention_html()
    
    if is_admin:
        # 管理员/群主：只删消息+记录，不封禁不禁言
        logger.info(f"Admin spam deleted (no ban): user={user.id} reason={reason}")
    elif warnings >= _get_max_warnings():
        try:
            action = _get_warning_limit_action()
            # Telegram ban_chat_member removes the member and blocks rejoining.
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            if action == 'kick':
                # Only the kick mode unbans, allowing future rejoining.
                await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id, only_if_banned=True)

            # Check actual Telegram membership state before claiming success.
            try:
                member_after = await context.bot.get_chat_member(chat_id=chat_id, user_id=user.id)
            except Exception as verify_exc:
                logger.error("Cannot verify moderation chat=%s user=%s action=%s: %s",
                             chat_id, user.id, action, verify_exc)
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ 用户 {mention} 已达到 {warnings} 次违规，处罚请求已提交，但无法核验成员状态，请管理员检查。",
                    parse_mode="HTML", disable_web_page_preview=True
                )
                return

            status = member_after.status
            is_member = status in ('creator', 'administrator', 'member') or (
                status == 'restricted' and getattr(member_after, 'is_member', False)
            )
            expected = status in ('left', 'kicked', 'banned') if action == 'kick' else status in ('kicked', 'banned')
            if is_member or not expected:
                logger.error("Moderation verification mismatch chat=%s user=%s action=%s status=%s is_member=%s",
                             chat_id, user.id, action, status, is_member)
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ 用户 {mention} 已达到 {warnings} 次违规，但处罚后成员状态为 {status}，未确认成功移出/封禁，请管理员检查机器人权限。",
                    parse_mode="HTML", disable_web_page_preview=True
                )
                return

            await _clear_warning_messages(context, chat_id, user.id)
            action_text = ('已被踢出群组（允许重新加入）' if action == 'kick'
                           else '已被永久封禁并移出群组（禁止重新加入）')
            await context.bot.send_message(chat_id,
                f"🚫 用户 {mention} 因累计 {warnings} 次违规{action_text}。",
                parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Warning limit action failed: {e}")
    else:
        try:
            until_date = datetime.now(timezone.utc) + timedelta(seconds=_get_mute_duration())
            await context.bot.restrict_chat_member(
                chat_id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            warning_message = await context.bot.send_message(
                chat_id,
                f"⚠️ {mention} 的违规消息已被删除。\n<b>原因:</b> {sanitize_reason(reason)}\n<b>警告:</b> {warnings}/{_get_max_warnings()}\n达到上限将被{'踢出群组' if _get_warning_limit_action() == 'kick' else ('永久封禁并禁止重新加入' if _get_warning_limit_action() == 'ban_and_kick' else '永久封禁')}。",
                parse_mode="HTML", disable_web_page_preview=True
            )
            try:
                _save_warning_message(chat_id, user.id, warning_message.message_id)
            except Exception as exc:
                logger.error("Failed to store warning message ID: %s", exc)
        except Exception as e:
            logger.error(f"Restrict failed: {e}")

async def handle_flood_violation(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, reason: str, is_admin: bool = False):
    """处理刷屏违规"""
    logger.info(f"Antiflood triggered by {user.id}: {reason}")
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete flood msg failed: {e}")
    
    if is_admin:
        logger.info(f"Admin flood deleted (no action): user={user.id}")
        return
    
    mention = user.mention_html()
    settings = get_antiflood_settings()
    action = settings["action"]
    if action == "kick":
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            await context.bot.unban_chat_member(chat_id, user.id)
            await context.bot.send_message(chat_id, f"🚫 {mention} 因刷屏已被移出群组。", parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Kick flood user failed: {e}")
    elif action == "mute":
        try:
            mute_sec = _get_mute_duration()
            until_date = datetime.now(timezone.utc) + timedelta(seconds=mute_sec)
            await context.bot.restrict_chat_member(
                chat_id, user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await context.bot.send_message(
                chat_id,
                f"⚠️ {mention} 因刷屏消息已被删除并禁言 {mute_sec // 60} 分钟。",
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Mute flood user failed: {e}")
    else:
        try:
            await context.bot.send_message(
                chat_id,
                f"⚠️ {mention} 请注意，不要刷屏！",
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Warn flood user failed: {e}")

message_handler = MessageHandler(filters.TEXT | filters.CAPTION | filters.PHOTO | filters.Document.IMAGE | filters.Sticker.ALL | filters.ANIMATION, handle_message)
