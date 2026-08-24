import sys
from pathlib import Path

from _paths import ROOT, SERVER  # noqa: E402,F401

if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from rag_bridge import build_grounding_context


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_grounding_bridge_tra_markdown_va_tra_context_items(tmp_path):
    brain = tmp_path / "brain"
    _write(
        brain / "sources" / "policy.md",
        "# Chính sách đổi trả\n"
        "Khách được đổi trả trong 7 ngày nếu sản phẩm còn nguyên tem.\n"
        "# Vận chuyển\n"
        "Giao hàng nội thành trong 24 giờ.",
    )

    settings = {
        "context_runtime": {
            "grounded_docs": {
                "enabled": True,
                "top_k": 4,
            }
        }
    }

    result = build_grounding_context(brain, "Chính sách đổi trả thế nào?", settings)
    assert result.hit_count == 1
    assert result.source_count >= 1
    assert result.items
    item = result.items[0]
    assert item.kind == "grounded_docs"
    assert "đổi trả" in item.content.lower()
    assert item.source_ref.startswith("rag:")
