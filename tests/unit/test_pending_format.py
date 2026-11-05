"""scripts/relay's pending listing: grouping by thread and signature
stripping. No relay or database needed:

    python3 tests/unit/test_pending_format.py
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts" / "relay"
_loader = importlib.machinery.SourceFileLoader("relay_client", str(_path))
_spec = importlib.util.spec_from_loader("relay_client", _loader)
relay = importlib.util.module_from_spec(_spec)
_loader.exec_module(relay)


def _m(mid, tid, sender, body, topic=None):
    return {"message_id": mid, "thread_id": tid, "topic": topic or f"topic {tid}",
            "sender_role": sender, "body": body, "created_at": "2026-01-01T00:00:00"}


class StripSignature(unittest.TestCase):
    def test_strips_own_signature(self):
        self.assertEqual(relay._strip_signature("🤖 Claude · alpha. Re #3: ok", "alpha"), "Re #3: ok")

    def test_signature_on_its_own_line(self):
        self.assertEqual(relay._preview("🤖 Claude · alpha\n\nBody here", sender_role="alpha"), "Body here")

    def test_other_roles_signature_kept(self):
        body = "🤖 Claude · beta. text"
        self.assertEqual(relay._strip_signature(body, "alpha"), body)

    def test_role_prefix_not_matched(self):
        body = "🤖 Claude · alpha-two. text"
        self.assertEqual(relay._strip_signature(body, "alpha"), body)

    def test_no_signature(self):
        self.assertEqual(relay._preview("Agreed", sender_role="operator"), "Agreed")


class ByThread(unittest.TestCase):
    def test_groups_in_first_message_order(self):
        pending = [_m(5, 2, "a", "x"), _m(1, 1, "a", "x"), _m(3, 2, "b", "x"), _m(4, 1, "b", "x")]
        groups = relay._by_thread(pending)
        self.assertEqual([[m["message_id"] for m in g] for g in groups], [[1, 4], [3, 5]])

    def test_short_output(self):
        pending = [_m(1, 7, "alpha", "🤖 Claude · alpha. hello", topic="T"),
                   _m(2, 7, "operator", "Agreed", topic="T")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relay._print_short(pending)
        self.assertEqual(out.getvalue().splitlines(), [
            'thread 7 "T" (2 unread)',
            "  #1 alpha: hello",
            "  #2 operator: Agreed",
        ])


if __name__ == "__main__":
    unittest.main()
