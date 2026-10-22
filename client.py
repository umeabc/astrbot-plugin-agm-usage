"""Async AGM admin API client (aiohttp only, read-only GETs).

Error taxonomy: AGMError (base) with ``code`` in
{auth, rate_limit, server, unavailable, json, schema, too_large, timeout}.
Authorization: ``Authorization: Bearer <admin_password>`` on every request
except /api/health. The password is never logged or embedded in exceptions.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

import aiohttp

from .models import AccountInfo, QuotaBucket, QuotaGroup

logger = logging.getLogger("astrbot.agm_usage")

READ_CHUNK = 65536


class AGMError(Exception):
    """Base error; ``code`` is a stable identifier for the fixed reply text."""

    code = "internal"

    def __init__(self, message: str = "agm error") -> None:
        super().__init__(message)


class AGMAuthError(AGMError):
    code = "auth"


class AGMRateLimitError(AGMError):
    code = "rate_limit"


class AGMServerError(AGMError):
    code = "server"


class AGMUnavailableError(AGMError):
    code = "unavailable"


class AGMJsonError(AGMError):
    code = "json"


class AGMSchemaError(AGMError):
    code = "schema"


class AGMTooLargeError(AGMError):
    code = "too_large"


class AGMTimeoutError(AGMError):
    code = "timeout"


@dataclass(frozen=True)
class Observation:
    """One request observation (for /agmdiag). Never holds secrets."""

    path: str
    status: int | None = None
    elapsed_ms: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ClientResult:
    """A successful fetch plus its observation."""

    value: Any
    observation: Observation


def _raise_for_status(status: int) -> None:
    """Map an HTTP status to the error taxonomy. 3xx counts as schema."""
    if status in (401, 403):
        raise AGMAuthError(f"status {status}")
    if status == 429:
        raise AGMRateLimitError("rate limited")
    if status >= 500:
        raise AGMServerError(f"server error {status}")
    if status >= 400 or 300 <= status < 400:
        raise AGMSchemaError(f"unexpected status {status}")


def _parse_iso_utc(value: object) -> Optional[datetime]:
    """Parse an ISO8601 timestamp (usually 'Z'-suffixed UTC) into an aware
    datetime; returns None for anything unparseable — never raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _clean_text(value: object) -> str:
    """Normalize a display string: NFC, CR/LF/TAB -> space, drop controls."""
    import unicodedata

    if value is None:
        return ""
    text = str(value)
    cleaned = "".join(
        " " if ch in ("\r", "\n", "\t") else ch
        for ch in unicodedata.normalize("NFC", text)
        if ch == "\r" or ch == "\n" or ch == "\t" or not unicodedata.category(ch).startswith("C")
    )
    return cleaned.strip()


def _to_nonneg_int(value: Any) -> int:
    """Strict non-negative integer extraction; raises AGMSchemaError."""
    if isinstance(value, bool) or value is None:
        raise AGMSchemaError("invalid integer field")
    if isinstance(value, int):
        if value < 0:
            raise AGMSchemaError("negative integer field")
        return value
    raise AGMSchemaError("integer field has wrong type")


def _to_nonneg_float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise AGMSchemaError("invalid numeric field")
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")) or f < 0:
            raise AGMSchemaError("invalid numeric field")
        return f
    raise AGMSchemaError("numeric field has wrong type")


def validate_summary(raw: Any) -> float:
    """Extract total_tokens from GET /api/stats/summary."""
    if not isinstance(raw, Mapping):
        raise AGMSchemaError("summary is not an object")
    if "total_tokens" not in raw:
        raise AGMSchemaError("summary missing total_tokens")
    return float(_to_nonneg_int(raw["total_tokens"]))


def validate_health(raw: Any) -> Tuple[str, str]:
    """Extract (status, version) from GET /api/health."""
    if not isinstance(raw, Mapping):
        raise AGMSchemaError("health is not an object")
    status = raw.get("status")
    version = raw.get("version")
    if not isinstance(status, str) or not status:
        raise AGMSchemaError("health missing status")
    if not isinstance(version, str):
        raise AGMSchemaError("health missing version")
    return status, version


