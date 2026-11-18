"""Telegram Mini App identity and Cloudflare Turnstile verification.

Never trust user_id, chat_id or a browser-side success flag.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from app.config import Config
from app.database import get_conn, get_setting

log = logging.getLogger(__name__)
MAX_INIT_DATA_AGE = 600
MAX_ATTEMPTS = 2
COOLDOWN_SECONDS = 600


def verification_mode():
    return get_setting("verification_mode", "math")


def verification_url():
    value = get_setting("turnstile_verify_url", "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return value


def turnstile_ready():
    return bool(verification_url() and get_setting("turnstile_site_key", "") and get_setting("turnstile_secret_key", ""))


def telegram_identity(init_data):
    if not isinstance(init_data, str) or len(init_data) > 8192:
        raise ValueError("无效的 Telegram 身份信息")
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(pairs) != len(data) or "hash" not in data:
        raise ValueError("Telegram 签名缺失或重复字段")
    signature = data.pop("hash")
    if len(signature) != 64:
        raise ValueError("Telegram 签名格式错误")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Telegram 身份签名验证失败")
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("Telegram 用户信息无效")
    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > MAX_INIT_DATA_AGE:
        raise ValueError("Telegram 登录信息已过期，请重新打开验证页面")
    return user_id


def verify_turnstile(token, remote_ip=None):
    secret = get_setting("turnstile_secret_key", "")
    if not secret or not token or len(token) > 4096:
        return False
    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.load(response)
    except (URLError, HTTPError, TimeoutError, ValueError):
        log.exception("Turnstile Siteverify request failed")
        return False
    hostname = urlparse(verification_url()).hostname
    return (result.get("success") is True
            and result.get("hostname", "").lower() == hostname.lower()
            and result.get("action") == "join_verify")


def telegram_api(method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{Config.BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        result = json.load(response)
    if result.get("ok") is not True:
        raise RuntimeError(f"Telegram {method} failed: {result.get('description', 'unknown')}")
    return result.get("result")


def get_pending_session(user_id, challenge):
    """Only accept server-generated opaque challenges bound to Telegram identity."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM web_verifications WHERE challenge=? AND user_id=?",
            (challenge, user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_challenge(chat_id, user_id, kind, session_id=None, expires_at=None):
    if not turnstile_ready():
        raise ValueError("Cloudflare 网页验证未配置完成")
    now = int(time.time())
    if expires_at is None:
        expires_at = now + 300
    challenge = secrets.token_urlsafe(24)
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                "UPDATE web_verifications SET status='superseded' WHERE chat_id=? AND user_id=? AND kind=? AND status='pending'",
                (chat_id, user_id, kind),
            )
            conn.execute(
                "INSERT INTO web_verifications(challenge,chat_id,user_id,kind,session_id,expires_at,status) VALUES (?,?,?,?,?,?,'pending')",
                (challenge, chat_id, user_id, kind, session_id, expires_at),
            )
    finally:
        conn.close()
    return challenge


def verify_and_claim(user_id, challenge, cf_token, remote_ip=None):
    row = get_pending_session(user_id, challenge)
    now = int(time.time())
    if not row or row["status"] != "pending" or row["expires_at"] <= now:
        raise ValueError("验证会话已失效")
    if not verify_turnstile(cf_token, remote_ip):
        raise ValueError("Cloudflare 验证失败，请重试")
    conn = get_conn()
    try:
        with conn:
            changed = conn.execute(
                "UPDATE web_verifications SET status='processing' WHERE challenge=? AND user_id=? AND status='pending' AND expires_at>?",
                (challenge, user_id, int(time.time())),
            ).rowcount
        if not changed:
            raise ValueError("验证会话已使用")
    finally:
        conn.close()
    return row


def finish_challenge(challenge, status):
    conn = get_conn()
    try:
        with conn:
            conn.execute("UPDATE web_verifications SET status=? WHERE challenge=? AND status='processing'", (status, challenge))
    finally:
        conn.close()


def apply_verification(row, user_id):
    """Perform the Telegram action only after both server-side checks succeed."""
    chat_id = row["chat_id"]
    conn = get_conn()
    try:
        if row["kind"] == "request":
            req = conn.execute(
                "SELECT status,token,expires_at FROM join_requests WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if not req or req["status"] != "pending" or req["expires_at"] <= int(time.time()):
                raise ValueError("入群申请已过期")
            with conn:
                updated = conn.execute(
                    "UPDATE join_requests SET status='approving' WHERE chat_id=? AND user_id=? AND status='pending' AND expires_at>?",
                    (chat_id, user_id, int(time.time())),
                ).rowcount
            if not updated:
                raise ValueError("申请正在处理中")
            try:
                telegram_api("approveChatJoinRequest", chat_id=chat_id, user_id=user_id)
            except Exception:
                with conn:
                    conn.execute("UPDATE join_requests SET status='pending' WHERE chat_id=? AND user_id=? AND status='approving'", (chat_id, user_id))
                raise
            with conn:
                conn.execute("UPDATE join_requests SET status='passed' WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        elif row["kind"] == "direct":
            direct = conn.execute(
                "SELECT status,session_id,expires_at FROM direct_join_verifications WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if not direct or direct["status"] != "pending" or direct["session_id"] != row["session_id"] or direct["expires_at"] <= int(time.time()):
                raise ValueError("直接入群验证已过期")
            with conn:
                updated = conn.execute(
                    "UPDATE direct_join_verifications SET status='approving' WHERE chat_id=? AND user_id=? AND session_id=? AND status='pending' AND expires_at>?",
                    (chat_id, user_id, row["session_id"], int(time.time())),
                ).rowcount
            if not updated:
                raise ValueError("验证正在处理中")
            try:
                chat = telegram_api("getChat", chat_id=chat_id)
                permissions = chat.get("permissions") or {"can_send_messages": True}
                telegram_api("restrictChatMember", chat_id=chat_id, user_id=user_id, permissions=permissions)
            except Exception:
                with conn:
                    conn.execute("UPDATE direct_join_verifications SET status='pending' WHERE chat_id=? AND user_id=? AND session_id=? AND status='approving'", (chat_id, user_id, row["session_id"]))
                raise
            with conn:
                conn.execute("UPDATE direct_join_verifications SET status='passed' WHERE chat_id=? AND user_id=? AND session_id=?", (chat_id, user_id, row["session_id"]))
                conn.execute("UPDATE verification_sessions SET status='passed' WHERE id=? AND status='pending'", (row["session_id"],))
            # Preserve existing post-verification observation and clear the group prompt.
            from app.handlers.media_guard import mark_observation
            from app.handlers.join_request import VERIFYING_USERS
            mark_observation(chat_id, user_id)
            VERIFYING_USERS.pop((chat_id, user_id), None)
            prompt = conn.execute(
                "SELECT message_id FROM direct_join_verifications WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            if prompt and prompt["message_id"]:
                try:
                    telegram_api("deleteMessage", chat_id=chat_id, message_id=prompt["message_id"])
                except Exception:
                    log.warning("Could not remove direct verification prompt chat=%s user=%s", chat_id, user_id)
        else:
            raise ValueError("未知验证类型")
        with conn:
            conn.execute("INSERT INTO users(user_id,passed_verification) VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET passed_verification=1", (user_id,))
    finally:
        conn.close()
    log.info("Turnstile verification passed chat=%s user=%s kind=%s", chat_id, user_id, row["kind"])
