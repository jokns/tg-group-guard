"""Shared math CAPTCHA generation and per-user cooldown."""
import secrets
import time
from app.database import get_conn, get_setting

def difficulty():
    value = get_setting("math_difficulty", "medium")
    return value if value in ("easy", "medium", "hard") else "medium"

def new_question():
    d = difficulty()
    if d == "easy":
        a = secrets.randbelow(80) + 10
        b = secrets.randbelow(50) + 10
        if secrets.randbelow(2):
            question, answer = f"{a} + {b}", a + b
        else:
            question, answer = f"{a + b} − {b}", a
    elif d == "medium":
        a = secrets.randbelow(12) + 8
        b = secrets.randbelow(7) + 3
        c = secrets.randbelow(20) + 3
        question, answer = f"{a} × {b} − {c}", a * b - c
    else:
        quotient = secrets.randbelow(12) + 3
        divisor = secrets.randbelow(8) + 2
        a = secrets.randbelow(8) + 3
        b = secrets.randbelow(8) + 2
        question, answer = f"{quotient * divisor} ÷ {divisor} + {a} × {b}", quotient + a * b
    wrong = set()
    while len(wrong) < 3:
        delta = secrets.randbelow(19) - 9
        candidate = answer + delta
        if candidate != answer and candidate >= 0:
            wrong.add(candidate)
    options = list(wrong) + [answer]
    secrets.SystemRandom().shuffle(options)
    return question, answer, options

def cooldown_remaining(chat_id, user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT until_ts FROM verification_cooldowns WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return max(0, row["until_ts"] - int(time.time())) if row else 0
    finally:
        conn.close()

def start_cooldown(chat_id, user_id):
    conn = get_conn()
    try:
        with conn:
            conn.execute("INSERT INTO verification_cooldowns(chat_id,user_id,until_ts) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET until_ts=excluded.until_ts",
                         (chat_id, user_id, int(time.time()) + 600))
    finally:
        conn.close()
