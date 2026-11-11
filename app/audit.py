"""Minimal 7-day persistent audit trail. Never store bot tokens or message bodies."""
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
    c=get_conn();c.execute('DELETE FROM audit_logs WHERE created_at < ?', (int(time.time())-7*86400,));c.commit();c.close()
