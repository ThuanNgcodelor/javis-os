"""
chat_pipeline.py — High-Performance Fast-Path Chatbot Pipeline cho ZeO & CFC
Đạt tốc độ phản hồi < 50ms - 300ms (Nhanh gấp 20 - 50 lần flow n8n cũ)

Quy trình:
  1. Per-Sender Request Sequencing (Chống race condition / lock theo sender_id)
  2. Fast-Path Regex & Normalize: Chào hỏi, cảm ơn, nhận diện SĐT, khiếu nại (< 5ms)
  3. Bóc tách Customer Profile Recall: Đọc trực tiếp từ Redis Profile, cách ly 100% khỏi RAG
  4. Shopee Catalog Matcher: Khớp link Shopee Mall chính hãng (< 10ms)
  5. In-Memory Lexical & RediSearch KNN RAG: Tra cứu FAQ chuẩn xác (< 5ms)
  6. Context Memory & Covered Fact Exclusion: Loại trừ fact cũ khi khách hỏi follow-up
  7. Granular Fallback Reasons: Phân loại nguyên nhân fallback chính xác
"""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

# pyrefly: ignore [missing-import]
from pydantic import BaseModel

from conversation_store import (
    BoundedTTLCache,
    ConversationStoreConfig,
    load_json,
    persist_session,
    sender_lease,
)
from dialogue_router import build_route_decision
from grounding_policy import assess_grounding
from message_idempotency import begin_message, complete_message, release_message
from rag_search import get_redis, get_faq_by_intent, semantic_search, refresh_knowledge_cache
from shopee_matcher import (
    load_shopee_catalog,
    match_shopee_product,
    match_shopee_product_reference,
    is_shopee_inquiry,
    is_budget_inquiry,
    is_price_extreme_inquiry,
    match_price_extreme,
    match_products_by_budget,
    match_specific_product_price,
    is_bestseller_inquiry,
    is_new_arrival_inquiry,
    match_best_sellers,
    match_new_arrivals,
    match_need_preference,
    is_bulk_or_restaurant_inquiry,
    match_bulk_or_restaurant_need,
    is_skin_care_dishwashing_inquiry,
    match_skin_care_dishwashing,
    is_baby_or_sensitive_laundry_inquiry,
    match_baby_or_sensitive_laundry,
    is_front_load_washer_inquiry,
    match_front_load_washer,
    is_stain_removal_or_efficacy_inquiry,
    match_stain_removal_or_efficacy,
    is_fabric_softener_inquiry,
    match_fabric_softener_products,
)
from ai_engine import synthesize_cskh_answer, reason_and_answer_cskh, plan_chat_intent_with_ollama
from nlu_shadow import schedule_nlu_shadow
from query_understanding import build_query_plan
from telegram_notifier import notify_new_lead, notify_admin_unanswered, notify_urgent_complaint

logger = logging.getLogger(__name__)

# Lock per-sender để tuần tự hóa các tin nhắn gửi dồn dập
_sender_locks: dict[str, asyncio.Lock] = {}
_sender_lock_users: dict[str, int] = {}
_global_lock = asyncio.Lock()

# Cache giữ contract dict cũ nhưng có TTL/LRU để không tăng vô hạn.
_local_session_cache: BoundedTTLCache[dict] = BoundedTTLCache(maxsize=5000, ttl_seconds=3600)
_local_customer_cache: BoundedTTLCache[dict] = BoundedTTLCache(maxsize=5000, ttl_seconds=3600)


def _load_conversation_runtime_config() -> tuple[ConversationStoreConfig, int]:
    values: dict[str, Any] = {}
    cfg_path = Path(__file__).parent / "settings.json"
    if cfg_path.exists():
        try:
            raw_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            candidate = raw_cfg.get("conversation", {})
            if isinstance(candidate, dict):
                values = candidate
        except Exception as exc:
            logger.debug("Không đọc được cấu hình conversation: %s", exc)

    def _int_value(name: str, default: int, minimum: int) -> int:
        env_name = f"CHAT_{name.upper()}"
        try:
            return max(minimum, int(os.getenv(env_name, values.get(name, default))))
        except (TypeError, ValueError):
            return default

    try:
        lock_wait = max(0.1, float(os.getenv(
            "CHAT_SENDER_LOCK_WAIT_SECONDS",
            values.get("sender_lock_wait_seconds", 3.0),
        )))
    except (TypeError, ValueError):
        lock_wait = 3.0

    config = ConversationStoreConfig(
        session_ttl_seconds=_int_value("session_ttl_seconds", 2592000, 60),
        history_ttl_seconds=_int_value("history_ttl_seconds", 2592000, 60),
        history_limit=_int_value("history_limit", 50, 1),
        sender_lock_ttl_seconds=_int_value("sender_lock_ttl_seconds", 30, 5),
        sender_lock_wait_seconds=lock_wait,
    )
    return config, _int_value("idempotency_ttl_seconds", 86400, 60)


_conversation_store_config, _idempotency_ttl_seconds = _load_conversation_runtime_config()


def _llm_nlu_config() -> tuple[str, float, float]:
    """
    Cấu hình lớp Ollama NLU planner.
    Mặc định off để không làm chậm test/eval; hỗ trợ:
      LLM_NLU_MODE=shadow  # chỉ ghi trace, không đổi câu trả lời
      LLM_NLU_MODE=assist  # cho phép planner chọn deterministic tool
    """
    mode = os.getenv("LLM_NLU_MODE", "").strip().lower()
    timeout_raw = os.getenv("LLM_NLU_TIMEOUT", "").strip()
    threshold_raw = os.getenv("LLM_NLU_CONFIDENCE", "").strip()

    cfg_path = Path(__file__).parent / "settings.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            nlu_cfg = cfg.get("llm_nlu", {}) if isinstance(cfg.get("llm_nlu", {}), dict) else {}
            mode = mode or str(nlu_cfg.get("mode", "")).strip().lower()
            timeout_raw = timeout_raw or str(nlu_cfg.get("timeout_seconds", "")).strip()
            threshold_raw = threshold_raw or str(nlu_cfg.get("min_confidence", "")).strip()
        except Exception as exc:
            logger.debug("Không đọc được cấu hình llm_nlu: %s", exc)

    if mode not in {"assist", "shadow", "off"}:
        mode = "off"
    try:
        timeout = float(timeout_raw)
    except Exception:
        timeout = 1.6
    try:
        threshold = float(threshold_raw)
    except Exception:
        threshold = 0.72
    return mode, max(0.3, min(timeout, 5.0)), max(0.5, min(threshold, 0.98))


async def _get_sender_lock(lock_key: str) -> asyncio.Lock:
    async with _global_lock:
        if lock_key not in _sender_locks:
            _sender_locks[lock_key] = asyncio.Lock()
        _sender_lock_users[lock_key] = _sender_lock_users.get(lock_key, 0) + 1
        return _sender_locks[lock_key]


@asynccontextmanager
async def _local_sender_lock(lock_key: str):
    sender_lock = await _get_sender_lock(lock_key)
    try:
        async with sender_lock:
            yield
    finally:
        async with _global_lock:
            remaining = max(0, _sender_lock_users.get(lock_key, 1) - 1)
            if remaining:
                _sender_lock_users[lock_key] = remaining
            else:
                _sender_lock_users.pop(lock_key, None)
            if remaining == 0 and _sender_locks.get(lock_key) is sender_lock and not sender_lock.locked():
                _sender_locks.pop(lock_key, None)


# Cấu hình từ viết tắt tiếng Việt
VIETNAMESE_ALIASES = {
    "k": "khong", "ko": "khong", "kh": "khong", "hok": "khong", "hem": "khong", "hong": "khong",
    "dc": "duoc", "dk": "duoc", "sp": "san pham", "ib": "nhan tin", "nt": "nhan tin",
    "nhiu": "nhieu", "oplis": "oplus", "oplus": "oplus",
    "bn": "bao nhieu", "mn": "minh", "ship": "giao hang", "cty": "cong ty",
    "web": "website", "wed": "website", "wep": "website",
    "sdt": "so dien thoai", "ssdt": "so dien thoai", "dt": "dien thoai", "npp": "nha phan phoi",
}

PHONE_REGEX = re.compile(r"(?:\+84|84|0)(?:3[2-9]|5[2689]|7[06789]|8[0-9]|9[0-9])[0-9]{7}\b")
AREA_KEYWORDS = [
    "tinh", "thanh pho", "tp", "huyen", "quan", "q", "xa", "phuong", "thi xa", "khu vuc",
    "can tho", "thai binh", "kien giang", "rach gia", "tra noc", "tphcm", "ho chi minh",
    "binh duong", "dong nai", "long an", "vung tau", "da nang", "ha noi", "hai phong",
    "an giang", "dong thap", "soc trang", "bac lieu", "ca mau", "hau giang", "vinh long", "tien giang",
]

CFC_GOAL_BY_INTENT = {
    "cfc_dealer_location_request": "dealer_lookup",
    "cfc_dealer_location_received": "dealer_lookup",
    "cfc_dealer_location_unavailable": "dealer_lookup",
    "cfc_inventory_request": "inventory_check",
    "cfc_inventory_unavailable": "inventory_check",
    "cfc_order_status_request": "order_tracking",
    "cfc_order_status_unavailable": "order_tracking",
    "cfc_loyalty_lookup_request": "loyalty_lookup",
    "cfc_loyalty_unavailable": "loyalty_lookup",
    "cfc_wholesale_policy_request": "wholesale_policy",
    "cfc_wholesale_policy_unverified": "wholesale_policy",
    "wholesale_dealer": "wholesale_policy",
    "cfc_price_unverified": "price_quote",
    "cfc_dosage_usage_review": "agronomy_consultation",
    "cfc_crop_consultation_request": "agronomy_consultation",
    "cfc_agronomy_review_request": "agronomy_consultation",
    "cfc_rice_fertilizer_guide": "agronomy_consultation",
}

CFC_REQUIRED_SLOTS = {
    "dealer_lookup": ("phone", "area"),
    "inventory_check": ("phone", "area", "product"),
    "order_tracking": ("phone", "order_id"),
    "loyalty_lookup": ("phone",),
    "wholesale_policy": ("phone", "area"),
    "price_quote": ("phone", "area", "crop", "product"),
    "agronomy_consultation": ("phone", "area", "crop", "crop_stage"),
}

SENSITIVE_KEYWORDS = [
    "hoan tien", "doi tra", "khieu nai", "lua dao", "san pham loi", "hang gia", "tai khoan ngan hang", "chuyen khoan", "so tai khoan"
]

RETURN_CONTEXT_INTENTS = {
    "return_eligible_cases",
    "return_policy_scope",
    "return_process",
    "return_claim_deadlines",
    "return_resolution_options",
    "refund_processing_time",
    "return_fee_unverified",
}

ZEO_COMPETITOR_PRODUCT_PATTERNS = [
    r"\bomo\b", r"\bariel\b", r"\btide\b", r"\bsurf\b", r"\baba\b", r"\blix\b", r"\bnet\b", r"\bdowny\b", r"\bcomfort\b",
]

PRODUCT_MEMORY_BY_INTENT = {
    "zeo_detergent_technology": [
        {"name": "Bột giặt ZeO", "category": "laundry", "intent": "zeo_detergent_technology"},
    ],
    "zeo_detergent_certification": [
        {"name": "Bột giặt ZeO", "category": "laundry", "intent": "zeo_detergent_certification"},
    ],
    "zeo_detergent_fragrance": [
        {"name": "Bột giặt ZeO", "category": "laundry", "intent": "zeo_detergent_fragrance"},
    ],
    "oplus_detergent_ion_technology": [
        {"name": "Bột giặt Oplus", "category": "laundry", "intent": "oplus_detergent_ion_technology"},
    ],
    "oplus_detergent_features": [
        {"name": "Bột giặt Oplus", "category": "laundry", "intent": "oplus_detergent_features"},
    ],
    "oplus_detergent_usp": [
        {"name": "Bột giặt Oplus", "category": "laundry", "intent": "oplus_detergent_usp"},
    ],
    "pano_laundry_fragrance_options": [
        {"name": "Bột giặt & Nước giặt PANO", "category": "laundry", "intent": "pano_laundry_fragrance_options"},
    ],
    "pano_veilex_odor_control": [
        {"name": "Bột giặt & Nước giặt PANO", "category": "laundry", "intent": "pano_veilex_odor_control"},
    ],
    "zeo_product_catalog_overview": [
        {"name": "Giặt giũ ZeO/PANO/Oplus", "category": "laundry", "intent": "zeo_laundry_product_overview"},
        {"name": "Rửa chén ZeO/ZIF/PANO/Oplus", "category": "dishwashing", "intent": "zeo_dishwashing_product_overview"},
        {"name": "Lau sàn ZeO/Oplus", "category": "floor_cleaner", "intent": "zeo_floor_cleaner_product_overview"},
        {"name": "Tẩy rửa vệ sinh ZeO/PANO", "category": "cleaning_hygiene", "intent": "zeo_cleaning_hygiene_product_overview"},
    ],
    "zeo_laundry_product_overview": [
        {"name": "Bột giặt ZeO", "category": "laundry", "intent": "zeo_detergent_technology"},
        {"name": "Bột giặt Oplus", "category": "laundry", "intent": "oplus_detergent_features"},
        {"name": "Bột giặt & Nước giặt PANO", "category": "laundry", "intent": "pano_product_type"},
    ],
    "zeo_dishwashing_product_overview": [
        {"name": "Nước rửa chén ZeO/ZIF", "category": "dishwashing", "intent": "zeo_zif_dishwashing_liquid"},
        {"name": "PANO Rửa Chén Chanh", "category": "dishwashing", "intent": "pano_dishwashing_lemon_and_vitamin_e"},
        {"name": "PANO Rửa Chén Vitamin E", "category": "dishwashing", "intent": "pano_dishwashing_lemon_and_vitamin_e"},
        {"name": "Oplus Rửa Chén", "category": "dishwashing", "intent": "oplus_dishwashing_liquid"},
    ],
    "zeo_floor_cleaner_product_overview": [
        {"name": "Nước lau sàn ZeO", "category": "floor_cleaner", "intent": "zeo_floor_cleaner_product_overview"},
        {"name": "Nước lau sàn Oplus", "category": "floor_cleaner", "intent": "zeo_floor_cleaner_product_overview"},
    ],
    "zeo_cleaning_hygiene_product_overview": [
        {"name": "Javen ZeO", "category": "cleaning_hygiene", "intent": "zeo_cleaning_hygiene_product_overview"},
        {"name": "Tẩy Toilet ZeO", "category": "cleaning_hygiene", "intent": "zeo_toilet_cleaner"},
        {"name": "Tẩy màu ZeO", "category": "cleaning_hygiene", "intent": "zeo_color_bleach"},
        {"name": "Lau kính ZeO", "category": "cleaning_hygiene", "intent": "zeo_cleaning_hygiene_product_overview"},
        {"name": "Xịt tẩy đa năng PANO", "category": "cleaning_hygiene", "intent": "pano_multipurpose_cleaner"},
    ],
    "pano_product_type": [
        {"name": "Bột giặt & Nước giặt PANO", "category": "laundry", "intent": "pano_product_type"},
        {"name": "Nước rửa chén PANO", "category": "dishwashing", "intent": "pano_dishwashing_lemon_and_vitamin_e"},
        {"name": "Xịt tẩy đa năng PANO", "category": "cleaning_hygiene", "intent": "pano_multipurpose_cleaner"},
    ],
    "pano_dishwashing_lemon_and_vitamin_e": [
        {"name": "PANO Rửa Chén Chanh", "category": "dishwashing", "intent": "pano_dishwashing_lemon_and_vitamin_e"},
        {"name": "PANO Rửa Chén Vitamin E", "category": "dishwashing", "intent": "pano_dishwashing_lemon_and_vitamin_e"},
    ],
    "product_lines": [
        {"name": "Dinh dưỡng cây trồng cao cấp CFC Cò Bay", "category": "fertilizer", "intent": "product_lines"},
        {"name": "Phân bón hữu cơ sinh học CFC Cò Bay", "category": "fertilizer", "intent": "cfc_organic_fertilizer_info"},
        {"name": "Phân bón NPK CFC Cò Bay", "category": "fertilizer", "intent": "cfc_npk_product_info"},
    ],
}

TECH_CONTEXT_INTENTS = {
    "zeo_detergent": {
        "intents": ["zeo_detergent_technology", "zeo_detergent_certification", "zeo_detergent_fragrance"],
        "product_pattern": r"bot giat zeo|\bzeo\b",
        "technology_intent": "zeo_detergent_technology",
        "related_intents": ["zeo_detergent_technology", "zeo_detergent_certification", "zeo_detergent_fragrance", "oplus_detergent_ion_technology", "pano_veilex_odor_control"],
    },
    "oplus_detergent": {
        "intents": ["oplus_detergent_ion_technology", "oplus_detergent_features", "oplus_detergent_usp"],
        "product_pattern": r"bot giat oplus|\boplus\b",
        "technology_intent": "oplus_detergent_ion_technology",
        "related_intents": ["oplus_detergent_ion_technology", "oplus_detergent_features", "oplus_detergent_usp", "zeo_detergent_technology", "pano_veilex_odor_control"],
    },
    "pano_laundry": {
        "intents": ["pano_product_type", "pano_laundry_fragrance_options", "pano_veilex_odor_control"],
        "product_pattern": r"pano|nuoc giat pano|bot giat pano",
        "technology_intent": "pano_veilex_odor_control",
        "related_intents": ["pano_veilex_odor_control", "pano_laundry_fragrance_options", "pano_product_type", "zeo_detergent_technology"],
    },
}

PRODUCT_ENTITY_PATTERNS = [
    ("zeo_detergent_technology", "Bột giặt ZeO", "laundry", r"bot giat zeo|enzyme thuy dien"),
    ("pano_product_type", "Bột giặt & Nước giặt PANO", "laundry", r"bot giat pano|nuoc giat pano"),
    ("oplus_detergent_features", "Bột giặt Oplus", "laundry", r"bot giat oplus"),
    ("zeo_zif_dishwashing_liquid", "Nước rửa chén ZeO/ZIF", "dishwashing", r"\bzif\b|nuoc rua chen zeo|rua chen zeo"),
    ("pano_product_type", "PANO", "product_family", r"\bpano\b"),
    ("oplus_detergent_features", "Oplus", "product_family", r"\boplus\b"),
    ("zeo_laundry_product_overview", "Giặt giũ ZeO/PANO/Oplus", "laundry", r"giat giu|nuoc giat|bot giat|giat quan ao"),
    ("zeo_dishwashing_product_overview", "Rửa chén ZeO/ZIF/PANO/Oplus", "dishwashing", r"nuoc rua chen|rua chen|rua bat"),
    ("zeo_floor_cleaner_product_overview", "Lau sàn ZeO/Oplus", "floor_cleaner", r"nuoc lau san|lau san|lau nha|tay san|san nha|tay san nha|lau san nha"),
    ("zeo_toilet_cleaner", "Tẩy Toilet ZeO", "cleaning_hygiene", r"toilet|bon cau|tay toilet"),
    ("zeo_cleaning_hygiene_product_overview", "Tẩy rửa vệ sinh ZeO/PANO", "cleaning_hygiene", r"javen|ve sinh|lau kinh|xit tay|tay mau"),
    ("cfc_npk_product_info", "Phân bón NPK CFC Cò Bay", "fertilizer", r"\bnpk\b"),
    ("cfc_organic_fertilizer_info", "Phân bón hữu cơ sinh học CFC Cò Bay", "fertilizer", r"huu co|sinh hoc"),
    ("product_lines", "Phân bón CFC Cò Bay", "fertilizer", r"phan bon|co bay|cfc"),
]


def _normalize_vn(text: str) -> str:
    """Loại bỏ dấu tiếng Việt và chuẩn hóa ký tự."""
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "d").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    # "kh" thường là "không", nhưng trong ngữ cảnh CSKH lại là "khách hàng".
    text = re.sub(r"\bcskh\b", "cham soc khach hang", text)
    text = re.sub(r"\b(cham soc|ho tro)\s+kh\b", r"\1 khach hang", text)
    tokens = [VIETNAMESE_ALIASES.get(t, t) for t in text.split() if t]
    return " ".join(tokens)


def _sanitize_area_candidate(value: str) -> str:
    candidate = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,.;:?!")
    if not candidate:
        return ""

    candidate = re.split(
        r"(?i)\s+(?:có đại lý|co dai ly|còn nợ|con no|nợ tiền|no tien|công nợ|cong no|"
        r"nhiều không|nhieu khong|không em|khong em|không shop|khong shop|được không|duoc khong|"
        r"giúp anh|giup anh|giúp em|giup em|bao nhiêu|bao nhieu|thế nào|the nao|sao|"
        r"muốn mua|muon mua|mua ở đâu|mua o dau|bán ở đâu|ban o dau|ở đâu|o dau|chỗ nào|cho nao|"
        r"có nhà phân phối|co nha phan phoi|giao tận|giao tan|ghé đại lý|ghe dai ly|"
        r"kiểm tra|kiem tra|cho anh|cho mình|cho minh|cho tôi|cho toi|để mua|de mua|"
        r"thì nên|thi nen|chưa em|chua em|chưa shop|chua shop)\b",
        candidate,
        maxsplit=1,
    )[0].strip(" ,.;:?!")
    candidate = re.sub(
        r"(?i)^(?:tôi|minh|mình|em|anh|chị|chi|bên mình|ben minh)\s+(?:ở|o|tại|tai)\s+",
        "",
        candidate,
    )
    candidate = re.sub(r"(?i)^(?:ở|o|tại|tai|khu vực|khu vuc)\s+", "", candidate)
    candidate = re.sub(r"(?i)^gần\s+", "", candidate)
    candidate = re.sub(r"(?i)\s+(?:gần nhất|gan nhat|nhất|nhat|đây|day)$", "", candidate).strip(" ,.;:?!")

    normalized = _normalize_vn(candidate)
    forbidden = (
        "muon mua", "co dai ly", "co nha phan phoi", "giao tan nha",
        "kiem tra giup", "bao gia", "san pham nao", "khong shop",
        "con no", "no tien", "cong no", "nhieu khong", "khong em",
    )
    if not candidate or len(normalized.split()) > 10 or any(term in normalized for term in forbidden):
        return ""
    return candidate


