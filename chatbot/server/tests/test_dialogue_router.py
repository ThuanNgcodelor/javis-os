import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from dialogue_router import build_route_decision  # noqa: E402
from query_understanding import build_query_plan  # noqa: E402


class DialogueRouterTests(unittest.TestCase):
    def _plan(self, raw, normalized, **kwargs):
        return build_query_plan(
            raw_text=raw,
            norm_text=normalized,
            brand=kwargs.get("brand", "zeo"),
            query_entities={},
            reference_resolution=kwargs.get("reference_resolution", {}),
            conversation_state=kwargs.get("state", {}),
        )

    def test_unresolved_ordinal_requires_clarification(self):
        plan = self._plan("Cái thứ hai giá bao nhiêu", "cai thu hai gia bao nhieu")
        decision = build_route_decision(plan, {})
        self.assertEqual(decision.action, "clarify")
        self.assertEqual(decision.reason, "ORDINAL_WITHOUT_OPTIONS")

    def test_pending_link_confirmation_selects_catalog_tool(self):
        state = {
            "pending_action": {
                "name": "send_product_link",
                "status": "waiting_confirmation",
            }
        }
        plan = self._plan("Ok shop", "ok shop", state=state)
        decision = build_route_decision(plan, state)
        self.assertEqual(decision.tool, "shopee_product_reference")
        self.assertEqual(decision.reason, "PENDING_LINK_CONFIRMED")

    def test_non_affirmation_does_not_execute_pending_action(self):
        state = {
            "pending_action": {
                "name": "send_product_link",
                "status": "waiting_confirmation",
            }
        }
        plan = self._plan("Giá bao nhiêu", "gia bao nhieu", state=state)
        decision = build_route_decision(plan, state)
        self.assertEqual(decision.action, "legacy")

    def test_customer_brand_correction_requests_missing_product(self):
        plan = self._plan(
            "Không phải Pano, ý mình là ZeO",
            "khong phai pano y minh la zeo",
        )
        decision = build_route_decision(plan, {})
        self.assertEqual(decision.action, "clarify")
        self.assertEqual(decision.reason, "CORRECTION_REQUIRES_PRODUCT")

    def test_cfc_operational_requests_route_to_capability_boundaries(self):
        cases = [
            (
                "NPK 16-16-8 trong kho còn hàng không",
                "npk 16 16 8 trong kho con hang khong",
                "cfc_inventory_unavailable",
            ),
            (
                "Tra cứu đơn DH-2026-889",
                "tra cuu don dh 2026 889",
                "cfc_order_status_unavailable",
            ),
            (
                "Số tôi có tích điểm chưa",
                "so toi co tich diem chua",
                "cfc_loyalty_unavailable",
            ),
        ]
        for raw, normalized, expected_intent in cases:
            with self.subTest(raw=raw):
                plan = self._plan(raw, normalized, brand="cfc")
                decision = build_route_decision(plan, {})
                self.assertEqual(decision.action, "capability_boundary")
                self.assertEqual(decision.intent, expected_intent)


if __name__ == "__main__":
    unittest.main()
