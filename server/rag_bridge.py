"""Bridge tối thiểu cho nguồn grounding kiểu RAG trong Javis OS.

Mục tiêu của module này không phải thay `memory_index.py`, mà là bọc engine
grounding Markdown có sẵn (`chatbot_grounding.py`) thành một nguồn context
độc lập. Nhờ vậy có thể cắm corpus ngoài vào pipeline hiện tại mà không phải
đụng logic compile ngữ cảnh.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from chatbot_grounding import thu_thap
from context_compiler import ContextItem, HeuristicTokenizer


_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GroundingBridgeResult:
    items: tuple[ContextItem, ...]
    roots: tuple[str, ...]
    hit_count: int
    source_count: int


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


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_REPO_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path.resolve())


def _expand_root(raw: str | Path, brain: Path) -> Path | None:
    try:
        candidate = Path(raw)
    except TypeError:
        return None
    if not str(candidate).strip():
        return None
    if not candidate.is_absolute():
        candidate = (_REPO_ROOT / candidate).resolve()
    else:
        candidate = candidate.expanduser().resolve()
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        return candidate.parent
    if str(raw).strip().casefold() in ("brain", "."):
        return brain
    return None


def _configured_roots(settings: dict, brain: Path) -> list[Path]:
    runtime = (settings or {}).get("context_runtime") or {}
    grounded = runtime.get("grounded_docs")
    grounded = grounded if isinstance(grounded, dict) else {}

    roots: list[Path] = [brain]
    raw_roots = grounded.get("source_dirs") or grounded.get("source_dir") or []
    if isinstance(raw_roots, (str, Path)):
        raw_roots = [raw_roots]
    for raw in raw_roots:
        root = _expand_root(raw, brain)
        if root and root not in roots:
            roots.append(root)
    return roots


def _build_item(root: Path, query: str, hit: dict, index: int) -> ContextItem:
    content = hit.get("khoi") or ""
    tokenizer = HeuristicTokenizer("rag", "grounding")
    sources = tuple(hit.get("nguon") or ())
    source_ref = "rag:" + _repo_relative(root)
    if sources:
        source_ref = source_ref + ":" + ",".join(sources[:4])
    confidence = 0.72 + min(0.2, 0.03 * len(sources))
    return ContextItem(
        id=f"grounding:{index}:{root.name or 'brain'}",
        kind="grounded_docs",
        content=content,
        source_ref=source_ref,
        token_cost=tokenizer.count_text(content),
        relevance=max(0.72, 0.96 - index * 0.05),
        confidence=min(0.98, confidence),
        authority=0.72,
        freshness=1.0,
        required=False,
        trust="grounding_source",
        metadata={
            "query": query[:240],
            "root": _repo_relative(root),
            "source_count": len(sources),
        },
    )


def build_grounding_context(
    brain: str | Path,
    query: str,
    settings: dict | None = None,
) -> GroundingBridgeResult:
    """Trả context items từ nền Markdown-grounding hiện có.

    Cửa này cố ý fail-soft: root nào lỗi thì bỏ root đó, không phá lượt chat.
    """
    runtime = (settings or {}).get("context_runtime") or {}
    grounded = runtime.get("grounded_docs")
    grounded = grounded if isinstance(grounded, dict) else {}
    if not _as_bool(grounded.get("enabled")):
        return GroundingBridgeResult(items=(), roots=(), hit_count=0, source_count=0)

    brain_path = Path(brain).resolve()
    top_k = _as_int(grounded.get("top_k"), 4)
    max_roots = _as_int(grounded.get("max_roots"), 4)

    items: list[ContextItem] = []
    roots = _configured_roots(settings or {}, brain_path)[:max_roots]
    sources_seen = 0
    for index, root in enumerate(roots):
        try:
            hit = thu_thap(root, query, k=top_k)
        except Exception:
            continue
        if not hit.get("co") or not hit.get("khoi"):
            continue
        sources_seen += len(hit.get("nguon") or ())
        items.append(_build_item(root, query, hit, index))

    return GroundingBridgeResult(
        items=tuple(items),
        roots=tuple(_repo_relative(root) for root in roots),
        hit_count=len(items),
        source_count=sources_seen,
    )