def _extract_area_from_text(text: str, norm: str) -> str:
    patterns = [
        r"(?i)\b(?:khu vực|khu vuc)\s+((?:xã|xa|phường|phuong|huyện|huyen|quận|quan|"
        r"thị xã|thi xa|thành phố|thanh pho|tỉnh|tinh)\s+[^?!.]{2,90})",
        r"(?i)\b(?:tôi|toi|mình|minh|em|anh|chị|chi)\s+(?:ở|o|tại|tai)\s+([^?!.]{2,90})",
        r"(?i)\b(?:ở|o|tại|tai)\s+((?:gần|gan\s+)?[^?!.]{2,90})",
        r"(?i)\b(?:gần|gan)\s+([^?!.]{2,60}?)(?:\s+(?:nhất|nhat)\b|[,?!.]|$)",
        r"(?i)\bbên\s+đại\s+lý\s+([^?!.]{2,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            area = _sanitize_area_candidate(match.group(1))
            if area:
                return area

    # Tin nhắn chỉ chứa một địa danh có trong danh sách chuẩn.
    if len(norm.split()) <= 8:
        for keyword in sorted(AREA_KEYWORDS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(keyword)}\b", norm):
                return _sanitize_area_candidate(text)
    return ""


def _sanitize_stored_area(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = _normalize_vn(raw)
    extracted = _extract_area_from_text(raw, normalized)
    if extracted:
        return extracted
    if len(normalized.split()) <= 12 and not re.search(
        r"\b(muon mua|co dai ly|giao tan|kiem tra|bao gia|san pham|khong shop)\b",
        normalized,
    ):
        return _sanitize_area_candidate(raw)
    return ""


def _extract_phone_and_area(text: str, norm: str) -> Tuple[str, str]:
    """Trích xuất SĐT và khu vực theo cụm ngắn, không lưu nguyên câu hỏi."""
    phone_match = PHONE_REGEX.search(text)
    phone = phone_match.group(0).strip() if phone_match else ""
    if not phone:
        match_general = re.search(r"\b(?:0|\+84|84)\d{8,10}\b", text)
        if match_general:
            phone = match_general.group(0).strip()
        else:
            digits = re.sub(r"\D", "", text)
            if len(digits) in (9, 10, 11) and ("so dien thoai" in norm or "sdt" in norm or "lien he" in norm or "tich diem" in norm):
                phone = digits

    area = ""
    # Nếu là câu hỏi hỏi vị trí (mua ở đâu, địa chỉ ở đâu) -> Không phải cung cấp khu vực
    asks_area_question = (
        re.search(r"(dia chi|khu vuc|noi o|tinh thanh).*(cua )?(toi|minh|em|anh|chi)", norm)
        or re.search(r"(o dau|tai dau|cho nao|dia chi.*o dau|mua o dau|ban o dau)", norm)
    )
    asks_company_contact = bool(
        re.search(r"\b(so cham soc|cham soc khach hang|hotline|tong dai)\b", norm)
        or re.search(
            r"(so dien thoai|so lien he).{0,30}(cong ty|zeo|pano|oplus|cfc|co bay|shop|ben minh)",
            norm,
        )
    )
    asks_sensitive_or_debt = bool(
        re.search(
            r"\b(con no|no tien|cong no|no bao nhieu|tien no|chua thanh toan|thong tin khach hang)\b",
            norm,
        )
    )
    if asks_area_question or asks_company_contact or asks_sensitive_or_debt:
        return phone, ""

    text_without_phone = text.replace(phone, "").strip() if phone else text
    area = _extract_area_from_text(text_without_phone, _normalize_vn(text_without_phone))

    return phone, area


def _active_goal_name(state: dict[str, Any]) -> str:
    active_goal = state.get("active_goal") or {}
    if isinstance(active_goal, str):
        return active_goal
    if isinstance(active_goal, dict):
        return str(active_goal.get("name") or "")
    return ""


async def _lookup_sales_locations_from_redis(
    *,
    user_message: str = "",
    province: str = "",
    district: str = "",
    ward: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 30.0,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Tra cứu đại lý/điểm bán từ snapshot Redis (hỗ trợ cả GPS GEO và Text matching)."""
    try:
        redis_client = await get_redis()
        raw = await redis_client.get("amis:public:sales-locations:active")
        if not raw:
            return []
        data = json.loads(raw)
        items = data.get("items", [])
        if not items:
            return []
    except Exception:
        return []

    # 1. Tra cứu theo tọa độ GEO nếu có GPS
    if lat is not None and lon is not None:
        try:
            geo_results = await redis_client.geosearch(
                "amis:public:sales-locations:geo",
                longitude=lon,
                latitude=lat,
                radius=radius_km,
                unit="km",
                withdist=True,
                count=top_k,
            )
            if geo_results:
                items_by_id = {it.get("location_id"): it for it in items if it.get("location_id")}
                matched = []
                for res in geo_results:
                    loc_id = res[0] if isinstance(res, (list, tuple)) else str(res)
                    dist = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else None
                    if loc_id in items_by_id:
                        entry = dict(items_by_id[loc_id])
                        if dist is not None:
                            entry["distance_km"] = round(float(dist), 1)
                        matched.append(entry)
                if matched:
                    return matched

            # Fallback 500km if 30km radius has no locations
            geo_wide = await redis_client.geosearch(
                "amis:public:sales-locations:geo",
                longitude=lon,
                latitude=lat,
                radius=500.0,
                unit="km",
                withdist=True,
                count=top_k,
            )
            if geo_wide:
                items_by_id = {it.get("location_id"): it for it in items if it.get("location_id")}
                matched = []
                for res in geo_wide:
                    loc_id = res[0] if isinstance(res, (list, tuple)) else str(res)
                    dist = res[1] if isinstance(res, (list, tuple)) and len(res) > 1 else None
                    if loc_id in items_by_id:
                        entry = dict(items_by_id[loc_id])
                        if dist is not None:
                            entry["distance_km"] = round(float(dist), 1)
                        matched.append(entry)
                if matched:
                    return matched
            if items:
                return items[:top_k]
        except Exception:
            pass

    # 2. Tra cứu theo địa danh (Tỉnh / Huyện / Xã / Tên điểm bán)
    def _fold(s: Any) -> str:
        t = unicodedata.normalize("NFD", str(s or ""))
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        return re.sub(r"\s+", " ", t.replace("đ", "d").replace("Đ", "D")).strip().lower()

    q_fold = _fold(user_message)
    p_fold = _fold(province)
    d_fold = _fold(district)
    w_fold = _fold(ward)

    DISTRICT_PROVINCE_MAP = {
        "thoi lai": "can tho", "o mon": "can tho", "co do": "can tho", "vinh thanh": "can tho", "phong dien": "can tho",
        "cai rang": "can tho", "ninh kieu": "can tho", "binh thuy": "can tho", "thot not": "can tho", "dinh mon": "can tho",
        "thap muoi": "dong thap", "cao lanh": "dong thap", "sa dec": "dong thap", "lai vung": "dong thap",
        "lap vo": "dong thap", "tam nong": "dong thap", "hong ngu": "dong thap", "thanh binh": "dong thap",
        "tri ton": "an giang", "tinh bien": "an giang", "chau doc": "an giang", "long xuyen": "an giang",
        "hon dat": "an giang", "phu tan": "an giang", "thoai son": "an giang", "cho moi": "an giang",
        "bao loc": "lam dong", "da lat": "lam dong", "duc trong": "lam dong", "don duong": "lam dong", "di linh": "lam dong",
    }

    KNOWN_PROVINCES = [
        "can tho", "an giang", "dong thap", "hau giang", "soc trang", "kien giang",
        "vinh long", "tien giang", "ben tre", "tra vinh", "ca mau", "bac lieu",
        "lam dong", "tay ninh", "dong nai", "dak lak", "gia lai", "khanh hoa", "tphcm", "ho chi minh"
    ]
    detected_prov = p_fold
    if not detected_prov:
        for kp in KNOWN_PROVINCES:
            if re.search(rf"\b{kp}\b", q_fold):
                detected_prov = kp
                break
        if not detected_prov:
            for dist_k, prov_v in DISTRICT_PROVINCE_MAP.items():
                if dist_k in q_fold:
                    detected_prov = prov_v
                    break

    scored: list[tuple[int, dict[str, Any]]] = []
    for it in items:
        name = _fold(it.get("display_name"))
        addr = _fold(it.get("public_address"))
        prov = _fold(it.get("province"))
        dist = _fold(it.get("district"))
        wd = _fold(it.get("ward"))
        full_text = f" {name} {addr} {prov} {dist} {wd} "

        score = 0
        if w_fold and f" {w_fold} " in full_text:
            score += 80
        if d_fold and f" {d_fold} " in full_text:
            score += 60
        if detected_prov and f" {detected_prov} " in full_text:
            score += 30
        elif detected_prov and prov and detected_prov != prov:
            continue

        for dist_term in DISTRICT_PROVINCE_MAP:
            if dist_term in q_fold and dist_term in full_text:
                score += 50

        if score > 0:
            scored.append((score, it))

    if scored:
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[:top_k]]

    # Fallback to provincial dealers if no direct commune/district hit
    if detected_prov:
        prov_dealers = [it for it in items if _fold(it.get("province")) == detected_prov]
        if prov_dealers:
            return prov_dealers[:top_k]

    return []


def _format_sales_locations_reply(locations: list[dict[str, Any]], area_str: str) -> str:
    if not locations:
        return ""
    PROV_NAME_MAP = {
        "lam dong": "Tỉnh Lâm Đồng",
        "dong thap": "Tỉnh Đồng Tháp",
        "can tho": "TP Cần Thơ",
        "an giang": "Tỉnh An Giang",
        "vinh long": "Tỉnh Vĩnh Long",
        "hau giang": "Tỉnh Hậu Giang",
        "soc trang": "Tỉnh Sóc Trăng",
        "kien giang": "Tỉnh Kiên Giang",
        "tien giang": "Tỉnh Tiền Giang",
        "ben tre": "Tỉnh Bến Tre",
        "tra vinh": "Tỉnh Trà Vinh",
        "ca mau": "Tỉnh Cà Mau",
        "bac lieu": "Tỉnh Bạc Liêu",
        "tay ninh": "Tỉnh Tây Ninh",
        "dong nai": "Tỉnh Đồng Nai",
        "dak lak": "Tỉnh Đắk Lắk",
    }
    folded_area = _normalize_vn(area_str)
    pretty_area = PROV_NAME_MAP.get(folded_area, area_str)
    header_area = f"khu vực {pretty_area}" if pretty_area else "gần bạn nhất"
    lines = [f"Dạ tại {header_area}, CFC - Phân bón Cò Bay có các đại lý phục vụ bạn:"]
    for i, loc in enumerate(locations, start=1):
        name = loc.get("display_name", "").strip()
        addr = loc.get("public_address", "").strip()
        phone = loc.get("public_phone", "").strip()
        dist = loc.get("distance_km")
        dist_str = f" (~{dist} km)" if dist else ""
        phone_str = f" - 📞 SĐT: {phone}" if phone else ""
        lat_val = loc.get("latitude")
        lon_val = loc.get("longitude")
        if lat_val and lon_val:
            maps_str = f"\n   🗺️ Chỉ đường: https://www.google.com/maps/dir/?api=1&destination={lat_val},{lon_val}"
        else:
            q_addr = urllib.parse.quote(f"{name}, {addr}")
            maps_str = f"\n   🗺️ Chỉ đường: https://www.google.com/maps/search/?api=1&query={q_addr}"
        lines.append(f"{i}. 🏪 **{name}**{dist_str}\n   📍 Địa chỉ: {addr}{phone_str}{maps_str}")
    lines.append("\nBạn có thể ghé trực tiếp hoặc liên hệ đại lý gần nhất để được hỗ trợ giao hàng tận nơi nhé!")
    return "\n".join(lines)


def _extract_cfc_confirmed_slots(
    user_message: str,
    query_entities: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    norm = _normalize_vn(user_message)
    phone, area = _extract_phone_and_area(user_message, norm)
    slots: dict[str, Any] = {}
    if phone:
        slots["phone"] = phone
    if area:
        slots["area"] = area

    formula_match = re.search(
        r"\b(?:npk\s+)?(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})(?:\s+(te))?\b",
        norm,
    )
    if formula_match:
        formula = "-".join(formula_match.group(index) for index in range(1, 4))
        if formula_match.group(4):
            formula += " TE"
        slots["formula"] = formula
        slots["product"] = f"NPK {formula}"
    elif re.search(r"\bnpk\s+chuyen lua\b", norm):
        slots["product"] = "NPK chuyên lúa"
    elif query_entities and query_entities.get("product"):
        slots["product"] = str(query_entities["product"])

    package_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(kg|g)\b", norm)
    if package_match:
        slots["package"] = f"{package_match.group(1)}{package_match.group(2)}"

    quantity_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(bao|tan|thung)\b", norm)
    if quantity_match:
        unit = {"tan": "tấn"}.get(quantity_match.group(2), quantity_match.group(2))
        slots["quantity"] = f"{quantity_match.group(1)} {unit}"

    order_match = re.search(r"\b(?:ma\s+don\s+)?#?dh\s+(\d{4})\s+(\d+)\b", norm)
    if order_match:
        slots["order_id"] = f"DH-{order_match.group(1)}-{order_match.group(2)}"

    crop_labels = {
        "sau rieng": "sầu riêng",
        "lua": "lúa",
        "cay an trai": "cây ăn trái",
        "rau mau": "rau màu",
        "ca phe": "cà phê",
        "tieu": "tiêu",
        "buoi": "bưởi",
        "cam": "cam",
        "xoai": "xoài",
        "mit": "mít",
        "thanh long": "thanh long",
    }
    for term, label in crop_labels.items():
        if re.search(rf"\b{term}\b", norm):
            slots["crop"] = label
            break

    stage_labels = {
        "nuoi trai non": "nuôi trái non",
        "trai non": "trái non",
        "xu ly ra hoa": "xử lý ra hoa",
        "ra hoa": "ra hoa",
        "dau trai": "đậu trái",
        "nuoi trai": "nuôi trái",
        "xuong giong": "xuống giống",
        "de nhanh": "đẻ nhánh",
        "lam dong": "làm đòng",
    }
    for term, label in stage_labels.items():
        if re.search(rf"\b{term}\b", norm):
            slots["crop_stage"] = label
            break

    symptom_labels = {
        "rung hat chuoi": "rụng hạt chuỗi",
        "rung trai non": "rụng trái non",
        "vang la": "vàng lá",
        "thoi re": "thối rễ",
        "xoan la": "xoăn lá",
        "cham lon": "chậm lớn",
    }
    for term, label in symptom_labels.items():
        if re.search(rf"\b{term}\b", norm):
            slots["symptom"] = label
            break

    acreage_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(hecta|ha|cong)\b", norm)
    if acreage_match:
        unit = "ha" if acreage_match.group(2) in {"hecta", "ha"} else "công"
        slots["acreage"] = f"{acreage_match.group(1)} {unit}"

    dealer_level = re.search(r"\bdai ly cap\s*(\d+)\b", norm)
    if dealer_level:
        slots["dealer_level"] = f"cấp {dealer_level.group(1)}"
    return slots


def _cfc_missing_slots(goal: str, confirmed_slots: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for slot in CFC_REQUIRED_SLOTS.get(goal, ()):
        if slot == "area":
            location = confirmed_slots.get("location") or {}
            if confirmed_slots.get("area") or (
                isinstance(location, dict)
                and location.get("latitude") is not None
                and location.get("longitude") is not None
            ):
                continue
        elif slot == "product":
            if confirmed_slots.get("product") or confirmed_slots.get("formula"):
                continue
        elif confirmed_slots.get(slot):
            continue
        missing.append(slot)
    return missing


def _merged_cfc_slots(
    state: dict[str, Any],
    user_message: str,
    *,
    phone: str = "",
    area: str = "",
    query_entities: Optional[dict[str, Any]] = None,
    location: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    slots = dict(state.get("confirmed_slots") or {})
    slots.update(_extract_cfc_confirmed_slots(user_message, query_entities))
    if phone:
        slots["phone"] = phone
    if area:
        slots["area"] = area
    if location:
        slots["location"] = location
    return slots


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 4:
        return "số bạn vừa gửi"
    return f"***{digits[-4:]}"


def _is_phone_only_submission(text: str, phone: str) -> bool:
    if not phone:
        return False
    remainder = text.replace(phone, " ")
    normalized = _normalize_vn(remainder)
    normalized = re.sub(
        r"\b(so dien thoai|dien thoai|sdt|so cua toi|so cua minh|cua toi|cua minh|"
        r"toi la|minh la|la|day|nhe|nha|a)\b",
        " ",
        normalized,
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return True
    # Cho phép khách gửi kèm đúng một cụm địa bàn, nhưng không nuốt câu hỏi nghiệp vụ.
    _, submitted_area = _extract_phone_and_area(text, _normalize_vn(text))
    return bool(submitted_area and _normalize_vn(submitted_area) == normalized)


def _cfc_context_summary(goal: str, slots: dict[str, Any]) -> str:
    field_order = {
        "dealer_lookup": ("area",),
        "inventory_check": ("product", "package", "quantity", "area"),
        "order_tracking": ("order_id",),
        "loyalty_lookup": ("phone",),
        "wholesale_policy": ("dealer_level", "area"),
        "price_quote": ("product", "package", "crop", "area"),
        "agronomy_consultation": ("crop", "crop_stage", "symptom", "acreage", "area"),
    }
    labels = {
        "product": "sản phẩm",
        "package": "quy cách",
        "quantity": "số lượng",
        "order_id": "mã đơn",
        "phone": "SĐT",
        "dealer_level": "hạng đại lý",
        "crop": "cây trồng",
        "crop_stage": "giai đoạn",
        "symptom": "hiện tượng",
        "acreage": "diện tích",
        "area": "khu vực",
    }
    parts: list[str] = []
    for field in field_order.get(goal, ()):
        value = slots.get(field)
        if not value:
            continue
        rendered = _mask_phone(str(value)) if field == "phone" else str(value)
        parts.append(f"{labels[field]} {rendered}")
    return "; ".join(parts)


def _cfc_missing_slots_prompt(missing_slots: list[str], *, expert: bool = False) -> str:
    labels = {
        "phone": "số điện thoại",
        "area": "khu vực/tỉnh thành",
        "product": "tên hoặc công thức sản phẩm",
        "order_id": "mã đơn hàng",
        "crop": "loại cây trồng",
        "crop_stage": "giai đoạn cây hiện tại",
    }
    missing = [labels.get(slot, slot) for slot in missing_slots]
    if not missing:
        return (
            "Mình đã có đủ dữ kiện tiếp nhận; kỹ sư nông nghiệp cần đối chiếu trước khi đưa khuyến nghị kỹ thuật ạ."
            if expert
            else "Mình đã có đủ thông tin tiếp nhận để admin đối chiếu yêu cầu này ạ."
        )
    if len(missing) == 1:
        rendered = missing[0]
    else:
        rendered = ", ".join(missing[:-1]) + " và " + missing[-1]
    owner = "kỹ sư nông nghiệp" if expert else "admin"
    return f"Bạn gửi thêm {rendered} để {owner} kiểm tra đúng yêu cầu, mình không hỏi lại các thông tin đã có nha."


def _format_b2b_large_order_reply(user_message: str, query_entities: Optional[dict[str, Any]] = None, phone: str = "") -> str:
    try:
        from domains.amis.live_crm import create_cskh_ticket
        create_cskh_ticket("b2b_vip_lead", customer_phone=phone, issue_description=user_message)
    except Exception:
        pass
    lines = [
        "Dạ CFC - Phân bón Cò Bay xin kính chào Quý Khách hàng / Quý Hợp tác xã!",
        "",
        "Với nhu cầu đặt hàng khối lượng lớn (từ 5 tấn - 30 tấn trở lên) phục vụ sản xuất quy mô trang trại và hợp tác xã, Ban Giám Đốc & Phòng Kinh Doanh CFC sẽ trực tiếp làm việc để cung cấp chính sách giá xuất xưởng và hợp đồng thương mại ưu đãi nhất.",
        "",
        "📞 **Hotline Trực Tiếp Ban Giám Đốc / Phòng Kinh Doanh:** 0292 3841 815 - 0906 929 292",
    ]
    if phone:
        lines.append(f"\nBên mình đã ghi nhận số điện thoại liên hệ: **{phone}**. Giám đốc Kinh doanh khu vực sẽ liên hệ lại trực tiếp cho bạn ngay trong 15 phút nhé ạ!")
    else:
        lines.append("\nBạn vui lòng để lại **Tên người đại diện** và **Số điện thoại**, Giám đốc Kinh doanh khu vực sẽ liên hệ làm việc trực tiếp ngay nhé ạ!")
    return "\n".join(lines)


def _format_complaint_sop_reply(user_message: str, phone: str = "") -> str:
    try:
        from domains.amis.live_crm import create_cskh_ticket
        create_cskh_ticket("product_complaint_sop", customer_phone=phone, issue_description=user_message)
    except Exception:
        pass
    lines = [
        "Dạ CFC - Phân bón Cò Bay thành thật xin lỗi bạn vì sự cố phân bón bị vón cục/lỗi bao bì đã làm ảnh hưởng đến công việc canh tác của mình ạ.",
        "",
        "Để bộ phận Đảm bảo Chất lượng (QA/QC) kiểm tra mẫu lưu và tiến hành quy trình đổi trả hàng khẩn cấp theo đúng quy chuẩn SOP, bạn vui lòng hỗ trợ bên mình:",
        "1. 📸 Gửi ảnh chụp hiện trạng phân bón và vị trí in **Mã Lô sản xuất (Lot No.) / Ngày sản xuất (NSX)** trên vỏ bao.",
        "2. 📞 Cung cấp Số điện thoại và Địa chỉ điểm giao để chuyên viên CSKH liên hệ xử lý trực tiếp trong vòng 24 giờ làm việc nhé ạ!",
    ]
    return "\n".join(lines)


def _build_cfc_capability_boundary(intent: str, slots: dict[str, Any], raw_text: str = "") -> tuple[str, str]:
    goal = CFC_GOAL_BY_INTENT.get(intent) or ""
    context = _cfc_context_summary(goal, slots)
    context_line = f" Mình đang giữ thông tin: {context}." if context else ""
    missing_prompt = _cfc_missing_slots_prompt(_cfc_missing_slots(goal, slots))
    norm = _normalize_vn(raw_text)

    if intent == "cfc_inventory_unavailable":
        from domains.amis.live_crm import lookup_inventory_atp
        prod_info = lookup_inventory_atp(raw_text)
        if prod_info:
            p_name = prod_info.get("product_name") or "sản phẩm"
            p_code = prod_info.get("product_code") or ""
            p_unit = prod_info.get("unit") or "Bao"
            wh_loc = prod_info.get("warehouse_location") or "Tổng kho Nhà máy Cần Thơ"
            answer = (
                f"Dạ dòng sản phẩm **{p_name}** (Mã: {p_code}, Quy cách: {p_unit}) hiện có sẵn trong danh mục sản xuất chính thức tại {wh_loc}. "
                "Nhà máy và hệ thống phân phối hoàn toàn có khả năng đáp ứng đơn hàng theo yêu cầu. "
                "Bạn vui lòng để lại Số điện thoại và Khu vực/Địa chỉ nhận hàng để nhân viên kinh doanh đối chiếu kho gần nhất và báo lịch giao xe nhé ạ!"
            )
            return answer, "INVENTORY_ATP_QUALIFIED"
        else:
            answer = (
                f"Dạ CFC Cò Bay đã đối chiếu trên danh mục sản phẩm của nhà máy nhưng chưa tìm thấy mã hàng tương ứng với yêu cầu của bạn.{context_line} "
                "Bạn vui lòng để lại Số điện thoại và quy cách cần tìm để nhân viên kỹ thuật hỗ trợ tra cứu công thức phù hợp nhé ạ!"
            )
            return answer, "INVENTORY_PRODUCT_NOT_FOUND"

    if intent == "cfc_order_status_unavailable":
        from domains.amis.live_crm import lookup_order_status, format_order_status_response
        order_match = re.search(r"#?([A-Za-z]{2,5}[-_ ]?\d{2,6}[-_ ]?\d{2,6}|[A-Za-z]{2,5}\d{3,8}|#\d{3,8})", raw_text)
        extracted_order_code = order_match.group(0).strip() if order_match else ""
        dealer_match = re.search(r"(?:dai ly|khach hang)\s+([A-Za-z0-9\s_À-ỹ]+)", raw_text, re.IGNORECASE)
        extracted_dealer = dealer_match.group(1).strip() if dealer_match else ("vinh thanh" if "vinh thanh" in norm or "anh ba" in norm else "")

        order_data = lookup_order_status(order_code=extracted_order_code, dealer_name=extracted_dealer)
        if order_data:
            answer = format_order_status_response(order_data, query_order_code=extracted_order_code)
            return answer, "ORDER_REALTIME_LOOKUP"

        answer = (
            f"Dạ mình hiểu bạn cần kiểm tra tiến độ đơn hàng/xe bốc hàng.{context_line} "
            "Hệ thống chat hiện chưa kết nối dữ liệu đơn hàng và vận tải nên mình chưa thể xác nhận trạng thái thực tế. "
            f"{missing_prompt}"
        )
        return answer, "ORDER_TRACKING_NOT_CONNECTED"

    if intent == "cfc_loyalty_unavailable":
        from domains.amis.live_crm import lookup_loyalty_info, format_loyalty_response
        phone_raw = slots.get("phone") or ""
        loyalty_data = lookup_loyalty_info(phone_raw)
        if loyalty_data:
            answer = format_loyalty_response(loyalty_data, phone=phone_raw)
            return answer, "LOYALTY_REALTIME_LOOKUP"
        phone_disp = _mask_phone(slots.get("phone", ""))
        answer = (
            f"Dạ CFC Cò Bay đã tra cứu trên hệ thống AMIS CRM nhưng *không tìm thấy* thông tin hội viên cho số điện thoại {phone_disp}. "
            "Có thể số này chưa được đăng ký hoặc đăng ký với SĐT khác. "
            "Anh/chị vui lòng kiểm tra lại SĐT hoặc liên hệ nhân viên kinh doanh để đăng ký hồ sơ nhé ạ!"
        )
        return answer, "LOYALTY_NOT_FOUND"

    answer = (
        f"Dạ mình hiểu bạn cần bảng giá sỉ và chính sách chiết khấu hiện hành.{context_line} "
        "Chính sách chiết khấu và ưu đãi đại lý cấp 1 được áp dụng theo từng vụ mùa và sản lượng hợp đồng. Để bảo mật chính sách thương mại nội bộ, "
        "bạn vui lòng để lại Số điện thoại và Khu vực kinh doanh, Trưởng phòng Kinh doanh khu vực sẽ liên hệ gửi bảng chính sách trực tiếp nhé ạ!"
    )
    return answer, "WHOLESALE_POLICY_NOT_VERIFIED"


def _build_cfc_agronomy_intake_answer(slots: dict[str, Any], raw_text: str = "") -> str:
    goal = "agronomy_consultation"
    context = _cfc_context_summary(goal, slots)
    context_line = f" Mình đang ghi nhận: {context}." if context else ""
    missing_prompt = _cfc_missing_slots_prompt(_cfc_missing_slots(goal, slots), expert=True)
    return (
        f"Dạ mình hiểu đây là yêu cầu tư vấn kỹ thuật nông nghiệp và quy trình bón phân.{context_line} "
        "Công thức NPK và liều lượng bón tối ưu cần được kỹ sư đối chiếu chuẩn xác theo cây trồng, giai đoạn sinh trưởng, chất đất và khu vực canh tác. "
        f"{missing_prompt}"
    )


def _default_conversation_state(brand: str) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "brand": brand.upper(),
        "conversation_topic": "",
        "current_intent": "",
        "current_goal": "",
        "active_entities": {
            "product": "",
            "product_intent": "",
            "category": "",
            "product_id": "",
            "shopee_url": "",
            "price": None,
            "rank": None,
        },
        "last_products_shown": [],
        "customer_constraints": {},
        "confirmed_slots": {},
        "active_goal": {"name": "", "stage": ""},
        "pending_request": {},
        "last_capability_boundary": {},
        "active_flow": {"name": "", "stage": ""},
        "pending_action": {"name": "", "status": ""},
        "pending_question": "",
        "pending_slots": [],
        "pending_options": [],
        "topic_stack": [],
        "corrections": [],
        "takeover_state": {"status": "none", "owner": "", "reason": ""},
        "covered_fact_ids": [],
        "recent_turns": [],
        "conversation_summary": "",
        "last_source_id": "",
        "updated_at": "",
    }


def _load_conversation_state(existing_session: dict, brand: str) -> dict[str, Any]:
    raw_state = existing_session.get("conversation_state") or {}
    if isinstance(raw_state, str):
        try:
            raw_state = json.loads(raw_state)
        except Exception:
            raw_state = {}
    if not isinstance(raw_state, dict):
        raw_state = {}

    state = _default_conversation_state(brand)
    state.update({k: v for k, v in raw_state.items() if v not in (None, "")})
    active_entities = state.get("active_entities") if isinstance(state.get("active_entities"), dict) else {}
    state["active_entities"] = {
        "product": active_entities.get("product") or existing_session.get("current_product", ""),
        "product_intent": active_entities.get("product_intent") or active_entities.get("intent", ""),
        "category": active_entities.get("category") or existing_session.get("current_category", ""),
        "product_id": active_entities.get("product_id", ""),
        "shopee_url": active_entities.get("shopee_url", ""),
        "price": active_entities.get("price"),
        "rank": active_entities.get("rank"),
    }
    if not isinstance(state.get("last_products_shown"), list):
        state["last_products_shown"] = []
    if not isinstance(state.get("customer_constraints"), dict):
        state["customer_constraints"] = {}
    if not isinstance(state.get("confirmed_slots"), dict):
        state["confirmed_slots"] = {}
    stored_area = _sanitize_stored_area(str(state["confirmed_slots"].get("area") or ""))
    if stored_area:
        state["confirmed_slots"]["area"] = stored_area
    else:
        state["confirmed_slots"].pop("area", None)
    if isinstance(state.get("active_goal"), str):
        state["active_goal"] = {"name": state["active_goal"], "stage": "active"}
    elif not isinstance(state.get("active_goal"), dict):
        state["active_goal"] = {"name": "", "stage": ""}
    if not isinstance(state.get("pending_request"), dict):
        state["pending_request"] = {}
    if not isinstance(state.get("last_capability_boundary"), dict):
        state["last_capability_boundary"] = {}
    if not isinstance(state.get("active_flow"), dict):
        state["active_flow"] = {"name": "", "stage": ""}
    if not isinstance(state.get("pending_action"), dict):
        state["pending_action"] = {"name": "", "status": ""}
    if not isinstance(state.get("pending_question"), str):
        state["pending_question"] = ""
    for key in ("pending_slots", "pending_options", "topic_stack", "corrections"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("takeover_state"), dict):
        state["takeover_state"] = {"status": "none", "owner": "", "reason": ""}
    if not isinstance(state.get("covered_fact_ids"), list):
        state["covered_fact_ids"] = []
    if not isinstance(state.get("recent_turns"), list):
        state["recent_turns"] = []
    return state


def _sanitized_chat_history(conversation_state: dict[str, Any], limit: int = 6) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for turn in (conversation_state.get("recent_turns") or [])[-max(1, limit):]:
        if not isinstance(turn, dict):
            continue
        for role, field in (("user", "user"), ("assistant", "bot")):
            content = str(turn.get(field) or "").strip()
            if not content:
                continue
            content = re.sub(r"(?<!\d)(?:\+?84|0)(?:[\s.()-]*\d){8,10}(?!\d)", "[PHONE]", content)
            content = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[EMAIL]", content, flags=re.I)
            history.append({"role": role, "content": content[:600]})
    return history[-(max(1, limit) * 2):]


def _copy_product_item(item: dict) -> dict[str, Any]:
    product = {
        "name": str(item.get("name", "")).strip(),
        "category": str(item.get("category", "")).strip(),
        "intent": str(item.get("intent", "")).strip(),
    }
    product_id = item.get("item_id") or item.get("product_id") or item.get("id")
    shopee_url = item.get("link_shopee") or item.get("shopee_url") or item.get("url")
    if product_id not in (None, ""):
        product["product_id"] = str(product_id).strip()
    if shopee_url:
        product["shopee_url"] = str(shopee_url).strip()
    for field in ("price", "original_price", "discount", "rank", "source_version", "shown_at"):
        if item.get(field) not in (None, ""):
            # pyrefly: ignore [bad-assignment]
            product[field] = item.get(field)
    return product


def _extract_query_entities(norm_text: str, brand: str) -> dict[str, Any]:
    matched_entities = []
    brand_l = brand.lower()
    for intent, name, category, pattern in PRODUCT_ENTITY_PATTERNS:
        if brand_l == "zeo" and (intent.startswith("cfc_") or name.startswith("Phân bón CFC")):
            continue
        if brand_l == "cfc" and (
            intent.startswith("zeo_")
            or intent.startswith("pano_")
            or intent.startswith("oplus_")
            or name in {"PANO", "Oplus"}
        ):
            continue
        if re.search(pattern, norm_text):
            matched_entities.append({
                "product": name,
                "product_intent": intent,
                "category": category,
            })

    primary = matched_entities[0] if matched_entities else {}
    return {
        "product": primary.get("product", ""),
        "product_intent": primary.get("product_intent", ""),
        "category": primary.get("category", ""),
        "matched_entities": matched_entities,
    }


def _has_reference_signal(norm_text: str) -> bool:
    tokens = norm_text.split()
    if _has_any(norm_text, [
        r"\b(cai|loai|dong|nhom|san pham|phan|mon)\s+(nay|do|tren|hoi nay|vua roi|vua noi)\b",
        r"\b(no|do|nay|tren)\s+(gia|bao nhieu|nhieu tien|ship|giao hang|con hang|con khong|con)\b",
        r"\b(cai|loai|dong|nhom|san pham|phan)\s+(dau tien|thu nhat|thu hai|thu ba|thu tu|thu 1|thu 2|thu 3|thu 4|so 1|so 2|so 3|so 4|\d)\b",
        r"\b(dau tien|thu nhat|thu hai|thu ba|thu tu|thu 1|thu 2|thu 3|thu 4|so 1|so 2|so 3|so 4|nhom 1|nhom 2|nhom 3|nhom 4)\b",
        r"\b(vua hoi|vua noi|hoi nay|luc nay|o tren|y la gia|y la|gia cua can|gia can|can to|can lon|tui to|tui lon)\b",
        r"\b(\d+[\s\.]*\d*\s*(?:kg|g|ml|lit|can|tui|chai)|can\s+\d+|tui\s+\d+|chai\s+\d+)\b",
    ]):
        return True
    return len(tokens) <= 5 and any(t in {"no", "do", "nay", "tren"} for t in tokens)


def _ordinal_reference_index(norm_text: str) -> Optional[int]:
    ordinal_patterns = [
        (0, r"\b(dau tien|thu nhat|thu 1|so 1|muc 1|loai 1|cai 1|nhom 1|1)\b"),
        (1, r"\b(thu hai|thu 2|so 2|muc 2|loai 2|cai 2|nhom 2|2)\b"),
        (2, r"\b(thu ba|thu 3|so 3|muc 3|loai 3|cai 3|nhom 3|3)\b"),
        (3, r"\b(thu tu|thu 4|so 4|muc 4|loai 4|cai 4|nhom 4|4)\b"),
    ]
    for idx, pattern in ordinal_patterns:
        if re.search(pattern, norm_text):
            return idx
    return None


def _resolve_reference(raw_text: str, norm_text: str, conversation_state: dict[str, Any]) -> dict[str, Any]:
    products = [
        _copy_product_item(item)
        for item in (conversation_state.get("last_products_shown") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    active = conversation_state.get("active_entities") or {}

    if not _has_reference_signal(norm_text) or (not products and not active.get("product")):
        return {
            "references_previous_turn": False,
            "resolved": False,
            "product": "",
            "product_intent": "",
            "category": "",
            "product_id": "",
            "shopee_url": "",
            "price": None,
            "rank": None,
            "resolved_query": raw_text,
            "reason": "no_reference",
        }
    idx = _ordinal_reference_index(norm_text)
    chosen: dict[str, str] = {}
    reason = "unresolved"

    if idx is not None and 0 <= idx < len(products):
        chosen = products[idx]
        reason = "ordinal"
    else:
        # Tìm theo biến thể / quy cách xuất hiện trong last_products_shown
        if products:
            for p in products:
                p_name_norm = _normalize_vn(p.get("name", ""))
                if any(_normalize_vn(term) in norm_text and _normalize_vn(term) in p_name_norm for term in ["3.8kg", "9kg", "5.5kg", "3.5kg", "2.4kg", "650ml", "400g", "720g", "vitamin e", "nha dam"]):
                    chosen = p
                    reason = "variant_match"
                    break
        if not chosen and active.get("product"):
            active_name = str(active.get("product", ""))
            # pyrefly: ignore [bad-assignment]
            chosen = next(
                (
                    product_item
                    for product_item in products
                    if _normalize_vn(product_item.get("name", "")) == _normalize_vn(active_name)
                ),
                {
                    "name": active_name,
                    "intent": str(active.get("product_intent", "")),
                    "category": str(active.get("category", "")),
                    "product_id": str(active.get("product_id", "")),
                    "shopee_url": str(active.get("shopee_url", "")),
                    "price": active.get("price"),
                    "rank": active.get("rank"),
                },
            )
            reason = "active_entity"
        elif not chosen and len(products) == 1:
            chosen = products[0]
            reason = "single_last_product"

    if not chosen:
        return {
            "references_previous_turn": True,
            "resolved": False,
            "product": "",
            "product_intent": "",
            "category": "",
            "product_id": "",
            "shopee_url": "",
            "price": None,
            "rank": None,
            "resolved_query": raw_text,
            "reason": reason,
        }

    product = chosen.get("name", "").strip()
    return {
        "references_previous_turn": True,
        "resolved": True,
        "product": product,
        "product_intent": chosen.get("intent", ""),
        "category": chosen.get("category", ""),
        "product_id": chosen.get("product_id", ""),
        "shopee_url": chosen.get("shopee_url", ""),
        "price": chosen.get("price"),
        "rank": chosen.get("rank"),
        "resolved_query": f"{raw_text} ({product})" if product else raw_text,
        "reason": reason,
    }


def _looks_like_shipping_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"(giao hang|van chuyen|gui hang|giao ve|ship|phi ship|cuoc|cod|thanh toan khi nhan)",
        r"(ve|toi|den).{0,30}(tinh|tp|thanh pho|huyen|quan|can tho|tphcm|ha noi|da nang|long an|dong nai|binh duong)",
    ])


def _looks_like_availability_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"(con hang|het hang|con khong|con hong|con ko|co san hang|co san khong|co san hong|co san ko|ton kho|het chua|con nua khong|con nua hong)",
    ])


def _detect_third_party_customer_lookup(norm_text: str) -> bool:
    """Chặn tra cứu khách theo tên; chỉ profile của chính sender_id được phép recall."""
    if re.search(r"\b(cua toi|cua minh|cua em|cua anh|cua chi)\b", norm_text):
        return False
    return _has_any(norm_text, [
        r"\b(thong tin|ho so|so dien thoai|dien thoai|sdt|dia chi)\s+(cua\s+)?(khach hang|khach|nguoi mua)\b",
        r"\b(tra cuu|tim|cho xem|cho toi)\s+.{0,20}\b(khach hang|khach|nguoi mua)\b",
        r"^thong tin\s+(khach hang|khach)\s+.+$",
    ])


def _detect_return_followup_intent(
    norm_text: str,
    previous_intent: str,
    conversation_state: dict[str, Any],
) -> Optional[str]:
    """Giữ luồng đổi trả cho câu rút gọn/typo mà không kéo câu ngoài ngữ cảnh vào policy."""
    active_flow = conversation_state.get("active_flow") or {}
    in_return_flow = previous_intent in RETURN_CONTEXT_INTENTS or active_flow.get("name") == "return_request"

    if re.search(r"^(tra hang|doi tra|doi hang)$", norm_text):
        return "return_eligible_cases"
    if re.search(
        r"(lien he|goi|nhan tin).{0,30}(tra hang|doi tra|doi hang)|(tra hang|doi tra|doi hang).{0,30}(lien he|goi ai|lam sao)",
        norm_text,
    ):
        return "return_process"
    if re.search(r"(quy trinh doi tra|cac buoc doi tra|lam sao de doi tra|lam sao de tra hang)", norm_text):
        return "return_process"
    if re.search(r"(thoi han doi tra|bao lau.{0,20}(doi tra|tra hang)|doi tra.{0,20}bao lau)", norm_text):
        return "return_claim_deadlines"
    if re.search(r"(doi|tra hang|doi tra).{0,20}(ton phi|mat phi|phi bao nhieu|co phi)", norm_text):
        return "return_fee_unverified"

    if in_return_flow:
        if re.search(r"^(lien he sao|lien he the nao|goi ai|nhan tin ai|can gui gi|gui gi|lam sao)$", norm_text):
            return "return_process"
        if re.search(r"\b(dien|doi).{0,20}(ton phi|mat phi|co phi|phi khong)\b", norm_text):
            return "return_fee_unverified"
        if re.search(r"^(bao lau|may ngay|thoi han bao lau)$", norm_text):
            return "return_claim_deadlines"
    return None


def _detect_need_choice(norm_text: str) -> Optional[str]:
    """Nhận diện lựa chọn nhu cầu của khách hàng (tiết kiệm, thơm lâu, sạch sâu, dịu nhẹ)."""
    if re.search(r"\b(cai nao|loai nao|dong nao|san pham nao|biet cai nao|goi y|tu van)\b.*\b(thom|luu huong|mui huong)\b", norm_text):
        return "thom_lau"

    # Nếu câu hỏi dạng 'có ... không' (vd: có thơm lâu không, có sạch không) -> Factual FAQ, không phải chọn nhu cầu
    if re.search(r"\bco\s+.*\s+(khong|hong|ko|k)\b", norm_text):
        return None

    # Nếu là câu hỏi về sản phẩm cụ thể
    if any(p in norm_text for p in ["bot giat zeo", "bot giat oplus", "nuoc giat pano", "zif", "javen"]):
        return None

    # 1. Tiết kiệm
    if re.search(r"\b(nhu cau tiet kiem|tiet kiem|loai re|re nhat|gia re nhat|it tien|kinh te|re tien|tiet kiem tien|re hon|muon re|re nhat di)\b", norm_text):
        return "tiet_kiem"
    # 2. Thơm lâu
    if re.search(r"\b(nhu cau thom|thom lau|thom thom|mui nuoc hoa|nuoc hoa|luu huong|thom nhat|mui thom|huong thom|thom nhat di)\b", norm_text):
        return "thom_lau"
    # 3. Sạch sâu
    if re.search(r"\b(nhu cau sach|sach sau|vet ban|vet ban cung dau|sach manh|tay sach|danh bay vet ban|sach sau di)\b", norm_text):
        return "sach_sau"
    # 4. Dịu nhẹ
    if re.search(r"\b(nhu cau diu|diu nhe|duong da|em be|da tay|da nhay cam|an toan cho da|khong hai da|diu nhe di)\b", norm_text):
        return "diu_nhe"
    return None


def _should_try_llm_nlu(norm_text: str, brand: str) -> bool:
    """Chỉ gọi Ollama NLU cho câu hỏi bán hàng ZeO có khả năng cần tool/catalog."""
    if brand.lower() != "zeo":
        return False
    if len(norm_text.strip()) < 5:
        return False
    return bool(re.search(
        r"\b("
        r"gia|bao nhieu|mac|dat|cao|re|thap|"
        r"san pham|sp|link|mua|dat hang|shop|shopee|"
        r"con hang|het hang|ton kho|co san pham|"
        r"cai nao|loai nao|dong nao|goi y|tu van|"
        r"thom|mui huong|luu huong|sach|vet ban|diu nhe|tiet kiem|"
        r"nuoc giat|bot giat|giat do|nuoc xa|xa vai|rua chen|lau san|tay rua|"
        r"pano|zeo|oplus|zif|javen"
        r")\b",
        norm_text,
    ))


def _active_product_context_key(conversation_state: dict[str, Any], previous_intent: str = "") -> str:
    active = conversation_state.get("active_entities") or {}
    candidates = [
        str(active.get("product_intent", "")),
        previous_intent,
        str(active.get("product", "")),
    ]
    products = conversation_state.get("last_products_shown") or []
    if len(products) == 1 and isinstance(products[0], dict):
        candidates.extend([str(products[0].get("intent", "")), str(products[0].get("name", ""))])

    combined = _normalize_vn(" ".join(candidates))
    for context_key, spec in TECH_CONTEXT_INTENTS.items():
        if any(intent in candidates for intent in spec["intents"]):
            return context_key
        if re.search(spec["product_pattern"], combined):
            return context_key
    return ""


def _detect_contextual_technology_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"^(co )?cong nghe gi$",
        r"^(co )?cong nghe nao khac( khong| ko| hong)?$",
        r"^(con )?cong nghe nao( nua| khac)?( khong| ko| hong)?$",
        r"^cong nghe gi nua( khong| ko)?$",
    ])


def _detect_vague_more_followup(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"^(con gi nua|con gi nua khong|con gi nua ko|con gi nua hong|con nua khong|con nua hong|con nua ko|co gi nua|co gi nua khong|them gi nua|nua khong|nua ko)$",
        r"^(con.*khac.*khong|con.*khac.*ko|con.*khac.*hong)$",
        r"^(ngoai ra.*gi|ngoai.*cai.*do.*con.*gi)$",
    ])


async def _build_contextual_more_info_answer(
    brand: str,
    context_key: str,
    only_technology: bool = False,
    covered_facts: Optional[list[str]] = None,
) -> tuple[str, str, str]:
    """Tạo câu trả lời bổ sung thông tin, tự động loại trừ các fact đã trả lời trước đó."""
    if brand.lower() != "zeo" or context_key not in TECH_CONTEXT_INTENTS:
        return "", "", ""

    spec = TECH_CONTEXT_INTENTS[context_key]
    candidate_intents = spec["related_intents"] if not only_technology else [spec["technology_intent"], "oplus_detergent_ion_technology", "pano_veilex_odor_control"]
    covered_set = set(covered_facts or [])

    # Lọc ra các intent chưa được trả lời
    remaining_intents = [it for it in candidate_intents if it not in covered_set]
    if not remaining_intents:
        remaining_intents = [spec["technology_intent"]]

    chosen_intent = remaining_intents[0]
    # pyrefly: ignore [bad-argument-type]
    item = await get_faq_by_intent(brand, chosen_intent)
    answer = str(item.get("answer", "")).strip()
    if not answer:
        return "", "", ""

    product_name = {
        "zeo_detergent": "Bột giặt ZeO",
        "oplus_detergent": "Bột giặt Oplus",
        "pano_laundry": "Bột giặt & Nước giặt PANO",
    }.get(context_key, "sản phẩm này")

    if only_technology:
        msg = (
            f"Dạ với {product_name}, hiện hệ thống đang có thông tin công nghệ đã xác nhận là:\n\n"
            f"1. {answer}\n\n"
            f"Mình chưa thấy dữ liệu xác nhận công nghệ khác ngoài các thông tin trên, nên mình không tự bổ sung thêm nha."
        )
        # pyrefly: ignore [bad-return]
        return msg, "contextual_technology_more_info", chosen_intent

    msg = (
        f"Dạ với {product_name}, hiện hệ thống đang có các thông tin đã xác nhận:\n\n"
        f"1. {answer}\n\n"
        f"Bạn muốn mình kiểm tra tiếp giá, quy cách hay cách mua hàng cho sản phẩm này không ạ?"
    )
    # pyrefly: ignore [bad-return]
    return msg, "contextual_product_more_info", chosen_intent


def _product_memory_for_intent(intent: str, answer: str, brand: str) -> list[dict[str, str]]:
    products = []

    # 1. Bóc tách các sản phẩm cụ thể xuất hiện trong bot_reply (từ Shopee Catalog) - ƯU TIÊN CAO NHẤT
    bold_items = re.findall(r"\*\*(.+?)\*\*", answer)
    for b_item in bold_items:
        b_clean = b_item.strip()
        b_norm = _normalize_vn(b_clean)
        if len(b_clean) > 10 and any(k in b_norm for k in ["giat", "rua chen", "lau san", "tay", "javen", "zif", "pano", "zeo", "oplus"]):
            item = {"name": b_clean, "category": "shopee_product", "intent": intent}
            if item not in products:
                products.append(item)

    if products:
        return products[:8]

    if intent in PRODUCT_MEMORY_BY_INTENT:
        return [_copy_product_item(item) for item in PRODUCT_MEMORY_BY_INTENT[intent]]

    norm_answer = _normalize_vn(answer)
    if not norm_answer:
        return []

    for entity_intent, name, category, pattern in PRODUCT_ENTITY_PATTERNS:
        if brand.lower() == "zeo" and (entity_intent.startswith("cfc_") or name.startswith("Phân bón CFC")):
            continue
        if brand.lower() == "cfc" and (
            entity_intent.startswith("zeo_")
            or entity_intent.startswith("pano_")
            or entity_intent.startswith("oplus_")
            or name in {"PANO", "Oplus"}
        ):
            continue
        if entity_intent == intent or re.search(pattern, norm_answer):
            item = {"name": name, "category": category, "intent": entity_intent}
            if item not in products:
                products.append(item)

    return products[:8]


def _build_next_conversation_state(
    previous_state: dict[str, Any],
    *,
    brand: str,
    user_message: str,
    bot_reply: str,
    intent: str,
    lead_stage: str,
    query_entities: dict[str, Any],
    reference_resolution: dict[str, Any],
    source_id: str = "",
    products_shown: Optional[list[dict[str, Any]]] = None,
    state_patch: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    state = _load_conversation_state({"conversation_state": previous_state}, brand)
    now_str = datetime.now(timezone.utc).isoformat()
    products = []
    if products_shown is not None:
        for index, item in enumerate(products_shown, start=1):
            if not isinstance(item, dict):
                continue
            product = _copy_product_item(item)
            if not product.get("name"):
                continue
            product["intent"] = product.get("intent") or intent
            product["rank"] = product.get("rank") or index
            product["shown_at"] = product.get("shown_at") or now_str
            products.append(product)
    else:
        products = _product_memory_for_intent(intent, bot_reply, brand)

    if products_shown is not None:
        state["last_products_shown"] = products
    elif products:
        state["last_products_shown"] = products
    elif query_entities.get("product"):
        state["last_products_shown"] = [{
            "name": query_entities.get("product", ""),
            "category": query_entities.get("category", ""),
            "intent": query_entities.get("product_intent", ""),
        }]
    state["pending_options"] = [dict(item) for item in state.get("last_products_shown", [])[:8]]

    active_product = query_entities.get("product") or reference_resolution.get("product") or ""
    active_intent = query_entities.get("product_intent") or reference_resolution.get("product_intent") or ""
    active_category = query_entities.get("category") or reference_resolution.get("category") or ""
    if active_product:
        state["active_entities"] = {
            "product": active_product,
            "product_intent": active_intent,
            "category": active_category,
            "product_id": reference_resolution.get("product_id", ""),
            "shopee_url": reference_resolution.get("shopee_url", ""),
            "price": reference_resolution.get("price"),
            "rank": reference_resolution.get("rank"),
        }
    elif len(state.get("last_products_shown", [])) == 1:
        only_product = state["last_products_shown"][0]
        state["active_entities"] = {
            "product": only_product.get("name", ""),
            "product_intent": only_product.get("intent", ""),
            "category": only_product.get("category", ""),
            "product_id": only_product.get("product_id", ""),
            "shopee_url": only_product.get("shopee_url", ""),
            "price": only_product.get("price"),
            "rank": only_product.get("rank"),
        }
    elif products_shown is not None and not products:
        state["active_entities"] = {
            "product": "",
            "product_intent": "",
            "category": "",
            "product_id": "",
            "shopee_url": "",
            "price": None,
            "rank": None,
        }

    # Cập nhật covered_fact_ids
    covered = state.get("covered_fact_ids") or []
    if source_id and source_id not in covered:
        covered.append(source_id)
    if intent and intent not in covered:
        covered.append(intent)
    state["covered_fact_ids"] = covered[-10:]

    if area_match := re.search(r"\b(o|tai|ve|den)\s+(.{2,40})$", _normalize_vn(user_message)):
        state["customer_constraints"]["last_area_hint"] = area_match.group(2).strip()

    state["brand"] = brand.upper()
    state["schema_version"] = 3
    state["current_intent"] = intent
    state["current_goal"] = lead_stage or state.get("current_goal", "")
    if intent in RETURN_CONTEXT_INTENTS:
        state["active_flow"] = {"name": "return_request", "stage": intent}
    else:
        # Không để ngữ cảnh đổi trả bám vô hạn rồi bắt nhầm các câu ngắn ở chủ đề mới.
        state["active_flow"] = {"name": "", "stage": ""}
    state["conversation_topic"] = active_category or state.get("conversation_topic", "")
    if active_category:
        topics = [topic for topic in (state.get("topic_stack") or []) if topic != active_category]
        state["topic_stack"] = (topics + [active_category])[-5:]

    normalized_user = _normalize_vn(user_message)
    correction_match = re.search(r"\b(khong phai|nham|y minh la|y toi la)\b.{0,80}", normalized_user)
    if correction_match:
        corrections = state.get("corrections") or []
        corrections.append({"text": user_message[:240], "timestamp": now_str})
        state["corrections"] = corrections[-5:]

    normalized_reply = _normalize_vn(bot_reply)
    asks_for_link_confirmation = bool(re.search(
        r"(muon minh gui link|gui link shopee|gui link mua|gui link san pham|gui link khong)",
        normalized_reply,
    ))
    if asks_for_link_confirmation:
        state["pending_action"] = {
            "name": "send_product_link",
            "status": "waiting_confirmation",
            "product": state.get("active_entities", {}).get("product", ""),
            "created_at": now_str,
        }
        state["pending_question"] = "confirm_send_product_link"
    elif intent in {"shopee_product_link", "shopee_specific_product"}:
        state["pending_action"] = {"name": "", "status": ""}
        state["pending_question"] = ""

    cfc_goal = ""
    if brand.lower() == "cfc":
        confirmed_slots = dict(state.get("confirmed_slots") or {})
        if intent not in {"privacy_sensitive_lookup", "company_overview", "unknown", "empty_input"}:
            confirmed_slots.update(_extract_cfc_confirmed_slots(user_message, query_entities))
        patch_slots = (state_patch or {}).get("confirmed_slots") if isinstance(state_patch, dict) else {}
        if isinstance(patch_slots, dict):
            confirmed_slots.update({key: value for key, value in patch_slots.items() if value not in (None, "")})
        if confirmed_slots.get("area"):
            sanitized_area = _sanitize_stored_area(str(confirmed_slots["area"]))
            if sanitized_area:
                confirmed_slots["area"] = sanitized_area
            else:
                confirmed_slots.pop("area", None)
        state["confirmed_slots"] = confirmed_slots

        cfc_goal = CFC_GOAL_BY_INTENT.get(intent) or _active_goal_name(state)
        if cfc_goal:
            missing_slots = _cfc_missing_slots(cfc_goal, confirmed_slots)
            capability_boundary = intent.endswith(("_unavailable", "_unverified"))
            if capability_boundary:
                goal_stage = "waiting_human_check" if not missing_slots else "collecting_slots"
            else:
                goal_stage = "ready_for_handoff" if not missing_slots else "collecting_slots"
            state["active_goal"] = {
                "name": cfc_goal,
                "stage": goal_stage,
                "updated_at": now_str,
            }
            state["current_goal"] = cfc_goal
            state["pending_slots"] = missing_slots
            state["pending_request"] = {
                "goal": cfc_goal,
                "intent": intent,
                "status": goal_stage,
                "missing_slots": missing_slots,
                "updated_at": now_str,
            }
            if capability_boundary:
                state["last_capability_boundary"] = {
                    "intent": intent,
                    "goal": cfc_goal,
                    "reason": "operational_or_expert_data_not_connected",
                    "timestamp": now_str,
                }

    if not cfc_goal:
        if lead_stage == "collecting_contact":
            state["pending_slots"] = ["phone", "area"]
        elif lead_stage == "lead_ready":
            state["pending_slots"] = []
    if lead_stage == "escalated":
        takeover = state.get("takeover_state") or {}
        if takeover.get("status") != "pending":
            state["takeover_state"] = {
                "status": "pending",
                "owner": "",
                "reason": intent,
                "requested_at": now_str,
            }
    state["last_source_id"] = source_id or state.get("last_source_id", "")
    state["updated_at"] = now_str
    state["conversation_summary"] = (
        f"Intent gần nhất: {intent}; "
        f"sản phẩm/ngữ cảnh: {state.get('active_entities', {}).get('product') or 'chưa rõ'}; "
        f"lead_stage: {lead_stage}; "
        f"active_goal: {_active_goal_name(state) or 'chưa có'}; "
        f"confirmed_slots: {', '.join(sorted((state.get('confirmed_slots') or {}).keys())) or 'chưa có'}."
    )

    recent_turns = state.get("recent_turns") or []
    recent_turns.append({
        "user": user_message,
        "bot": bot_reply[:600],
        "intent": intent,
        "timestamp": now_str,
    })
    state["recent_turns"] = recent_turns[-6:]
    return state


def _format_inline_numbered_list(answer: str) -> str:
    """Chuyển danh sách đánh số viết liền trong Sheet thành từng dòng dễ đọc."""
    matches = list(re.finditer(r"(?<!\d)(\d{1,2})\.\s+", answer))
    if len(matches) < 2 or "\n" in answer:
        return answer

    prefix = answer[:matches[0].start()].strip()
    items = []
    tail = ""

    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(answer)
        item = answer[match.end():end].strip(" ;")

        if idx == len(matches) - 1:
            tail_match = re.search(
                r"(?<!\d)\.\s+(Bạn|Anh/chị|Anh chị|Nếu|Mình|Dạ bạn)\b",
                item,
            )
            if tail_match:
                tail = item[tail_match.start() + 2:].strip()
                item = item[:tail_match.start() + 1].strip()

        items.append(f"{match.group(1)}. {item}")

    parts = []
    if prefix:
        parts.append(prefix)
    parts.append("\n".join(items))
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


def _prettify_answer(answer: str) -> str:
    """Chuẩn hóa output Messenger: gọn khoảng trắng, xuống dòng danh sách rõ ràng, lọc bỏ icon xấu."""
    text = str(answer or "").strip()
    if not text:
        return text
    # Lọc bỏ emoji phản cảm / sến súa theo yêu cầu
    text = re.sub(r"[🔥💥💣⚡😈💯]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _format_inline_numbered_list(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _has_product_view_action(norm_text: str) -> bool:
    if re.search(
        r"(muon xem|xem ve|xem dong|cho.*xem|tim hieu|hoi ve|thong tin ve|tu van|can xem|gui.*thong tin|co.*gi|co.*loai nao|co.*cai nao|co.*dong nao|co.*dong phan|gom nhung gi|dong san pham|san pham nao|san pham gi|can mua|muon mua|mua cho|dung cho|quan an|nha hang|bep an|can lon|can to|\bco\b.*\b(khong|hong|ko|k)\b)",
        norm_text,
    ):
        return True

    # Nhận diện khách nhắn tên danh mục độc lập dạng ngắn (<= 4 từ)
    tokens = norm_text.split()
    if len(tokens) <= 4:
        category_terms = [
            "nuoc giat", "bot giat", "giat giu", "giat xa", "do giat",
            "nuoc rua chen", "rua chen", "rua bat", "nuoc rua bat",
            "nuoc lau san", "lau san", "lau nha", "nuoc lau nha",
            "tay toilet", "toilet", "bon cau", "tay rua", "javen", "tay mau",
            "npk", "huu co", "sinh hoc", "phan bon", "phan co bay"
        ]
        if any(term in norm_text for term in category_terms):
            return True

    return False


def _has_price_signal(norm_text: str) -> bool:
    return bool(
        re.search(r"(^|\s)(gia|bao gia|xin gia|bang gia|gia ban|gia ca|tim hieu gia|hoi gia|gia phan|gia npk)(\s|$)", norm_text)
        or re.search(r"(bao nhieu tien|nhieu tien|bao nhieu)$", norm_text)
        or re.search(r"(gia .{1,80} bao nhieu|bao nhieu tien|tim hieu gia)", norm_text)
    )


def _detect_product_group_intent(norm_text: str, brand: str) -> Optional[str]:
    """Nhận diện câu hỏi xem/tìm hiểu nhóm sản phẩm bằng tiếng Việt tự nhiên."""
    if _has_price_signal(norm_text):
        return None
    view_action = _has_product_view_action(norm_text)
    if not view_action:
        return None

    if brand.lower() == "cfc":
        if re.search(r"(npk)\b", norm_text):
            return "cfc_npk_product_info"
        if re.search(r"(huu co|sinh hoc)\b", norm_text):
            return "cfc_organic_fertilizer_info"
        if re.search(r"(phan bon|phan co bay|cac loai phan|dong phan|dong phan nao|san pham cfc|san pham co bay)", norm_text):
            return "product_lines"
        return None

    zeo_groups = [
        ("zeo_dishwashing_product_overview", r"(nuoc rua chen|nuoc rua bat|rua chen|rua bat|zif)"),
        ("zeo_laundry_product_overview", r"(giat giu|nuoc giat|bot giat|giat quan ao|do giat|giat xa)"),
        ("zeo_floor_cleaner_product_overview", r"(nuoc lau san|lau san|nuoc lau nha|lau nha|tay san|san nha|tay san nha|lau san nha)"),
        ("zeo_toilet_cleaner", r"(tay toilet|toilet|bon cau|nuoc tay bon cau)"),
        ("zeo_cleaning_hygiene_product_overview", r"(tay rua ve sinh|tay rua|ve sinh|javen|lau kinh|xit tay|tay mau|nha tam)"),
        ("pano_product_type", r"\bpano\b"),
        ("zeo_product_catalog_overview", r"(san pham|mat hang|dong san pham|nhom san pham|zeo co gi|shop co gi)"),
    ]
    for intent, pattern in zeo_groups:
        if re.search(pattern, norm_text):
            return intent
    return None


def _has_any(norm_text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, norm_text) for pattern in patterns)


def _is_internal_content_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"\b(noi dung|bai dang|kich ban|script|mau|caption|video|reels|quang cao)\b",
        r"(viet.*(bai|noi dung|caption|kich ban))",
    ])


def _detect_company_overview_intent(norm_text: str, brand: str) -> Optional[str]:
    # Nếu đang hỏi về danh mục các dòng sản phẩm -> Nhường cho Catalog Overview
    if re.search(r"(dong san pham|cac san pham|danh muc san pham|co san pham gi|nhung san pham)", norm_text):
        return None
    if _has_any(norm_text, [
        r"(gioi thieu|so luoc|thong tin).{0,30}(cong ty|cty|thuong hieu|zeo|pano|oplus|cfc|co bay)",
        r"(cong ty|cty|thuong hieu).{0,30}(la gi|lam gi|thuoc|cua ai|san xuat|thanh lap|bao nhieu nam)",
        r"(zeo|pano|oplus|cfc|co bay).{0,30}(thuoc cong ty|la cua cong ty|la thuong hieu gi)",
        r"(cfc homecare|homecare).{0,40}(cong ty|cty|thuoc|cua)",
    ]):
        return "company_overview"
    return None


def _detect_address_intent(norm_text: str, brand: str) -> Optional[str]:
    if _has_any(norm_text, [
        r"(dia chi|nha may|tru so|van phong).{0,40}(o dau|tai dau|cho nao|cong ty|cty|shop)?",
        r"(cong ty|cty|shop|nha may|tru so).{0,25}(o dau|tai dau|nam o dau|dia chi)",
        r"\b(cty|cong ty|shop) o dau\b",
        r"^dia chi o dau$",
        r"^dia chi cong ty o dau$",
        r"^dia chi cong ty\b",
    ]):
        return "company_address" if brand.lower() == "zeo" else "address"
    return None


def _has_company_contact_signal(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"^(so dien thoai|dien thoai|hotline|tong dai|lien he|sdt|sdt cong tu|sdt cong ty)$",
        r"(so dien thoai|hotline|tong dai|so lien he|lien he).{0,30}(cong ty|cong tu|cty|shop|admin|ben minh)?",
        r"(cong ty|cong tu|cty|shop|admin|ben minh).{0,30}(so dien thoai|hotline|tong dai|so lien he|lien he)",
        r"\b(cho|xin)\s+so\s+(cham soc|ho tro)(\s+khach hang)?\b",
        r"\bso\s+(cham soc|ho tro)(\s+khach hang)?\b",
        r"\b(cho|xin)?\s*so\s+(cua\s+)?(cong ty|cty|zeo|pano|oplus|cfc|co bay|shop|ben minh)\b",
        r"\b(cham soc khach hang|bo phan ho tro).{0,25}(so nao|so may|so dien thoai|hotline|lien he)\b",
        r"\bcall\b",
    ])


def _detect_contact_intent(norm_text: str, brand: str) -> Optional[str]:
    if _has_company_contact_signal(norm_text):
        return "company_contact_information" if brand.lower() == "zeo" else "cfc_company_website"
    return None


def _detect_official_channel_request(norm_text: str) -> Optional[str]:
    if "shopee" in norm_text or "sopi" in norm_text or "shoppe" in norm_text:
        return None
    has_channel = re.search(r"(tiktok|tik tok|lazada|zalo|facebook|fb)", norm_text)
    if has_channel and (len(norm_text.split()) <= 4 or re.search(r"(zeo|pano|oplus|cfc|co bay|cong ty|cty)", norm_text)):
        return "official_channel_unverified"
    if _has_any(norm_text, [
        r"^(tiktok|tik tok|lazada|zalo|facebook|fb)$",
        r"(link|kenh|trang|shop|official|chinh thuc).{0,30}(tiktok|tik tok|lazada|zalo|facebook|fb)",
        r"(tiktok|tik tok|lazada|zalo|facebook|fb).{0,30}(link|kenh|trang|shop|official|chinh thuc|co khong|ko)",
    ]):
        return "official_channel_unverified"
    return None


def _detect_customer_correction(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"(sai|khong dung|chua dung|nham).{0,40}(dia chi|thong tin|tra loi|noi dung|so dien thoai|hotline)",
        r"(de toi|toi|minh).{0,20}(chinh|sua|cap nhat).{0,30}(lai|cho)",
        r"(dia chi|so dien thoai|hotline).{0,30}(moi|dung|phai la)",
    ])


def _detect_language_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"(noi|tra loi|tu van).{0,20}(tieng trung|tieng anh|english|chinese|mandarin)",
        r"^(tieng trung|tieng anh|english|chinese)$",
    ])


def _detect_out_of_scope_general_question(norm_text: str) -> bool:
    """Chặn câu hỏi đời sống/chung chung để RAG không kéo nhầm sang FAQ sản phẩm."""
    if _has_any(norm_text, [
        r"^soan$",
        r"^viet cho (zeo|zeo vietnam|zeo viet nam|cfc|co bay|cfc co bay)$",
        r"^(viet|soan) (tin|bai|noi dung)$",
    ]):
        return True

    in_scope_words = [
        "shop", "admin", "zeo", "pano", "oplus", "cfc", "co bay", "san pham", "phan bon",
        "nuoc giat", "bot giat", "nuoc rua chen", "lau san", "javen", "toilet", "npk",
        "gia", "ship", "giao hang", "mua", "dat hang", "hotline", "dia chi", "mo cua", "lam viec",
    ]
    if any(word in norm_text for word in in_scope_words):
        return False

    return _has_any(norm_text, [
        r"^(hom nay )?(thu may|ngay may)$",
        r"\bhom nay\s+(la\s+)?(thu may|ngay may|ngay gi)\b",
        r"\b(bay gio|gio nay|luc nay)\s+(la\s+)?(may gio|gio nao)\b",
        r"^(may gio roi|ngay may roi|thu may roi)$",
        r"\b(thoi tiet|mua khong|nang khong|nhiet do)\b",
        r"\b(tin tuc|bong da|xo so|ket qua xo so|lich am|ngay le)\b",
        r"\b(ke chuyen|hat bai|lam tho|giai toan|dich cau nay)\b",
    ])


def _detect_out_of_scope_personal_question(norm_text: str) -> bool:
    """Bắt các câu hỏi cá nhân, nhân sự nội bộ, hỏi về sếp, anh Thuận, ai làm ra bot."""
    return _has_any(norm_text, [
        r"\b(co biet|biet khong|biet ko|la ai|la nguoi nao)\b.{0,30}\b(anh|chi|em|ong|ba|ban|sep|chu tich|giam doc|thuan|tuan|nguyen|nam|duc)\b",
        r"\b(anh|chi|ong|ba)\s+(thuan|tuan|dung|hoa|nam|hung|duc|hien)\s+(la ai|la nguoi nao|o dau|lam gi)\b",
        r"\b(ai tao ra|ai lam ra|ai viet ra|ai sinh ra|ai lap trinh)\s+(bot|ban|em|chatbot|may)\b",
        r"\b(ten gi|bao nhieu tuoi|que o dau|co nguoi yeu chua|doc than khong|yeu ai|cuoi chua)\b",
    ])


def _detect_purchase_signal(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"\b(muon mua|can mua|dat hang|chot don|lay hang|lay \d+|mua \d+|cho minh \d+|cho toi \d+)\b",
        r"\b(mua|dat|lay)\s+(oplus|pano|zeo|zif|javen|nuoc giat|bot giat|nuoc rua chen|nuoc tay|lau san|toilet)\b",
    ])


def _detect_contextual_dosage_followup(norm_text: str, previous_intent: str) -> bool:
    if previous_intent not in {"zeo_usage_safety_review", "cfc_dosage_usage_review"}:
        return False
    return _has_any(norm_text, [
        r"^(vay|the|neu vay).{0,20}\d+\s*(bo|kg|lit|ml|cong|bao)",
        r"^\d+\s*(bo|kg|lit|ml|cong|bao).{0,30}(sao|duoc khong|duoc ko|thi sao)?$",
    ])


def _detect_new_product_request(norm_text: str) -> bool:
    return _has_any(norm_text, [
        r"(san pham|hang|dong).{0,20}(moi ra mat|moi nhat|moi ve|moi co)",
        r"(moi ra mat|moi nhat).{0,20}(san pham|hang|dong)",
    ])


def _detect_competitor_product(norm_text: str, brand: str) -> bool:
    if brand.lower() != "zeo":
        return False
    return any(re.search(pattern, norm_text) for pattern in ZEO_COMPETITOR_PRODUCT_PATTERNS)


def _detect_cfc_cross_brand(norm_text: str, brand: str) -> bool:
    if brand.lower() != "cfc":
        return False
    return _has_any(norm_text, [
        r"(nuoc giat|bot giat|nuoc rua chen|rua chen|lau san|nuoc lau san|tay toilet|javen|xit tay|nuoc tay|pano|oplus|zeo)",
    ])


def _detect_proof_or_certification_intent(norm_text: str, brand: str, previous_intent: str = "") -> Optional[str]:
    if brand.lower() != "zeo":
        return None
    asks_proof = _has_any(norm_text, [
        r"(giay to|chung minh|chung nhan|kiem dinh|kiem nghiem|bang chung|co so nao|tai lieu).{0,40}(cong nghe|diet khuan|san pham|bot giat|zeo|do)",
        r"(pasteur|singapore|chung nhan|kiem dinh|kiem nghiem)",
    ])
    follows_tech = previous_intent in {"zeo_detergent_technology", "zeo_detergent_certification"}
    if asks_proof or ("cong nghe do" in norm_text and follows_tech):
        return "zeo_detergent_certification"
    return None


def _detect_usage_safety_gap(norm_text: str, brand: str) -> bool:
    if brand.lower() == "cfc":
        # Loại trừ câu hỏi tư vấn chọn loại phân (bón phân gì, dùng phân nào, bón gì)
        if re.search(r"\b(bon phan gi|dung phan gi|xai phan gi|phan nao|loai nao|tu van phan|bon gi|nen bon phan gi|bon loai nao)\b", norm_text):
            return False
        return _has_any(norm_text, [
            r"(lieu luong|bao nhieu kg|may kg|pha bao nhieu|tron bao nhieu|cach bon|bon nhu the nao|1 cong bon|mot cong bon|thuoc sau|tri benh|dao on)",
        ])
    return _has_any(norm_text, [
        r"(lieu luong|dung bao nhieu|bao nhieu ml|bao nhieu kg|may bo do|\d+\s*(bo|kg|lit|ml).{0,20}(bo do|bo|quan ao)|bao nhieu nuoc)",
        r"(uong duoc|vao mat|dinh vao mat|nuot phai|an phai)",
    ])


def _detect_specific_product_intent(norm_text: str, brand: str) -> Optional[str]:
    if _has_price_signal(norm_text):
        return None
    if brand.lower() == "cfc":
        if re.search(r"\bnpk\b", norm_text):
            return "cfc_npk_product_info"
        if re.search(r"(huu co|sinh hoc)", norm_text):
            return "cfc_organic_fertilizer_info"
        return None

    # 1. Enzyme / Công nghệ Thụy Điển
    if _has_any(norm_text, [r"\benzyme\b", r"thuy dien"]):
        return "zeo_detergent_technology"

    # 2. Tẩy Toilet (Ưu tiên trước general detergent)
    if _has_any(norm_text, [r"tay toilet", r"bon cau", r"nuoc tay bon cau", r"tay bon cau"]):
        return "zeo_toilet_cleaner"

    # 3. ZIF Rửa chén
    if re.search(r"\bzif\b", norm_text):
        return "zeo_zif_dishwashing_liquid"

    # 3.5. VEILEX Khử mùi
    if "veilex" in norm_text:
        return "pano_veilex_odor_control"

    # 4. PANO
    if "pano" in norm_text:
        if re.search(r"(rua chen|rua bat)", norm_text):
            return "pano_dishwashing_lemon_and_vitamin_e"
        if re.search(r"(mui|huong|mau|do xanh hong cam tim)", norm_text):
            return "pano_laundry_fragrance_options"
        if re.search(r"(veilex|khu mui)", norm_text):
            return "pano_veilex_odor_control"
        if re.search(r"(quy cach|tui|can|dong goi)", norm_text):
            return "pano_laundry_packaging_and_segment"
        if re.search(r"(nuoc giat|bot giat|giat)", norm_text):
            return "pano_product_type"
        return "pano_product_type"

    # 5. Oplus
    if "oplus" in norm_text:
        if re.search(r"(rua chen|rua bat)", norm_text):
            return "oplus_dishwashing_liquid"
        if re.search(r"(cong nghe|ion|trang sang)", norm_text):
            return "oplus_detergent_ion_technology"
        if re.search(r"(nuoc xa|xa vai)", norm_text):
            return "oplus_fabric_softener_unverified"
        if re.search(r"(bot giat|giat)", norm_text):
            return "oplus_detergent_features"

    # 6. Lau sàn / Tẩy sàn nhà
    if re.search(r"(lau san|nuoc lau nha|lau nha|tay san|tay san nha|san nha|lau san nha)", norm_text):
        return "zeo_floor_cleaner_product_overview"

    # 7. ZeO Bột giặt
    if re.search(r"(bot giat|nuoc giat).{0,20}\bzeo\b|\bzeo\b.{0,20}(bot giat|nuoc giat)", norm_text):
        if re.search(r"(chung nhan|pasteur|kiem dinh|kiem nghiem|giay to|chung minh)", norm_text):
            return "zeo_detergent_certification"
        if re.search(r"(mui|huong|thom)", norm_text):
            return "zeo_detergent_fragrance"
        return "zeo_detergent_technology"

    # 8. Tẩy màu / Javen
    if re.search(r"(tay mau|tay quan ao mau|ao trang bi o vang)", norm_text):
        return "zeo_color_bleach"
    if re.search(r"(javen|nuoc tay|thuoc tay|tay trang)", norm_text):
        return "zeo_javen_bleach"

    return None


class ChatPipelineRequest(BaseModel):
    brand: str = "zeo"                  # "zeo" hoặc "cfc"
    sender_id: str                      # Messenger PSID
    text: str                           # Tin nhắn của khách
    fb_name: Optional[str] = ""         # Tên hiển thị Facebook
    message_id: Optional[str] = ""      # Message ID từ Facebook webhook
    input_kind: Optional[str] = "text"  # text, location, attachment hoặc empty
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    attachment_type: Optional[str] = ""


class ChatPipelineResponse(BaseModel):
    ok: bool = True
    answer: str
    intent: str
    confidence: str
    score: float
    brand: str
    has_phone: bool = False
    phone: str = ""
    area: str = ""
    lead_stage: str = "new"
    shopee_url: Optional[str] = None
    fallback_reason: Optional[str] = ""
    latency_ms: float = 0.0
    duplicate: bool = False
    idempotency_status: str = ""
    message_id: str = ""
    suppress_send: bool = False


async def _sheet_fast_response(
    brand: str,
    start_time: float,
    intent: str,
    *,
    lead_stage: str = "new",
    unavailable_intent: Optional[str] = None,
    unavailable_answer: Optional[str] = None,
) -> ChatPipelineResponse:
    item = await get_faq_by_intent(brand, intent)
    answer = item.get("answer", "").strip()
    if answer:
        return _fast_response(answer, intent, brand, start_time, lead_stage=lead_stage)

    fallback = unavailable_answer or (
        "Dạ hiện mục thông tin này chưa tải được từ hệ thống kiến thức. "
        "Admin sẽ kiểm tra lại dữ liệu và phản hồi bạn chính xác hơn nha."
    )
    return _fast_response(fallback, unavailable_intent or f"{intent}_unavailable", brand, start_time, lead_stage=lead_stage)


async def _detect_and_process_multi_intent(
    raw_text: str,
    norm_text: str,
    brand: str,
    conversation_state: dict,
    start_time: float,
    phone: str = "",
    area: str = "",
    has_phone: bool = False,
    lead_stage: str = "new",
) -> Optional[ChatPipelineResponse]:
    """Phát hiện và xử lý câu hỏi ghép 2 ý định (Multi-Intent Compound Query)."""
    # CFC chạy grounded-only. Không tách câu rồi dùng LLM ghép các FAQ độc lập vì
    # cách này từng tạo câu trả lời địa chỉ/giao hàng không liên quan đến yêu cầu chính.
    if brand.lower() == "cfc":
        return None
    # Các câu mô tả triệu chứng/hiệu quả thường dùng dấu phẩy để nối một ý
    # ("ăn da tay, tay bị bong tróc"; "dùng ổn, tẩy được vết máu").
    # Để matcher chuyên biệt xử lý toàn câu thay vì tách nhầm thành 2 intent.
    if is_stain_removal_or_efficacy_inquiry(raw_text) or is_skin_care_dishwashing_inquiry(raw_text):
        return None

    clauses: list[tuple[str, str]] = []

    # 1. Thử tách theo dấu phẩy / chấm hỏi / chấm phẩy nếu có 2 vế câu độc lập
    if any(sep in raw_text for sep in [",", "?", ";"]):
        raw_parts = [p.strip() for p in re.split(r"[,?;]\s*", raw_text) if len(p.strip()) >= 4]
        if len(raw_parts) >= 2:
            clauses = [(_normalize_vn(p), p) for p in raw_parts if len(_normalize_vn(p)) >= 4]

    # 2. Nếu chưa tách được, thử tách theo liên từ
    if not clauses or len(clauses) < 2:
        # "còn không/còn hàng/còn nữa" là availability/follow-up, không phải liên từ tách câu.
        allows_con_split = not re.search(r"\bcon\s+(?:hang|khong|hong|ko|nua)\b", norm_text)
        conjunctions = r"va|voi lai|kem theo|tien the|cho minh hoi them|dong thoi|kèm|tien the cho hoi|dong thoi cho hoi"
        if allows_con_split:
            conjunctions = rf"{conjunctions}|con"
        conj_match = re.search(rf"\b({conjunctions})\b", norm_text)
        if conj_match:
            conj = conj_match.group(0)
            parts_norm = norm_text.split(conj, 1)
            raw_splits = re.split(rf"(?i)\b{conj}\b", raw_text, maxsplit=1)
            if len(parts_norm) >= 2 and len(parts_norm[0].strip()) >= 4 and len(parts_norm[1].strip()) >= 4:
                clauses = [
                    (parts_norm[0].strip(), raw_splits[0].strip() if len(raw_splits) > 0 else parts_norm[0].strip()),
                    (parts_norm[1].strip(), raw_splits[1].strip() if len(raw_splits) > 1 else parts_norm[1].strip()),
                ]

    if not clauses or len(clauses) < 2:
        return None

    facts = []
    intents = []
    shopee_url = None

    for p_norm, p_raw in clauses[:2]:
        # 1. Thử hỏi theo ngân sách / tầm giá (vd: dưới 200k, dưới 100k)
        if is_budget_inquiry(p_raw):
            b_res = match_products_by_budget(p_raw, brand=brand)
            if b_res:
                facts.append(b_res["suggested_reply"])
                intents.append("shopee_budget_filter")
                if b_res.get("shopee_url"):
                    shopee_url = b_res.get("shopee_url")
                continue

        # 2. Thử hỏi về hiệu quả làm sạch / tẩy vết bẩn
        if is_stain_removal_or_efficacy_inquiry(p_raw):
            stain_res = match_stain_removal_or_efficacy(p_raw, brand=brand, context=conversation_state)
            if stain_res:
                facts.append(stain_res["suggested_reply"])
                intents.append("laundry_stain_removal_guide")
                continue

        # 3. Thử hỏi về giao hàng / ship về tỉnh / khu vực
        has_ship_verb = bool(re.search(r"\b(ship|giao hang|giao ve|ship ve|co ship|co giao|freeship|cuoc|phi ship|may ngay|giao duoc|giao toi|van chuyen)\b", p_norm))
        if has_ship_verb:
            loc_found = ""
            for loc in ["rach gia", "kien giang", "can tho", "binh duong", "dong nai", "long an", "ha noi", "da nang", "tphcm", "ho chi minh", "sai gon", "vung tau", "tien giang", "an giang", "ca mau"]:
                if re.search(rf"\b{loc}\b", p_norm):
                    loc_found = loc.title()
                    break
            loc_str = f" về tận {loc_found}" if loc_found else " toàn quốc"
            brand_title = "ZeO Vietnam" if brand.lower() == "zeo" else "CFC Cò Bay"
            ship_fact = (
                f"Dạ {brand_title} có giao hàng{loc_str} qua các đối tác vận chuyển (GHTK, J&T) và hỗ trợ mã Freeship Extra trên gian hàng chính hãng Shopee Mall bạn nhé!"
            )
            facts.append(ship_fact)
            intents.append("shipping_time_and_fee")
            continue

        # 4. Thử hỏi hotline / liên hệ
        if any(k in p_norm for k in ["hotline", "so dien thoai", "sdt", "tong dai"]):
            facts.append("Hotline hỗ trợ CSKH và tư vấn đặt hàng chính thức: 1900 5307 (ZeO) / 0292 3841 818 (CFC Cò Bay).")
            intents.append("company_contact_information")
            continue

        # 5. Thử Shopee matcher (giá cụ thể, sản phẩm)
        p_shopee = match_shopee_product(p_raw, brand=brand)
        if p_shopee and p_shopee.get("matched"):
            facts.append(p_shopee.get("suggested_reply", ""))
            intents.append(p_shopee.get("intent", "shopee_product_link"))
            if p_shopee.get("shopee_url"):
                shopee_url = p_shopee.get("shopee_url")
            continue

        # 6. Thử RAG search
        rag_res = await semantic_search(query=p_norm, brand=brand, top_k=2)
        if rag_res.get("score", 0) >= 0.50 and rag_res.get("answer"):
            facts.append(rag_res["answer"])
            intents.append(rag_res["intent"])

    if len(facts) >= 2 and intents[0] != intents[1]:
        combined_facts_str = f"Ý 1 ({intents[0]}):\n{facts[0]}\n\nÝ 2 ({intents[1]}):\n{facts[1]}"

        synthesized = await synthesize_cskh_answer(
            user_query=raw_text,
            brand=brand,
            retrieved_facts=combined_facts_str,
            conversation_summary=conversation_state.get("conversation_summary", ""),
            chat_history=_sanitized_chat_history(conversation_state),
            timeout=2.5,
        )

        if synthesized and len(synthesized) >= 30:
            final_ans = synthesized
        else:
            if "shipping" in intents[1]:
                final_ans = f"{facts[1]}\n\n{facts[0]}"
            else:
                final_ans = f"{facts[0]}\n\n{facts[1]}"

        compound_intent = f"multi_{intents[0]}_{intents[1]}"

        return ChatPipelineResponse(
            answer=_prettify_answer(final_ans),
            intent=compound_intent,
            confidence="high",
            score=0.96,
            brand=brand.upper(),
            has_phone=has_phone,
            phone=phone,
            area=area,
            lead_stage=lead_stage,
            shopee_url=shopee_url,
            latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
        )

    return None


async def _process_chat_pipeline_once(req: ChatPipelineRequest) -> ChatPipelineResponse:
    start_time = time.perf_counter()
    brand = req.brand.lower()
    has_location = bool(
        req.latitude is not None
        and req.longitude is not None
        and -90 <= req.latitude <= 90
        and -180 <= req.longitude <= 180
    )
    raw_text = (req.text or "").strip() or ("Gửi vị trí hiện tại" if has_location else "")
    sender_id = req.sender_id.strip()
    fb_name = (req.fb_name or "").strip()

    if not raw_text:
        return ChatPipelineResponse(
            answer=_prettify_answer("Dạ bạn cần bên mình hỗ trợ thông tin gì ạ?"),
            intent="empty_input",
            confidence="high",
            score=1.0,
            brand=brand.upper(),
            latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
        )

    # Khóa theo sender_id để xử lý tuần tự (Tránh race condition ghi đè session)
    lock_key = f"{brand}:{sender_id}"
    async with _local_sender_lock(lock_key):
        norm_text = _normalize_vn(raw_text)
        incoming_phone, incoming_area = _extract_phone_and_area(raw_text, norm_text)
        phone, area = incoming_phone, incoming_area
        has_phone = bool(incoming_phone)

        # Đọc profile & session cũ từ Redis
        r = await get_redis()
        customer_key = f"{brand}:customer:messenger:{sender_id}"
        session_key = f"{brand}:session:messenger:{sender_id}"

        existing_profile = _local_customer_cache.get(customer_key) or {}
        existing_session = _local_session_cache.get(session_key) or {}

        if not existing_profile or not existing_session:
            try:
                raw_p, raw_s = await asyncio.gather(
                    r.get(customer_key),
                    r.get(session_key),
                    return_exceptions=True,
                )
                if not existing_profile and isinstance(raw_p, (str, bytes)) and raw_p:
                    existing_profile = json.loads(raw_p)
                if not existing_session and isinstance(raw_s, (str, bytes)) and raw_s:
                    existing_session = json.loads(raw_s)
            except Exception as e:
                logger.warning("Redis read error in pipeline: %s", e)

        stored_phone = (
            existing_profile.get("phone")
            or existing_profile.get("customer_phone")
            or existing_session.get("customer_phone")
            or existing_session.get("phone")
            or ""
        )
        stored_area = _sanitize_stored_area(str(
            existing_profile.get("area")
            or existing_profile.get("customer_location")
            or existing_session.get("customer_location")
            or existing_session.get("area")
            or ""
        ))
        if not phone and stored_phone:
            phone = stored_phone
        if not area and stored_area:
            area = stored_area

        lead_stage = existing_profile.get("lead_stage", "new")
        previous_intent = existing_session.get("last_intent", "")
        conversation_state = _load_conversation_state(existing_session, brand)
        query_entities = _extract_query_entities(norm_text, brand)
        reference_resolution = _resolve_reference(raw_text, norm_text, conversation_state)
        if reference_resolution.get("resolved") and not query_entities.get("product"):
            query_entities = {
                "product": reference_resolution.get("product", ""),
                "product_intent": reference_resolution.get("product_intent", ""),
                "category": reference_resolution.get("category", ""),
                "matched_entities": [{
                    "product": reference_resolution.get("product", ""),
                    "product_intent": reference_resolution.get("product_intent", ""),
                    "category": reference_resolution.get("category", ""),
                }],
            }

        query_plan = build_query_plan(
            raw_text=raw_text,
            norm_text=norm_text,
            brand=brand,
            query_entities=query_entities,
            reference_resolution=reference_resolution,
            conversation_state=conversation_state,
        )
        query_plan_dict = query_plan.to_dict()
        loc_constraints = query_plan.constraints or {}
        explicit_loc = (
            incoming_area
            or loc_constraints.get("location")
            or loc_constraints.get("district")
            or loc_constraints.get("ward")
            or ""
        )
        if explicit_loc:
            area = explicit_loc
        elif not area and stored_area:
            area = stored_area

        route_decision = build_route_decision(query_plan, conversation_state)
        pipeline_trace_extra: dict[str, Any] = {"dialogue_router": route_decision.to_dict()}
        state_patch: dict[str, Any] = {"confirmed_slots": {}}
        if phone:
            state_patch["confirmed_slots"]["phone"] = phone
        if area:
            state_patch["confirmed_slots"]["area"] = area
        if has_location:
            state_patch["confirmed_slots"]["location"] = {
                "latitude": req.latitude,
                "longitude": req.longitude,
                "source": "messenger_location",
            }

        def _remember_response(
            answer: str,
            intent: str,
            stage: str,
            *,
            confidence: str = "high",
            score: float = 1.0,
            source_id: str = "",
            fallback_reason: str = "",
            trace_extra: Optional[dict[str, Any]] = None,
            products_shown: Optional[list[dict[str, Any]]] = None,
        ) -> None:
            if not source_id and products_shown:
                first_product = next((item for item in products_shown if isinstance(item, dict)), {})
                product_id = first_product.get("item_id") or first_product.get("product_id") or first_product.get("id")
                if product_id:
                    source_id = f"{brand}:shopee_catalog:{product_id}"
            next_state = _build_next_conversation_state(
                conversation_state,
                brand=brand,
                user_message=raw_text,
                bot_reply=answer,
                intent=intent,
                lead_stage=stage,
                query_entities=query_entities,
                reference_resolution=reference_resolution,
                source_id=source_id,
                products_shown=products_shown,
                state_patch=state_patch,
            )
            trace = {
                "normalized_text": norm_text,
                "resolved_query": reference_resolution.get("resolved_query", raw_text),
                "reference": {
                    "used": bool(reference_resolution.get("references_previous_turn")),
                    "resolved": bool(reference_resolution.get("resolved")),
                    "reason": reference_resolution.get("reason", ""),
                    "product": reference_resolution.get("product", ""),
                },
                "query_entities": query_entities,
                "query_plan": query_plan_dict,
                "source_id": source_id,
                "confidence": confidence,
                "score": score,
                "fallback_reason": fallback_reason,
                "grounding": assess_grounding(
                    intent=intent,
                    source_id=source_id,
                    fallback_reason=fallback_reason,
                ).to_dict(),
            }
            if pipeline_trace_extra:
                trace.update(pipeline_trace_extra)
            if trace_extra:
                trace.update(trace_extra)
            # Cập nhật ngay RAM cache để turn kế tiếp đọc tức thì (0ms)
            _local_session_cache[session_key] = {
                "revision": int(existing_session.get("revision") or 0),
                "last_user_message": raw_text,
                "last_bot_reply": _prettify_answer(answer),
                "last_intent": intent,
                "lead_stage": stage,
                "customer_phone": phone,
                "customer_location": area,
                "conversation_state": next_state,
                "last_trace": trace,
            }

        async def _sheet_response_remember(
            intent: str,
            *,
            stage: str = "new",
            unavailable_intent: Optional[str] = None,
            unavailable_answer: Optional[str] = None,
        ) -> ChatPipelineResponse:
            item = await get_faq_by_intent(brand, intent)
            answer = item.get("answer", "").strip()
            response_intent = intent
            source_id = item.get("source_id", "")
            if brand.lower() == "cfc" and intent in {"cfc_npk_product_info", "cfc_price_unverified"}:
                from domains.amis.live_crm import lookup_inventory_atp
                prod = lookup_inventory_atp(raw_text)
                if prod:
                    p_name = prod.get("product_name") or ""
                    p_unit = prod.get("unit") or "Bao"
                    answer = (
                        f"Dạ sản phẩm **{p_name}** (Quy cách: {p_unit}) là dòng sản phẩm chính thức của Cò Bay. "
                        "Bảng giá niêm yết và chính sách chiết khấu phụ thuộc vào quy cách đóng bao và khu vực phân phối của từng đại lý. "
                        "Bạn gửi giúp mình Số điện thoại và Khu vực canh tác để kỹ sư Cò Bay gửi bảng giá chính xác nhất nhé ạ!"
                    )
            if not answer:
                response_intent = unavailable_intent or f"{intent}_unavailable"
                answer = unavailable_answer or (
                    "Dạ hiện mục thông tin này chưa tải được từ hệ thống kiến thức. "
                    "Admin sẽ kiểm tra lại dữ liệu và phản hồi bạn chính xác hơn nha."
                )
            _remember_response(answer, response_intent, stage, source_id=source_id)
            return _fast_response(answer, response_intent, brand, start_time, lead_stage=stage)

        def _fast_response_remember(answer: str, intent: str, *, stage: str = "new", fallback_reason: str = "") -> ChatPipelineResponse:
            _remember_response(answer, intent, stage, fallback_reason=fallback_reason)
            return _fast_response(answer, intent, brand, start_time, lead_stage=stage, fallback_reason=fallback_reason)

        def _cfc_grounded_response(
            answer: str,
            intent: str,
            *,
            stage: str,
            fallback_reason: str,
            score: float = 1.0,
        ) -> ChatPipelineResponse:
            return ChatPipelineResponse(
                answer=_prettify_answer(answer),
                intent=intent,
                confidence="high",
                score=score,
                brand=brand.upper(),
                has_phone=has_phone,
                phone=phone,
                area=area,
                lead_stage=stage,
                fallback_reason=fallback_reason,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        contact_capture_queued = False

        def _queue_incoming_contact(need: str) -> None:
            nonlocal contact_capture_queued
            if contact_capture_queued or not incoming_phone:
                return
            contact_capture_queued = True
            existing_profile.update({
                "brand": brand.upper(),
                "sender_id": sender_id,
                "fb_name": fb_name or existing_profile.get("fb_name", ""),
                "phone": incoming_phone,
                "customer_phone": incoming_phone,
                "area": area or existing_profile.get("area", ""),
                "customer_location": area or existing_profile.get("customer_location", ""),
                "lead_stage": "lead_ready",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            })
            _local_customer_cache[customer_key] = dict(existing_profile)
            asyncio.create_task(_async_save_profile_and_notify(
                brand=brand,
                sender_id=sender_id,
                profile=existing_profile,
                phone=incoming_phone,
                area=area,
                fb_name=fb_name,
                need=need,
            ))

        if (conversation_state.get("takeover_state") or {}).get("status") == "pending":
            return ChatPipelineResponse(
                answer="",
                intent="human_handoff_active",
                confidence="high",
                score=1.0,
                brand=brand.upper(),
                lead_stage="escalated",
                suppress_send=True,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        if route_decision.action == "clarify" and route_decision.reason == "ORDINAL_WITHOUT_OPTIONS":
            return _fast_response_remember(
                "Dạ mình chưa có danh sách sản phẩm nào ở lượt trước để xác định ‘cái thứ hai’. "
                "Bạn cho mình tên hoặc nhóm sản phẩm cần hỏi giá, mình kiểm tra đúng sản phẩm cho bạn nha.",
                "context_clarification",
                stage="browsing_catalog",
                fallback_reason="UNRESOLVED_REFERENCE",
            )

        if route_decision.action == "clarify" and route_decision.reason == "CORRECTION_REQUIRES_PRODUCT":
            corrected_brand = str(query_plan.constraints.get("corrected_brand") or "ZeO")
            display_brand = "ZeO" if corrected_brand == "zeo" else corrected_brand.upper()
            return _fast_response_remember(
                f"Dạ mình hiểu rồi: bạn đang hỏi {display_brand}, không phải thương hiệu vừa nêu trước đó. "
                "Bạn đang quan tâm nước giặt, bột giặt, nước rửa chén hay nhóm sản phẩm nào của "
                f"{display_brand} để mình tra đúng thông tin cho bạn ạ?",
                "customer_correction_clarify",
                stage="browsing_catalog",
                fallback_reason="CORRECTION_REQUIRES_PRODUCT",
            )

        if brand == "cfc":
            location_slot = state_patch["confirmed_slots"].get("location")
            cfc_slots = _merged_cfc_slots(
                conversation_state,
                raw_text,
                phone=phone,
                area=area,
                query_entities=query_plan.entities,
                location=location_slot,
            )

            if route_decision.intent == "cfc_contact_information_unavailable":
                item = await get_faq_by_intent(brand, "cfc_company_website")
                official_channel = item.get("answer", "").strip()
                answer = (
                    "Dạ Knowledge CFC hiện chưa có số hotline/tổng đài đã được xác minh nên mình không tự cung cấp số. "
                    + (
                        f"Kênh chính thức đang có trong Knowledge: {official_channel}"
                        if official_channel
                        else "Admin cần bổ sung nguồn liên hệ chính thức trước khi bot có thể trả lời ạ."
                    )
                )
                _remember_response(
                    answer,
                    route_decision.intent,
                    "browsing_catalog",
                    source_id=item.get("source_id", ""),
                    fallback_reason="CFC_CONTACT_NOT_VERIFIED",
                    trace_extra={"source_intent": "cfc_company_website"},
                )
                return _cfc_grounded_response(
                    answer,
                    route_decision.intent,
                    stage="browsing_catalog",
                    fallback_reason="CFC_CONTACT_NOT_VERIFIED",
                    score=route_decision.confidence,
                )

            if has_location:
                item = await get_faq_by_intent(brand, "cfc_dealer_location_request")
                matched_locations = await _lookup_sales_locations_from_redis(
                    user_message=raw_text,
                    lat=req.latitude,
                    lon=req.longitude,
                )
                if matched_locations:
                    answer = _format_sales_locations_reply(matched_locations, "gần vị trí bạn gửi")
                    _remember_response(
                        answer,
                        "cfc_dealer_location_request",
                        "browsing_catalog",
                        source_id="amis:public:sales-locations:active",
                        trace_extra={"dealer_count": len(matched_locations), "input_kind": "location"},
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(answer),
                        intent="cfc_dealer_location_request",
                        confidence="high",
                        score=1.0,
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage="browsing_catalog",
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

                missing = _cfc_missing_slots("dealer_lookup", cfc_slots)
                answer = (
                    "Dạ Cò Bay đã nhận vị trí bạn gửi. Hệ thống chat hiện chưa kết nối bản đồ/danh sách đại lý theo tọa độ, "
                    "nên mình chưa thể chỉ đích danh điểm bán gần nhất. "
                    f"{_cfc_missing_slots_prompt(missing)}"
                )
                _queue_incoming_contact("Tìm đại lý theo vị trí Messenger")
                _remember_response(
                    answer,
                    "cfc_dealer_location_received",
                    "collecting_contact",
                    source_id=item.get("source_id", ""),
                    fallback_reason="DEALER_GEO_TOOL_NOT_CONNECTED",
                    trace_extra={"source_intent": "cfc_dealer_location_request", "input_kind": "location"},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent="cfc_dealer_location_received",
                    confidence="high",
                    score=1.0,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    fallback_reason="DEALER_GEO_TOOL_NOT_CONNECTED",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if route_decision.tool == "b2b_intake":
                answer = _format_b2b_large_order_reply(raw_text, query_plan.entities, phone)
                _queue_incoming_contact("Khách hàng B2B / Hợp tác xã số lượng lớn (VIP)")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    fallback_reason="B2B_VIP_LEAD_FORWARDED",
                    trace_extra={"b2b_vip": True},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent=route_decision.intent,
                    confidence="high",
                    score=1.0,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if route_decision.tool == "complaint_sop":
                answer = _format_complaint_sop_reply(raw_text, phone)
                state_patch["takeover_state"] = {"status": "pending", "owner": "cskh_qa", "reason": "product_complaint_sop"}
                _queue_incoming_contact("Khiếu nại sản phẩm / sự cố chất lượng (SOP)")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    fallback_reason="COMPLAINT_SOP_HANDOFF",
                    trace_extra={"complaint_sop": True},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent=route_decision.intent,
                    confidence="high",
                    score=1.0,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if route_decision.action == "capability_boundary":
                answer, boundary_reason = _build_cfc_capability_boundary(route_decision.intent, cfc_slots, raw_text)
                capability_goal = CFC_GOAL_BY_INTENT.get(route_decision.intent) or route_decision.intent
                _queue_incoming_contact(f"Yêu cầu CFC: {capability_goal}")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    fallback_reason=boundary_reason,
                    trace_extra={"capability_boundary": True},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent=route_decision.intent,
                    confidence="high",
                    score=route_decision.confidence,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    fallback_reason=boundary_reason,
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if route_decision.tool in {"faq_by_intent", "sales_location_search"} and route_decision.intent == "cfc_dealer_location_request":
                loc_constraints = query_plan.constraints or {}
                current_loc = area or loc_constraints.get("location") or loc_constraints.get("district") or loc_constraints.get("ward") or ""
                if current_loc:
                    cfc_slots["area"] = current_loc
                    state_patch["confirmed_slots"]["area"] = current_loc
                area_query = current_loc or cfc_slots.get("area", "") or "bạn yêu cầu"
                matched_locations = await _lookup_sales_locations_from_redis(
                    user_message=raw_text,
                    province=loc_constraints.get("location", ""),
                    district=loc_constraints.get("district", ""),
                    ward=loc_constraints.get("ward", ""),
                )

                if matched_locations:
                    answer = _format_sales_locations_reply(matched_locations, area_query)
                    _remember_response(
                        answer,
                        "cfc_dealer_location_request",
                        "browsing_catalog",
                        source_id="amis:public:sales-locations:active",
                        trace_extra={"dealer_count": len(matched_locations), "input_kind": "text"},
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(answer),
                        intent="cfc_dealer_location_request",
                        confidence="high",
                        score=1.0,
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage="browsing_catalog",
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

                item = await get_faq_by_intent(brand, route_decision.intent)
                context = _cfc_context_summary("dealer_lookup", cfc_slots)
                context_line = f" Mình đang giữ thông tin: {context}." if context else ""
                answer = (
                    f"Dạ mình hiểu bạn cần tìm điểm bán/đại lý gần nhất.{context_line} "
                    "Hệ thống hiện tại chưa có danh sách đại lý theo từng địa bàn để mình tự nêu tên hoặc địa chỉ. "
                    f"{_cfc_missing_slots_prompt(_cfc_missing_slots('dealer_lookup', cfc_slots))}"
                )
                _queue_incoming_contact("Tìm đại lý CFC theo khu vực")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    source_id=item.get("source_id", ""),
                    fallback_reason="DEALER_DIRECTORY_NOT_CONNECTED",
                )
                return _cfc_grounded_response(
                    answer,
                    route_decision.intent,
                    stage="collecting_contact",
                    fallback_reason="DEALER_DIRECTORY_NOT_CONNECTED",
                )

            if route_decision.tool == "faq_by_intent" and route_decision.intent == "cfc_dosage_usage_review":
                item = await get_faq_by_intent(brand, route_decision.intent)
                answer = _build_cfc_agronomy_intake_answer(cfc_slots, raw_text)
                _queue_incoming_contact("Tư vấn kỹ thuật nông nghiệp CFC")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    source_id=item.get("source_id", ""),
                    fallback_reason="AGRONOMY_REQUIRES_EXPERT_REVIEW",
                    trace_extra={"source_intent": route_decision.intent, "expert_intake": True},
                )
                return _cfc_grounded_response(
                    answer,
                    route_decision.intent,
                    stage="collecting_contact",
                    fallback_reason="AGRONOMY_REQUIRES_EXPERT_REVIEW",
                )

            if route_decision.tool == "faq_by_intent" and route_decision.intent == "cfc_price_unverified":
                item = await get_faq_by_intent(brand, route_decision.intent)
                from domains.amis.live_crm import lookup_inventory_atp
                prod = lookup_inventory_atp(raw_text)
                if prod:
                    p_name = prod.get("product_name") or ""
                    p_unit = prod.get("unit") or "Bao"
                    answer = (
                        f"Dạ bảng giá dòng sản phẩm **{p_name}** (Quy cách: {p_unit}) phụ thuộc vào số lượng và khu vực phân phối của từng đại lý.\n\n"
                        "Bạn gửi giúp mình Số điện thoại và Khu vực canh tác để kỹ sư Cò Bay gửi bảng giá niêm yết chính xác nhất nhé ạ!"
                    )
                else:
                    context = _cfc_context_summary("price_quote", cfc_slots)
                    context_line = f" Mình đang giữ thông tin: {context}." if context else ""
                    answer = (
                        f"Dạ bảng giá phân bón Cò Bay phụ thuộc dòng sản phẩm, quy cách và khu vực phân phối.{context_line} "
                        "Hệ thống hiện tại không có giá bán đã xác minh cho trường hợp này nên mình không tự báo số tiền. "
                        f"{_cfc_missing_slots_prompt(_cfc_missing_slots('price_quote', cfc_slots))}"
                    )
                _queue_incoming_contact("Yêu cầu báo giá phân bón CFC")
                _remember_response(
                    answer,
                    route_decision.intent,
                    "collecting_contact",
                    source_id=item.get("source_id", ""),
                    fallback_reason="PRICE_NOT_VERIFIED",
                )
                return _cfc_grounded_response(
                    answer,
                    route_decision.intent,
                    stage="collecting_contact",
                    fallback_reason="PRICE_NOT_VERIFIED",
                )

            if query_plan.intent == "privacy_sensitive_lookup":
                answer = (
                    "Dạ CFC - Phân bón Cò Bay xin phép bảo mật thông tin tài chính, công nợ và dữ liệu của khách hàng/đại lý theo quy định bảo mật nội bộ ạ.\n\n"
                    "Nếu bạn là chủ tài khoản hoặc đại diện đại lý cần đối chiếu công nợ, vui lòng liên hệ trực tiếp Trưởng phòng Kinh Doanh hoặc Giám sát Bán hàng khu vực để được hỗ trợ bảo mật nhé ạ!"
                )
                return _cfc_grounded_response(
                    answer,
                    "privacy_sensitive_lookup",
                    stage="collecting_contact",
                    fallback_reason="PRIVACY_SENSITIVE_PROTECTED",
                )

            if query_plan.intent in {"return_policy_or_claim", "cfc_product_complaint_request"} or route_decision.tool == "complaint_sop":
                answer = _format_complaint_sop_reply(raw_text, phone)
                state_patch["takeover_state"] = {"status": "pending", "owner": "cskh_qa", "reason": "product_complaint_sop"}
                _queue_incoming_contact("Khiếu nại sản phẩm / sự cố chất lượng (SOP)")
                _remember_response(
                    answer,
                    "cfc_product_complaint_request",
                    "collecting_contact",
                    fallback_reason="COMPLAINT_SOP_HANDOFF",
                    trace_extra={"complaint_sop": True},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent="cfc_product_complaint_request",
                    confidence="high",
                    score=1.0,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if query_plan.intent == "cfc_b2b_large_order_request" or route_decision.tool == "b2b_intake":
                answer = _format_b2b_large_order_reply(raw_text, query_plan.entities, phone)
                _queue_incoming_contact("Khách hàng B2B / Hợp tác xã số lượng lớn (VIP)")
                _remember_response(
                    answer,
                    "cfc_b2b_large_order_request",
                    "collecting_contact",
                    fallback_reason="B2B_VIP_LEAD_FORWARDED",
                    trace_extra={"b2b_vip": True},
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(answer),
                    intent="cfc_b2b_large_order_request",
                    confidence="high",
                    score=1.0,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="collecting_contact",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

            if query_plan.intent in {"cfc_wholesale_policy_request", "cfc_wholesale_policy_unverified", "wholesale_dealer"}:
                answer, boundary_reason = _build_cfc_capability_boundary("cfc_wholesale_policy_unverified", cfc_slots, raw_text)
                _queue_incoming_contact("Chính sách giá sỉ / Đại lý CFC")
                return _cfc_grounded_response(
                    answer,
                    "cfc_wholesale_policy_unverified",
                    stage="collecting_contact",
                    fallback_reason=boundary_reason,
                )

        # QueryPlan guard: câu bồn cầu/toilet/cặn vôi phải đi nhóm tẩy rửa,
        # không được rơi sang matcher vết bẩn quần áo chỉ vì có "ố vàng".
        if brand == "zeo" and query_plan.intent == "cleaning_toilet_stain":
            target_intent = (
                "zeo_cleaning_hygiene_product_overview"
                if any(k in norm_text for k in ["can voi", "o vang", "lau nam", "nha tam"])
                else "zeo_toilet_cleaner"
            )
            return await _sheet_response_remember(target_intent, stage="browsing_catalog")

        if brand == "zeo" and query_plan.intent == "brand_ecosystem_overview":
            item = await get_faq_by_intent(brand, "company_overview")
            answer = item.get("answer", "").strip() or (
                "Dạ ZeO, PANO và Oplus là các nhãn hàng thuộc cùng hệ sản phẩm của công ty. "
                "Mỗi nhãn có nhóm sản phẩm và định vị riêng; bạn đang muốn so sánh dòng giặt giũ, rửa chén hay lau sàn ạ?"
            )
            _remember_response(
                answer,
                "brand_ecosystem_overview",
                "browsing_catalog",
                source_id=item.get("source_id", ""),
                trace_extra={"source_intent": "company_overview"},
            )
            return _fast_response(answer, "brand_ecosystem_overview", brand, start_time, lead_stage="browsing_catalog")

        if brand == "zeo" and re.search(r"\b(nong nac|hac mui|mui hoi|con vit|kho tho)\b", norm_text) and (
            "toilet" in norm_text
            or "bon cau" in norm_text
            or "tay" in norm_text
            or conversation_state.get("active_entities", {}).get("category") in {"toilet_cleaner", "cleaning_hygiene", "bleach"}
            or previous_intent in {"zeo_cleaning_hygiene_product_overview", "zeo_toilet_cleaner"}
        ):
            item = await get_faq_by_intent(brand, "zeo_toilet_cleaner")
            answer = item.get("answer", "").strip() or (
                "Dạ với nhóm tẩy toilet/bồn cầu, dữ liệu hiện có mô tả sản phẩm ZeO có hương trái cây và hỗ trợ khử mùi. "
                "Nếu bạn nhạy mùi, mình khuyên mở cửa thông thoáng và dùng đúng hướng dẫn trên bao bì nha."
            )
            _remember_response(
                answer,
                "cleaning_fragrance_safety",
                "browsing_catalog",
                source_id=item.get("source_id", ""),
                trace_extra={"source_intent": "zeo_toilet_cleaner"},
            )
            return _fast_response(answer, "cleaning_fragrance_safety", brand, start_time, lead_stage="browsing_catalog")

        if brand == "cfc" and not has_phone and query_plan.intent == "agriculture_advisory_query" and re.search(r"\b(lua|xuong giong|hecta|ha|kien giang)\b", norm_text):
            item = await get_faq_by_intent(brand, "cfc_dosage_usage_review")
            base_answer = item.get("answer", "").strip()
            answer = (
                "Dạ với lúa mới chuẩn bị xuống giống, Cò Bay cần biết thêm giống lúa, giai đoạn đất, diện tích/khu vực và tình trạng ruộng để kỹ sư tư vấn đúng quy trình.\n\n"
                f"{base_answer or 'Mình chưa có công thức bón cố định trong hệ thống chat nên không tự đưa liều lượng để tránh sai kỹ thuật.'}\n\n"
                "Bạn gửi giúp mình số điện thoại và khu vực canh tác, kỹ sư/đại lý Cò Bay sẽ liên hệ tư vấn sát ruộng hơn nha."
            )
            _remember_response(
                answer,
                "cfc_rice_fertilizer_guide",
                "collecting_contact",
                confidence="medium",
                score=query_plan.intent_confidence,
                source_id=item.get("source_id", ""),
                fallback_reason="AGRONOMY_REQUIRES_EXPERT_REVIEW",
                trace_extra={"source_intent": "cfc_dosage_usage_review"},
            )
            return _fast_response(
                answer,
                "cfc_rice_fertilizer_guide",
                brand,
                start_time,
                lead_stage="collecting_contact",
                fallback_reason="AGRONOMY_REQUIRES_EXPERT_REVIEW",
            )

        # QueryPlan guard: câu hỏi tương thích máy giặt cửa trước chỉ trả lời thận trọng,
        # không bịa cam kết kỹ thuật nếu Sheet/catalog chưa có fact kiểm chứng.
        if (
            brand == "zeo"
            and query_plan.intent == "product_compatibility"
            and (
                query_plan.entities.get("variant")
                or any(b in {"pano", "oplus", "zeo"} for b in query_plan.entities.get("mentioned_brands", []))
            )
        ):
            compatibility_msg = (
                "Dạ với máy giặt cửa trước/cửa ngang, mình khuyên ưu tiên nước giặt dễ hòa tan và dùng đúng lượng theo hướng dẫn của máy để hạn chế trào bọt.\n\n"
                "Hiện dữ liệu chat chưa có tài liệu kỹ thuật riêng để cam kết từng mã PANO/Oplus là “chuyên dụng” cho mọi dòng máy cửa trước, nên mình không nói quá phần này ạ.\n"
                "Bạn gửi giúp mình model máy hoặc quy cách sản phẩm đang xem, admin sẽ kiểm tra kỹ hơn; nếu cần mình có thể gửi các lựa chọn nước giặt PANO/Oplus đang có trên Shopee Mall."
            )
            _remember_response(
                compatibility_msg,
                "pano_washing_machine_compatibility",
                "browsing_catalog",
                confidence="medium",
                score=query_plan.intent_confidence,
                fallback_reason="TECHNICAL_FACT_LIMITED",
            )
            return _fast_response(
                compatibility_msg,
                "pano_washing_machine_compatibility",
                brand,
                start_time,
                lead_stage="browsing_catalog",
                fallback_reason="TECHNICAL_FACT_LIMITED",
            )

        # Privacy guard phải chạy trước lưu SĐT/profile để không nhận nhầm dữ liệu của người thứ ba.
        if _detect_third_party_customer_lookup(norm_text):
            privacy_msg = (
                "Dạ để bảo vệ quyền riêng tư, mình không thể tra cứu hoặc cung cấp thông tin của khách hàng khác theo tên. "
                "Mình chỉ có thể hỗ trợ thông tin của chính bạn trong phiên Messenger này; nếu cần xử lý hồ sơ nội bộ, admin vui lòng dùng trang quản trị có phân quyền ạ."
            )
            return _fast_response_remember(
                privacy_msg,
                "customer_privacy_protected",
                stage="new",
                fallback_reason="PRIVACY_GUARD",
            )

        # ─────────────────────────────────────────────────────────────
        # FAST-PATH 1: KHÁCH ĐỂ LẠI SỐ ĐIỆN THOẠI & ĐỊA CHỈ (< 20ms)
        # ─────────────────────────────────────────────────────────────
        cfc_phone_only = bool(
            brand == "cfc"
            and has_phone
            and _is_phone_only_submission(raw_text, incoming_phone)
        )
        active_cfc_goal = _active_goal_name(conversation_state) if brand == "cfc" else ""
        if cfc_phone_only and active_cfc_goal:
            resume_intents = {
                "inventory_check": "cfc_inventory_unavailable",
                "order_tracking": "cfc_order_status_unavailable",
                "loyalty_lookup": "cfc_loyalty_unavailable",
                "wholesale_policy": "cfc_wholesale_policy_unverified",
                "dealer_lookup": "cfc_dealer_location_request",
                "price_quote": "cfc_price_unverified",
                "agronomy_consultation": "cfc_dosage_usage_review",
            }
            resumed_intent = resume_intents.get(active_cfc_goal, "contact_phone_provided")
            source_id = ""
            if resumed_intent in {
                "cfc_inventory_unavailable",
                "cfc_order_status_unavailable",
                "cfc_loyalty_unavailable",
                "cfc_wholesale_policy_unverified",
            }:
                final_reply, fallback_reason = _build_cfc_capability_boundary(resumed_intent, cfc_slots)
            elif resumed_intent == "cfc_dosage_usage_review":
                item = await get_faq_by_intent(brand, resumed_intent)
                source_id = item.get("source_id", "")
                final_reply = _build_cfc_agronomy_intake_answer(cfc_slots)
                fallback_reason = "AGRONOMY_REQUIRES_EXPERT_REVIEW"
            elif resumed_intent == "cfc_dealer_location_request":
                item = await get_faq_by_intent(brand, resumed_intent)
                source_id = item.get("source_id", "")
                context = _cfc_context_summary("dealer_lookup", cfc_slots)
                final_reply = (
                    f"Dạ mình tiếp tục yêu cầu tìm đại lý của bạn. Mình đang giữ thông tin: {context}. "
                    "Hệ thống hiện tại chưa có danh sách đại lý theo địa bàn để nêu điểm bán cụ thể. "
                    f"{_cfc_missing_slots_prompt(_cfc_missing_slots('dealer_lookup', cfc_slots))}"
                )
                fallback_reason = "DEALER_DIRECTORY_NOT_CONNECTED"
            else:
                item = await get_faq_by_intent(brand, resumed_intent)
                source_id = item.get("source_id", "")
                context = _cfc_context_summary("price_quote", cfc_slots)
                final_reply = (
                    f"Dạ mình tiếp tục yêu cầu báo giá của bạn. Mình đang giữ thông tin: {context}. "
                    "Hệ thống hiện tại chưa có giá bán đã xác minh nên mình không tự báo số tiền. "
                    f"{_cfc_missing_slots_prompt(_cfc_missing_slots('price_quote', cfc_slots))}"
                )
                fallback_reason = "PRICE_NOT_VERIFIED"

            _queue_incoming_contact(f"Tiếp tục yêu cầu CFC: {active_cfc_goal}")
            _remember_response(
                final_reply,
                resumed_intent,
                "collecting_contact",
                source_id=source_id,
                fallback_reason=fallback_reason,
                trace_extra={"resumed_active_goal": active_cfc_goal},
            )
            return ChatPipelineResponse(
                answer=_prettify_answer(final_reply),
                intent=resumed_intent,
                confidence="high",
                score=1.0,
                brand=brand.upper(),
                has_phone=True,
                phone=phone,
                area=area,
                lead_stage="collecting_contact",
                fallback_reason=fallback_reason,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        if brand == "cfc" and has_phone:
            _queue_incoming_contact("Khách CFC gửi SĐT kèm yêu cầu")

        if has_phone and (brand != "cfc" or cfc_phone_only):
            lead_stage = "lead_ready"
            if brand == "zeo":
                final_reply = (
                    f"Dạ ZeO Vietnam đã nhận được số điện thoại {phone}"
                    f"{f' tại {area}' if area else ''} của bạn. "
                    "Chuyên viên tư vấn ZeO sẽ liên hệ trực tiếp để hỗ trợ chốt đơn và gửi ưu đãi cho bạn ngay nha!"
                )
            else:
                final_reply = (
                    f"Dạ Cò Bay đã nhận được số điện thoại {_mask_phone(phone)}"
                    f"{f' tại khu vực {area}' if area else ''} của bạn. "
                    "Mình chưa thấy yêu cầu nghiệp vụ cụ thể ở tin nhắn này; bạn cho biết cần báo giá, tư vấn kỹ thuật, tìm đại lý hay kiểm tra đơn hàng để admin xử lý đúng việc ạ."
                )

            existing_profile.update({
                "brand": brand.upper(),
                "sender_id": sender_id,
                "fb_name": fb_name or existing_profile.get("fb_name", ""),
                "phone": phone,
                "customer_phone": phone,
                "area": area or existing_profile.get("area", ""),
                "customer_location": area or existing_profile.get("area", ""),
                "lead_stage": "lead_ready",
                "last_intent": "contact_phone_provided",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            })

            _local_customer_cache[customer_key] = dict(existing_profile)
            _queue_incoming_contact("Khách để lại SĐT trên Messenger")

            return ChatPipelineResponse(
                answer=_prettify_answer(final_reply),
                intent="contact_phone_provided",
                confidence="high",
                score=1.0,
                brand=brand.upper(),
                has_phone=True,
                phone=phone,
                area=area,
                lead_stage="lead_ready",
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        # ─────────────────────────────────────────────────────────────
        # FAST-PATH 1.5: CUSTOMER PROFILE RECALL (Cách ly 100% khỏi FAQ) (< 10ms)
        # ─────────────────────────────────────────────────────────────
        # Phải loại trừ trường hợp hỏi địa chỉ công ty hoặc hỏi mua ở đâu
        is_asking_company_address = _detect_address_intent(norm_text, brand) is not None
        is_asking_buy_online = bool(re.search(r"(mua|ban|dat).*(o dau|cho nao|tai dau)", norm_text))

        asks_saved_phone = not is_asking_company_address and not is_asking_buy_online and bool(
            re.search(r"(so dien thoai|dien thoai|sdt)\s+(cua\s+)?(toi|minh|em|anh|chi)\b", norm_text)
            or re.search(r"(ban|shop|ad|admin)\s+(con nho|nho|co luu|da luu)\s+(so dien thoai|dien thoai|sdt|so cua)\b", norm_text)
            or re.search(r"(toi|minh|em|anh|chi)\s+(da gui|co gui|gui roi)\s+(so dien thoai|dien thoai|sdt)\b", norm_text)
            or re.search(r"(shop|ban)\s+nho\s+so\s+toi\b", norm_text)
        )
        is_asking_dealer_location = bool(re.search(r"\b(dai ly|nha phan phoi|npp|diem mua)\b", norm_text))
        asks_saved_area = not is_asking_company_address and not is_asking_buy_online and not is_asking_dealer_location and bool(
            re.search(r"(dia chi|khu vuc|noi o|tinh thanh)\s+(cua\s+)?(toi|minh|em|anh|chi)\b", norm_text)
            or re.search(r"(ban|shop|ad|admin)\s+(con nho|nho|co luu|da luu)\s+(dia chi|khu vuc|noi o|tinh thanh|cho o)\b", norm_text)
            or re.search(r"(toi|minh|em|anh|chi)\s+(dang o dau|o tinh nao|o khu vuc nao)", norm_text)
        )
        asks_profile_recall = not is_asking_company_address and not is_asking_buy_online and bool(
            re.search(r"(thong tin|ho so)\s+(cua\s+)?(toi|minh|em|anh|chi)\b", norm_text)
            or re.search(r"(ban|shop|ad|admin)\s+(con nho|nho|co luu|da luu)\s+(toi|minh|em|anh|chi)\b", norm_text)
            or re.search(r"^ban con nho toi khong$", norm_text)
            or re.search(r"^shop con nho toi khong$", norm_text)
            or re.search(r"^toi ten gi$", norm_text)
        )

        if asks_saved_phone or asks_saved_area or asks_profile_recall:
            brand_display = "ZeO" if brand == "zeo" else "Cò Bay"
            if asks_saved_phone and asks_saved_area:
                if phone and area:
                    final_reply = f"Dạ có, {brand_display} đang lưu số điện thoại của bạn là {phone} và khu vực/địa chỉ là {area}."
                elif phone:
                    final_reply = f"Dạ {brand_display} đang lưu số điện thoại của bạn là {phone}. Mình chưa thấy khu vực/địa chỉ trong hồ sơ chat này, bạn gửi thêm giúp mình nha."
                elif area:
                    final_reply = f"Dạ {brand_display} đang lưu khu vực/địa chỉ của bạn là {area}. Mình chưa thấy số điện thoại trong hồ sơ chat này, bạn gửi thêm giúp mình nha."
                else:
                    final_reply = f"Dạ hiện {brand_display} chưa thấy lưu số điện thoại và khu vực/địa chỉ trong hồ sơ chat này. Bạn gửi lại giúp mình để bên mình lưu và hỗ trợ đúng hơn nha."
            elif asks_saved_phone:
                final_reply = (
                    f"Dạ số điện thoại {brand_display} đang lưu của bạn là {phone}."
                    if phone
                    else f"Dạ hiện {brand_display} chưa thấy lưu số điện thoại trong hồ sơ chat này. Bạn gửi lại số điện thoại giúp mình nha."
                )
            elif asks_saved_area:
                final_reply = (
                    f"Dạ khu vực/địa chỉ {brand_display} đang lưu của bạn là {area}."
                    if area
                    else f"Dạ hiện {brand_display} chưa thấy lưu khu vực/địa chỉ trong hồ sơ chat này. Bạn gửi lại khu vực/tỉnh thành giúp mình nha."
                )
            elif phone and area:
                final_reply = f"Dạ có, {brand_display} đang lưu số điện thoại {phone} và khu vực/địa chỉ {area} của bạn."
            elif phone:
                final_reply = f"Dạ có, {brand_display} đang lưu số điện thoại của bạn là {phone}. Bạn gửi thêm khu vực/tỉnh thành để bên mình hỗ trợ đúng hơn nha."
            elif area:
                final_reply = f"Dạ có, {brand_display} đang lưu khu vực/địa chỉ của bạn là {area}. Bạn gửi thêm số điện thoại để bên mình tiện liên hệ nha."
            else:
                final_reply = f"Dạ hiện {brand_display} chưa thấy có đủ thông tin của bạn trong hồ sơ chat này. Bạn gửi lại số điện thoại và khu vực/tỉnh thành giúp mình nha."

            return ChatPipelineResponse(
                answer=_prettify_answer(final_reply),
                intent="customer_profile_lookup",
                confidence="high",
                score=1.0,
                brand=brand.upper(),
                has_phone=bool(phone),
                phone=phone or "",
                area=area or "",
                lead_stage=lead_stage,
                fallback_reason="PROFILE_RECALL",
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            )

        # ─────────────────────────────────────────────────────────────
        # FAST-PATH 2: CHÀO HỎI, CẢM ƠN, XÁC NHẬN (< 10ms)
        # ─────────────────────────────────────────────────────────────
        token_count = len(norm_text.split())
        # 0. Dấu chấm hỏi / thắc mắc ngắn dạng '???', 'là sao', 'sao vậy'
        if re.match(r"^[\?\!！？\s\.\,\…]+$", raw_text.strip()) or norm_text in ["la sao", "sao vay", "y la sao", "nghia la sao", "sao the"]:
            clarify_msg = "Dạ bạn cần bên mình giải thích rõ hơn phần nào hoặc cần tư vấn sản phẩm nào ạ? Bạn cứ nhắn chi tiết mình hỗ trợ ngay nha! 💙" if brand == "zeo" else "Dạ bạn cần Cò Bay tư vấn thêm thông tin nào ạ? Bạn cứ nhắn cho mình nha!"
            return _fast_response(clarify_msg, "clarification_request", brand, start_time)

        # 0.5. Từ chối / Không quan tâm / Thôi khỏi ('ko quan tam', 'ko can', 'ko can biet', 'thoi khoi')
        if re.search(r"\b(ko quan tam|khong quan tam|ko can|khong can|ko can biet|khong can biet|thoi khoi|thoi bo qua|khong can dau|khoi can)\b", norm_text):
            dismiss_msg = "Dạ vâng ạ! Nếu sau này bạn cần tìm hiểu thêm về sản phẩm hoặc cần hỗ trợ đặt hàng, bạn cứ nhắn tin lại cho bên mình bất kỳ lúc nào nhé! Chúc bạn một ngày tốt lành ạ! 💙" if brand == "zeo" else "Dạ vâng ạ! Khi nào cần tư vấn phân bón hoặc kỹ thuật canh tác, bạn cứ nhắn lại cho Cò Bay nha!"
            return _fast_response(dismiss_msg, "customer_dismiss_polite", brand, start_time)

        if token_count <= 6:
            # 1. Cảm ơn (Ưu tiên trước acknowledgement)
            if re.search(r"(cam on|thanks|thank you|da cam on|ok cam on|tks)\b", norm_text):
                thanks = "Dạ ZeO cảm ơn bạn đã quan tâm! Cần hỗ trợ thêm bạn cứ nhắn shop nha." if brand == "zeo" else "Dạ Cò Bay cảm ơn bạn! Chúc bạn một vụ mùa bội thu ạ."
                return _fast_response(thanks, "thanks", brand, start_time)

            # 2. Chào hỏi (Không bắt nhầm các câu 'shop có ship không', 'shop mở cửa lúc mấy giờ')
            is_pure_greeting = bool(re.search(r"^(xin chao|chao|hello|hi|alo|alo shop|alo co ai truc khong|shop oi|admin oi|ad oi|chao cong ty co bay|chao cong ty cfc|chao co bay|chao cfc)$|^shop$", norm_text)) or bool(
                re.search(r"^(xin chao|chao ban|chao shop|chao cong ty|hello|hi|alo)\b", norm_text)
                and not any(k in norm_text for k in ["ship", "mo cua", "gia", "san pham", "mua", "dia chi", "hotline", "website", "doi tra"])
            )
            if is_pure_greeting:
                greeting = "Dạ ZeO Vietnam chào bạn! Bạn đang cần tư vấn về nước giặt sinh học, nước rửa chén hay mua hàng ạ?" if brand == "zeo" else "Dạ phân bón Cò Bay (CFC) chào bạn! Bạn đang cần tư vấn phân bón cho cây lúa, cây ăn trái hay đại lý phân phối ạ?"
                return _fast_response(greeting, "greeting", brand, start_time)

            # 2.5. Đồng ý nhận link sản phẩm khi lượt trước bot vừa hỏi 'Bạn muốn mình gửi link...'
            previous_bot_raw = str(existing_session.get("last_bot_reply", ""))
            previous_bot_norm = _normalize_vn(previous_bot_raw)
            is_link_affirmation = bool(re.search(
                r"^(ok|oke|okay|z ok|vay ok|da ok|ok nha|ok nhe|ok shop|oke shop|da|vang|u|uh|um|uk|roi|duoc|gui|gui di|gui link di|gui giup minh|cho minh xin|cho xin|co|yes|gui giup|gui nhe|xin link|gui link|co nha|co chu|gui em|gui minh|cho em xin|gui link giup|gui giup em|cho xin link di|cho minh xin link|cho em xin link|dung roi|chinh xac)\b",
                norm_text
            ))
            asked_to_send_link = route_decision.reason == "PENDING_LINK_CONFIRMED" or bool(re.search(
                r"(gui link|link shopee|link mua|link dat hang|muon minh gui link|gui link khong|link mua hang|link san pham|link web|link website)",
                previous_bot_norm
            ))
            if is_link_affirmation and asked_to_send_link:
                candidate_product = None
                products_shown = conversation_state.get("last_products_shown") or []
                if products_shown and isinstance(products_shown[0], dict) and products_shown[0].get("name"):
                    candidate_product = products_shown[0].get("name")
                
                # Ưu tiên tìm sản phẩm cụ thể xuất hiện trong nội dung bot vừa nói
                catalog = load_shopee_catalog(brand=brand)
                best_score = 0
                for p in catalog:
                    p_norm = _normalize_vn(p.get("name", ""))
                    words = [w for w in p_norm.split() if len(w) > 2 and w in previous_bot_norm]
                    score = len(words)
                    for key_term in ["lau san", "hoa ha", "sa chanh", "chanh", "bac ha", "y lang", "baby", "bio enzyme", "vitamin e", "tiet kiem nuoc", "rua chen", "bot giat", "nuoc giat"]:
                        if key_term in p_norm and key_term in previous_bot_norm:
                            score += 6
                    if "pano" in p_norm and "pano" in previous_bot_norm:
                        score += 5
                    if "zeo" in p_norm and "zeo" in previous_bot_norm:
                        score += 3
                    if "oplus" in p_norm and "oplus" in previous_bot_norm:
                        score += 3
                    if score > best_score:
                        best_score = score
                        candidate_product = p.get("name")

                if not candidate_product:
                    active_prod = conversation_state.get("active_entities", {}).get("product")
                    if active_prod and str(active_prod).lower() not in {"pano", "zeo", "oplus", "cfc"}:
                        candidate_product = str(active_prod)
                
                if candidate_product:
                    shopee_match = match_shopee_product(candidate_product, brand=brand)
                    if shopee_match and shopee_match.get("shopee_url"):
                        matched_product = shopee_match.get("matched_product")
                        _remember_response(
                            shopee_match["suggested_reply"],
                            shopee_match.get("intent", "shopee_product_link"),
                            "browsing_catalog",
                            products_shown=[matched_product] if isinstance(matched_product, dict) else None,
                        )
                        return ChatPipelineResponse(
                            answer=_prettify_answer(shopee_match["suggested_reply"]),
                            intent=shopee_match.get("intent", "shopee_product_link"),
                            confidence="high",
                            score=0.99,
                            brand=brand.upper(),
                            has_phone=has_phone,
                            phone=phone,
                            area=area,
                            lead_stage="browsing_catalog",
                            shopee_url=shopee_match.get("shopee_url"),
                            latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                        )

            # 3. Xác nhận / Kết thúc hội thoại ('z ok', 'vay ok', 'da ok', 'ok nha', 'the thoi')
            if re.search(r"^(z ok|vay ok|da ok|ok nha|ok nhe|ok shop|oke shop|the thoi|the nha|vay dc roi|vay duoc roi|ok roi|ok|oke|okay|da|vang|uh|um|roi|duoc|biet roi|hieu roi)\b", norm_text) and not re.search(r"(cam on|thanks)", norm_text):
                ack = "Dạ vâng ạ! Bạn cần thêm thông tin gì cứ nhắn ZeO nhé." if brand == "zeo" else "Dạ vâng ạ! Khi nào cần phân bón chất lượng cao bạn cứ nhắn Cò Bay nha."
                return _fast_response(ack, "acknowledgement", brand, start_time)

            # 4. Phím nhánh hotline 02
            if re.fullmatch(r"0?2", norm_text):
                previous_bot = _normalize_vn(existing_session.get("last_bot_reply", ""))
                if "1900 5307" in previous_bot or "phim nhanh" in previous_bot or "phim nhanh so 02" in previous_bot or "phim nhanh so 2" in previous_bot:
                    branch_msg = (
                        "Dạ số 02 là phím nhánh mua hàng khi bạn gọi hotline 1900 5307. "
                        "Nếu bạn đang nhắn tại đây, bạn có thể gửi tên sản phẩm cần mua hoặc số điện thoại để admin hỗ trợ tiếp nha."
                    )
                    return _fast_response(branch_msg, "hotline_branch_02", brand, start_time, lead_stage="browsing_catalog")
                clarify_msg = "Dạ bạn muốn chọn mục nào ạ? Bạn nhắn giúp mình nhu cầu như xem sản phẩm, link website, mua hàng hoặc cần hỗ trợ đơn hàng nha."
                return _fast_response(clarify_msg, "short_numeric_clarify", brand, start_time)

            if re.search(r"^(it vay|it the|it vay thoi|chi vay|chi co vay|co vay thoi)\b", norm_text):
                if brand == "zeo":
                    expand_msg = (
                        "Dạ không chỉ một nhóm đâu ạ. ZeO Vietnam hiện có 4 nhóm chính: "
                        "Giặt giũ, Rửa chén, Lau sàn và Tẩy rửa vệ sinh. "
                        "Nếu bạn muốn, mình có thể liệt kê chi tiết từng nhóm sản phẩm cho bạn nha."
                    )
                else:
                    expand_msg = (
                        "Dạ Cò Bay hiện tập trung vào phân bón, gồm các dòng như NPK và phân hữu cơ. "
                        "Bạn cho mình biết loại cây trồng để bên mình tư vấn dòng phù hợp hơn ạ."
                    )
                return _fast_response(expand_msg, "catalog_followup_expand", brand, start_time, lead_stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # FAST-PATH 3: PHÁT HIỆN KHIẾU NẠI GAY GẮT / HÀNG LỖI BỂ VỠ (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"\b(gap|noi chuyen voi|chuyen cho|cho gap)\s+(admin|nhan vien|nguoi that|tu van vien|cskh)\b", norm_text):
            handoff_msg = (
                "Dạ mình đã chuyển yêu cầu sang nhân viên phụ trách. Từ lượt tiếp theo bot sẽ tạm dừng để "
                "nhân viên tiếp nhận đúng nội dung bạn đang cần hỗ trợ ạ."
            )
            asyncio.create_task(notify_admin_unanswered(
                brand=brand,
                query=raw_text,
                sender_id=sender_id,
                score=1.0,
            ))
            return _fast_response_remember(
                handoff_msg,
                "human_handoff_requested",
                stage="escalated",
                fallback_reason="HUMAN_HANDOFF",
            )

        URGENT_DAMAGE_TRIGGERS = [
            "nut nap", "be nap", "rach bao", "chay nuoc", "uot het", "chay het",
            "be vo", "hu hong", "giao sai", "giao thieu", "lam an kieu gi",
            "giao be", "vo chai", "vo can", "loi bao bi", "bung nap", "rach nap"
        ]
        is_asking_policy = bool(re.search(r"\b(duoc doi khong|duoc doi tra khong|co duoc doi|co duoc tra|chinh sach|quy dinh)\b", norm_text))
        if any(k in norm_text for k in URGENT_DAMAGE_TRIGGERS) and not is_asking_policy:
            lead_stage = "escalated"
            brand_display = "ZeO Vietnam" if brand == "zeo" else "CFC Cò Bay"
            policy_item = await get_faq_by_intent(brand, "return_process" if brand == "zeo" else "cfc_status_check")
            damage_msg = (
                f"Dạ {brand_display} chân thành xin lỗi bạn về sự cố hư hỏng/sai sót đơn hàng đáng tiếc này ạ. "
                "CSKH cần kiểm tra đơn hàng và bằng chứng theo chính sách trước khi xác nhận phương án đổi hàng hoặc hoàn tiền.\n\n"
                "Bạn vui lòng gửi ảnh/video sản phẩm bị lỗi kèm số điện thoại nhận hàng; quản trị viên sẽ tiếp nhận và phản hồi phương án chính xác ạ."
            )
            asyncio.create_task(notify_urgent_complaint(brand=brand, query=raw_text, phone=phone, sender_id=sender_id, fb_name=fb_name))
            _remember_response(
                damage_msg,
                "urgent_damage_complaint",
                "escalated",
                source_id=policy_item.get("source_id", ""),
                fallback_reason="HUMAN_HANDOFF",
            )
            return _fast_response(damage_msg, "urgent_damage_complaint", brand, start_time, lead_stage="escalated")

        COMPLAINT_TRIGGERS = [
            "bot ngu", "tra loi gi ky", "khong lien quan", "chui", "chan ghe", "that vong",
            "lua dao", "hang gia", "hang kem chat luong", "gian lan", "an quyt", "thai do kem", "to cao"
        ]
        if any(k in norm_text for k in COMPLAINT_TRIGGERS):
            lead_stage = "escalated"
            complaint_msg = (
                "Dạ xin lỗi bạn vì trải nghiệm chưa tốt vừa rồi. Vấn đề này em xin phép chuyển thẳng cho Admin phụ trách xử lý ngay. "
                "Bạn để lại số điện thoại hoặc mô tả chi tiết giúp em nhé ạ!"
            )
            asyncio.create_task(notify_admin_unanswered(brand=brand, query=raw_text, sender_id=sender_id, score=0.0))
            return _fast_response_remember(
                complaint_msg,
                "bot_complaint_escalate",
                stage="escalated",
                fallback_reason="HUMAN_HANDOFF",
            )

        # ─────────────────────────────────────────────────────────────
        # PATH 3.4: INTENT-FIRST ROUTER CHỐNG RAG BẮT NHẦM (< 15ms)
        # ─────────────────────────────────────────────────────────────
        brand_display = "ZeO Vietnam" if brand == "zeo" else "CFC Cò Bay"

        if _detect_language_request(norm_text):
            msg = (
                f"Dạ hiện {brand_display} hỗ trợ tư vấn chính bằng tiếng Việt để đảm bảo thông tin sản phẩm, giá và chính sách không bị sai lệch. "
                "Bạn cứ nhắn nhu cầu bằng tiếng Việt, mình sẽ hỗ trợ đúng theo dữ liệu hệ thống nha."
            )
            return _fast_response(msg, "language_support_vi", brand, start_time)

        if _detect_out_of_scope_general_question(norm_text):
            msg = (
                f"Dạ câu này nằm ngoài dữ liệu tư vấn sản phẩm/dịch vụ của {brand_display}, "
                "nên mình xin từ chối trả lời ạ. "
                "Bạn cần xem sản phẩm, giá, giao hàng hay thông tin liên hệ bên mình không ạ?"
            )
            return _fast_response_remember(msg, "out_of_scope_general_question", stage=lead_stage, fallback_reason="OUT_OF_SCOPE")

        if _detect_out_of_scope_personal_question(norm_text):
            msg = (
                f"Dạ mình là trợ lý tư vấn tự động của {brand_display}, chuyên hỗ trợ thông tin sản phẩm, báo giá, khuyến mãi và đơn hàng ạ. "
                "Bạn cần mình hỗ trợ thông tin gì về sản phẩm hay dịch vụ bên mình không ạ? 💙"
            )
            return _fast_response(msg, "out_of_scope_personal_question", brand, start_time)

        if _detect_customer_correction(norm_text):
            msg = (
                "Dạ mình ghi nhận góp ý/cập nhật thông tin của bạn rồi ạ. "
                "Phần này mình sẽ chuyển admin kiểm tra với dữ liệu chính thức trước khi cập nhật, để tránh tự sửa sai hoặc trả lời nhầm cho khách khác nha."
            )
            asyncio.create_task(notify_admin_unanswered(brand=brand, query=raw_text, sender_id=sender_id, score=0.0))
            return _fast_response(msg, "customer_correction_review", brand, start_time, lead_stage="escalated")

        if _detect_competitor_product(norm_text, brand):
            msg = (
                "Dạ hiện dữ liệu ZeO chưa có thông tin sản phẩm/thương hiệu bạn vừa hỏi. "
                "ZeO Vietnam đang có các nhóm giặt giũ, rửa chén, lau sàn và tẩy rửa vệ sinh thuộc hệ ZeO/PANO/Oplus. "
                "Bạn muốn mình gửi danh mục ZeO hiện có để chọn đúng sản phẩm không ạ?"
            )
            return _fast_response(msg, "competitor_product_unavailable", brand, start_time, lead_stage="browsing_catalog")

        if _detect_cfc_cross_brand(norm_text, brand):
            return await _sheet_response_remember(
                "cfc_cross_brand_out_of_scope",
                stage="browsing_catalog",
                unavailable_answer=(
                    "Dạ CFC Cò Bay hiện là thương hiệu phân bón nông nghiệp. "
                    "Các sản phẩm tẩy rửa gia dụng như nước giặt/nước rửa chén/lau sàn thuộc hệ ZeO/PANO/Oplus nha."
                ),
            )

        if _detect_new_product_request(norm_text):
            msg = (
                f"Dạ hiện hệ thống kiến thức của {brand_display} chưa có mục xác nhận sản phẩm mới nhất/mới ra mắt. "
                "Để tránh báo sai, bạn cho mình biết nhóm sản phẩm đang quan tâm hoặc để lại số điện thoại, admin sẽ kiểm tra thông tin mới nhất giúp mình nha."
            )
            return _fast_response(msg, "new_product_unverified", brand, start_time, lead_stage="browsing_catalog")

        if brand == "zeo" and _detect_purchase_signal(norm_text) and re.search(r"\boplus\b", norm_text) and not re.search(r"(bot giat|nuoc rua chen|rua chen|lau san)", norm_text):
            msg = (
                "Dạ mình hiểu bạn muốn mua Oplus. Hiện dữ liệu hệ thống có Bột giặt Oplus và Nước rửa chén Oplus. "
                "Bạn muốn mua loại nào, quy cách bao nhiêu, và khu vực giao hàng ở đâu để admin kiểm tra đúng đơn giúp mình nha?"
            )
            return _fast_response_remember(msg, "oplus_purchase_clarify", stage="collecting_contact")

        # ─────────────────────────────────────────────────────────────
        # SMART AI CS ROUTING: BUDGET, NEEDS, PRICING & SHOPEE
        # ─────────────────────────────────────────────────────────────
        is_return_or_claim = bool(re.search(r"\b(doi tra|tra hang|bi loi|bi hong|bao hanh|hoan tien|khieu nai)\b", norm_text))

        return_followup_intent = _detect_return_followup_intent(norm_text, previous_intent, conversation_state)
        if return_followup_intent == "return_fee_unverified":
            return_fee_msg = (
                "Dạ nếu bạn đang hỏi đổi/trả hàng có tốn phí không: chính sách hiện xác nhận công ty sẽ thu hồi hàng lỗi, "
                "giao sản phẩm thay thế hoặc hoàn tiền sau khi CSKH duyệt, nhưng chưa có mức phí chung được xác nhận cho mọi trường hợp. "
                "Mình không tự khẳng định miễn phí để tránh báo sai. Bạn liên hệ hotline 1900 5307 và gửi mã đơn cùng ảnh/video; "
                "CSKH sẽ xác nhận chi phí đúng theo nguyên nhân và kênh mua hàng nha."
            )
            return _fast_response_remember(
                return_fee_msg,
                "return_fee_unverified",
                stage="browsing_catalog",
                fallback_reason="NO_VERIFIED_RETURN_FEE",
            )
        if return_followup_intent:
            return await _sheet_response_remember(return_followup_intent, stage="browsing_catalog")

        # 0. Multi-Intent / Compound Query Processing (Ưu tiên xử lý câu hỏi ghép 2 ý trước khi vào đơn lẻ)
        multi_res = await _detect_and_process_multi_intent(
            raw_text=raw_text,
            norm_text=norm_text,
            brand=brand,
            conversation_state=conversation_state,
            start_time=start_time,
            phone=phone,
            area=area,
            has_phone=has_phone,
            lead_stage=lead_stage,
        )
        if multi_res:
            _remember_response(multi_res.answer, multi_res.intent, "browsing_catalog")
            return multi_res

        # 0.2. Ollama NLU planner (optional): hiểu câu hỏi tự nhiên -> chọn deterministic tool.
        llm_nlu_plan: Optional[dict[str, Any]] = None
        llm_nlu_mode, llm_nlu_timeout, llm_nlu_threshold = _llm_nlu_config()
        should_try_llm_nlu = bool(
            llm_nlu_mode in {"assist", "shadow"}
            and not is_return_or_claim
            and _should_try_llm_nlu(norm_text, brand)
            and (brand != "cfc" or llm_nlu_mode == "shadow")
        )
        nlu_conversation_summary = (
            f"previous_intent={previous_intent}; "
            f"last_products={json.dumps(conversation_state.get('last_products_shown', [])[:3], ensure_ascii=False)}"
        )
        if should_try_llm_nlu and llm_nlu_mode == "shadow":
            shadow_status = schedule_nlu_shadow(
                brand=brand,
                sender_id=sender_id,
                message_id=str(req.message_id or ""),
                raw_text=raw_text,
                normalized_text=norm_text,
                conversation_summary=nlu_conversation_summary,
                deterministic_plan=query_plan_dict,
                confidence_threshold=llm_nlu_threshold,
            )
            pipeline_trace_extra["llm_nlu"] = {
                "mode": "shadow",
                "status": shadow_status,
                "affects_response": False,
            }
        elif should_try_llm_nlu and llm_nlu_mode == "assist":
            llm_nlu_plan = await plan_chat_intent_with_ollama(
                user_query=raw_text,
                brand=brand,
                conversation_summary=nlu_conversation_summary,
                timeout=llm_nlu_timeout,
            )

        if llm_nlu_plan and float(llm_nlu_plan.get("confidence", 0.0)) >= llm_nlu_threshold:
            pipeline_trace_extra["llm_nlu"] = {
                "mode": llm_nlu_mode,
                "provider": llm_nlu_plan.get("provider", "ollama"),
                "model": llm_nlu_plan.get("model", ""),
                "intent": llm_nlu_plan.get("intent", ""),
                "confidence": llm_nlu_plan.get("confidence", 0.0),
                "sort": llm_nlu_plan.get("sort", ""),
                "need_type": llm_nlu_plan.get("need_type", ""),
                "category": llm_nlu_plan.get("category", ""),
                "product": llm_nlu_plan.get("product", ""),
                "reference": bool(llm_nlu_plan.get("reference", False)),
                "reason": llm_nlu_plan.get("reason", ""),
            }
        if (
            llm_nlu_mode == "assist"
            and llm_nlu_plan
            and float(llm_nlu_plan.get("confidence", 0.0)) >= llm_nlu_threshold
        ):
            llm_trace = {"llm_nlu": dict(pipeline_trace_extra.get("llm_nlu", {}))}
            plan_query = raw_text
            plan_product = str(llm_nlu_plan.get("product", "") or "").strip()
            plan_category = str(llm_nlu_plan.get("category", "") or "").strip()
            if plan_product:
                plan_query = f"{plan_query} {plan_product}"
            if plan_category:
                plan_query = f"{plan_query} {plan_category}"

            if llm_nlu_plan.get("intent") == "price_extreme" and llm_nlu_plan.get("sort") in {"highest", "lowest"}:
                extreme_res = match_price_extreme(plan_query, brand=brand, mode=str(llm_nlu_plan.get("sort")))
                if extreme_res:
                    _remember_response(
                        extreme_res["suggested_reply"],
                        extreme_res.get("intent", "shopee_price_extreme"),
                        "browsing_catalog",
                        score=float(extreme_res.get("score", 0.99)),
                        trace_extra={
                            **llm_trace,
                            "price_extreme": extreme_res.get("price_extreme", ""),
                            "selected_product_ids": [
                                str(product.get("item_id", ""))
                                for product in extreme_res.get("selected_products", [])
                                if product.get("item_id")
                            ],
                            "no_results": bool(extreme_res.get("no_results")),
                        },
                        products_shown=extreme_res.get("selected_products", []),
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(extreme_res["suggested_reply"]),
                        intent=extreme_res.get("intent", "shopee_price_extreme"),
                        confidence="high",
                        score=float(extreme_res.get("score", 0.99)),
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage=lead_stage,
                        shopee_url=extreme_res.get("shopee_url"),
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

            if llm_nlu_plan.get("intent") == "budget_filter":
                budget_res = match_products_by_budget(plan_query, brand=brand)
                if budget_res:
                    _remember_response(
                        budget_res["suggested_reply"],
                        budget_res.get("intent", "shopee_budget_filter"),
                        "browsing_catalog",
                        score=float(budget_res.get("score", 0.98)),
                        trace_extra={
                            **llm_trace,
                            "price_constraint": budget_res.get("price_constraint", {}),
                            "range_widened": bool(budget_res.get("range_widened")),
                            "no_results": bool(budget_res.get("no_results")),
                        },
                        products_shown=budget_res.get("selected_products", []),
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(budget_res["suggested_reply"]),
                        intent=budget_res.get("intent", "shopee_budget_filter"),
                        confidence="high",
                        score=float(budget_res.get("score", 0.98)),
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage=lead_stage,
                        shopee_url=budget_res.get("shopee_url"),
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

            if llm_nlu_plan.get("intent") == "need_consultation" and llm_nlu_plan.get("need_type"):
                need_res = match_need_preference(str(llm_nlu_plan.get("need_type")), brand=brand)
                if need_res:
                    _remember_response(
                        need_res["suggested_reply"],
                        need_res.get("intent", "need_consultation"),
                        "browsing_catalog",
                        trace_extra=llm_trace,
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(need_res["suggested_reply"]),
                        intent=need_res.get("intent", "need_consultation"),
                        confidence="high",
                        score=0.98,
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage=lead_stage,
                        shopee_url=need_res.get("shopee_url"),
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

            if llm_nlu_plan.get("intent") == "specific_price":
                specific_price_res = match_specific_product_price(plan_query, brand=brand, context=conversation_state)
                if specific_price_res:
                    price_products = specific_price_res.get("selected_products")
                    matched_product = specific_price_res.get("matched_product")
                    if not isinstance(price_products, list) and isinstance(matched_product, dict):
                        price_products = [matched_product]
                    _remember_response(
                        specific_price_res["suggested_reply"],
                        specific_price_res.get("intent", "specific_product_pricing"),
                        "browsing_catalog",
                        trace_extra=llm_trace,
                        products_shown=price_products if isinstance(price_products, list) else None,
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(specific_price_res["suggested_reply"]),
                        intent=specific_price_res.get("intent", "specific_product_pricing"),
                        confidence="high",
                        score=0.99,
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage=lead_stage,
                        shopee_url=specific_price_res.get("shopee_url"),
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

            if llm_nlu_plan.get("intent") == "product_link":
                shopee_match = None
                if llm_nlu_plan.get("reference") or reference_resolution.get("resolved"):
                    shopee_match = match_shopee_product_reference(reference_resolution, brand=brand)
                if not shopee_match:
                    shopee_query = (
                        reference_resolution.get("resolved_query", plan_query)
                        if reference_resolution.get("resolved")
                        else plan_query
                    )
                    shopee_match = match_shopee_product(shopee_query, brand=brand)
                if shopee_match:
                    matched_product = shopee_match.get("matched_product")
                    _remember_response(
                        shopee_match["suggested_reply"],
                        shopee_match.get("intent", "shopee_product_link"),
                        "browsing_catalog",
                        trace_extra=llm_trace,
                        products_shown=[matched_product] if isinstance(matched_product, dict) else None,
                    )
                    return ChatPipelineResponse(
                        answer=_prettify_answer(shopee_match["suggested_reply"]),
                        intent=shopee_match.get("intent", "shopee_product_link"),
                        confidence="high",
                        score=0.95,
                        brand=brand.upper(),
                        has_phone=has_phone,
                        phone=phone,
                        area=area,
                        lead_stage=lead_stage,
                        shopee_url=shopee_match.get("shopee_url") or shopee_match.get("matched_product", {}).get("shopee_url"),
                        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    )

        # 0.5. Tư vấn hiệu quả làm sạch / tẩy vết máu / vết ố / dùng ổn không
        if brand.lower() == "zeo" and is_stain_removal_or_efficacy_inquiry(raw_text) and not is_return_or_claim:
            resolved_text = reference_resolution.get("resolved_query", raw_text)
            stain_res = match_stain_removal_or_efficacy(resolved_text, brand=brand, context=conversation_state)
            if stain_res:
                _remember_response(stain_res["suggested_reply"], stain_res.get("intent", "laundry_stain_removal_guide"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(stain_res["suggested_reply"]),
                    intent=stain_res.get("intent", "laundry_stain_removal_guide"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=stain_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1. Tầm giá / Ngân sách (vd: dưới 100k, 50k-100k)
        if brand.lower() == "zeo" and is_price_extreme_inquiry(raw_text) and not is_return_or_claim:
            extreme_res = match_price_extreme(raw_text, brand=brand)
            if extreme_res:
                _remember_response(
                    extreme_res["suggested_reply"],
                    extreme_res.get("intent", "shopee_price_extreme"),
                    "browsing_catalog",
                    score=float(extreme_res.get("score", 0.99)),
                    trace_extra={
                        "price_extreme": extreme_res.get("price_extreme", ""),
                        "selected_product_ids": [
                            str(product.get("item_id", ""))
                            for product in extreme_res.get("selected_products", [])
                            if product.get("item_id")
                        ],
                        "no_results": bool(extreme_res.get("no_results")),
                    },
                    products_shown=extreme_res.get("selected_products", []),
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(extreme_res["suggested_reply"]),
                    intent=extreme_res.get("intent", "shopee_price_extreme"),
                    confidence="high",
                    score=float(extreme_res.get("score", 0.99)),
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=extreme_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1. Tầm giá / Ngân sách (vd: dưới 100k, 50k-100k)
        if brand.lower() == "zeo" and is_budget_inquiry(raw_text) and not is_return_or_claim:
            budget_res = match_products_by_budget(raw_text, brand=brand)
            if budget_res:
                _remember_response(
                    budget_res["suggested_reply"],
                    budget_res.get("intent", "shopee_budget_filter"),
                    "browsing_catalog",
                    score=float(budget_res.get("score", 0.98)),
                    trace_extra={
                        "price_constraint": budget_res.get("price_constraint", {}),
                        "range_widened": bool(budget_res.get("range_widened")),
                        "no_results": bool(budget_res.get("no_results")),
                        "selected_product_ids": [
                            str(product.get("item_id", ""))
                            for product in budget_res.get("selected_products", [])
                            if product.get("item_id")
                        ],
                    },
                    products_shown=budget_res.get("selected_products", []),
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(budget_res["suggested_reply"]),
                    intent=budget_res.get("intent", "shopee_budget_filter"),
                    confidence="high",
                    score=0.98,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=budget_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.4. Nước xả vải phải đọc catalog hiện hành, không dùng FAQ "chưa xác minh" đã cũ.
        if brand.lower() == "zeo" and is_fabric_softener_inquiry(raw_text) and not is_return_or_claim:
            softener_res = match_fabric_softener_products(raw_text, brand=brand)
            if softener_res:
                selected_products = softener_res.get("selected_products", [])
                _remember_response(
                    softener_res["suggested_reply"],
                    softener_res.get("intent", "zeo_fabric_softener_catalog"),
                    "browsing_catalog",
                    score=float(softener_res.get("score", 0.99)),
                    trace_extra={
                        "catalog_source": "shopee_catalog",
                        "selected_product_ids": [
                            str(product.get("item_id", ""))
                            for product in selected_products
                            if isinstance(product, dict) and product.get("item_id")
                        ],
                    },
                    products_shown=selected_products if isinstance(selected_products, list) else None,
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(softener_res["suggested_reply"]),
                    intent=softener_res.get("intent", "zeo_fabric_softener_catalog"),
                    confidence="high",
                    score=float(softener_res.get("score", 0.99)),
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage="browsing_catalog",
                    shopee_url=softener_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.5. Tư vấn chuyên sâu ăn da tay / tróc da tay khi rửa chén
        if brand.lower() == "zeo" and is_skin_care_dishwashing_inquiry(raw_text) and not is_return_or_claim:
            skin_res = match_skin_care_dishwashing(raw_text, brand=brand)
            if skin_res:
                _remember_response(skin_res["suggested_reply"], skin_res.get("intent", "pano_dishwashing_features"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(skin_res["suggested_reply"]),
                    intent=skin_res.get("intent", "pano_dishwashing_features"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=skin_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.6. Báo giá sản phẩm đích danh có trong Shopee Catalog (kết hợp ngữ cảnh nếu hỏi tắt can/túi)
        if brand.lower() == "zeo" and not is_return_or_claim:
            resolved_text = reference_resolution.get("resolved_query", raw_text)
            specific_price_res = match_specific_product_price(resolved_text, brand=brand, context=conversation_state)
            if specific_price_res:
                price_products = specific_price_res.get("selected_products")
                matched_product = specific_price_res.get("matched_product")
                if not isinstance(price_products, list) and isinstance(matched_product, dict):
                    price_products = [matched_product]
                _remember_response(
                    specific_price_res["suggested_reply"],
                    specific_price_res.get("intent", "specific_product_pricing"),
                    "browsing_catalog",
                    products_shown=price_products if isinstance(price_products, list) else None,
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(specific_price_res["suggested_reply"]),
                    intent=specific_price_res.get("intent", "specific_product_pricing"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=specific_price_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.7. Tư vấn can lớn / Quán ăn / Nhà hàng / Bếp ăn
        if brand.lower() == "zeo" and is_bulk_or_restaurant_inquiry(raw_text) and not is_return_or_claim:
            bulk_res = match_bulk_or_restaurant_need(raw_text, brand=brand)
            if bulk_res:
                _remember_response(bulk_res["suggested_reply"], bulk_res.get("intent", "pano_dishwashing_product_overview"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(bulk_res["suggested_reply"]),
                    intent=bulk_res.get("intent", "pano_dishwashing_product_overview"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=bulk_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.8. Tư vấn đồ em bé / trẻ nhỏ / da nhạy cảm
        if brand.lower() == "zeo" and is_baby_or_sensitive_laundry_inquiry(raw_text) and not is_return_or_claim:
            baby_res = match_baby_or_sensitive_laundry(raw_text, brand=brand)
            if baby_res:
                _remember_response(baby_res["suggested_reply"], baby_res.get("intent", "zeo_laundry_product_overview"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(baby_res["suggested_reply"]),
                    intent=baby_res.get("intent", "zeo_laundry_product_overview"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=baby_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 1.9. Tư vấn máy giặt cửa trước / ít bọt
        if brand.lower() == "zeo" and is_front_load_washer_inquiry(raw_text) and not is_return_or_claim:
            washer_res = match_front_load_washer(raw_text, brand=brand)
            if washer_res:
                _remember_response(washer_res["suggested_reply"], washer_res.get("intent", "zeo_laundry_product_overview"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(washer_res["suggested_reply"]),
                    intent=washer_res.get("intent", "zeo_laundry_product_overview"),
                    confidence="high",
                    score=0.99,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=washer_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 2. Tư vấn theo nhu cầu khách hàng (tiết kiệm, thơm lâu, sạch sâu, dịu nhẹ)
        need_type = _detect_need_choice(norm_text)
        if brand.lower() == "zeo" and need_type and not is_return_or_claim:
            need_res = match_need_preference(need_type, brand=brand)
            if need_res:
                _remember_response(need_res["suggested_reply"], need_res.get("intent", "need_consultation"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(need_res["suggested_reply"]),
                    intent=need_res.get("intent", "need_consultation"),
                    confidence="high",
                    score=0.98,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=need_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 4. Bán chạy & Mới ra mắt (bao gồm lọc theo danh mục vd: nước rửa chén nào bán chạy)
        if brand.lower() == "zeo" and (is_bestseller_inquiry(raw_text) or is_new_arrival_inquiry(raw_text)) and not is_return_or_claim:
            if is_bestseller_inquiry(raw_text):
                bs_res = match_best_sellers(raw_text, brand=brand)
            else:
                bs_res = match_new_arrivals(raw_text, brand=brand)
            if bs_res:
                _remember_response(bs_res["suggested_reply"], bs_res.get("intent", "bestsellers"), "browsing_catalog")
                return ChatPipelineResponse(
                    answer=_prettify_answer(bs_res["suggested_reply"]),
                    intent=bs_res.get("intent", "bestsellers"),
                    confidence="high",
                    score=0.98,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=bs_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # 5. Shopee Link & Product Matcher
        shopee_match = None
        if is_shopee_inquiry(raw_text) and not is_return_or_claim:
            if reference_resolution.get("resolved"):
                shopee_match = match_shopee_product_reference(reference_resolution, brand=brand)
            if not shopee_match:
                shopee_query = (
                    reference_resolution.get("resolved_query", raw_text)
                    if reference_resolution.get("resolved")
                    else raw_text
                )
                shopee_match = match_shopee_product(shopee_query, brand=brand)
            if shopee_match:
                matched_product = shopee_match.get("matched_product")
                _remember_response(
                    shopee_match["suggested_reply"],
                    shopee_match.get("intent", "shopee_product_link"),
                    "browsing_catalog",
                    products_shown=[matched_product] if isinstance(matched_product, dict) else None,
                )
                return ChatPipelineResponse(
                    answer=_prettify_answer(shopee_match["suggested_reply"]),
                    intent=shopee_match.get("intent", "shopee_product_link"),
                    confidence="high",
                    score=0.95,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=shopee_match.get("shopee_url") or shopee_match.get("matched_product", {}).get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # Follow-up Technology & More info (Loại trừ fact đã trả lời trước đó)
        context_key = _active_product_context_key(conversation_state, previous_intent)
        covered_facts = conversation_state.get("covered_fact_ids") or []

        if context_key and _detect_contextual_technology_request(norm_text):
            msg, response_intent, source_intent = await _build_contextual_more_info_answer(
                brand,
                context_key,
                only_technology=True,
                covered_facts=covered_facts,
            )
            if msg:
                _remember_response(msg, response_intent, "browsing_catalog", source_id=source_intent)
                return _fast_response(msg, response_intent, brand, start_time, lead_stage="browsing_catalog")

        if context_key and _detect_vague_more_followup(norm_text):
            msg, response_intent, source_intent = await _build_contextual_more_info_answer(
                brand,
                context_key,
                only_technology=False,
                covered_facts=covered_facts,
            )
            if msg:
                _remember_response(msg, response_intent, "browsing_catalog", source_id=source_intent)
                return _fast_response(msg, response_intent, brand, start_time, lead_stage="browsing_catalog")

        proof_intent = _detect_proof_or_certification_intent(norm_text, brand, previous_intent)
        if proof_intent:
            return await _sheet_response_remember(proof_intent, stage="browsing_catalog")

        contact_intent = _detect_contact_intent(norm_text, brand)
        if contact_intent:
            return await _sheet_response_remember(
                contact_intent,
                stage="browsing_catalog",
                unavailable_intent="company_contact_information_unavailable",
                unavailable_answer=(
                    f"Dạ hiện dữ liệu chưa có số hotline chính thức của {brand_display} để mình báo chắc chắn. "
                    "Bạn để lại số điện thoại hoặc nhu cầu, admin sẽ kiểm tra và phản hồi thông tin liên hệ chính xác nha."
                ),
            )

        company_overview_intent = _detect_company_overview_intent(norm_text, brand)
        if company_overview_intent:
            return await _sheet_response_remember(company_overview_intent, stage="browsing_catalog")

        address_intent = _detect_address_intent(norm_text, brand)
        if address_intent:
            return await _sheet_response_remember(address_intent, stage="browsing_catalog")

        official_channel_intent = _detect_official_channel_request(norm_text)
        if official_channel_intent and not _is_internal_content_request(norm_text):
            msg = (
                f"Dạ hiện hệ thống kiến thức của {brand_display} chưa có link chính thức cho kênh bạn vừa hỏi. "
                "Để tránh gửi nhầm link giả, bạn có thể dùng website chính thức hoặc để lại nhu cầu, admin sẽ kiểm tra và gửi đúng kênh chính thức nha."
            )
            return _fast_response(msg, official_channel_intent, brand, start_time, lead_stage="browsing_catalog")

        if reference_resolution.get("references_previous_turn"):
            resolved_product = reference_resolution.get("product", "")
            explicit_cfc_price_query = brand.lower() == "cfc" and bool(
                re.search(r"\b(npk|bao|kg|chuyen lua|phan bon|huu co)\b", norm_text)
            )
            if (
                reference_resolution.get("resolved")
                and _has_price_signal(norm_text)
                and not explicit_cfc_price_query
                and not any(k in norm_text for k in ["gia si", "mua si", "lay si", "chinh sach si", "chiet khau", "dai ly"])
            ):
                msg = (
                    f"Dạ mình hiểu bạn đang hỏi giá của {resolved_product}. "
                    "Hiện hệ thống chưa có giá chính xác cho sản phẩm/nhóm này nên mình không tự báo giá để tránh sai. "
                    "Bạn gửi thêm quy cách cần mua hoặc số điện thoại/khu vực, admin sẽ kiểm tra báo giá đúng cho mình nha."
                )
                return _fast_response_remember(msg, "contextual_price_unverified", stage="browsing_catalog", fallback_reason="NO_KNOWLEDGE")

            if reference_resolution.get("resolved") and _looks_like_availability_request(norm_text):
                msg = (
                    f"Dạ mình hiểu bạn đang hỏi {resolved_product} còn hàng không. "
                    "Hiện hệ thống chat chưa có dữ liệu tồn kho realtime, nên mình chưa xác nhận chắc được. "
                    "Bạn để lại số điện thoại/khu vực hoặc nhắn quy cách cần mua, admin sẽ kiểm tra tồn kho chính xác giúp mình nha."
                )
                return _fast_response_remember(msg, "contextual_availability_unverified", stage="collecting_contact", fallback_reason="NO_KNOWLEDGE")

            if reference_resolution.get("resolved") and _looks_like_shipping_request(norm_text):
                shipping_intent = "nationwide_shipping_no_cod" if brand.lower() == "zeo" else "shipping_methods"
                item = await get_faq_by_intent(brand, shipping_intent)
                sheet_answer = item.get("answer", "").strip()
                if sheet_answer:
                    msg = f"Dạ mình đang hiểu bạn hỏi giao hàng cho {resolved_product}.\n\n{sheet_answer}"
                    _remember_response(msg, "contextual_shipping", "browsing_catalog", source_id=item.get("source_id", ""))
                    return _fast_response(msg, "contextual_shipping", brand, start_time, lead_stage="browsing_catalog")
            if reference_resolution.get("resolved") and not (_has_price_signal(norm_text) or _looks_like_availability_request(norm_text) or _looks_like_shipping_request(norm_text)):
                resolved_intent = reference_resolution.get("product_intent")
                if resolved_intent and (resolved_intent.endswith("_overview") or "overview" in resolved_intent or "technology" in resolved_intent or "features" in resolved_intent or "usp" in resolved_intent or "type" in resolved_intent or "liquid" in resolved_intent or "cleaner" in resolved_intent):
                    return await _sheet_response_remember(resolved_intent, stage="browsing_catalog")

            if not reference_resolution.get("resolved") and (
                _has_price_signal(norm_text) or _looks_like_availability_request(norm_text) or _looks_like_shipping_request(norm_text)
            ):
                msg = (
                    "Dạ bạn đang hỏi sản phẩm/nhóm nào trong danh sách vừa rồi ạ? "
                    "Bạn nhắn tên sản phẩm hoặc số thứ tự như số 1, số 2 để mình kiểm tra đúng thông tin nha."
                )
                return _fast_response_remember(msg, "context_reference_clarify", stage="browsing_catalog", fallback_reason="AMBIGUOUS_INTENT")

        if _detect_contextual_dosage_followup(norm_text, previous_intent) or _detect_usage_safety_gap(norm_text, brand):
            if brand == "cfc":
                usage_intent = "cfc_dosage_usage_review"
                return await _sheet_response_remember(usage_intent, stage="collecting_contact")
            msg = (
                "Dạ phần liều lượng/cách dùng hoặc tình huống an toàn cần kiểm tra theo đúng sản phẩm và hướng dẫn trên bao bì. "
                "Hiện hệ thống chưa có đủ dữ liệu để mình tự hướng dẫn chi tiết. Bạn gửi tên sản phẩm hoặc số điện thoại, admin sẽ tư vấn chính xác hơn nha."
            )
            return _fast_response_remember(msg, "zeo_usage_safety_review", stage="collecting_contact", fallback_reason="MISSING_SLOT")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.4.8: SPECIFIC SUB-BRAND & PRODUCT INTENT (< 15ms)
        # ─────────────────────────────────────────────────────────────
        specific_product_intent = _detect_specific_product_intent(norm_text, brand)
        if specific_product_intent and not (_has_price_signal(norm_text) or any(k in norm_text for k in ["ship", "giao hang", "phi", "doi tra", "bao hanh", "loi", "hong", "nhap", "si", "dai ly", "lay si", "gia si"])):
            return await _sheet_response_remember(specific_product_intent, stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.4.9: PRODUCT GROUP VIEW INQUIRY (< 15ms)
        # ─────────────────────────────────────────────────────────────
        product_group_intent = _detect_product_group_intent(norm_text, brand)
        if product_group_intent and not (_has_price_signal(norm_text) or any(k in norm_text for k in ["ship", "giao hang", "phi", "doi tra", "bao hanh", "loi", "hong", "nhap", "si", "dai ly", "lay si", "gia si"])):
            return await _sheet_response_remember(product_group_intent, stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.5: PROMOTIONS, DEALS & VOUCHERS (< 15ms)
        # ─────────────────────────────────────────────────────────────
        from shopee_matcher import is_promotion_inquiry, match_promotions_and_deals
        if is_promotion_inquiry(raw_text):
            promo_res = match_promotions_and_deals(raw_text, brand=brand)
            if promo_res:
                return ChatPipelineResponse(
                    answer=_prettify_answer(promo_res["suggested_reply"]),
                    intent="promotion_deals",
                    confidence="high",
                    score=0.96,
                    brand=brand.upper(),
                    has_phone=has_phone,
                    phone=phone,
                    area=area,
                    lead_stage=lead_stage,
                    shopee_url=promo_res.get("shopee_url"),
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )

        # ─────────────────────────────────────────────────────────────
        # PATH 3.6: HOTLINE, WEBSITE & PRICE INQUIRY (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"(website|web site|trang web|link website|link web|xin link website|xin link web|co link website|co link web|zeo vn|zeo\.vn|cfc web|co bay web|cfccobay|cfc co bay)\b", norm_text):
            website_intent = "company_website" if brand.lower() == "zeo" else "cfc_company_website"
            return await _sheet_response_remember(website_intent, stage="browsing_catalog")

        if re.search(r"(gia(?: .{1,80})? bao nhieu|bao nhieu tien|bang gia|xin gia|gia ban|gia ca|nhieu tien|bao gia)\b", norm_text) and not any(k in norm_text for k in ["ship", "phi", "van chuyen", "cuoc"]):
            price_intent = "zeo_price_inquiry_general" if brand.lower() == "zeo" else "cfc_price_unverified"
            return await _sheet_response_remember(price_intent, stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.7: WHOLESALE & DISTRIBUTOR INQUIRY (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"\b(can nhap|muon nhap|nhap hang|nhap lo|nhap ve|nhap dai ly|lay si|muon lam dai ly|dang ky dai ly|phan phoi|chinh sach si|nhap so luong lon|kinh doanh zeo|dai li|gia si|co gia si|mua si|lay gia si)\b", norm_text):
            wholesale_intent = "wholesale_inquiry" if brand.lower() == "zeo" else "wholesale_dealer"
            # Nếu khách nêu rõ dòng sản phẩm cụ thể muốn nhập, cá nhân hóa câu trả lời tư vấn sỉ 5 sao
            prod_hints = []
            if "oplus" in norm_text: prod_hints.append("Oplus")
            if "pano" in norm_text: prod_hints.append("PANO")
            if "zeo" in norm_text: prod_hints.append("ZeO")
            if "rua chen" in norm_text or "rua bat" in norm_text: prod_hints.append("Nước rửa chén")
            elif "bot giat" in norm_text: prod_hints.append("Bột giặt")
            elif "nuoc giat" in norm_text: prod_hints.append("Nước giặt")
            elif "lau san" in norm_text: prod_hints.append("Nước lau sàn")

            if prod_hints and brand.lower() == "zeo":
                target_name = " ".join(prod_hints)
                custom_reply = (
                    f"Dạ đối với nhu cầu nhập sỉ / mở đại lý dòng **{target_name}**, công ty có chính sách chiết khấu rất tốt và hỗ trợ giao hàng tận nơi ạ!\n\n"
                    f"⭐️ Bạn vui lòng để lại **Số điện thoại** và **Khu vực (Quận/Huyện, Tỉnh/Thành)**, chuyên viên kinh doanh khu vực sẽ liên hệ gửi bảng giá sỉ và chính sách phân phối ngay nhé!\n\n"
                    f"👉 Nếu bạn cần mua lẻ trải nghiệm trước, bạn có thể tham khảo trực tiếp trên Shopee Mall: https://shopee.vn/zeovietnamofficial (hỗ trợ Freeship Extra nha)."
                )
                return _fast_response_remember(custom_reply, "wholesale_inquiry", stage="collecting_contact")

            return await _sheet_response_remember(wholesale_intent, stage="collecting_contact")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.7.6: GENERAL SHIPPING & FREESHIP INQUIRY (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"\b(shop co ship|co ship khong|co giao hang khong|ship tinh khong|co ship toan quoc|co freeship|freeship khong|phi ship bao nhieu|cuoc van chuyen|thoi gian giao hang)\b", norm_text) and not is_return_or_claim:
            if re.search(r"\b(freeship|phi ship|cuoc van chuyen|thoi gian giao)\b", norm_text):
                ship_intent = "shipping_time_and_fee" if brand.lower() == "zeo" else "cfc_delivery_time"
            else:
                ship_intent = "nationwide_shipping_no_cod" if brand.lower() == "zeo" else "shipping_methods"
            return await _sheet_response_remember(ship_intent, stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.7.7: CORPORATE INVOICE & VAT SUPPORT (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"\b(hoa don do|hoa don vat|xuat vat|xuat hoa don|vat khong|vat ko|hoa don tai chinh|mst|ma so thue)\b", norm_text):
            invoice_intent = "corporate_invoice_support"
            return await _sheet_response_remember(invoice_intent, stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.8: GENERAL CATALOG OVERVIEW INQUIRY (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"(cac san pham|nhung san pham|danh muc san pham|co san pham gi|co san pham nao|san pham nao|co nhung gi|co nhung loai nao|cac dong san pham|dong san pham|co dong nao|co nhom nao|gioi thieu san pham|hoi ve cac san pham|ban nhung gi|nhom san pham|mat hang nao|co phan bon gi|phan bon gi|phan bon nao|cac loai phan bon)", norm_text) and not any(k in norm_text for k in ["doi tra", "doi", "tra", "bao hanh", "chinh sach", "loi", "hong", "hoan tien"]):
            catalog_intent = "zeo_product_catalog_overview" if brand.lower() == "zeo" else "product_lines"
            catalog_item = await get_faq_by_intent(brand, catalog_intent)
            catalog_reply = catalog_item.get("answer", "").strip()
            if catalog_reply:
                _remember_response(catalog_reply, catalog_intent, "browsing_catalog", source_id=catalog_item.get("source_id", ""))
                return _fast_response(catalog_reply, catalog_intent, brand, start_time, lead_stage="browsing_catalog")

            fallback_msg = (
                "Dạ hiện danh mục sản phẩm chưa tải được từ hệ thống kiến thức. "
                "Bạn nhắn rõ nhóm sản phẩm muốn xem, hoặc admin sẽ kiểm tra lại dữ liệu giúp mình nha."
            )
            return _fast_response_remember(fallback_msg, "catalog_overview_unavailable", stage="browsing_catalog", fallback_reason="NO_KNOWLEDGE")

        # ─────────────────────────────────────────────────────────────
        # PATH 3.9: OPENING HOURS (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"(mo cua|dong cua|may gio|gio mo cua|gio lam viec|cuoi tuan|mo cua luc|lam viec den may gio)\b", norm_text) and not (_has_price_signal(norm_text) or any(k in norm_text for k in ["phi", "doi tra"])):
            hours_intent = "shop_opening_hours" if brand.lower() == "zeo" else "opening_hours"
            return await _sheet_response_remember(hours_intent)

        # ─────────────────────────────────────────────────────────────
        # PATH 3.10: POLICY & FAST-PATHS (< 15ms)
        # ─────────────────────────────────────────────────────────────
        if re.search(r"(quy trinh doi tra|cac buoc doi tra|lam sao de doi tra)\b", norm_text):
            if brand.lower() == "zeo":
                return await _sheet_response_remember("return_process", stage="browsing_catalog")

        if re.search(r"(chinh sach doi tra|thoi han doi tra|doi tra nhu the nao|duoc doi tra khong|doi tra ap dung|kenh nao duoc doi tra)\b", norm_text):
            if brand.lower() == "zeo":
                return await _sheet_response_remember("return_policy_scope", stage="browsing_catalog")

        # ─────────────────────────────────────────────────────────────
        # PATH 5: HYBRID SEMANTIC SEARCH (IN-MEMORY LEXICAL + REDIS VECTOR) (< 5ms)
        # ─────────────────────────────────────────────────────────────
        rag_query = reference_resolution.get("resolved_query") if reference_resolution.get("resolved") else raw_text
        rag_result = await semantic_search(
            query=rag_query or raw_text,
            brand=brand,
            top_k=10,
            exclude_fact_ids=covered_facts,
        )
        best_score = rag_result.get("score", 0.0)
        intent = rag_result.get("intent", "general_faq")
        raw_answer = rag_result.get("answer", "")
        retrieval_method = rag_result.get("retrieval_method", "redis_vector_knn")

        # ─────────────────────────────────────────────────────────────
        # PATH 6: SEMANTIC ANCHOR GUARDRAILS (Chống Bắt Nhầm Lạc Đề)
        # ─────────────────────────────────────────────────────────────
        def _check_intent_guardrails(target_intent: str, query_norm: str) -> bool:
            """Kiểm tra từ khóa neo bắt buộc để tránh gán nhầm câu hỏi không liên quan."""
            # Ship hỏa tốc 2 giờ / express chưa được verify
            if "shipping" in target_intent and any(k in query_norm for k in ["hoa toc", "2 gio", "2h", "express"]):
                return False
            # Câu hỏi về chi nhánh / cửa hàng / dung tích chưa có dữ liệu
            if any(k in query_norm for k in ["chi nhanh", "cua hang", "showroom", "20 lit", "20l", "50 lit", "100 lit"]):
                return False
            # 1. Đổi trả / Bảo hành
            if "return" in target_intent or "policy" in target_intent or "warranty" in target_intent:
                return bool(re.search(r"\b(doi|tra|loi|hong|bao hanh|hoan tien|rach|be|vo|khieu nai)\b", query_norm))
            # 2. Thanh toán / Chuyển khoản
            if ("payment" in target_intent or "cod_payment" in target_intent) and "shipping" not in target_intent:
                return bool(re.search(r"\b(thanh toan|chuyen khoan|momo|tien mat|cod|ngan hang|stk|tra sau)\b", query_norm))
            # 3. Giờ mở cửa
            if "opening_hours" in target_intent or "hours" in target_intent:
                return bool(re.search(r"\b(gio|mo cua|dong cua|may gio|cuoi tuan|thoi gian)\b", query_norm))
            # 4. Địa chỉ
            if "address" in target_intent or "location" in target_intent:
                if _detect_company_overview_intent(query_norm, brand):
                    return False
                return bool(re.search(r"\b(dia chi|o dau|nha may|tru so|van phong|tai dau)\b", query_norm))
            # 5. Đại lý sỉ
            if "wholesale" in target_intent or "dealer" in target_intent:
                return bool(re.search(r"\b(si|dai ly|nhap hang|phan phoi|so luong lon|hop tac)\b", query_norm))
            # 6. Đặt hàng trực tiếp
            if "order_request" in target_intent:
                return bool(re.search(r"\b(dat hang|chot don|lay 1|lay 2|lay 3|mua 1|mua 2|mua 3|cho 1|cho 2|cho minh 1|cho minh 2|toi muon 2kg|toi muon mua 2kg)\b", query_norm))
            # 7. Thông tin liên hệ / Hotline
            if "contact" in target_intent or "hotline" in target_intent:
                return _has_company_contact_signal(query_norm)
            # 8. Tẩy Toilet / Bồn cầu
            if target_intent == "zeo_toilet_cleaner":
                return bool(re.search(r"\b(toilet|bon cau|be phot|men su|wc|con vit)\b", query_norm))
            # 9. Lau Kính
            if target_intent == "zeo_glass_cleaner":
                return bool(re.search(r"\b(kinh|guong|man hinh)\b", query_norm))
            # 10. Lau Sàn
            if target_intent == "zeo_floor_cleaner_product_overview":
                return bool(re.search(r"\b(lau san|tay san|san nha|lau nha)\b", query_norm))
            # 11. Nước rửa chén Zif
            if target_intent == "zeo_zif_dishwashing_liquid":
                return bool(re.search(r"\b(zif|rua chen|rua bat|chen dia)\b", query_norm))
            if "tiktok" in target_intent or "zalo" in target_intent:
                return _is_internal_content_request(query_norm)
            return True

        is_guardrail_passed = _check_intent_guardrails(intent, norm_text)

        # ─────────────────────────────────────────────────────────────
        # PATH 7: TIERED RESPONSE SELECTION
        # ─────────────────────────────────────────────────────────────
        confidence = "low"
        final_answer = ""
        fallback_reason = ""

        cfc_source_present = bool(rag_result.get("source_id"))
        if best_score >= 0.65 and is_guardrail_passed and (brand != "cfc" or cfc_source_present):
            confidence = "high"
            final_answer = raw_answer
            if brand != "cfc" and rag_result.get("answer_mode") == "rewrite":
                synthesized = await synthesize_cskh_answer(
                    user_query=raw_text,
                    brand=brand,
                    retrieved_facts=raw_answer,
                    conversation_summary=conversation_state.get("conversation_summary", ""),
                    chat_history=_sanitized_chat_history(conversation_state),
                    timeout=2.0,
                )
                if synthesized and len(synthesized) >= 20:
                    final_answer = synthesized
        elif best_score >= 0.50 and is_guardrail_passed and (brand != "cfc" or cfc_source_present):
            confidence = "medium"
            final_answer = raw_answer
            if brand != "cfc" and rag_result.get("answer_mode") == "rewrite":
                synthesized = await synthesize_cskh_answer(
                    user_query=raw_text,
                    brand=brand,
                    retrieved_facts=raw_answer,
                    conversation_summary=conversation_state.get("conversation_summary", ""),
                    chat_history=_sanitized_chat_history(conversation_state),
                    timeout=2.0,
                )
                if synthesized and len(synthesized) >= 20:
                    final_answer = synthesized
        else:
            confidence = "low"
            brand_display = "ZeO Vietnam" if brand.lower() == "zeo" else "CFC Cò Bay"

            purchase_signal = _has_price_signal(norm_text) or bool(
                re.search(r"(^|\s)(mua|dat|chai|lit|kg)(\s|$)", norm_text)
                or re.search(r"(bao phan|\d+\s*bao)", norm_text)
            )

            # Nếu khách đã nói rõ nhóm ngành sản phẩm (nước rửa chén, giặt giũ...) thì ưu tiên trả về danh mục nhóm đó
            detected_group = _detect_product_group_intent(norm_text, brand)
            if detected_group:
                group_item = await get_faq_by_intent(brand, detected_group)
                if group_item.get("answer"):
                    final_answer = group_item["answer"].strip()
                    intent = detected_group
                    confidence = "medium"
            elif purchase_signal and not any(w in norm_text for w in ["co nhung", "cac san pham", "san pham nao", "dong san pham", "gioi thieu"]):
                fallback_intent = "zeo_price_request_needs_product" if brand.lower() == "zeo" else "cfc_price_unverified"
                fallback_item = await get_faq_by_intent(brand, fallback_intent)
                final_answer = fallback_item.get("answer", "").strip() or (
                    f"Dạ hiện dữ liệu chưa đủ để báo chính xác. Bạn nhắn rõ tên sản phẩm và nhu cầu cụ thể, "
                    f"hoặc gửi số điện thoại/khu vực để admin {brand_display} kiểm tra và phản hồi nha."
                )
                lead_stage = "browsing_catalog"
                fallback_reason = "MISSING_ENTITY"
            elif any(w in norm_text for w in ["dai ly", "si", "nhap", "hop tac", "npp", "phan phoi"]):
                fallback_intent = "wholesale_inquiry" if brand.lower() == "zeo" else "wholesale_dealer"
                fallback_item = await get_faq_by_intent(brand, fallback_intent)
                final_answer = fallback_item.get("answer", "").strip() or (
                    f"Dạ bạn gửi giúp mình số điện thoại và khu vực dự kiến kinh doanh. "
                    f"Admin {brand_display} sẽ kiểm tra thông tin phù hợp và phản hồi chính xác nha."
                )
                lead_stage = "collecting_contact"
                fallback_reason = "MISSING_SLOT"
            elif brand.lower() == "cfc":
                final_answer = (
                    "Dạ mình chưa tìm thấy câu trả lời có nguồn phù hợp trong Knowledge CFC cho yêu cầu này. "
                    "Để tránh trả lời sai, mình không tự suy đoán. Bạn nói rõ hơn tên sản phẩm/công thức, cây trồng, "
                    "khu vực hoặc mã đơn; nếu đây là dữ liệu tồn kho, đơn hàng, tích điểm hay chiết khấu thì admin cần kiểm tra trên hệ thống nghiệp vụ ạ."
                )
                confidence = "medium"
                intent = "cfc_grounded_fallback"
                lead_stage = "collecting_contact"
                fallback_reason = "NO_GROUNDED_KNOWLEDGE"
                asyncio.create_task(notify_admin_unanswered(
                    brand=brand,
                    query=raw_text,
                    sender_id=sender_id,
                    score=best_score,
                ))
            else:
                # Thử cho Agent CSKH suy luận dựa trên câu hỏi và facts từ hệ thống
                ai_attempt = await reason_and_answer_cskh(
                    user_query=raw_text,
                    brand=brand,
                    retrieved_facts=raw_answer if raw_answer else "",
                    conversation_summary=conversation_state.get("conversation_summary", ""),
                    chat_history=_sanitized_chat_history(conversation_state),
                    timeout=2.5,
                )
                if ai_attempt and len(ai_attempt) >= 20:
                    final_answer = ai_attempt
                    confidence = "medium"
                    intent = "ai_assisted_cskh_reply"
                    lead_stage = "browsing_catalog"
                else:
                    final_answer = (
                        f"Dạ câu hỏi này mình chưa có sẵn thông tin chính xác trong hệ thống. "
                        f"Bạn có thể nói rõ hơn nhu cầu (như mua hàng, xem sản phẩm hay cần hỗ trợ đơn hàng) để mình hỗ trợ đúng trọng tâm nhé ạ! "
                        f"Hoặc bạn để lại số điện thoại để admin liên hệ giải đáp cho mình nha."
                    )
                    lead_stage = "collecting_contact"
                    fallback_reason = "NO_KNOWLEDGE"
                    asyncio.create_task(notify_admin_unanswered(brand=brand, query=raw_text, sender_id=sender_id, score=best_score))

        # ─────────────────────────────────────────────────────────────
        # ASYNC SAVE SESSION & CHAT HISTORY
        # ─────────────────────────────────────────────────────────────
        final_answer = _prettify_answer(final_answer)
        final_intent = intent if confidence in {"high", "medium"} else "unanswered_query"
        final_state = _build_next_conversation_state(
            conversation_state,
            brand=brand,
            user_message=raw_text,
            bot_reply=final_answer,
            intent=final_intent,
            lead_stage=lead_stage,
            query_entities=query_entities,
            reference_resolution=reference_resolution,
            source_id=rag_result.get("source_id", ""),
            state_patch=state_patch,
        )
        trace = {
            "normalized_text": norm_text,
            "rag_query": rag_query,
            "matched_intent": intent,
            "final_intent": final_intent,
            "score": best_score,
            "vector_score": rag_result.get("vector_score"),
            "rerank_adjustment": rag_result.get("rerank_adjustment"),
            "source_id": rag_result.get("source_id", ""),
            "guardrail_passed": is_guardrail_passed,
            "confidence": confidence,
            "retrieval_method": retrieval_method,
            "fallback_reason": fallback_reason,
            "reference": {
                "used": bool(reference_resolution.get("references_previous_turn")),
                "resolved": bool(reference_resolution.get("resolved")),
                "reason": reference_resolution.get("reason", ""),
                "product": reference_resolution.get("product", ""),
            },
            "query_entities": query_entities,
            "query_plan": query_plan_dict,
            "grounding": assess_grounding(
                intent=final_intent,
                source_id=rag_result.get("source_id", ""),
                fallback_reason=fallback_reason,
            ).to_dict(),
        }
        _local_session_cache[session_key] = {
            "revision": int(existing_session.get("revision") or 0),
            "last_user_message": raw_text,
            "last_bot_reply": final_answer,
            "last_intent": final_intent,
            "lead_stage": lead_stage,
            "customer_phone": phone,
            "customer_location": area,
            "conversation_state": final_state,
            "last_trace": trace,
        }

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return ChatPipelineResponse(
            answer=final_answer,
            intent=final_intent,
            confidence=confidence,
            score=best_score,
            brand=brand.upper(),
            has_phone=has_phone,
            phone=phone,
            area=area,
            lead_stage=lead_stage,
            fallback_reason=fallback_reason,
            latency_ms=elapsed_ms,
        )


def _response_to_dict(response: ChatPipelineResponse) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return response.dict()


async def _finalize_pipeline_response(req: ChatPipelineRequest, response: ChatPipelineResponse) -> None:
    """Make every return branch update RAM and Redis before the API responds."""
    brand = req.brand.lower()
    sender_id = req.sender_id.strip()
    has_location = bool(
        req.latitude is not None
        and req.longitude is not None
        and -90 <= req.latitude <= 90
        and -180 <= req.longitude <= 180
    )
    raw_text = (req.text or "").strip() or ("Gửi vị trí hiện tại" if has_location else "")
    session_key = f"{brand}:session:messenger:{sender_id}"
    history_key = f"{brand}:history:messenger:{sender_id}"
    redis_client = await get_redis()

    snapshot = _local_session_cache.get(session_key) or {}
    if snapshot.get("last_user_message") != raw_text:
        if not snapshot:
            snapshot = await load_json(redis_client, session_key)
        previous_state = _load_conversation_state(snapshot, brand)
        norm_text = _normalize_vn(raw_text)
        query_entities = _extract_query_entities(norm_text, brand)
        reference_resolution = _resolve_reference(raw_text, norm_text, previous_state)
        next_state = _build_next_conversation_state(
            previous_state,
            brand=brand,
            user_message=raw_text,
            bot_reply=response.answer,
            intent=response.intent,
            lead_stage=response.lead_stage,
            query_entities=query_entities,
            reference_resolution=reference_resolution,
            source_id="",
            state_patch={
                "confirmed_slots": {
                    **({"phone": response.phone} if response.phone else {}),
                    **({"area": response.area} if response.area else {}),
                    **({
                        "location": {
                            "latitude": req.latitude,
                            "longitude": req.longitude,
                            "source": "messenger_location",
                        }
                    } if has_location else {}),
                }
            },
        )
        query_plan = build_query_plan(
            raw_text=raw_text,
            norm_text=norm_text,
            brand=brand,
            query_entities=query_entities,
            reference_resolution=reference_resolution,
            conversation_state=previous_state,
        )
        snapshot = {
            "last_user_message": raw_text,
            "last_bot_reply": response.answer,
            "last_intent": response.intent,
            "lead_stage": response.lead_stage,
            "customer_phone": response.phone,
            "customer_location": response.area,
            "conversation_state": next_state,
            "last_trace": {
                "normalized_text": norm_text,
                "query_plan": query_plan.to_dict(),
                "source_id": "",
                "confidence": response.confidence,
                "score": response.score,
                "fallback_reason": response.fallback_reason or "",
                "finalized_by": "pipeline_wrapper",
            },
        }
        _local_session_cache[session_key] = snapshot

    now_str = datetime.now(timezone.utc).isoformat()
    revision = int(snapshot.get("revision") or 0) + 1
    snapshot["revision"] = revision
    snapshot["sender_id"] = sender_id
    snapshot["brand"] = brand.upper()
    snapshot["last_seen_at"] = now_str
    conversation_state = snapshot.get("conversation_state") or {}
    if isinstance(conversation_state, dict):
        conversation_state["state_revision"] = revision

    history_record = {
        "message_id": str(req.message_id or ""),
        "user_message": raw_text,
        "bot_reply": response.answer,
        "intent": response.intent,
        "trace": snapshot.get("last_trace") or {},
        "timestamp": now_str,
        "revision": revision,
    }
    trace = snapshot.get("last_trace") or {}
    if isinstance(trace, dict) and "grounding" not in trace:
        trace["grounding"] = assess_grounding(
            intent=response.intent,
            source_id=str(trace.get("source_id") or ""),
            fallback_reason=str(response.fallback_reason or ""),
        ).to_dict()
    if not all(callable(getattr(redis_client, name, None)) for name in ("set", "rpush", "ltrim")):
        return
    try:
        await persist_session(
            redis_client,
            session_key=session_key,
            history_key=history_key,
            session_data=snapshot,
            history_record=history_record,
            config=_conversation_store_config,
        )
    except Exception as exc:
        logger.warning("Conversation persistence degraded for %s: %s", session_key, exc)


async def process_chat_pipeline(req: ChatPipelineRequest) -> ChatPipelineResponse:
    """Idempotent public entrypoint around one deterministic pipeline execution."""
    brand = req.brand.lower()
    sender_id = req.sender_id.strip()
    message_id = str(req.message_id or "").strip()
    redis_client = await get_redis()
    decision = await begin_message(redis_client, brand=brand, message_id=message_id)

    if decision.status == "cached" and decision.cached_response:
        cached = dict(decision.cached_response)
        cached.update({"duplicate": True, "idempotency_status": "cached", "message_id": message_id})
        return ChatPipelineResponse(**cached)
    if decision.status == "in_flight":
        return ChatPipelineResponse(
            answer="",
            intent="duplicate_in_flight",
            confidence="high",
            score=1.0,
            brand=brand.upper(),
            duplicate=True,
            idempotency_status="in_flight",
            message_id=message_id,
        )

    try:
        if hasattr(redis_client, "set"):
            async with sender_lease(
                redis_client,
                brand=brand,
                sender_id=sender_id,
                config=_conversation_store_config,
            ):
                response = await _process_chat_pipeline_once(req)
                await _finalize_pipeline_response(req, response)
        else:
            response = await _process_chat_pipeline_once(req)
            await _finalize_pipeline_response(req, response)
        response.message_id = message_id
        response.idempotency_status = "processed" if decision.status == "acquired" else decision.status
        try:
            await complete_message(
                redis_client,
                decision,
                _response_to_dict(response),
                response_ttl_seconds=_idempotency_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Idempotency response cache degraded for %s: %s", message_id, exc)
        return response
    except Exception:
        await release_message(redis_client, decision)
        raise


def _fast_response(answer: str, intent: str, brand: str, start_time: float, lead_stage: str = "new", fallback_reason: str = "") -> ChatPipelineResponse:
    return ChatPipelineResponse(
        answer=_prettify_answer(answer),
        intent=intent,
        confidence="high",
        score=1.0,
        brand=brand.upper(),
        lead_stage=lead_stage,
        fallback_reason=fallback_reason,
        latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
    )


async def _async_save_profile_and_notify(brand: str, sender_id: str, profile: dict, phone: str, area: str, fb_name: str, need: str):
    """Cập nhật Redis profile và gửi thông báo Telegram trong nền."""
    try:
        r = await get_redis()
        customer_key = f"{brand}:customer:messenger:{sender_id}"
        await r.set(customer_key, json.dumps(profile, ensure_ascii=False))
        if phone:
            await notify_new_lead(
                brand=brand,
                phone=phone,
                area=area,
                fb_name=fb_name,
                need=need,
                sender_id=sender_id,
            )
    except Exception as e:
        logger.warning("Error in _async_save_profile_and_notify: %s", e)


async def _async_save_session(
    brand: str,
    sender_id: str,
    user_message: str,
    bot_reply: str,
    intent: str,
    lead_stage: str,
    conversation_state: Optional[dict[str, Any]] = None,
    trace: Optional[dict[str, Any]] = None,
):
    """Lưu session và lịch sử hội thoại trong nền."""
    try:
        r = await get_redis()
        session_key = f"{brand}:session:messenger:{sender_id}"
        history_key = f"{brand}:history:messenger:{sender_id}"
        now_str = datetime.now(timezone.utc).isoformat()

        session_data = {
            "sender_id": sender_id,
            "brand": brand.upper(),
            "last_user_message": user_message,
            "last_bot_reply": bot_reply,
            "last_intent": intent,
            "lead_stage": lead_stage,
            "last_seen_at": now_str,
        }
        if conversation_state:
            active_entities = conversation_state.get("active_entities", {})
            # pyrefly: ignore [no-matching-overload]
            session_data.update({
                "conversation_state": conversation_state,
                "current_product": active_entities.get("product", ""),
                "current_category": active_entities.get("category", ""),
                "last_products_shown": conversation_state.get("last_products_shown", []),
                "covered_fact_ids": conversation_state.get("covered_fact_ids", []),
                "conversation_summary": conversation_state.get("conversation_summary", ""),
                "last_source_id": conversation_state.get("last_source_id", ""),
            })
        if trace:
            # pyrefly: ignore [bad-assignment]
            session_data["last_trace"] = trace
        await r.set(session_key, json.dumps(session_data, ensure_ascii=False))

        msg_record = json.dumps({
            "user_message": user_message,
            "bot_reply": bot_reply,
            "intent": intent,
            "trace": trace or {},
            "timestamp": now_str,
        }, ensure_ascii=False)
        # pyrefly: ignore [not-async]
        await r.rpush(history_key, msg_record)
        # pyrefly: ignore [not-async]
        await r.ltrim(history_key, -50, -1)
    except Exception as e:
        logger.warning("Error in _async_save_session: %s", e)