def _validate_quota_group(raw: Any, where: str) -> QuotaGroup:
    if not isinstance(raw, Mapping):
        raise AGMSchemaError(f"{where} is not an object")
    display = _clean_text(raw.get("display_name"))
    buckets_raw = raw.get("buckets")
    buckets = []
    if isinstance(buckets_raw, list):
        for i, b in enumerate(buckets_raw):
            if not isinstance(b, Mapping):
                raise AGMSchemaError(f"{where}.buckets[{i}] is not an object")
            window = b.get("window")
            if not isinstance(window, str) or not window:
                raise AGMSchemaError(f"{where}.buckets[{i}] missing window")
            fraction = b.get("remaining_fraction")
            if fraction is not None:
                fraction = _to_nonneg_float(fraction)
            buckets.append(
                QuotaBucket(
                    window=window,
                    remaining_fraction=fraction,
                    reset_time=_parse_iso_utc(b.get("reset_time")),
                )
            )
    return QuotaGroup(display_name=display, buckets=tuple(buckets))


def validate_accounts(raw: Any) -> Tuple:
    """Validate GET /api/accounts into a tuple of AccountInfo."""
    if not isinstance(raw, Mapping):
        raise AGMSchemaError("accounts payload is not an object")
    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list):
        raise AGMSchemaError("accounts is not a list")
    result = []
    for i, entry in enumerate(accounts_raw):
        if not isinstance(entry, Mapping):
            raise AGMSchemaError(f"accounts[{i}] is not an object")
        acct_id = entry.get("id")
        if not isinstance(acct_id, str) or not acct_id:
            raise AGMSchemaError(f"accounts[{i}] missing id")
        email = _clean_text(entry.get("email"))
        name = _clean_text(entry.get("name"))
        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            raise AGMSchemaError(f"accounts[{i}].disabled must be bool")
        validation_blocked = entry.get("validation_blocked", False)
        if not isinstance(validation_blocked, bool):
            raise AGMSchemaError(f"accounts[{i}].validation_blocked must be bool")
        quota = entry.get("quota")  # None is legal
        groups: Tuple[QuotaGroup, ...] = ()
        tier = None
        is_forbidden = False
        if isinstance(quota, Mapping):
            tier = quota.get("subscription_tier")
            tier = _clean_text(tier) if tier is not None else None
            forbidden = quota.get("is_forbidden", False)
            if not isinstance(forbidden, bool):
                raise AGMSchemaError(f"accounts[{i}].quota.is_forbidden must be bool")
            is_forbidden = forbidden
            groups_raw = quota.get("quota_groups")
            if groups_raw is not None:
                if not isinstance(groups_raw, list):
                    raise AGMSchemaError(f"accounts[{i}].quota.quota_groups is not a list")
                groups = tuple(
                    _validate_quota_group(g, f"accounts[{i}].quota.quota_groups[{j}]")
                    for j, g in enumerate(groups_raw)
                )
        result.append(
            AccountInfo(
                id=acct_id,
                email=email,
                name=name,
                disabled=disabled,
                disabled_reason=entry.get("disabled_reason") if isinstance(entry.get("disabled_reason"), str) else None,
                validation_blocked=validation_blocked,
                validation_blocked_reason=entry.get("validation_blocked_reason") if isinstance(entry.get("validation_blocked_reason"), str) else None,
                quota=quota if isinstance(quota, Mapping) else None,
                quota_groups=groups,
                subscription_tier=tier,
                is_forbidden=is_forbidden,
            )
        )
    return tuple(result)


def validate_models(raw: Any) -> Tuple:
    """Validate GET /api/stats/models into a tuple of (model, total_tokens)."""
    if not isinstance(raw, list):
        raise AGMSchemaError("models payload is not a list")
    result = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise AGMSchemaError(f"models[{i}] is not an object")
        model = entry.get("model")
        if not isinstance(model, str) or not model:
            raise AGMSchemaError(f"models[{i}] missing model")
        total = entry.get("total_tokens")
        if total is None:
            raise AGMSchemaError(f"models[{i}] missing total_tokens")
        result.append((model, float(_to_nonneg_int(total))))
    return tuple(result)


def validate_daily(raw: Any) -> Tuple:
    """Validate GET /api/stats/daily into a tuple of (period, total_tokens)."""
    if not isinstance(raw, list):
        raise AGMSchemaError("daily payload is not a list")
    result = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise AGMSchemaError(f"daily[{i}] is not an object")
        period = entry.get("period")
        if not isinstance(period, str) or not period:
            raise AGMSchemaError(f"daily[{i}] missing period")
        total = entry.get("total_tokens")
        if total is None:
            raise AGMSchemaError(f"daily[{i}] missing total_tokens")
        result.append((period, float(_to_nonneg_int(total))))
    return tuple(result)


