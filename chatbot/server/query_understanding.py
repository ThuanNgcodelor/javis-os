"""
query_understanding.py — Deterministic Vietnamese QueryPlan for chatbot routing.

Module này chỉ hiểu câu hỏi và tạo kế hoạch truy xuất. Nó không tạo fact,
không báo giá, không quyết định link/tồn kho. Mọi fact vẫn phải lấy từ
Sheet/Redis/catalog/RAG ở các lớp phía sau.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Optional
import json
import os
from pathlib import Path
# pyrefly: ignore [missing-import]
import redis
import time

_redis_sync_client = None
_DYNAMIC_CROP_TERMS: list[str] = []
_DYNAMIC_PROVINCES: list[str] = []
_DYNAMIC_DISTRICTS: list[str] = []
_LAST_SYNC_TIME: float = 0.0

def _get_redis_sync() -> redis.Redis:
    global _redis_sync_client
    if _redis_sync_client is None:
        try:
            settings_path = Path(__file__).resolve().parent / "settings.json"
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            r_cfg = settings.get("redis", {})
            _redis_sync_client = redis.Redis(
                host=r_cfg.get("host", "127.0.0.1"),
                port=r_cfg.get("port", 6379),
                password=r_cfg.get("password"),
                db=r_cfg.get("db", 0),
                decode_responses=True
            )
        except Exception:
            _redis_sync_client = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    return _redis_sync_client

def _sync_dynamic_lists() -> None:
    global _DYNAMIC_CROP_TERMS, _DYNAMIC_PROVINCES, _DYNAMIC_DISTRICTS, _LAST_SYNC_TIME
    now = time.time()
    if now - _LAST_SYNC_TIME < 300:
        return
    try:
        r = _get_redis_sync()
        # Crop terms from Redis
        crops_raw = r.get("cfc:knowledge:crop_terms")
        if crops_raw:
            try:
                crops_list = json.loads(crops_raw)
                if isinstance(crops_list, dict) and "items" in crops_list:
                    crops_list = crops_list["items"]
                if isinstance(crops_list, list):
                    _DYNAMIC_CROP_TERMS = [str(c).strip().lower() for c in crops_list if str(c).strip()]
            except json.JSONDecodeError:
                _DYNAMIC_CROP_TERMS = [c.strip().lower() for c in str(crops_raw).split(",") if c.strip()]
        
        # Provinces/Districts from AMIS
        amis_locations_raw = r.get("amis:public:sales-locations:active")
        if amis_locations_raw:
            try:
                locations = json.loads(amis_locations_raw)
                if isinstance(locations, dict) and "items" in locations:
                    locations = locations["items"]
                provinces = set()
                districts = set()
                if isinstance(locations, list):
                    for loc in locations:
                        p = str(loc.get("province", "")).strip().lower()
                        d = str(loc.get("district", "")).strip().lower()
                        if p:
                            provinces.add(p)
                        if d:
                            districts.add(d)
                if provinces:
                    _DYNAMIC_PROVINCES = list(provinces)
                if districts:
                    _DYNAMIC_DISTRICTS = list(districts)
            except Exception:
                pass
        
        _LAST_SYNC_TIME = now
    except Exception:
        pass  # Fallback to hardcoded if Redis fails


@dataclass
class QueryPlan:
    original_query: str
    normalized_query: str
    brand: str
    intent: str = "unknown"
    intent_confidence: float = 0.0
    entities: dict[str, Any] = field(default_factory=dict)
    references: dict[str, Any] = field(default_factory=dict)
    attributes: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    needs_context: bool = False
    needs_retrieval: bool = True
    needs_product_tool: bool = False
    rewritten_query: str = ""
    ambiguity_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _ordinal_index(text: str) -> Optional[int]:
    patterns = [
        (1, r"\b(cai|loai|san pham|sp|muc|so|nhom)?\s*(dau tien|thu nhat|so 1|1)\b"),
        (2, r"\b(cai|loai|san pham|sp|muc|so|nhom)?\s*(thu hai|so 2|2)\b"),
        (3, r"\b(cai|loai|san pham|sp|muc|so|nhom)?\s*(thu ba|so 3|3)\b"),
        (4, r"\b(cai|loai|san pham|sp|muc|so|nhom)?\s*(thu tu|so 4|4)\b"),
        (5, r"\b(cai|loai|san pham|sp|muc|so|nhom)?\s*(thu nam|so 5|5)\b"),
    ]
    for idx, pattern in patterns:
        if re.search(pattern, text):
            return idx
    return None


def _detect_attributes(text: str) -> list[str]:
    attrs: list[str] = []
    if (
        re.search(r"\b(gia|bao nhieu|bao nhieu tien|nhiu|mac nhat|dat nhat|re nhat)\b", text)
        or re.search(r"\b(duoi|tren|tam|khoang|gan)\s*\d", text)
    ):
        attrs.append("price")
    if re.search(r"\b(link|shopee|mua online|dat mua|gian hang)\b", text):
        attrs.append("link")
    if re.search(r"\b(con hang|co san|ton kho|trong kho|het hang|con khong|con ko|con nhieu|co lien|kho con|con loai|con ma|co xuat kho|xuat kho khong|giao lien|giao duoc lien|giao ngay|lay lien)\b", text):
        attrs.append("availability")
    if re.search(r"\b(cach dung|huong dan|su dung|dung sao|dung nhu the nao|lieu luong|cach bon|nen bon|cong thuc nao)\b", text):
        attrs.append("usage")
    if re.search(r"\b(thom|mui|huong|luu huong)\b", text):
        attrs.append("fragrance")
    if re.search(r"\b(an da|hai da|troc da|di ung|da nhay cam|em be|tre nho|so sinh)\b", text):
        attrs.append("safety")
    if re.search(r"\b(cua truoc|cua ngang|it bot|trao bot|may giat)\b", text):
        attrs.append("compatibility")
    if re.search(r"\b(doi tra|tra hang|hoan tien|bao hanh|khieu nai|loi hang)\b", text):
        attrs.append("policy")
    return _unique(attrs)


def _detect_entities(text: str, query_entities: Optional[dict[str, Any]]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    if query_entities:
        for key in ("product", "product_intent", "category"):
            value = query_entities.get(key)
            if value:
                entities[key] = value

    category_terms = {
        "dishwashing": ["rua chen", "rua bat", "chen dia", "zif"],
        "laundry": ["giat", "bot giat", "nuoc giat", "xa vai", "quan ao"],
        "floor_cleaner": ["lau san", "tay san", "san nha", "lau nha"],
        "toilet_cleaner": ["bon cau", "toilet", "wc", "men su", "can voi", "o vang nha tam"],
        "bleach": ["javen", "nuoc tay", "tay quan ao"],
    }
    for category, terms in category_terms.items():
        if _has_any(text, terms):
            entities.setdefault("category", category)
            break

    brands = [b for b in ["zeo", "pano", "oplus", "zif", "cfc", "co bay"] if b in text]
    if brands:
        entities["mentioned_brands"] = brands

    variant_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(kg|g|gram|lit|l|ml)\b", text)
    if variant_match:
        entities["variant"] = variant_match.group(0)

    formula_match = re.search(
        r"\b(?:npk\s+)?(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})(?:\s+(te))?\b",
        text,
    )
    if formula_match:
        entities["formula"] = "-".join(formula_match.group(index) for index in range(1, 4))
        if formula_match.group(4):
            entities["formula"] += " TE"

    order_match = (
        re.search(r"#([a-z0-9\-_]+)", text)
        or re.search(r"\b(?:don hang|ma don|don so|don)\s*(?:so|ma)?\s*[:#]?\s*([a-z0-9\-_]+)", text)
        or re.search(r"\b(0000\d{4}|dh[-_ ]?\d{4,8})\b", text)
    )
    if order_match:
        val = order_match.group(1) if order_match.groups() else order_match.group(0)
        val_clean = val.strip()
        if len(val_clean) >= 4 and not val_clean.startswith(("nay", "giup", "cho", "nha", "shop")):
            entities["order_id"] = val_clean

    acreage_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(hecta|ha|cong)\b", text)
    if acreage_match:
        entities["acreage"] = f"{acreage_match.group(1)} {acreage_match.group(2)}"

    crop_terms = _DYNAMIC_CROP_TERMS if _DYNAMIC_CROP_TERMS else [
        "sau rieng", "lua", "cay an trai", "rau mau", "ca phe", "tieu", "cao su", "dieu",
        "oi", "cay oi", "buoi", "cam", "quyt", "xoai", "mit", "thanh long", "man", "tao",
        "chanh", "tac", "mang cau", "chom chom", "nhan", "khoai lang", "dua hau"
    ]
    crop = next((term for term in crop_terms if re.search(rf"\b{term}\b", text)), "")
    if not crop:
        crop_match = re.search(r"\bcay\s+([a-z0-9\s]+?)(?:,|\.|\s+dien|\s+hecta|\s+ha|\s+o\b|\s+bi\b|\s+giai\b|$)", text)
        if crop_match:
            cand = crop_match.group(1).strip()
            if len(cand) >= 2 and cand not in {"an trai", "nong nghiep", "trong"}:
                crop = cand
    if crop:
        entities["crop"] = crop.replace("cay ", "").strip()

    stage_patterns = [
        "nuoi trai non", "trai non", "ra hoa", "xu ly ra hoa", "dau trai",
        "nuoi trai", "xuong giong", "de nhanh", "lam dong", "dot 1", "dot 2", "dot 3",
    ]
    crop_stage = next((term for term in stage_patterns if re.search(rf"\b{term}\b", text)), "")
    if crop_stage:
        entities["crop_stage"] = crop_stage

    symptom_match = re.search(r"\b(rung hat chuoi|rung trai non|vang la|thoi re|xoan la|cham lon)\b", text)
    if symptom_match:
        entities["symptom"] = symptom_match.group(1)

    dealer_level = re.search(r"\bdai ly cap\s*(\d+)\b", text)
    if dealer_level:
        entities["dealer_level"] = f"cap {dealer_level.group(1)}"

    return entities


def _detect_constraints(text: str) -> dict[str, Any]:
    constraints: dict[str, Any] = {}

    quantity = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(chai|can|tui|goi|thung|hop|bao|bo|tan)\b", text)
    if quantity:
        raw_quantity = quantity.group(1).replace(",", ".")
        numeric_quantity = float(raw_quantity)
        constraints["quantity"] = int(numeric_quantity) if numeric_quantity.is_integer() else numeric_quantity
        constraints["quantity_unit"] = quantity.group(2)

    budget = re.search(r"\b(duoi|tren|tam|khoang|gan)\s*(\d+(?:[.,]\d+)?)\s*(k|nghin|trieu)?\b", text)
    if budget:
        value = float(budget.group(2).replace(",", "."))
        unit = budget.group(3) or ""
        if unit in {"k", "nghin"}:
            value *= 1000
        elif unit == "trieu":
            value *= 1_000_000
        constraints["budget_operator"] = budget.group(1)
        constraints["budget_vnd"] = int(value)

    # Location slots extraction (provinces, districts, wards)
    provinces = _DYNAMIC_PROVINCES if _DYNAMIC_PROVINCES else [
        "can tho", "an giang", "dong thap", "hau giang", "soc trang", "kien giang",
        "vinh long", "tien giang", "ben tre", "tra vinh", "ca mau", "bac lieu",
        "lam dong", "da lat", "tphcm", "ho chi minh", "ha noi", "da nang"
    ]
    for prov in provinces:
        if re.search(rf"\b{prov}\b", text):
            constraints["location"] = prov
            break

    # Districts in Mekong Delta
    districts = _DYNAMIC_DISTRICTS if _DYNAMIC_DISTRICTS else [
        "o mon", "thoi lai", "co do", "vinh thanh", "phong dien",
        "binh thuy", "ninh kieu", "cai rang", "thot not", "cho moi",
        "tri ton", "thoai son", "tan hiep", "giong rieng", "chau thanh"
    ]
    for dist in districts:
        if re.search(rf"\b{dist}\b", text):
            constraints["district"] = dist
            break

    # Wards / communes
    ward_match = re.search(r"\bxa\s+([a-z0-9\s]+?)(?:\s*,|\s+huyen|\s+quan|\s+tinh|\s+co\b|\s+gan\b|\s*$)", text)
    if ward_match:
        constraints["ward"] = ward_match.group(1).strip()

    channels = [channel for channel in ("shopee", "website", "lazada", "facebook", "zalo") if channel in text]
    if channels:
        constraints["channels"] = channels

    negated_brand = re.search(r"\bkhong phai\s+(pano|zeo|oplus|zif|cfc|co bay)\b", text)
    if negated_brand:
        constraints["negated_brands"] = [negated_brand.group(1)]
    corrected_brand = re.search(r"\b(?:y minh la|y toi la|ma la)\s+(pano|zeo|oplus|zif|cfc|co bay)\b", text)
    if corrected_brand:
        constraints["corrected_brand"] = corrected_brand.group(1)
    return constraints


def _detect_intent(text: str, attrs: list[str], entities: dict[str, Any], brand: str) -> tuple[str, float]:
    mentioned = set(entities.get("mentioned_brands", []))
    if brand == "zeo" and re.search(
        r"\b(hotline|tong dai|so dien thoai|so lien he|so cham soc|cham soc khach hang)\b",
        text,
    ):
        return "company_contact_information", 0.94
    if brand == "zeo" and re.search(
        r"\b(nuoc lau san|nuoc lau bep|tay da nang|rua chen|tay bon cau|lau kinh|vien giat|nuoc giat)\b",
        text,
    ):
        return "product_category_query", 0.95
    if brand == "cfc" and re.search(
        r"\b(hop tac xa|htx|30 tan|20 tan|50 tan|100 tan|don hang lon|so luong lon|giam doc kinh doanh|gdkd|thuong luong hop dong|hop dong lon|mua si so luong)\b",
        text,
    ):
        return "cfc_b2b_large_order_request", 0.98
    if brand == "cfc" and (
        re.search(r"\b(von cuc|dong cuc|chay nuoc|rach bao|hong bao|hang loi|hang kem|kem chat luong)\b", text)
        or (re.search(r"\b(khieu nai|doi tra ngay|doi tra gap|tra hang)\b", text) and not re.search(r"\b(chinh sach|quy dinh)\b", text))
    ):
        return "cfc_product_complaint_request", 0.98
    if brand == "cfc" and (
        re.search(r"\b(tien do don hang|kiem tra don hang|kiem tra giup don|kiem tra don|tra cuu don|xe da boc|boc hang xong|da xuat kho chua|tien do xuat kho|van don|giao den dau|giao toi dau|dh[-\s]?\d+)\b", text)
        or (re.search(r"\b(don hang|don so|ma don)\b", text) and not re.search(r"\b(30 tan|20 tan|50 tan|100 tan|hop tac xa|htx)\b", text))
        or entities.get("order_id")
    ):
        return "cfc_order_status_request", 0.97
    if re.search(r"\b(vay tien|cho vay|tin dung|tra gop|vay von|cho muon tien)\b", text):
        return "financial_service_unsupported", 0.98
    if brand == "cfc" and re.search(
        r"\b(bang gia si|gia si|chiet khau quy|muc chiet khau|chinh sach chiet khau|dai ly cap|chiet khau cho dai ly|chiet khau cap)\b",
        text,
    ):
        return "cfc_wholesale_policy_request", 0.97
    if brand == "cfc" and re.search(
        r"\b(tich diem|diem thuong|diem tich luy|hang thanh vien|tai khoan dai ly|uu dai gi ko|uu dai gi khong|uu dai gi chua|uu dai gi|duoc bao nhieu %|tra cuu chiet khau)\b",
        text,
    ):
        return "cfc_loyalty_lookup_request", 0.96
    if brand == "cfc" and (
        ("availability" in attrs and (
            re.search(r"\b(npk|phan|cong thuc|bao|kho|chuyen lua|con loai|con ma|sieu tang truong)\b", text)
            or entities.get("formula")
        ))
        or re.search(r"\b(con hang|lay \d+ tan|co lien khong|trong kho con|kho con|con loai|con ma)\b", text)
    ):
        return "cfc_inventory_request", 0.97
    if re.search(r"\b(thong tin khach hang|khach hang .* la ai|so dien thoai .* cua|con no|no tien|cong no|no bao nhieu|tien no|chua thanh toan)\b", text):
        return "privacy_sensitive_lookup", 0.98
    if _has_any(text, ["tra hang", "doi tra", "hoan tien", "khieu nai"]):
        return "return_policy_or_claim", 0.95
    # Keep the dealer branch safe for short location follow-ups such as
    # "ở đó có không"; build_query_plan computes the same constraints later.
    constraints = _detect_constraints(text) if brand == "cfc" else {}
    if brand == "cfc" and (
        re.search(r"\b(dai ly|nha phan phoi|npp|diem mua|diem ban|cho ban|cho mua|cua hang|mua o dau|giao tan nha|giao tan noi)\b", text)
        or (
            re.search(r"\b(o dau|cho nao|co khong|co ko|gan nhat)\b", text)
            and (constraints.get("district") or constraints.get("ward") or constraints.get("location") or re.search(r"\b(o mon|thoi lai|co do|vinh thanh|dinh mon)\b", text))
        )
    ):
        return "cfc_dealer_location_request", 0.95
    if brand == "cfc" and (
        re.search(r"\b(muon mua|can mua|dat mua|dat hang|lay hang|mua)\b", text)
        and re.search(r"\b\d+(?:[.,]\d+)?\s*(kg|tan|bao|thung)\b", text)
    ):
        return "cfc_purchase_request", 0.97
    if brand == "cfc" and (
        "usage" in attrs
        or entities.get("symptom")
        or entities.get("crop")
        or entities.get("acreage")
        or re.search(r"\b(sau rieng|cay an trai|lua|ca phe|tieu|rau mau|hecta|ha|dien tich|nen bon|bon cong thuc|cong thuc nao|lieu luong|giai doan|bi rung|tu van|ky thuat)\b", text)
    ):
        return "cfc_agronomy_review_request", 0.95
    if brand == "cfc" and re.search(r"\b(gia|bao gia|xin gia|bang gia|bao nhieu tien)\b", text):
        return "cfc_price_unverified", 0.93
    if {"zeo", "pano", "oplus"}.issubset(mentioned) and re.search(r"\b(khac nhau|hay sao|la sao|cung|thuoc|hang|thuong hieu)\b", text):
        return "brand_ecosystem_overview", 0.90
    if _has_any(text, ["bon cau", "toilet", "wc", "men su", "can voi"]) and _has_any(text, ["o vang", "vet o", "tay", "can voi", "sach"]):
        return "cleaning_toilet_stain", 0.93
    if "compatibility" in attrs:
        return "product_compatibility", 0.86
    if len([a for a in attrs if a in {"price", "link", "availability"}]) >= 2:
        return "multi_attribute_product_query", 0.86
    if "price" in attrs:
        if _has_any(text, ["mac nhat", "dat nhat", "cao nhat", "gia chat nhat"]):
            return "price_extreme", 0.92
        return "product_price_query", 0.86
    if "link" in attrs:
        return "product_link_query", 0.84
    if "availability" in attrs:
        return "product_availability_query", 0.84
    if "fragrance" in attrs:
        return "product_fragrance_need", 0.82
    if "safety" in attrs:
        return "product_safety_need", 0.80
    if entities.get("category") or entities.get("product") or entities.get("product_intent"):
        return "product_information_query", 0.72
    if brand == "cfc" and _has_any(text, ["lua", "cay", "phan bon", "npk"]):
        return "agriculture_advisory_query", 0.72
    return "unknown", 0.35


def _build_rewritten_query(original: str, entities: dict[str, Any], references: dict[str, Any], attrs: list[str]) -> str:
    parts: list[str] = []
    product = references.get("product") or entities.get("product") or ""
    if product:
        parts.append(str(product))
    category = entities.get("category") or references.get("category") or ""
    if category and category not in parts:
        parts.append(str(category))
    variant = entities.get("variant")
    if variant:
        parts.append(str(variant))
    parts.extend(attrs)
    return " ".join(parts).strip() or original


def build_query_plan(
    *,
    raw_text: str,
    norm_text: str,
    brand: str,
    query_entities: Optional[dict[str, Any]] = None,
    reference_resolution: Optional[dict[str, Any]] = None,
    conversation_state: Optional[dict[str, Any]] = None,
) -> QueryPlan:
    """Build a fact-free QueryPlan used for routing, tracing and tests."""
    _sync_dynamic_lists()
    reference_resolution = reference_resolution or {}
    conversation_state = conversation_state or {}
    attrs = _detect_attributes(norm_text)
    entities = _detect_entities(norm_text, query_entities)
    constraints = _detect_constraints(norm_text)

    references: dict[str, Any] = {
        "mentions_previous_turn": bool(
            reference_resolution.get("references_previous_turn")
            or re.search(r"\b(cai nay|cai do|san pham do|loai do|loai nay|no|cai kia|muc do|cai dau tien|cai thu|so \d)\b", norm_text)
        ),
        "resolved": bool(reference_resolution.get("resolved")),
    }
    ordinal = _ordinal_index(norm_text)
    if ordinal:
        references["ordinal"] = ordinal
    for key in ("product", "product_intent", "category", "product_id", "shopee_url", "price"):
        if reference_resolution.get(key):
            references[key] = reference_resolution[key]

    intent, confidence = _detect_intent(norm_text, attrs, entities, brand.lower())
    active_goal = conversation_state.get("active_goal") or {}
    active_goal_name = active_goal if isinstance(active_goal, str) else str(active_goal.get("name") or "")
    if (
        brand.lower() == "cfc"
        and active_goal_name
        and re.fullmatch(
            r"(?:(?:la sao|y la sao|sao vay|noi gi vay|la nhu nao|giai thich lai)(?:\s+(?:chua hieu|khong hieu))?|(?:chua hieu|khong hieu)(?:\s+(?:la sao|y la sao))?)",
            norm_text.strip(),
        )
    ):
        intent, confidence = "cfc_clarification_request", 0.96
    if (
        brand.lower() == "cfc"
        and active_goal_name == "agronomy_consultation"
        and intent in {"unknown", "agriculture_advisory_query", "product_information_query"}
        and (
            entities.get("crop")
            or entities.get("crop_stage")
            or entities.get("acreage")
            or re.search(r"\b(dien tich|khu vuc|giai doan|trieu chung|hien tuong)\b", norm_text)
        )
    ):
        intent, confidence = "cfc_agronomy_review_request", 0.94
    needs_context = bool(references.get("mentions_previous_turn") and not references.get("resolved"))
    needs_product_tool = bool(
        brand.lower() == "zeo"
        and (
            intent in {
                "product_price_query",
                "price_extreme",
                "product_link_query",
                "product_availability_query",
                "multi_attribute_product_query",
                "product_fragrance_need",
                "product_safety_need",
                "product_compatibility",
            }
            or entities.get("category") in {"dishwashing", "laundry", "floor_cleaner", "toilet_cleaner", "bleach"}
        )
    )

    plan = QueryPlan(
        original_query=raw_text,
        normalized_query=norm_text,
        brand=brand.lower(),
        intent=intent,
        intent_confidence=confidence,
        entities=entities,
        references=references,
        attributes=attrs,
        constraints=constraints,
        needs_context=needs_context,
        needs_retrieval=not needs_product_tool or intent in {"unknown", "return_policy_or_claim", "agriculture_advisory_query"},
        needs_product_tool=needs_product_tool,
        rewritten_query=_build_rewritten_query(raw_text, entities, references, attrs),
        ambiguity_reason="UNRESOLVED_REFERENCE" if needs_context else "",
    )
    return plan
