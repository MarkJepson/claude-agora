"""Operator-token configuration: whitespace is ignored, blank means unset, and
REQUIRE_OPERATOR_TOKEN makes a blank token a startup error. No database needed.
Needs the API's dependencies (fastapi etc.); from the repo root:

    python3 tests/unit/test_operator_token.py
"""
import importlib
import os
import sys
import unittest
from pathlib import Path

_here = Path(__file__).resolve()
for d in [p / "api" for p in _here.parents] + [Path("/app")]:
    if (d / "main.py").exists():
        sys.path.insert(0, str(d))
        break


def load(token, require=None):
    """Imports main fresh with the given environment."""
    os.environ["DATABASE_URL"] = "mysql://x@localhost/x"
    for k, v in (("OPERATOR_TOKEN", token), ("REQUIRE_OPERATOR_TOKEN", require)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class OperatorToken(unittest.TestCase):
    def test_trailing_newline_is_ignored(self):
        self.assertEqual(load("abc123\n")._OPERATOR_TOKEN, "abc123")

    def test_surrounding_spaces_are_ignored(self):
        self.assertEqual(load("  abc123 \r\n")._OPERATOR_TOKEN, "abc123")

    def test_blank_or_missing_means_unset(self):
        self.assertIsNone(load("")._OPERATOR_TOKEN)
        self.assertIsNone(load("  \n")._OPERATOR_TOKEN)
        self.assertIsNone(load(None)._OPERATOR_TOKEN)

    def test_not_required_by_default(self):
        load("")._check_operator_token_config()  # must not raise

    def test_required_but_blank_refuses_to_start(self):
        for tok in ("", "  \n", None):
            with self.assertRaises(RuntimeError):
                load(tok, "true")._check_operator_token_config()

    def test_required_and_set_is_fine(self):
        load("abc\n", "true")._check_operator_token_config()

    def test_require_flag_spellings(self):
        for v in ("1", "true", "TRUE", "yes"):
            with self.assertRaises(RuntimeError):
                load("", v)._check_operator_token_config()
        load("", "false")._check_operator_token_config()


if __name__ == "__main__":
    unittest.main()
