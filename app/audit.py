"""Persistent audit trail. Never store bot tokens or message bodies."""
import time
from app.database import get_conn

def audit(category, actor, action, target='', detail=''):
    try:
        c=get_conn()
        c.execute('INSERT INTO audit_logs(created_at,category,actor,action,target,detail) VALUES(?,?,?,?,?,?)',
                  (int(time.time()),str(category)[:32],str(actor)[:80],str(action)[:64],str(target)[:80],str(detail)[:240]))
        c.commit();c.close()
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Audit insert failed')

def cleanup():
    from app.db_maintenance import cleanup_logs
    return cleanup_logs()
