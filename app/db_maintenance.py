"""SQLite maintenance. Audit/violation logs only; never reset user warnings."""
import os
import sqlite3
import time
from app.database import DB_PATH, get_conn, get_setting

RETENTION_CHOICES = ("7", "30", "90", "180", "365", "0")
RESTORE_PATH = DB_PATH + ".restore-pending"
REQUIRED_TABLES = {"settings", "users", "violations", "audit_logs", "verification_sessions"}

def cleanup_logs():
    days = get_setting("log_retention_days", "7")
    if days not in RETENTION_CHOICES or days == "0":
        return (0, 0)
    cutoff = int(time.time()) - int(days) * 86400
    conn = get_conn()
    try:
        with conn:
            audit_count = conn.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff,)).rowcount
            violation_count = conn.execute(
                "DELETE FROM violations WHERE created_at < datetime(?, 'unixepoch')", (cutoff,)
            ).rowcount
        return audit_count, violation_count
    finally:
        conn.close()

def clear_all_logs():
    conn = get_conn()
    try:
        with conn:
            audit_count = conn.execute("DELETE FROM audit_logs").rowcount
            violation_count = conn.execute("DELETE FROM violations").rowcount
        return audit_count, violation_count
    finally:
        conn.close()

def backup_database(destination):
    source = get_conn()
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

def validate_backup(path):
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性校验失败")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES.issubset(names):
            raise ValueError("备份缺少必要的数据表")
    finally:
        conn.close()

def apply_pending_restore():
    """Called at startup before web/bot threads open database connections."""
    if not os.path.isfile(RESTORE_PATH):
        return False
    validate_backup(RESTORE_PATH)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    # Keep a rollback copy of the current database before replacing it.
    if os.path.isfile(DB_PATH):
        backup_database(DB_PATH + ".pre-restore.bak")
    src = sqlite3.connect("file:" + RESTORE_PATH + "?mode=ro", uri=True)
    dst = sqlite3.connect(DB_PATH)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    os.unlink(RESTORE_PATH)
    return True
