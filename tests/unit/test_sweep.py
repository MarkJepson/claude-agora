"""The stale-thread sweep policy (api/sweep.py plan()): pure, no database.

    python3 tests/unit/test_sweep.py
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_here = Path(__file__).resolve()
for d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (d / "sweep.py").exists():
        sys.path.insert(0, str(d))
        break

import sweep  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
S = "scrum-claude"
_id = [0]


def ago(days):
    return NOW - timedelta(days=days)


def msg(days, sender="a", head="hello", acked=False, **kw):
    _id[0] += 1
    return {"message_id": _id[0], "sender_role": sender, "head": head, "created_at": ago(days),
            "acked": acked, "attn_kind": None, "attn_answered_by": None, "attn_cleared_at": None, **kw}


def thread(msgs, topic="a topic", created=30):
    return {"thread_id": 1, "topic": topic, "created_at": ago(created), "messages": msgs}


def action(t):
    out = sweep.plan([t], NOW, S)
    return out[0]["action"] if out else None


class Sweep(unittest.TestCase):
    def test_recent_activity_left_alone(self):
        self.assertIsNone(action(thread([msg(3)])))

    def test_idle_thread_is_prompted(self):
        t = thread([msg(8)])
        self.assertEqual(action(t), "prompt")
        self.assertEqual(sweep.plan([t], NOW, S)[0]["since"], ago(8).date().isoformat())

    def test_empty_old_thread_uses_creation_date(self):
        self.assertEqual(action(thread([], created=9)), "prompt")
        self.assertIsNone(action(thread([], created=2)))

    def test_prompt_not_repeated_while_waiting(self):
        self.assertIsNone(action(thread([msg(20), msg(3, S, sweep.CHECK_PREFIX)])))

    def test_our_check_is_not_activity(self):
        # last real activity 20d ago, check posted 8d ago, nobody replied -> archive
        self.assertEqual(action(thread([msg(20), msg(8, S, sweep.CHECK_PREFIX)])), "archive")

    def test_ack_archives_early(self):
        self.assertEqual(action(thread([msg(20), msg(1, S, sweep.CHECK_PREFIX, acked=True)])), "archive")

    def test_ack_ignored_if_discussion_continued(self):
        t = thread([msg(20), msg(5, S, sweep.CHECK_PREFIX, acked=True), msg(2, "b", "wait, one more thing")])
        self.assertIsNone(action(t))

    def test_other_reply_is_activity_not_resolution(self):
        t = thread([msg(20), msg(10, S, sweep.CHECK_PREFIX), msg(9, "b", "resolved, I think")])
        self.assertEqual(action(t), "prompt")  # idle again 9d after the reply
        t = thread([msg(20), msg(10, S, sweep.CHECK_PREFIX), msg(3, "b", "resolved, I think")])
        self.assertIsNone(action(t))

    def test_still_open_suppresses_for_four_weeks(self):
        t = thread([msg(40), msg(30, S, sweep.CHECK_PREFIX), msg(20, "b", "Still open, waiting on X")])
        self.assertIsNone(action(t))
        t = thread([msg(60), msg(50, S, sweep.CHECK_PREFIX), msg(40, "b", "still open")])
        self.assertEqual(action(t), "prompt")

    def test_security_finding_never_archived(self):
        t = thread([msg(20), msg(9, S, sweep.CHECK_PREFIX)], topic="Security finding: exposed bucket")
        self.assertIsNone(action(t))

    def test_security_escalated_once_after_two_sweeps(self):
        t = thread([msg(20), msg(15, S, sweep.CHECK_PREFIX)], topic="open vulnerability in X")
        self.assertEqual(action(t), "escalate")
        t["messages"].append(msg(1, S, sweep.ESCALATION_PREFIX))
        self.assertIsNone(action(t))

    def test_open_security_flag_counts(self):
        t = thread([msg(20, attn_kind="security"), msg(9, S, sweep.CHECK_PREFIX)])
        self.assertIsNone(action(t))
        t = thread([msg(20, attn_kind="security", attn_cleared_at=ago(10)), msg(9, S, sweep.CHECK_PREFIX)])
        self.assertEqual(action(t), "archive")

    def test_checks_from_other_senders_ignored(self):
        self.assertEqual(action(thread([msg(9, "b", sweep.CHECK_PREFIX)])), "prompt")


if __name__ == "__main__":
    unittest.main()
