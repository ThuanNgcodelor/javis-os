"""Bridge read-only vào runtime live của Javis.

Mục tiêu của module này là kéo trạng thái đang chạy của Javis về Javis OS như một
nguồn context nhỏ, an toàn, không side effect. Nó không gọi assistant chat, không
đổi workflow, không đụng dữ liệu ghi.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from context_compiler import ContextItem, HeuristicTokenizer


_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class LiveJavisBridgeResult:
    items: tuple[ContextItem, ...]
    roots: tuple[str, ...]
    endpoint_count: int
    source_count: int
    error_count: int


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in ("1", "true", "yes", "on", "enabled")


def _as_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _as_float(value, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _normalize_base_url(raw) -> str:
    base = str(raw or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


def _live_cfg(settings: dict | None) -> dict:
    runtime = (settings or {}).get("context_runtime") or {}
    raw = runtime.get("live_javis")
    return raw if isinstance(raw, dict) else {}


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_REPO_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path.resolve())


def _request_json(
    base_url: str,
    endpoint: str,
    timeout_seconds: float,
) -> dict:
    url = base_url + endpoint
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "javis-os-live-bridge/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - fail-soft boundary
        return {"error": f"{type(exc).__name__}: {exc}"}

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - fail-soft boundary
        return {"error": f"decode_error:{type(exc).__name__}: {exc}"}


def _clip(text: str, limit: int = 180) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _build_item(
    kind: str,
    base_url: str,
    endpoint: str,
    content: str,
    index: int,
    relevance: float,
    confidence: float,
) -> ContextItem:
    tokenizer = HeuristicTokenizer("javis", "live")
    source_ref = f"javis-live:{base_url}{endpoint}"
    return ContextItem(
        id=f"live:{index}:{kind}",
        kind=kind,
        content=content,
        source_ref=source_ref,
        token_cost=tokenizer.count_text(content),
        relevance=relevance,
        confidence=confidence,
        authority=0.9,
        freshness=1.0,
        required=False,
        trust="live_state",
        metadata={"base_url": base_url, "endpoint": endpoint},
    )


def _summarize_health(data: dict) -> str:
    lines = [
        "Javis live health:",
        f"- service={data.get('service', 'unknown')}",
        f"- ollama={data.get('ollama', 'unknown')}",
        f"- redis={data.get('redis', 'unknown')}",
    ]
    embed_model = data.get("embed_model")
    if embed_model:
        lines.append(f"- embed_model={embed_model}")
    indexes = data.get("vector_indexes")
    if isinstance(indexes, list):
        clipped = [_clip(str(x), 64) for x in indexes[:6]]
        lines.append(f"- vector_indexes={', '.join(clipped) if clipped else 'none'}")
    return "\n".join(lines)


def _summarize_workflows(data: dict, max_items: int) -> str:
    workflows = data.get("workflows")
    if not isinstance(workflows, list):
        workflows = []
    active = sum(1 for item in workflows if item.get("active"))
    lines = [
        "Javis n8n workflows:",
        f"- total={len(workflows)}",
        f"- active={active}",
    ]
    for item in workflows[:max_items]:
        tags = item.get("tags") or []
        tag_text = ", ".join(_clip(str(tag), 32) for tag in tags[:3]) if tags else "none"
        lines.append(
            f"- {item.get('name', '?')} | active={bool(item.get('active'))} "
            f"| updatedAt={_clip(item.get('updatedAt', ''), 32)} | tags={tag_text}"
        )
    if len(workflows) > max_items:
        lines.append(f"- ... +{len(workflows) - max_items} more")
    return "\n".join(lines)


def _summarize_executions(data: dict, max_items: int) -> str:
    executions = data.get("executions")
    if not isinstance(executions, list):
        executions = []
    lines = [
        "Javis n8n executions:",
        f"- total={len(executions)}",
    ]
    stats = data.get("stats")
    if isinstance(stats, dict):
        compact_stats = ", ".join(
            f"{key}={stats.get(key, 0)}" for key in ("success", "error", "running", "waiting")
        )
        lines.append(f"- stats={compact_stats}")
    for item in executions[:max_items]:
        lines.append(
            f"- {item.get('workflowName', '?')} | status={item.get('status', '?')} "
            f"| startedAt={_clip(item.get('startedAt', ''), 32)}"
        )
    if len(executions) > max_items:
        lines.append(f"- ... +{len(executions) - max_items} more")
    return "\n".join(lines)


def _summarize_file_status(data: dict, max_items: int) -> str:
    files = data.get("files")
    if not isinstance(files, list):
        files = []
    lines = [
        "Javis n8n file-status:",
        f"- tracked_files={len(files)}",
    ]
    note = str(data.get("note") or "").strip()
    if note:
        lines.append(f"- note={note}")
    for item in files[:max_items]:
        lines.append(
            f"- {item.get('workflow_file', '?')} | changed={bool(item.get('has_changes'))} "
            f"| baseline={_clip(item.get('baseline_source', ''), 24)}"
        )
    if len(files) > max_items:
        lines.append(f"- ... +{len(files) - max_items} more")
    return "\n".join(lines)


def _summarize_prompts(data: dict, max_items: int) -> str:
    prompts = data.get("prompts")
    if not isinstance(prompts, list):
        prompts = []
    lines = [
        "Javis assistant quick prompts:",
        f"- total={len(prompts)}",
    ]
    for item in prompts[:max_items]:
        label = _clip(item.get("label", "?"), 48)
        query = _clip(item.get("query", ""), 120)
        lines.append(f"- {label}: {query}")
    if len(prompts) > max_items:
        lines.append(f"- ... +{len(prompts) - max_items} more")
    return "\n".join(lines)


def build_live_javis_context(
    brain: str | Path,
    query: str,
    settings: dict | None = None,
    fetcher: Callable[[str, str, float], dict] | None = None,
) -> LiveJavisBridgeResult:
    """Lấy context live từ Javis runtime đang chạy.

    Bridge này chỉ đọc telemetry: nếu endpoint nào lỗi thì bỏ qua endpoint đó và tiếp tục.
    """
    live = _live_cfg(settings)
    if not _as_bool(live.get("enabled")):
        return LiveJavisBridgeResult(items=(), roots=(), endpoint_count=0, source_count=0, error_count=0)

    base_url = _normalize_base_url(live.get("base_url") or "http://127.0.0.1:8000")
    if not base_url:
        return LiveJavisBridgeResult(items=(), roots=(), endpoint_count=0, source_count=0, error_count=1)

    timeout_seconds = _as_float(live.get("timeout_seconds"), 4.0)
    max_workflows = _as_int(live.get("max_workflows"), 6)
    max_executions = _as_int(live.get("max_executions"), 8)
    max_prompts = _as_int(live.get("max_prompts"), 4)

    fetch = fetcher or _request_json
    items: list[ContextItem] = []
    errors = 0
    endpoint_count = 0

    query_hint = str(query or "").casefold()
    wants_n8n = any(token in query_hint for token in ("n8n", "workflow", "execut", "deploy", "file-status"))
    wants_assistant = any(token in query_hint for token in ("assistant", "bot", "prompt", "gợi ý", "trợ lý"))
    wants_health = any(token in query_hint for token in ("health", "sống", "live", "online", "running", "đang chạy"))

    requests: list[tuple[str, str, float, str, float, float]] = []
    if wants_health or not (wants_n8n or wants_assistant):
        requests.append(("/health", "live_javis_health", 0.98, 0.9, 0.9))
    if wants_n8n or not (wants_health or wants_assistant):
        requests.extend([
            ("/admin/n8n/workflows", "live_javis_workflows", 0.96, 0.88, 0.84),
            ("/admin/n8n/executions?limit=20", "live_javis_executions", 0.94, 0.86, 0.82),
            ("/admin/n8n/file-status", "live_javis_file_status", 0.91, 0.84, 0.8),
        ])
    if wants_assistant or not (wants_health or wants_n8n):
        requests.append(("/admin/assistant/quick-prompts", "live_javis_prompts", 0.87, 0.8, 0.78))

    # Khi câu hỏi vừa chạm trạng thái vừa chạm n8n thì luôn lấy health trước.
    ordered_requests: list[tuple[str, str, float, float, float]] = []
    seen = set()
    for endpoint, kind, relevance, confidence, authority in requests:
        if endpoint in seen:
            continue
        seen.add(endpoint)
        ordered_requests.append((endpoint, kind, relevance, confidence, authority))

    for endpoint, kind, relevance, confidence, authority in ordered_requests:
        endpoint_count += 1
        data = fetch(base_url, endpoint, timeout_seconds)
        if not isinstance(data, dict) or data.get("error"):
            errors += 1
            continue

        if kind == "live_javis_health":
            content = _summarize_health(data)
        elif kind == "live_javis_workflows":
            content = _summarize_workflows(data, max_workflows)
        elif kind == "live_javis_executions":
            content = _summarize_executions(data, max_executions)
        elif kind == "live_javis_file_status":
            content = _summarize_file_status(data, max_workflows)
        else:
            content = _summarize_prompts(data, max_prompts)

        if not content.strip():
            errors += 1
            continue

        items.append(
            _build_item(
                kind=kind,
                base_url=base_url,
                endpoint=endpoint,
                content=content,
                index=len(items),
                relevance=relevance,
                confidence=confidence,
            )
        )

    return LiveJavisBridgeResult(
        items=tuple(items),
        roots=(base_url,),
        endpoint_count=endpoint_count,
        source_count=len(items),
        error_count=errors,
    )