class AGMClient:
    """Read-only AGM admin API client; one instance per aiohttp session."""

    def __init__(
        self,
        base_url: str,
        admin_password: str,
        session: aiohttp.ClientSession,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 5242880,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._password = admin_password
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_bytes = max_response_bytes

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _read_json(self, resp: aiohttp.ClientResponse) -> Any:
        """Stream the body to EOF in bounded chunks and parse JSON."""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await resp.content.read(READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_bytes:
                raise AGMTooLargeError("response exceeds max_response_bytes")
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AGMJsonError("response is not valid JSON") from exc

    async def _get(self, path: str, *, params: Mapping[str, str] | None = None,
                   auth: bool = True) -> ClientResult:
        """One GET request with the common error mapping. auth=False skips
        the Authorization header (used by /api/health)."""
        headers = {"Authorization": f"Bearer {self._password}"} if auth else {}
        started = asyncio.get_running_loop().time()
        try:
            async with self._session.get(
                self._url(path),
                params=dict(params) if params else None,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            ) as resp:
                _raise_for_status(resp.status)
                value = await self._read_json(resp)
                elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
                return ClientResult(value=value, observation=Observation(path=path, status=resp.status, elapsed_ms=elapsed))
        except aiohttp.ClientConnectorError as exc:
            # Refused / unreachable host (fast fail on most platforms).
            elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
            raise AGMUnavailableError(f"connect failure on {path}") from exc
        except aiohttp.ClientError as exc:
            elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
            # aiohttp >= 3.10 raises ConnectionTimeoutError (a ClientError
            # subclass) when the CONNECT phase exceeds its budget; the
            # transport itself is fine, so classify it as unreachable rather
            # than a response timeout.
            if type(exc).__name__ == "ConnectionTimeoutError":
                raise AGMUnavailableError(f"connect failure on {path}") from exc
            raise AGMUnavailableError(f"transport failure on {path}") from exc
        except asyncio.TimeoutError as exc:
            # On the Windows proactor loop a refused connect sometimes still
            # surfaces as asyncio.TimeoutError (CancelledError chain) instead
            # of ClientConnectorError; it fails FAST (elapsed << budget),
            # while a genuine timer expiry takes (approximately) the full
            # budget. Classification must tolerate timer jitter: int()
            # truncation and loop scheduling make elapsed land slightly
            # BELOW budget on real timeouts (measured 199 vs 200ms), so a
            # naive `elapsed < budget` test is flaky.
            elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            budget_ms = int((self._timeout.total or 0) * 1000)
            if elapsed_ms < budget_ms * 0.5:
                # Fast fail: well under half the budget -> transport failure.
                raise AGMUnavailableError(f"connect failure on {path}") from exc
            # Grey zone (0.5x..1x budget) and at/above budget both mean the
            # timer really fired: the exception type itself is TimeoutError.
            raise AGMTimeoutError(f"timeout on {path}") from exc

    async def health(self) -> ClientResult:
        result = await self._get("/api/health", auth=False)
        status, version = validate_health(result.value)
        return ClientResult(value=(status, version), observation=result.observation)

    async def accounts(self) -> ClientResult:
        result = await self._get("/api/accounts")
        return ClientResult(value=validate_accounts(result.value), observation=result.observation)

    async def summary(self, hours: int) -> ClientResult:
        result = await self._get("/api/stats/summary", params={"hours": str(hours)})
        return ClientResult(value=validate_summary(result.value), observation=result.observation)

    async def model_stats(self, hours: int) -> ClientResult:
        result = await self._get("/api/stats/models", params={"hours": str(hours)})
        return ClientResult(value=validate_models(result.value), observation=result.observation)

    async def daily(self, days: int = 2) -> ClientResult:
        result = await self._get("/api/stats/daily", params={"days": str(days)})
        return ClientResult(value=validate_daily(result.value), observation=result.observation)

    async def weekly(self, weeks: int = 1) -> ClientResult:
        """GET /api/stats/weekly — natural-week buckets, e.g. 2026-W42.

        AGM labels periods with ``strftime('%Y-W%W', localtime)`` (Monday as
        the week start); the caller matches the label computed from the
        reporting timezone.
        """
        result = await self._get("/api/stats/weekly", params={"weeks": str(weeks)})
        return ClientResult(value=validate_daily(result.value), observation=result.observation)
