"""Membership status regression tests: Telegram restricted.is_member semantics."""
import unittest
from types import SimpleNamespace


class MembershipTransitionTests(unittest.TestCase):
    def test_rejoin_as_restricted_is_new_membership(self):
        from app.handlers.join_request import is_present_member
        left = SimpleNamespace(status="left")
        restricted = SimpleNamespace(status="restricted", is_member=True)
        self.assertFalse(is_present_member(left))
        self.assertTrue(is_present_member(restricted))

    def test_restriction_change_is_not_rejoin(self):
        from app.handlers.join_request import is_present_member
        member = SimpleNamespace(status="member")
        restricted = SimpleNamespace(status="restricted", is_member=True)
        self.assertTrue(is_present_member(member))
        self.assertTrue(is_present_member(restricted))

    def test_restricted_nonmember_is_absent(self):
        from app.handlers.join_request import is_present_member
        self.assertFalse(is_present_member(SimpleNamespace(status="restricted", is_member=False)))


if __name__ == "__main__":
    unittest.main()
