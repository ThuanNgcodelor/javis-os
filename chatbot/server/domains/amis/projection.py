"""Build strict public projections from raw AMIS records."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable, Optional

from .config import AmisConfig


FORBIDDEN_PUBLIC_FIELDS = {
    "price",
    "unit_price",
    "unit_price1",
    "unit_price2",
    "unit_price_fixed",
    "purchased_price",
    "unit_cost",
    "price_after_tax",
    "tax",
    "tax_code",
    "sale_order_amount",
    "invoiced_amount",
    "total_summary",
    "tax_summary",
    "discount",
    "discount_summary",
    "to_currency",
    "to_currency_summary",
    "total_receipted_amount",
    "debt",
    "debt_limit",
    "bank_account",
    "identification",
    "passport_number",
    "portal_username",
    "owner_name",
    "related_users",
}

PRICE_TEXT_PATTERNS = (
    re.compile(r"\b(?:giá|đơn giá|thành tiền)\s*[:=-]?\s*\d+", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(?:[.,]\d{3})+\s*(?:₫|vnd|vnđ|đồng|dong)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*(?:₫|vnd|vnđ)\b", re.IGNORECASE),
)

ADDRESS_FIELD_KEYS = {
    "public_address",
    "shipping_address",
    "billing_address",
    "province",
    "district",
    "ward",
    "shipping_province",
    "shipping_district",
    "shipping_ward",
    "billing_province",
    "billing_district",
    "billing_ward",
}


class PublicProjectionError(RuntimeError):
    """Raised when a public projection violates the allowlist contract."""


def fold_text(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", text.replace("đ", "d").replace("Đ", "D")).strip().lower()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return fold_text(value) in {"1", "true", "yes", "y", "co", "x"}


def as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal(0)
    raw = re.sub(r"[^0-9,.-]", "", str(value or ""))
    if not raw:
        return Decimal(0)
    if raw.count(".") > 1 or raw.count(",") > 1 or ("." in raw and "," in raw):
        raw = re.sub(r"[.,]", "", raw)
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal(0)


def parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = clean_text(value)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
            for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw, pattern)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def brand_scope(value: Any) -> str:
    folded = fold_text(value)
    if folded in {"cfc", "co bay", "cfc co bay", "cfc/co bay"}:
        return "cfc"
    if folded in {"zeo", "pano", "oplus", "o plus", "zif", "zeo/pano/oplus"}:
        return "zeo"
    return "unknown"


def _is_inactive(record: dict[str, Any]) -> bool:
    return as_bool(record.get("inactive"))


def assert_public_projection_safe(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in FORBIDDEN_PUBLIC_FIELDS:
                raise PublicProjectionError(f"Forbidden field in public projection: {path}.{key}")
            # Address fields are geographical, not product descriptions — skip price scanning on them
            if normalized_key in ADDRESS_FIELD_KEYS:
                continue
            assert_public_projection_safe(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_public_projection_safe(child, f"{path}[{index}]")
    elif isinstance(value, str) and any(pattern.search(value) for pattern in PRICE_TEXT_PATTERNS):
        raise PublicProjectionError(f"Price-like text in public projection: {path}")


def build_public_products(products: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    metrics = {
        "source_count": 0,
        "public_count": 0,
        "skipped_inactive": 0,
        "skipped_missing_identity": 0,
        "duplicate_count": 0,
        "unknown_brand_count": 0,
    }
    by_code: dict[str, dict[str, Any]] = {}
    for source in products:
        metrics["source_count"] += 1
        if _is_inactive(source):
            metrics["skipped_inactive"] += 1
            continue
        code = clean_text(source.get("product_code") or source.get("inventory_item_code") or source.get("item_code") or source.get("id"))
        name = clean_text(source.get("product_name") or source.get("inventory_item_name") or source.get("item_name") or source.get("name"))
        if not code or not name:
            metrics["skipped_missing_identity"] += 1
            continue
        if code in by_code:
            metrics["duplicate_count"] += 1
            continue

        raw_brand = clean_text(source.get("brand") or source.get("brand_name") or source.get("trademark") or "Cò Bay")
        scope = brand_scope(raw_brand)
        if scope == "unknown":
            metrics["unknown_brand_count"] += 1
        item = {
            "product_code": code,
            "product_name": name,
            "display_name": name,
            "brand": raw_brand,
            "brand_scope": scope,
            "product_category": clean_text(source.get("product_category") or source.get("inventory_item_category_name") or source.get("category_name")),
            "usage_unit": clean_text(source.get("usage_unit") or source.get("unit_name")),
            "description": clean_text(source.get("description")),
            "sale_description": clean_text(source.get("sale_description")),
            "avatar": clean_text(source.get("avatar")),
            "search_keywords": clean_text(source.get("search_keywords")),
            "source": "amis_crm",
        }
        assert_public_projection_safe(item)
        by_code[code] = item

    items = sorted(by_code.values(), key=lambda item: (item["brand_scope"], fold_text(item["product_name"])))
    metrics["public_count"] = len(items)
    return items, metrics


def _record_aliases(record: dict[str, Any], fields: tuple[str, ...]) -> set[str]:
    aliases = set()
    for field in fields:
        folded = fold_text(record.get(field))
        if folded:
            aliases.add(folded)
    return aliases


def _customer_key(customer: dict[str, Any]) -> str:
    for field in ("account_number", "account_id", "id", "account_name"):
        value = clean_text(customer.get(field))
        if value:
            return f"{field}:{fold_text(value)}"
    return ""


def _order_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw = (
        order.get("sale_order_product_mappings")
        or order.get("order_products")
        or order.get("products")
        or []
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _valid_coordinates(longitude: Any, latitude: Any) -> tuple[Optional[float], Optional[float]]:
    try:
        lon = float(str(longitude).strip())
        lat = float(str(latitude).strip())
    except (TypeError, ValueError):
        return None, None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None, None
    return lon, lat


def _approved(customer: dict[str, Any], config: AmisConfig) -> bool:
    if config.public_approval_field and as_bool(customer.get(config.public_approval_field)):
        return True
    account_number = fold_text(customer.get("account_number"))
    allowlist = {fold_text(value) for value in config.public_account_allowlist}
    if allowlist:
        return bool(account_number and account_number in allowlist)
    # Pilot mode: no approval field match and no allowlist → approve all eligible.
    if config.pilot_approve_all:
        return True
    return False


def _order_is_qualified(
    order: dict[str, Any],
    config: AmisConfig,
    *,
    now: datetime,
) -> tuple[bool, str]:
    if not as_bool(order.get("is_invoiced")):
        return False, "not_invoiced"
    if as_decimal(order.get("invoiced_amount")) <= 0:
        return False, "zero_invoiced_amount"

    revenue_status = fold_text(order.get("revenue_status"))
    allowed_statuses = {fold_text(value) for value in config.allowed_revenue_statuses}
    if allowed_statuses and revenue_status not in allowed_statuses:
        return False, "revenue_status"

    order_status = fold_text(order.get("status"))
    if any(fold_text(value) in order_status for value in config.blocked_order_status_fragments if fold_text(value)):
        return False, "blocked_order_status"

    if config.public_recency_days > 0:
        order_date = parse_datetime(
            order.get("invoice_date")
            or order.get("sale_order_date")
            or order.get("book_date")
        )
        if order_date is None:
            return False, "missing_order_date"
        if order_date < now - timedelta(days=config.public_recency_days):
            return False, "stale_order"
    return True, "qualified"


def build_public_sales_locations(
    customers: Iterable[dict[str, Any]],
    sale_orders: Iterable[dict[str, Any]],
    products: Iterable[dict[str, Any]],
    config: AmisConfig,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    customer_records = list(customers)
    product_records = list(products)
    order_records = list(sale_orders)
    metrics: dict[str, Any] = {
        "customer_source_count": len(customer_records),
        "order_source_count": len(order_records),
        "qualified_order_count": 0,
        "qualified_customer_count": 0,
        "public_count": 0,
        "with_coordinates_count": 0,
        "with_phone_count": 0,
        "skipped_order_reasons": {},
        "skipped_customer_reasons": {},
    }

    customers_by_key: dict[str, dict[str, Any]] = {}
    alias_to_key: dict[str, Optional[str]] = {}
    for customer in customer_records:
        key = _customer_key(customer)
        if not key:
            continue
        customers_by_key[key] = customer
        aliases = _record_aliases(
            customer,
            ("account_number", "account_id", "id", "account_name"),
        )
        for alias in aliases:
            if alias in alias_to_key and alias_to_key[alias] != key:
                alias_to_key[alias] = None
            else:
                alias_to_key[alias] = key

    product_scope_by_alias: dict[str, str] = {}
    for product in product_records:
        scope = brand_scope(product.get("brand"))
        if scope == "unknown":
            continue
        for alias in _record_aliases(product, ("product_code", "product_name", "id")):
            product_scope_by_alias[alias] = scope

    scopes_by_customer: dict[str, set[str]] = {}
    for order in order_records:
        qualified, reason = _order_is_qualified(order, config, now=current_time)
        if not qualified:
            reasons = metrics["skipped_order_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            continue

        order_aliases = _record_aliases(
            order,
            ("account_number", "account_id", "customer_id", "account_name"),
        )
        matched_keys = {alias_to_key.get(alias) for alias in order_aliases}
        matched_keys.discard(None)
        if len(matched_keys) != 1:
            reasons = metrics["skipped_order_reasons"]
            reasons["customer_not_unique"] = reasons.get("customer_not_unique", 0) + 1
            continue
        customer_key = next(iter(matched_keys))

        scopes: set[str] = set()
        for line in _order_lines(order):
            line_scope = brand_scope(line.get("brand"))
            if line_scope != "unknown":
                scopes.add(line_scope)
                continue
            for alias in _record_aliases(line, ("product_code", "product_name", "product_id")):
                scope = product_scope_by_alias.get(alias)
                if scope:
                    scopes.add(scope)
        if not scopes:
            reasons = metrics["skipped_order_reasons"]
            reasons["brand_not_resolved"] = reasons.get("brand_not_resolved", 0) + 1
            continue

        scopes_by_customer.setdefault(str(customer_key), set()).update(scopes)
        metrics["qualified_order_count"] += 1

    metrics["qualified_customer_count"] = len(scopes_by_customer)
    public_items: list[dict[str, Any]] = []

    # In pilot mode or standard sync, iterate through all customers.
    # If order linking found brand scopes, use them; otherwise in pilot mode
    # fallback to CFC brand scope (or resolve from customer list_product field).
    target_customers = (
        customers_by_key.items()
        if config.pilot_approve_all
        else [(k, customers_by_key[k]) for k in scopes_by_customer if k in customers_by_key]
    )

    for key, customer in target_customers:
        scopes = scopes_by_customer.get(key)
        if not scopes and config.pilot_approve_all:
            # Resolve from customer fields or default to CFC
            customer_products = str(customer.get("list_product_name") or customer.get("list_product") or "")
            if "zeo" in customer_products.lower():
                scopes = {"zeo"}
            else:
                scopes = {"cfc"}

        if not scopes:
            continue

        def skip(reason: str) -> None:
            reasons = metrics["skipped_customer_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1

        if _is_inactive(customer):
            skip("inactive")
            continue
        if not _approved(customer, config):
            skip("not_publicly_approved")
            continue

        display_name = clean_text(customer.get("account_name"))
        if not display_name:
            skip("missing_name")
            continue

        public_address = clean_text(customer.get(config.public_address_field)) if config.public_address_field else ""
        address_source = "public_field"
        if not public_address:
            public_address = clean_text(customer.get("shipping_address"))
            address_source = "shipping"
        if not public_address and config.allow_billing_address_fallback:
            public_address = clean_text(customer.get("billing_address"))
            address_source = "billing"
        if not public_address:
            skip("missing_public_address")
            continue

        public_phone = clean_text(customer.get(config.public_phone_field)) if config.public_phone_field else ""
        if not public_phone and config.allow_office_phone_fallback:
            public_phone = clean_text(customer.get("office_tel"))

        if address_source == "billing":
            province = clean_text(customer.get("billing_province"))
            district = clean_text(customer.get("billing_district"))
            ward = clean_text(customer.get("billing_ward"))
            longitude, latitude = _valid_coordinates(
                customer.get("billing_long"), customer.get("billing_lat")
            )
        else:
            province = clean_text(customer.get("shipping_province"))
            district = clean_text(customer.get("shipping_district"))
            ward = clean_text(customer.get("shipping_ward"))
            longitude, latitude = _valid_coordinates(
                customer.get("shipping_long"), customer.get("shipping_lat")
            )

        # Skip locations without GPS when require_coordinates is enabled.
        if config.require_coordinates and (longitude is None or latitude is None):
            skip("missing_coordinates")
            continue

        stable_source = clean_text(customer.get("account_number")) or display_name
        location_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
        item = {
            "location_id": location_id,
            "display_name": display_name,
            "location_type": "Điểm bán",
            "public_phone": public_phone,
            "public_address": public_address,
            "province": province,
            "district": district,
            "ward": ward,
            "longitude": longitude,
            "latitude": latitude,
            "brand_scopes": sorted(scopes),
            "source": "amis_crm",
        }
        assert_public_projection_safe(item)
        public_items.append(item)
        if longitude is not None and latitude is not None:
            metrics["with_coordinates_count"] += 1
        if public_phone:
            metrics["with_phone_count"] += 1

    public_items.sort(key=lambda item: (item["province"], item["district"], fold_text(item["display_name"])))
    metrics["public_count"] = len(public_items)
    return public_items, metrics
