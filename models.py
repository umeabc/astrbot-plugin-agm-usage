"""Pure data models for the AGM usage plugin. No AstrBot dependency."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Mapping, Tuple
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Health:
    """GET /api/health payload."""

    status: str
    version: str


@dataclass(frozen=True)
class QuotaBucket:
    """One quota bucket inside a quota group."""

    window: str
    remaining_fraction: float | None
    reset_time: datetime | None


@dataclass(frozen=True)
class QuotaGroup:
    """One quota group of an account."""

    display_name: str
    buckets: Tuple[QuotaBucket, ...] = ()


@dataclass(frozen=True)
class AccountInfo:
    """One account from GET /api/accounts."""

    id: str
    email: str = ""
    name: str = ""
    disabled: bool = False
    disabled_reason: str | None = None
    validation_blocked: bool = False
    validation_blocked_reason: str | None = None
    quota: Mapping | None = None
    quota_groups: Tuple[QuotaGroup, ...] = ()
    subscription_tier: str | None = None
    is_forbidden: bool = False


@dataclass(frozen=True)
class Window:
    """Time window of one report batch."""

    day: date
    timezone: str
    as_of: datetime
    hours: int
    week_hours: int


@dataclass(frozen=True)
class UsageReport:
    """Everything format_report() needs. None means the fetch failed."""

    instance: str
    window: Window
    today: float | None = None
    week: float | None = None
    all_time: float | None = None
    accounts: Tuple[AccountInfo, ...] | None = None
    models: Tuple[Tuple[str, float], ...] | None = None


@dataclass(frozen=True)
class ParsedConfig:
    """All effective config values plus validation outcome."""

    base_url: str = ""
    admin_password: str = ""
    instance_name: str = "AGM"
    timeout_seconds: float = 10.0
    default_timezone: str = "Asia/Shanghai"
    allowed_users: Tuple[str, ...] = ()
    allowed_groups: Tuple[str, ...] = ()
    allow_admin: bool = True
    allow_private: bool = True
    cooldown_seconds: int = 10
    max_lines_per_message: int = 30
    max_message_chars: int = 1800
    max_response_bytes: int = 5242880
    account_label: str = "short_id"
    show_model_breakdown: bool = True
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = False


def compute_window(now: datetime, timezone_name: str) -> Window:
    """Compute the day / H / HW window for one report batch.

    ``now`` must be a timezone-aware datetime; a naive one is rejected.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    tz_name = timezone_name or "Asia/Shanghai"
    local = now.astimezone(ZoneInfo(tz_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = max(1, math.ceil((local - midnight).total_seconds() / 3600))
    monday = midnight - timedelta(days=local.weekday())
    week_hours = max(1, math.ceil((local - monday).total_seconds() / 3600))
    return Window(day=local.date(), timezone=tz_name, as_of=now, hours=hours, week_hours=week_hours)
