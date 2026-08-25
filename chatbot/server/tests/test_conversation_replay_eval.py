import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from conversation_replay_eval import DEFAULT_CASES, load_cases, score_turn  # noqa: E402


class ConversationReplayEvalTests(unittest.TestCase):
    def test_gold_file_loads_multi_turn_cases(self):
        cases = load_cases(DEFAULT_CASES)
        self.assertGreaterEqual(len(cases), 11)
        self.assertTrue(any(len(case["turns"]) > 1 for case in cases))

    def test_invalid_case_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text('{"id":"missing-turns"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_scorer_checks_intent_source_and_context_state(self):
        failures = score_turn(
            {
                "expected_intent": "company_contact_information",
                "answer_contains": ["1900 5307"],
                "require_source": True,
                "state": {"pending_action": "send_product_link", "corrections_min": 1},
            },
            intent="fallback_low_confidence",
            answer="Chưa có dữ liệu",
            trace={},
            state={"pending_action": {}, "corrections": []},
        )
        self.assertTrue(any(item.startswith("intent=") for item in failures))
        self.assertIn("source_id_missing", failures)
        self.assertTrue(any(item.startswith("pending_action=") for item in failures))
        self.assertTrue(any(item.startswith("corrections=") for item in failures))


if __name__ == "__main__":
    unittest.main()
