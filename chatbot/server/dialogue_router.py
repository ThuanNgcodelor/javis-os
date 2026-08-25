"""Fact-free dialogue routing decisions built from QueryPlan and state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from query_understanding import QueryPlan


@dataclass(frozen=True)
class RouteDecision:
    action: str = "legacy"
    tool: str = ""
    intent: str = ""
    reason: str = "LEGACY_ROUTE"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_affirmation(text: str) -> bool:
    return bool(re.fullmatch(
        r"(ok|oke|okay|da|vang|u|uh|um|uk|duoc|gui|gui di|gui link|gui link di|"
        r"cho minh xin|cho xin|yes|co|dung roi|chinh xac)( nha| nhe| shop)?",
        text.strip(),
    ))


def build_route_decision(
    plan: QueryPlan,
    conversation_state: dict[str, Any],
) -> RouteDecision:
    """Choose only a route/tool. Facts remain owned by FAQ/catalog/RAG."""
    pending_action = conversation_state.get("pending_action") or {}
    corrected_brand = (plan.constraints or {}).get("corrected_brand")
    if corrected_brand:
        return RouteDecision(
            action="clarify",
            intent="customer_correction_clarify",
            reason="CORRECTION_REQUIRES_PRODUCT",
            confidence=0.98,
        )

    if (
        pending_action.get("name") == "send_product_link"
        and pending_action.get("status") == "waiting_confirmation"
        and _is_affirmation(plan.normalized_query)
    ):
        return RouteDecision(
            action="tool",
            tool="shopee_product_reference",
            intent="product_link_query",
            reason="PENDING_LINK_CONFIRMED",
            confidence=0.99,
        )

    if plan.needs_context and plan.references.get("ordinal"):
        return RouteDecision(
            action="clarify",
            intent="context_clarification",
            reason="ORDINAL_WITHOUT_OPTIONS",
            confidence=0.99,
        )

    if plan.intent == "company_contact_information":
        return RouteDecision(
            action="tool",
            tool="faq_by_intent",
            intent=plan.intent,
            reason="GROUNDED_CONTACT_LOOKUP",
            confidence=plan.intent_confidence,
        )

    if plan.intent == "cfc_dealer_location_request":
        return RouteDecision(
            action="tool",
            tool="faq_by_intent",
            intent=plan.intent,
            reason="DEALER_LOCATION_LOOKUP",
            confidence=plan.intent_confidence,
        )

    return RouteDecision()
