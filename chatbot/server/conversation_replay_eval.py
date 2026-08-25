"""Replay multi-turn gold conversations through the real chatbot pipeline."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable
import uuid

from chat_pipeline import ChatPipelineRequest, _local_customer_cache, _local_session_cache, process_chat_pipeline
from rag_search import get_redis


DEFAULT_CASES = Path(__file__).with_name("eval_conversation_replays.jsonl")


@dataclass
class TurnResult:
    case_id: str
    turn: int
    user: str
    intent: str
    answer: str
    source_id: str
    grounding_status: str
    suppress_send: bool
    passed: bool
    failures: list[str]


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not value.get("id") or not isinstance(value.get("turns"), list):
            raise ValueError(f"Invalid replay case at {path}:{line_number}")
        cases.append(value)
    return cases


def _expected_intents(turn: dict[str, Any]) -> set[str]:
    expected = turn.get("expected_intent", [])
    if isinstance(expected, str):
        return {expected} if expected else set()
    return {str(item) for item in expected if item}


def score_turn(
    turn: dict[str, Any],
    *,
    intent: str,
    answer: str,
    trace: dict[str, Any],
    state: dict[str, Any],
    suppress_send: bool = False,
) -> list[str]:
    failures: list[str] = []
    expected_intents = _expected_intents(turn)
    if expected_intents and intent not in expected_intents:
        failures.append(f"intent={intent!r}, expected={sorted(expected_intents)!r}")

    answer_folded = answer.casefold()
    for text in turn.get("answer_contains", []) or []:
        if str(text).casefold() not in answer_folded:
            failures.append(f"answer_missing={text!r}")
    for text in turn.get("answer_not_contains", []) or []:
        if str(text).casefold() in answer_folded:
            failures.append(f"answer_forbidden={text!r}")

    source_id = str(trace.get("source_id") or "")
    if turn.get("require_source") and not source_id:
        failures.append("source_id_missing")
    if "suppress_send" in turn and bool(turn["suppress_send"]) != suppress_send:
        failures.append(f"suppress_send={suppress_send!r}, expected={bool(turn['suppress_send'])!r}")

    state_expect = turn.get("state", {}) or {}
    if "pending_action" in state_expect:
        actual = (state.get("pending_action") or {}).get("name", "")
        if actual != state_expect["pending_action"]:
            failures.append(f"pending_action={actual!r}, expected={state_expect['pending_action']!r}")
    if "corrections_min" in state_expect:
        actual_count = len(state.get("corrections") or [])
        if actual_count < int(state_expect["corrections_min"]):
            failures.append(f"corrections={actual_count}, expected_min={state_expect['corrections_min']}")
    if "pending_slots" in state_expect:
        actual_slots = set(state.get("pending_slots") or [])
        expected_slots = set(state_expect["pending_slots"] or [])
        if not expected_slots.issubset(actual_slots):
            failures.append(f"pending_slots={sorted(actual_slots)!r}, expected={sorted(expected_slots)!r}")
    if "takeover_status" in state_expect:
        actual_status = (state.get("takeover_state") or {}).get("status", "")
        if actual_status != state_expect["takeover_status"]:
            failures.append(f"takeover_status={actual_status!r}, expected={state_expect['takeover_status']!r}")
    return failures


async def _reset_sender(redis_client: Any, brand: str, sender_id: str) -> None:
    session_key = f"{brand}:session:messenger:{sender_id}"
    history_key = f"{brand}:history:messenger:{sender_id}"
    customer_key = f"{brand}:customer:messenger:{sender_id}"
    _local_session_cache.pop(session_key, None)
    _local_customer_cache.pop(customer_key, None)
    try:
        await redis_client.delete(session_key, history_key, customer_key)
    except Exception:
        return


async def run_replays(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    case_list = list(cases)
    run_nonce = uuid.uuid4().hex[:10]
    redis_client = await get_redis()
    results: list[TurnResult] = []
    case_passes = 0
    source_required = 0
    source_present = 0

    for case in case_list:
        case_id = str(case["id"])
        brand = str(case.get("brand") or "zeo").lower()
        sender_id = f"eval-replay:{case_id}"
        await _reset_sender(redis_client, brand, sender_id)
        case_ok = True
        for index, turn in enumerate(case["turns"], start=1):
            response = await process_chat_pipeline(ChatPipelineRequest(
                brand=brand,
                sender_id=sender_id,
                text=str(turn.get("user") or ""),
                fb_name="Conversation Replay Eval",
                message_id=f"eval:{run_nonce}:{case_id}:{index}",
            ))
            session = _local_session_cache.get(f"{brand}:session:messenger:{sender_id}") or {}
            trace = session.get("last_trace") or {}
            state = session.get("conversation_state") or {}
            failures = score_turn(
                turn,
                intent=response.intent,
                answer=response.answer,
                trace=trace,
                state=state,
                suppress_send=response.suppress_send,
            )
            if turn.get("require_source"):
                source_required += 1
                source_present += int(bool(trace.get("source_id")))
            case_ok = case_ok and not failures
            results.append(TurnResult(
                case_id=case_id,
                turn=index,
                user=str(turn.get("user") or ""),
                intent=response.intent,
                answer=response.answer,
                source_id=str(trace.get("source_id") or ""),
                grounding_status=str((trace.get("grounding") or {}).get("status") or ""),
                suppress_send=response.suppress_send,
                passed=not failures,
                failures=failures,
            ))
        case_passes += int(case_ok)
        await _reset_sender(redis_client, brand, sender_id)

    result_dicts = [asdict(item) for item in results]
    total_turns = len(result_dicts)
    passed_turns = sum(1 for item in result_dicts if item["passed"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_mode": "live_pipeline_replay",
        "summary": {
            "cases": len(case_list),
            "cases_passed": case_passes,
            "turns": total_turns,
            "turns_passed": passed_turns,
            "turn_pass_rate": round(passed_turns / total_turns, 4) if total_turns else 0.0,
            "source_coverage": round(source_present / source_required, 4) if source_required else 1.0,
        },
        "results": result_dicts,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = await run_replays(load_cases(args.cases))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    redis_client = await get_redis()
    close = getattr(redis_client, "aclose", None)
    if callable(close):
        await close()
    return 0 if report["summary"]["turn_pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
