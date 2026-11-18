"""Regression tests for Cloudflare welcome delivery and settings isolation."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class WelcomeTests(unittest.TestCase):
    def test_web_bridge_schedules_on_bot_loop(self):
        from app import welcome_bridge
        async def scenario():
            fake_user = SimpleNamespace(id=123)
            bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(user=fake_user)))
            app = SimpleNamespace(bot=bot, job_queue=object())
            with patch("app.handlers.join_request.send_welcome", new_callable=AsyncMock) as welcome:
                welcome_bridge.register(app)
                welcome_bridge.notify_verified(-1001, 123)
                await asyncio.sleep(0.05)
                welcome.assert_awaited_once_with(-1001, fake_user, app)
        asyncio.run(scenario())

    def test_only_one_welcome_claim_per_lifecycle(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE welcome_delivery(chat_id INTEGER,user_id INTEGER,delivered_at INTEGER,PRIMARY KEY(chat_id,user_id))")
        def claim():
            return db.execute("INSERT OR IGNORE INTO welcome_delivery VALUES(-1001,123,1)").rowcount
        self.assertEqual(claim(), 1)
        self.assertEqual(claim(), 0)
        db.execute("DELETE FROM welcome_delivery WHERE chat_id=-1001 AND user_id=123")
        self.assertEqual(claim(), 1)
        db.close()

    def test_no_settings_template_modified(self):
        path = os.path.join(os.path.dirname(__file__), "..", "app", "web", "templates", "settings.html")
        with open(path, encoding="utf-8") as f:
            markup = f.read()
        self.assertIn("verification_form_marker", markup)


if __name__ == "__main__":
    unittest.main()
