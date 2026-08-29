"""Immutable, redacted fingerprint for the legacy chatbot worker."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Any


_SERVER_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SERVER_DIR.parents[1]
_TRACKED_FILES = (
    "chatbot/server/chat_pipeline.py",
    "chatbot/server/ai_engine.py",
    "chatbot/server/query_understanding.py",
    "chatbot/server/grounding_policy.py",
    "chatbot/server/rag_search.py",
    "chatbot/server/knowledge_sync.py",
)
_SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey", "authorization")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _redact_config(value: Any, key: str = "") -> Any:
    if any(marker in key.casefold() for marker in _SECRET_MARKERS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_config(item, key) for item in value]
    return value


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=_REPO_ROOT, check=False, capture_output=True, text=True, timeout=1.5
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_runtime_manifest(*, started_at: str | None = None) -> dict[str, Any]:
    """Build a manifest from the exact local sources, never source values of secrets."""
    files: dict[str, str] = {}
    for relative in _TRACKED_FILES:
        path = _REPO_ROOT / relative
        files[relative] = _sha256(path.read_bytes()) if path.exists() else "missing"

    settings_path = _SERVER_DIR / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        settings = {}
    config_version = _sha256(_canonical(_redact_config(settings)))
    started = started_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1,
        "started_at": started,
        "git_sha": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_value("status", "--porcelain") not in {"", "unknown"},
        "files": files,
        "config_version": config_version,
        "policy_versions": {"grounding": files.get("chatbot/server/grounding_policy.py", "missing")},
        "prompt_versions": {
            "customer_answer": "cskh.answer.v1",
            "conversation_plan": "conversation.plan.v1",
            "intent_plan": "intent.plan.v1",
            "cfc_semantic_plan": "cfc.semantic-plan.v1",
        },
        "python_version": platform.python_version(),
    }
    payload["runtime_manifest_id"] = _sha256(_canonical(payload))
    return payload


_MANIFEST = build_runtime_manifest()


def get_runtime_manifest() -> dict[str, Any]:
    """Return a copy so callers cannot mutate this worker's identity."""
    return deepcopy(_MANIFEST)


def runtime_manifest_status() -> dict[str, Any]:
    manifest = get_runtime_manifest()
    return {
        "runtime_manifest_id": manifest["runtime_manifest_id"],
        "started_at": manifest["started_at"],
        "git_sha": manifest["git_sha"],
        "git_dirty": manifest["git_dirty"],
        "files": manifest["files"],
        "config_version": manifest["config_version"],
        "policy_versions": manifest["policy_versions"],
        "prompt_versions": manifest["prompt_versions"],
        "python_version": manifest["python_version"],
    }
