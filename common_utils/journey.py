from __future__ import annotations

import re
import uuid
from typing import Any


JOURNEY_NAMESPACE = uuid.UUID("96db8077-6084-47d8-8f39-7c0d7708e77a")
JOURNEY_ID_PREFIX = "dj_"
JOURNEY_ID_RE = re.compile(r"^dj_[0-9a-f]{32}$")


def normalize_deal_journey_id(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if JOURNEY_ID_RE.match(normalized):
        return normalized
    return None


def build_deal_journey_id(*, source_name: str, source_id: str) -> str:
    source_key = f"{source_name.strip().lower()}:{source_id.strip()}"
    return f"{JOURNEY_ID_PREFIX}{uuid.uuid5(JOURNEY_NAMESPACE, source_key).hex}"


def extract_deal_journey_id(source_payload: dict[str, Any] | None, *, source_name: str, source_id: str) -> str:
    payload = source_payload or {}
    for key in ("deal_journey_id", "journey_id", "global_pipeline_id", "pipeline_id"):
        value = normalize_deal_journey_id(payload.get(key))
        if value:
            return value
    return build_deal_journey_id(source_name=source_name, source_id=source_id)
