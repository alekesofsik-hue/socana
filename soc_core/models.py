from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from dateutil import parser as dtparser
from pydantic import BaseModel, Field, field_validator


class AssetType(str, Enum):
    # Auto-created on first seen event; must be classified later via Telegram bot.
    UNCLASSIFIED = "UNCLASSIFIED"
    SERVER = "SERVER"
    WORKSTATION = "WORKSTATION"


class RiskLevel(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class KasperskyEvent(BaseModel):
    vendor_severity: str | None = None
    device: str | None = None
    event_type: str | None = None
    detection_name: str | None = None
    object_path: str | None = None
    process_name: str | None = None
    sha256: str | None = None
    user: str | None = None
    result: str | None = None
    event_time: datetime | None = Field(default=None, description="UTC datetime")

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, v: str | None) -> str | None:
        if not v:
            return v
        vv = v.strip().lower()
        if len(vv) == 64 and all(c in "0123456789abcdef" for c in vv):
            return vv
        return v.strip()

    @field_validator("event_time", mode="before")
    @classmethod
    def _parse_event_time_to_utc(cls, v: Any) -> datetime | None:
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            dt = v
        else:
            s = str(v).strip()
            # Example: "Tuesday, January 27, 2026 7:14:20 AM (GMT+00:00)"
            s = re.sub(r"\(\s*GMT([+-]\d{2}:\d{2})\s*\)", r"\1", s, flags=re.IGNORECASE)
            dt = dtparser.parse(s)
        if dt.tzinfo is None:
            # Если в письме нет TZ, считаем что это уже UTC (лучше чем "локаль" сервера)
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def fingerprint(self) -> str:
        """
        Fingerprint для дедупликации: device + event_type + detection_name + object + result
        """
        parts = [
            (self.device or "").strip().lower(),
            (self.event_type or "").strip().lower(),
            (self.detection_name or "").strip().lower(),
            (self.object_path or "").strip().lower(),
            (self.result or "").strip().lower(),
        ]
        data = "|".join(parts).encode("utf-8", errors="ignore")
        return hashlib.sha256(data).hexdigest()


class ParsedEmail(BaseModel):
    uid: str
    message_id: str | None = None
    subject: str | None = None
    from_email: str | None = None
    date: datetime | None = None
    raw_text: str
    event: KasperskyEvent


class EnrichedEvent(BaseModel):
    event: KasperskyEvent
    asset_type: AssetType | None = None
    risk_level: RiskLevel
    risk_reason: str | None = None


class DispatchMessage(BaseModel):
    text: str
    email_id: int
    risk_level: RiskLevel

