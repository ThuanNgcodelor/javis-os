import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import ai_engine  # noqa: E402
import chat_pipeline  # noqa: E402
import conversation_orchestrator  # noqa: E402
from conversation_orchestrator import (  # noqa: E402
    build_conversation_context,
    build_conversation_messages,
    is_safe_assist_plan,
    load_orchestrator_config,
    recover_contextual_followup_plan,
    select_tool_result_items,
    should_run_orchestrator,
    validate_orchestrator_plan,
)
from chat_pipeline import ChatPipelineRequest, process_chat_pipeline  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.expirations = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        values = self.lists.get(key, [])
        self.lists[key] = values[start:] if start < 0 else values[start:end + 1]

    async def expire(self, key, seconds):
        self.expirations[key] = seconds


PHONE_UPDATE_PLAN = {
    "intent": "customer_profile_update",
    "confidence": 0.98,
    "is_followup": True,
    "topic_changed": False,
    "reference": {"type": "none", "result_id": "", "entity_ids": []},
    "requested_fields": ["phone"],
    "tool": "customer_profile_update",
    "arguments": {"field": "phone", "operation": "replace"},
    "missing_slots": [],
    "reason_code": "CUSTOMER_REPLACES_PHONE",
}


class ConversationOrchestratorUnitTests(unittest.TestCase):
    def test_context_is_bounded_and_redacts_pii(self):
        state = {
            "conversation_summary": "Khách đang hỏi đại lý",
            "recent_turns": [{
                "user": "Số của tôi 0909123456",
                "bot": "Mình đã ghi nhận",
            }],
            "last_tool_results": [{"tool": "sales_location_search", "items": []}],
        }
        messages = build_conversation_messages(state, "xin số điện thoại", limit=2)
        context = build_conversation_context(state)
        rendered = json.dumps(messages, ensure_ascii=False) + json.dumps(context, ensure_ascii=False)
        self.assertNotIn("0909123456", rendered)
        self.assertIn("[PHONE]", rendered)
        self.assertEqual(messages[-1], {"role": "user", "content": "xin số điện thoại"})

    def test_assist_requires_followup_and_allowlisted_tool(self):
        plan = validate_orchestrator_plan({
            "intent": "dealer_contact_followup",
            "confidence": 0.95,
            "is_followup": True,
            "tool": "dealer_contact_lookup",
            "reference": {"entity_ids": ["location-1"]},
        })
        self.assertTrue(is_safe_assist_plan(plan, min_confidence=0.85))
        self.assertFalse(is_safe_assist_plan({**plan, "tool": "arbitrary_tool"}, min_confidence=0.85))
        self.assertFalse(is_safe_assist_plan({**plan, "confidence": 0.4}, min_confidence=0.85))

        profile_plan = validate_orchestrator_plan(PHONE_UPDATE_PLAN)
        self.assertEqual(profile_plan["intent"], "customer_profile_update")
        self.assertTrue(is_safe_assist_plan(profile_plan, min_confidence=0.85))

    def test_assist_only_reads_contextual_turns(self):
        state = {"recent_turns": [{"user": "Tìm đại lý", "bot": "Đây là danh sách"}]}
        self.assertFalse(should_run_orchestrator(
            "assist",
            conversation_state=state,
            normalized_text="cho toi biet CFC co NPK khong",
            brand="cfc",
        ))
        self.assertTrue(should_run_orchestrator(
            "assist",
            conversation_state=state,
            normalized_text="xin so dien thoai cac cho tren",
            brand="cfc",
        ))
        with patch.dict(os.environ, {"CHAT_CONVERSATION_MODE": "assist"}):
            self.assertEqual(load_orchestrator_config()["mode"], "assist")

    def test_tool_reference_can_select_one_previous_entity(self):
        result = {
            "items": [
                {"entity_id": "dealer-1", "display_name": "Đại lý Một"},
                {"entity_id": "dealer-2", "display_name": "Đại lý Hai"},
            ]
        }
        plan = {"reference": {"entity_ids": []}}
        selected = select_tool_result_items(result, plan, "cho so 2 o dau")
        self.assertEqual([item["entity_id"] for item in selected], ["dealer-2"])

    def test_context_recovery_selects_previous_dealers_for_public_phone_request(self):
        state = {
            "last_tool_results": [{
                "result_id": "dealer-result-1",
                "tool": "sales_location_search",
                "items": [{"entity_id": "dealer-1", "public_phone": "0292000000"}],
            }],
        }
        plan = recover_contextual_followup_plan(state, "cho xin so dien thoai cac cho do")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["intent"], "dealer_contact_followup")
        self.assertEqual(plan["tool"], "dealer_contact_lookup")
        self.assertEqual(plan["reference"]["result_id"], "dealer-result-1")

    def test_context_recovery_does_not_guess_company_hotline(self):
        state = {
            "last_tool_results": [{
                "result_id": "dealer-result-1",
                "tool": "sales_location_search",
                "items": [{"entity_id": "dealer-1", "public_phone": "0292000000"}],
            }],
        }
        self.assertIsNone(recover_contextual_followup_plan(state, "xin hotline cong ty"))


class ConversationOrchestratorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        chat_pipeline._local_session_cache.clear()
        chat_pipeline._local_customer_cache.clear()

    async def test_customer_phone_change_without_new_number_asks_for_it(self):
        redis_client = FakeRedis()
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline.load_orchestrator_config", return_value={
                    "mode": "assist", "min_confidence": 0.85, "history_limit": 12,
                }), \
                patch("chat_pipeline.plan_conversation_turn_with_ollama", new=AsyncMock(return_value=PHONE_UPDATE_PLAN)):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="phone-change-missing",
                message_id="phone-change-missing-1",
                text="Ê tôi đổi số điện thoại được ko",
            ))

        self.assertEqual(result.intent, "customer_phone_change_request")
        self.assertIn("gửi số điện thoại mới", result.answer)
        self.assertEqual(result.fallback_reason, "PHONE_CHANGE_MISSING_NEW_NUMBER")

    async def test_dealer_contact_followup_uses_previous_public_locations(self):
        redis_client = FakeRedis()
        session_key = "cfc:session:messenger:dealer-followup"
        chat_pipeline._local_session_cache[session_key] = {
            "revision": 1,
            "last_user_message": "Khu vực Thới Lai có đại lý nào?",
            "last_bot_reply": "Có các đại lý...",
            "last_intent": "cfc_dealer_location_request",
            "conversation_state": {
                "schema_version": 4,
                "active_goal": {"name": "dealer_lookup", "stage": "browsing"},
                "recent_turns": [{
                    "user": "Khu vực Thới Lai có đại lý nào?",
                    "bot": "Có các đại lý...",
                }],
                "last_tool_results": [{
                    "result_id": "dealer-result-1",
                    "tool": "sales_location_search",
                    "source_id": "amis:public:sales-locations:active",
                    "items": [
                        {
                            "entity_id": "location-1",
                            "display_name": "Đại lý Một",
                            "public_phone": "0292123456",
                            "public_address": "Thới Lai, Cần Thơ",
                        },
                        {
                            "entity_id": "location-2",
                            "display_name": "Đại lý Hai",
                            "public_phone": "",
                            "public_address": "Ô Môn, Cần Thơ",
                        },
                    ],
                }],
            },
        }

        plan = {
            "intent": "dealer_contact_followup",
            "confidence": 0.96,
            "is_followup": True,
            "topic_changed": False,
            "reference": {"type": "tool_result", "result_id": "dealer-result-1", "entity_ids": ["location-1", "location-2"]},
            "requested_fields": ["public_phone"],
            "tool": "dealer_contact_lookup",
            "arguments": {},
            "missing_slots": [],
            "reason_code": "PHONE_OF_PREVIOUS_DEALERS",
        }
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline.load_orchestrator_config", return_value={
                    "mode": "assist", "min_confidence": 0.85, "history_limit": 12,
                }), \
                patch("chat_pipeline.plan_conversation_turn_with_ollama", new=AsyncMock(return_value=plan)), \
                patch("chat_pipeline._llm_nlu_config", return_value=("off", 0.3, 0.72)):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="dealer-followup",
                message_id="dealer-followup-2",
                text="xin số điện thoại các chỗ trên",
            ))

        self.assertEqual(result.intent, "dealer_contact_followup")
        self.assertIn("0292123456", result.answer)
        self.assertIn("Đại lý Hai", result.answer)
        self.assertIn("chưa có SĐT công khai", result.answer)
        state = chat_pipeline._local_session_cache[session_key]["conversation_state"]
        self.assertEqual(state["last_tool_results"][0]["result_id"], "dealer-result-1")

    async def test_dealer_contact_followup_recovers_when_ollama_returns_empty(self):
        redis_client = FakeRedis()
        session_key = "cfc:session:messenger:dealer-followup-recovery"
        chat_pipeline._local_session_cache[session_key] = {
            "revision": 1,
            "last_user_message": "Khu vực Thới Lai có đại lý nào?",
            "last_bot_reply": "Có các đại lý...",
            "last_intent": "cfc_dealer_location_request",
            "conversation_state": {
                "schema_version": 4,
                "active_goal": {"name": "dealer_lookup", "stage": "browsing"},
                "recent_turns": [{
                    "user": "Khu vực Thới Lai có đại lý nào?",
                    "bot": "Có các đại lý...",
                }],
                "last_tool_results": [{
                    "result_id": "dealer-result-recovery",
                    "tool": "sales_location_search",
                    "source_id": "amis:public:sales-locations:active",
                    "items": [{
                        "entity_id": "location-1",
                        "display_name": "Đại lý Một",
                        "public_phone": "0292123456",
                        "public_address": "Thới Lai, Cần Thơ",
                    }],
                }],
            },
        }
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline.load_orchestrator_config", return_value={
                    "mode": "assist", "min_confidence": 0.85, "history_limit": 12,
                }), \
                patch("chat_pipeline.plan_conversation_turn_with_ollama", new=AsyncMock(return_value=None)), \
                patch("chat_pipeline._llm_nlu_config", return_value=("off", 0.3, 0.72)):
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="dealer-followup-recovery",
                message_id="dealer-followup-recovery-2",
                text="Cho xin số điện thoại các chỗ đó đi",
            ))

        self.assertEqual(result.intent, "dealer_contact_followup")
        self.assertIn("0292123456", result.answer)
        self.assertNotIn("cfccobay.com", result.answer)

    async def test_customer_can_replace_own_phone_without_new_lead_notification(self):
        redis_client = FakeRedis()
        session_key = "cfc:session:messenger:phone-change"
        customer_key = "cfc:customer:messenger:phone-change"
        chat_pipeline._local_session_cache[session_key] = {
            "revision": 1,
            "last_user_message": "0388509046",
            "last_bot_reply": "Mình đã ghi nhận",
            "last_intent": "contact_phone_provided",
            "conversation_state": {
                "schema_version": 4,
                "confirmed_slots": {"phone": "0388509046", "area": "Cần Thơ"},
                "recent_turns": [],
            },
        }
        chat_pipeline._local_customer_cache[customer_key] = {
            "brand": "CFC",
            "sender_id": "phone-change",
            "phone": "0388509046",
            "customer_phone": "0388509046",
            "area": "Cần Thơ",
            "lead_stage": "lead_ready",
        }
        with patch("chat_pipeline.get_redis", new=AsyncMock(return_value=redis_client)), \
                patch("chat_pipeline.load_orchestrator_config", return_value={
                    "mode": "assist", "min_confidence": 0.85, "history_limit": 12,
                }), \
                patch("chat_pipeline.plan_conversation_turn_with_ollama", new=AsyncMock(return_value=PHONE_UPDATE_PLAN)), \
                patch("chat_pipeline._async_update_customer_profile", new=AsyncMock()) as update_profile, \
                patch("chat_pipeline.notify_new_lead", new=AsyncMock()) as notify:
            result = await process_chat_pipeline(ChatPipelineRequest(
                brand="cfc",
                sender_id="phone-change",
                message_id="phone-change-2",
                text="0363050996 đổi qua số này nè",
            ))
            await asyncio.sleep(0)

        self.assertEqual(result.intent, "customer_phone_updated")
        self.assertIn("***0996", result.answer)
        self.assertEqual(chat_pipeline._local_customer_cache[customer_key]["phone"], "0363050996")
        update_profile.assert_awaited_once()
        notify.assert_not_awaited()


class ConversationPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_is_bounded_background_only(self):
        with patch.object(
            conversation_orchestrator,
            "_collect_conversation_shadow",
            new=AsyncMock(),
        ) as collect:
            status = conversation_orchestrator.schedule_conversation_shadow(
                brand="cfc",
                sender_id="shadow-user",
                message_id="shadow-message",
                user_query="xin số điện thoại các chỗ trên",
                conversation_messages=[{"role": "user", "content": "xin số điện thoại các chỗ trên"}],
                conversation_context={},
                deterministic_plan={"intent": "unknown"},
            )
            await conversation_orchestrator.drain_conversation_shadow_tasks()

        self.assertEqual(status, "scheduled")
        collect.assert_awaited_once()

    async def test_planner_accepts_full_history_and_validates_schema(self):
        raw = json.dumps({
            "intent": "dealer_contact_followup",
            "confidence": 0.94,
            "is_followup": True,
            "reference": {"type": "tool_result", "result_id": "r1", "entity_ids": ["d1"]},
            "requested_fields": ["public_phone"],
            "tool": "dealer_contact_lookup",
        })
        with patch.object(ai_engine, "call_ollama", new=AsyncMock(return_value=raw)) as call:
            result = await ai_engine.plan_conversation_turn_with_ollama(
                user_query="xin số điện thoại các chỗ trên",
                brand="cfc",
                conversation_messages=[
                    {"role": "user", "content": "Tìm đại lý"},
                    {"role": "assistant", "content": "Có danh sách"},
                    {"role": "user", "content": "xin số điện thoại các chỗ trên"},
                ],
                conversation_context={"last_tool_results": [{"result_id": "r1"}]},
            )

        self.assertEqual(result["intent"], "dealer_contact_followup")
        self.assertEqual(result["reference"]["result_id"], "r1")
        self.assertEqual(call.await_args.kwargs["messages"][-1]["content"], "Có danh sách")
