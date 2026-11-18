"""Static regression tests: verification controls must not nest in robot form."""
import os
import unittest
from html.parser import HTMLParser


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            if self.depth:
                self.nested = True
            self.depth += 1
            self.current = {"action": attrs.get("action", ""), "inputs": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"].append(attrs.get("name", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1
            if self.depth == 0:
                self.current = None


class SettingsFormTests(unittest.TestCase):
    def test_verification_settings_form_is_isolated(self):
        template = os.path.join(os.path.dirname(__file__), "..", "app", "web", "templates", "settings.html")
        with open(template, encoding="utf-8") as f:
            markup = f.read()
        forms = Forms()
        forms.feed(markup)
        self.assertFalse(forms.nested, "Nested forms can disable unrelated robot switches")
        self.assertEqual(forms.depth, 0)
        verification = [f for f in forms.forms if "save_verification_settings" in f["action"]]
        self.assertEqual(len(verification), 1)
        names = verification[0]["inputs"]
        self.assertIn("verification_form_marker", names)
        self.assertIn("_csrf_token", names)
        self.assertNotIn("observation_enabled", names)
        self.assertNotIn("ocr_enabled", names)
        self.assertNotIn("log_retention_days", names)


if __name__ == "__main__":
    unittest.main()
