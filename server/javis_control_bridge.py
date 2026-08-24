"""Allowlisted control bridge from Javis OS to the legacy Javis runtime.

This module deliberately talks to the existing HTTP API instead of importing the
legacy codebase. That keeps N8n/Javis untouched, avoids copying secrets, and
gives Javis OS one stable place to observe or trigger the old flow.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


Fetcher = Callable[[str, str, str, float, Any | None], dict]


class JavisControlError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ActionSpec:
    method: str
    path: str
    mode: str
    description: str
    query_fields: tuple[str, ...] = ()
    path_fields: tuple[str, ...] = ()
    body_allowed: bool = False


ACTION_SPECS: dict[str, ActionSpec] = {
    "health": ActionSpec("GET", "/health", "read", "Legacy Javis health, Redis, Ollama and vector indexes."),
    "system_status": ActionSpec("GET", "/admin/status", "read", "Runtime status for Redis, Ollama, n8n and Python API."),
    "stats_today": ActionSpec("GET", "/admin/stats/today", "read", "Today business/customer statistics."),
    "analytics_weekly": ActionSpec("GET", "/admin/analytics/weekly", "read", "Seven-day analytics snapshot."),
    "report_latest": ActionSpec("GET", "/admin/reports/latest", "read", "Latest executive report saved by legacy Javis."),
    "n8n_workflows": ActionSpec("GET", "/admin/n8n/workflows", "read", "List n8n workflows and active state."),
    "n8n_executions": ActionSpec("GET", "/admin/n8n/executions", "read", "Recent n8n executions.", ("limit",)),
    "n8n_file_status": ActionSpec("GET", "/admin/n8n/file-status", "read", "Local workflow file status."),
    "assistant_quick_prompts": ActionSpec("GET", "/admin/assistant/quick-prompts", "read", "Legacy assistant quick prompts."),
    "learning_queue": ActionSpec("GET", "/admin/learning-queue", "read", "Learning queue items.", ("brand", "limit")),
    "knowledge_documents": ActionSpec("GET", "/admin/documents", "read", "Knowledge document list."),
    "shopee_catalog": ActionSpec("GET", "/admin/shopee/catalog", "read", "Shopee catalog exposed by legacy Javis."),
    "sheet_preview": ActionSpec("POST", "/admin/sheets/preview", "read", "Preview a Google Sheet tab.", body_allowed=True),
    "assistant_chat": ActionSpec("POST", "/admin/assistant/chat", "write", "Ask the legacy executive assistant.", body_allowed=True),
    "sync_knowledge": ActionSpec("POST", "/admin/n8n/sync-knowledge", "write", "Sync FAQ knowledge to vector indexes.", ("brand",)),
    "report_generate": ActionSpec("POST", "/admin/reports/generate", "write", "Generate an executive report.", ("send_telegram",)),
    "learning_ai_suggest": ActionSpec("POST", "/admin/learning/ai-suggest", "write", "Generate AI suggestions for learning queue.", ("brand",)),
    "learning_dismiss": ActionSpec(
        "POST", "/admin/learning-queue/dismiss", "write", "Dismiss one learning queue item.",
        ("brand", "queue_key", "raw_value"),
    ),
    "learning_approve": ActionSpec(
        "POST", "/admin/learning-queue/approve", "write", "Approve a learning item into FAQ and resync.",
        ("brand",), body_allowed=True,
    ),
    "workflow_toggle": ActionSpec(
        "POST", "/admin/n8n/workflows/{workflow_id}/toggle", "write", "Toggle one n8n workflow.",
        path_fields=("workflow_id",),
    ),
    "workflow_deploy": ActionSpec("POST", "/admin/n8n/deploy", "write", "Deploy workflow code through legacy Javis.", body_allowed=True),
    "sheet_sync_direct": ActionSpec("POST", "/admin/sheets/sync-direct", "write", "Sync Google Sheet directly into Redis.", body_allowed=True),
    "shopee_sync_sheet": ActionSpec("POST", "/admin/shopee/sync-sheet", "write", "Sync Shopee catalog from a Sheet.", ("sheet_url",)),
    "documents_sync": ActionSpec("POST", "/admin/documents/sync", "write", "Vectorize knowledge documents."),
    "documents_import_sheet": ActionSpec(
        "POST", "/admin/documents/import-sheet", "write", "Import FAQ Sheet to Markdown knowledge.",
        ("sheet_url", "brand"),
    ),
}


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in ("1", "true", "yes", "on", "enabled")


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


def bridge_config(settings: dict | None) -> dict:
    runtime = (settings or {}).get("context_runtime") or {}
    live = runtime.get("live_javis") if isinstance(runtime.get("live_javis"), dict) else {}
    control = runtime.get("javis_control") if isinstance(runtime.get("javis_control"), dict) else {}
    return {
        "enabled": _as_bool(control.get("enabled"), _as_bool(live.get("enabled"))),
        "base_url": _normalize_base_url(control.get("base_url") or live.get("base_url") or "http://127.0.0.1:8000"),
        "timeout_seconds": _as_float(control.get("timeout_seconds", live.get("timeout_seconds")), 8.0),
        "allow_writes": _as_bool(control.get("allow_writes"), False),
        "confirm_writes": _as_bool(control.get("confirm_writes"), True),
        "confirm_phrase": str(control.get("confirm_phrase") or "JAVIS_WRITE"),
    }


def capabilities(settings: dict | None = None) -> dict:
    cfg = bridge_config(settings)
    actions = []
    for name, spec in sorted(ACTION_SPECS.items()):
        actions.append({
            "name": name,
            "method": spec.method,
            "path": spec.path,
            "mode": spec.mode,
            "description": spec.description,
            "query_fields": list(spec.query_fields),
            "path_fields": list(spec.path_fields),
            "body_allowed": spec.body_allowed,
            "enabled": cfg["enabled"] and (spec.mode == "read" or cfg["allow_writes"]),
            "requires_confirm": spec.mode == "write" and cfg["confirm_writes"],
        })
    return {
        "enabled": cfg["enabled"],
        "base_url": cfg["base_url"],
        "allow_writes": cfg["allow_writes"],
        "confirm_writes": cfg["confirm_writes"],
        "confirm_phrase": cfg["confirm_phrase"] if cfg["confirm_writes"] else "",
        "actions": actions,
    }


def _http_json(base_url: str, method: str, endpoint: str, timeout_seconds: float, body: Any | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json", "User-Agent": "javis-os-control-bridge/1.0"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(base_url + endpoint, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except Exception:
            detail = raw.decode("utf-8", errors="replace") if raw else str(exc)
        return {"ok": False, "status_code": exc.code, "error": str(exc), "detail": detail}
    except (URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "status_code": 502, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - network boundary
        return {"ok": False, "status_code": 502, "error": f"{type(exc).__name__}: {exc}"}
    if not raw:
        return {"ok": True, "status_code": status}
    try:
        decoded = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        decoded = {"raw": raw.decode("utf-8", errors="replace")}
    if isinstance(decoded, dict):
        decoded.setdefault("ok", status < 400)
        decoded.setdefault("status_code", status)
        return decoded
    return {"ok": status < 400, "status_code": status, "data": decoded}


def _endpoint_for(spec: ActionSpec, params: dict[str, Any]) -> str:
    path = spec.path
    for field in spec.path_fields:
        value = str(params.get(field) or "").strip()
        if not value:
            raise JavisControlError(f"Missing path param: {field}", 400)
        path = path.replace("{" + field + "}", value)
    query = {}
    for field in spec.query_fields:
        if field in params and params[field] is not None:
            query[field] = params[field]
    if query:
        path += "?" + urlencode(query, doseq=True)
    return path


def call_action(
    settings: dict | None,
    action: str,
    params: dict[str, Any] | None = None,
    body: Any | None = None,
    confirm: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict:
    cfg = bridge_config(settings)
    if not cfg["enabled"]:
        raise JavisControlError("Javis control bridge is disabled.", 409)
    if not cfg["base_url"]:
        raise JavisControlError("Javis control bridge has no base_url.", 409)

    action = str(action or "").strip()
    spec = ACTION_SPECS.get(action)
    if spec is None:
        raise JavisControlError(f"Unknown Javis control action: {action}", 404)
    if spec.mode == "write":
        if not cfg["allow_writes"]:
            raise JavisControlError("Write actions are disabled for Javis control bridge.", 403)
        if cfg["confirm_writes"] and str(confirm or "") != cfg["confirm_phrase"]:
            raise JavisControlError(f"Write action requires confirm='{cfg['confirm_phrase']}'.", 428)
    if body is not None and not spec.body_allowed:
        raise JavisControlError(f"Action '{action}' does not accept a JSON body.", 400)

    safe_params = params if isinstance(params, dict) else {}
    endpoint = _endpoint_for(spec, safe_params)
    fetch = fetcher or _http_json
    result = fetch(cfg["base_url"], spec.method, endpoint, cfg["timeout_seconds"], body)
    if not isinstance(result, dict):
        result = {"ok": True, "data": result}
    result.setdefault("ok", not bool(result.get("error")))
    result["_bridge"] = {
        "action": action,
        "mode": spec.mode,
        "method": spec.method,
        "endpoint": endpoint,
        "base_url": cfg["base_url"],
    }
    return result
