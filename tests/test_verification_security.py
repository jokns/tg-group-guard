"""Unit tests for verification security and mathematical challenge invariants.

Run: python -m unittest discover -s tests -p 'test_verification_security.py'
"""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from app.config import Config
from app.math_verification import new_question
from app.verification_web import telegram_identity


class TelegramIdentityTests(unittest.TestCase):
    def signed(self, user_id=12345, auth_date=None):
        data = {
            "auth_date": str(int(time.time()) if auth_date is None else auth_date),
            "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
        }
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", Config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(data)

    def test_valid_signature(self):
        with patch.object(Config, "BOT_TOKEN", "123456:TEST_SECRET"):
            self.assertEqual(telegram_identity(self.signed()), 12345)

    def test_forged_user_rejected(self):
        with patch.object(Config, "BOT_TOKEN", "123456:TEST_SECRET"):
            signed = self.signed()
            with self.assertRaises(ValueError):
                telegram_identity(signed.replace("12345", "99999"))

    def test_expired_identity_rejected(self):
        with patch.object(Config, "BOT_TOKEN", "123456:TEST_SECRET"):
            with self.assertRaises(ValueError):
                telegram_identity(self.signed(auth_date=int(time.time())-700))

    def test_repeated_fields_rejected(self):
        with patch.object(Config, "BOT_TOKEN", "123456:TEST_SECRET"):
            with self.assertRaises(ValueError):
                telegram_identity(self.signed() + "&user=attacker")


class MathQuestionTests(unittest.TestCase):
    def test_all_difficulties_have_one_correct_answer(self):
        from app import math_verification
        for difficulty in ("easy", "medium", "hard"):
            with self.subTest(difficulty=difficulty), patch.object(math_verification, "difficulty", return_value=difficulty):
                for _ in range(50):
                    question, answer, options = new_question()
                    self.assertEqual(len(options), 4)
                    self.assertEqual(len(set(options)), 4)
                    self.assertEqual(options.count(answer), 1)
                    self.assertTrue(question)


if __name__ == "__main__":
    unittest.main()
