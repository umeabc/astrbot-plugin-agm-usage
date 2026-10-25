"""Reporting layer: config parsing, masking, rendering, pagination.

Pure functions — no I/O, no AstrBot dependency.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Mapping, Optional, Sequence

from .client import AGMSchemaError
from .models import AccountInfo, ParsedConfig, UsageReport

# ---- fixed user-facing error strings (contract C.10) -----------------------
ERR_UNAUTHORIZED = "无权查询 AGM 用量。"
ERR_CONFIG = "AGM 查询配置无效，请联系管理员检查配置。"
ERR_TIMEOUT = "AGM 请求超时，请稍后重试。"
ERR_AUTH = "AGM 管理 API 认证失败，请联系管理员检查密码。"
ERR_SERVER = "AGM 服务异常，请稍后重试。"
ERR_UNAVAILABLE = "AGM 暂不可达，请检查网络与服务地址。"
ERR_JSON = "AGM 返回非 JSON 数据，请联系管理员检查服务地址。"
ERR_SCHEMA = "AGM 响应格式不兼容，请联系管理员。"
ERR_TOO_LARGE = "AGM 响应过大，请联系管理员检查配置。"
ERR_RATE_LIMIT = "AGM 请求受限，请稍后重试。"
ERR_COOLDOWN = "查询过于频繁，请稍后重试。"
ERR_INTERNAL = "AGM 查询发生内部错误，请联系管理员查看日志。"

ERROR_TEXT_BY_CODE = {
    "auth": ERR_AUTH,
    "timeout": ERR_TIMEOUT,
    "server": ERR_SERVER,
    "unavailable": ERR_UNAVAILABLE,
    "json": ERR_JSON,
    "schema": ERR_SCHEMA,
    "too_large": ERR_TOO_LARGE,
    "rate_limit": ERR_RATE_LIMIT,
    "internal": ERR_INTERNAL,
}

# Fixed priority used when several requests fail at once (plan §4.5).
ERROR_PRIORITY = ("auth", "timeout", "server", "unavailable", "json", "schema", "too_large", "rate_limit")


def error_text_for_code(code: str) -> str:
    return ERROR_TEXT_BY_CODE.get(code, ERR_INTERNAL)


def pick_error_text(codes: Sequence[str]) -> str:
    """Choose the fixed reply text by the documented priority."""
    rank = {code: i for i, code in enumerate(ERROR_PRIORITY)}
    best = min(codes, key=lambda c: rank.get(c, len(rank)))
    return error_text_for_code(best)


# ---- config parsing ---------------------------------------------------------

def _clamp(value, lo, hi, default):
    """Clamp into [lo, hi]; returns (value, warn) — warn when clamped/invalid."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default, True
    if v != v or v in (float("inf"), float("-inf")):
        return default, True
    if v < lo or v > hi:
        return min(hi, max(lo, v)), True
    return v, False


def parse_config(raw: Mapping) -> ParsedConfig:
    """Validate and clamp config; base_url/password problems set valid=False."""
    warnings = []
    valid = True

    base_url = str(raw.get("base_url", "") or "").strip().rstrip("/")
    if not base_url.lower().startswith(("http://", "https://")):
        warnings.append("base_url 必须以 http:// 或 https:// 开头")
        valid = False
    if "@" in base_url or "?" in base_url or "#" in base_url:
        warnings.append("base_url 不允许包含凭据、查询或片段")
        valid = False

    password = str(raw.get("admin_password", "") or "")
    if "\r" in password or "\n" in password:
        warnings.append("admin_password 不允许包含换行")
        valid = False

    timeout, w = _clamp(raw.get("timeout_seconds", 10.0), 1.0, 60.0, 10.0)
    if w:
        warnings.append("timeout_seconds 超出范围，已回退默认值")

    max_lines, w = _clamp(raw.get("max_lines_per_message", 30), 5, 100, 30)
    if w:
        warnings.append("max_lines_per_message 超出范围，已回退默认值")
    max_chars, w = _clamp(raw.get("max_message_chars", 1800), 300, 4000, 1800)
    if w:
        warnings.append("max_message_chars 超出范围，已回退默认值")
    max_bytes, w = _clamp(raw.get("max_response_bytes", 5242880), 65536, 52428800, 5242880)
    if w:
        warnings.append("max_response_bytes 超出范围，已回退默认值")
    cooldown, w = _clamp(raw.get("cooldown_seconds", 10), 0, 3600, 10)
    if w:
        warnings.append("cooldown_seconds 超出范围，已回退默认值")

    tz_name = str(raw.get("default_timezone", "") or "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz_name)
    except Exception:
        warnings.append("default_timezone 无效，已回退 Asia/Shanghai")
        tz_name = "Asia/Shanghai"

    def _id_tuple(value) -> tuple:
        if value is None:
            return ()
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(str(v).strip() for v in value if str(v).strip())
        if isinstance(value, str):
            parts = value.replace("，", ",").split(",")
            return tuple(p.strip() for p in parts if p.strip())
        return ()

    instance_name = str(raw.get("instance_name", "") or "").strip() or "AGM"

    account_label = str(raw.get("account_label", "") or "short_id").strip().lower()
    if account_label not in ("short_id", "masked_email", "email"):
        account_label = "short_id"

    check_hours, w = _clamp(raw.get("update_check_interval_hours", 6), 1, 168, 6)
    if w:
        warnings.append("update_check_interval_hours 超出范围，已回退默认值")

    return ParsedConfig(
        base_url=base_url,
        admin_password=password,
        instance_name=instance_name,
        timeout_seconds=timeout,
        default_timezone=tz_name,
        allowed_users=_id_tuple(raw.get("allowed_users")),
        allowed_groups=_id_tuple(raw.get("allowed_groups")),
        allow_admin=bool(raw.get("allow_admin", True)),
        allow_private=bool(raw.get("allow_private", True)),
        cooldown_seconds=int(cooldown),
        max_lines_per_message=int(max_lines),
        max_message_chars=int(max_chars),
        max_response_bytes=int(max_bytes),
        account_label=account_label,
        show_model_breakdown=bool(raw.get("show_model_breakdown", True)),
        update_notice=bool(raw.get("update_notice", True)),
        update_check_interval_hours=int(check_hours),
        warnings=tuple(warnings),
        valid=valid,
    )


# ---- masking ----------------------------------------------------------------

def mask_email(email: str, mode: str = "ellipsis") -> str:
    """Mask an email using ellipsis (…) to avoid Markdown issues; empty email -> '—'.
    
    Modes:
    - 'ellipsis': s…e@…com (safe for Markdown)
    - 'raw': return email as-is
    """
    if not email:
        return "—"
    if mode == "raw":
        return email
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    # local: first char + … + last char (or just first + … if len < 3)
    if len(local) < 3:
        masked_local = local[0] + "…" if local else "…"
    else:
        masked_local = local[0] + "…" + local[-1]
    # domain: … + . + last segment (or just …. + last if it's the only part)
    domain_parts = domain.split(".")
    if len(domain_parts) > 1:
        masked_domain = "…." + domain_parts[-1]
    else:
        masked_domain = "…"
    return f"{masked_local}@{masked_domain}"


def get_account_label(account: AccountInfo, mode: str = "short_id") -> str:
    """Get account label per mode: short_id (UUID[:8]), masked_email, or email.
    
    Returns '—' if identifier unavailable in the requested mode.
    """
    if mode == "short_id":
        account_id = (account.id or "").strip()
        if not account_id:
            return "—"
        return account_id[:8]
    elif mode == "masked_email":
        return mask_email(account.email, mode="ellipsis")
    elif mode == "email":
        if not account.email:
            return "—"
        return account.email
    else:
        # fallback to short_id
        account_id = (account.id or "").strip()
        if not account_id:
            return "—"
        return account_id[:8]


# ---- countdown --------------------------------------------------------------

def format_countdown(reset_time: Optional[datetime], now: datetime) -> str:
    """Format remaining time until reset_time per contract C.6."""
    if reset_time is None:
        return "即将"
    remaining = reset_time - now
    seconds = remaining.total_seconds()
    if seconds <= 0:
        return "即将"
    total_minutes = int(seconds // 60)
    days = total_minutes // (60 * 24)
    hours = (total_minutes % (60 * 24)) // 60
    minutes = total_minutes % 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    return "".join(parts) if parts else "0分"


def _group_label(display_name: str) -> str:
    lowered = display_name.lower()
    if "gemini" in lowered:
        return "Gemini:"
    if "claude" in lowered:
        return "Claude:"
    return display_name + ":"


_WINDOW_ORDER = {"5h": 0, "weekly": 1}

# AGM >= 5.0.0 QoS states (GET /api/accounts .qos_status). Healthy is serialized
# as null by AGM, so only the four non-healthy states ever reach us.
QOS_LABELS = {
    "suspicious": "QoS可疑",
    "jailed": "QoS隔离",
    "probing": "QoS探针",
    "half_open": "QoS缓刑",
}


def _format_seconds(total_seconds: int) -> str:
    """Render a remaining-seconds count as 天/小时/分 (empty when <= 0)."""
    total_minutes = int(total_seconds) // 60
    days = total_minutes // (60 * 24)
    hours = (total_minutes % (60 * 24)) // 60
    minutes = total_minutes % 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    return "".join(parts)


def format_account_section(account: AccountInfo, now: datetime, account_label: str = "short_id") -> list[str]:
    """Render one account section per contract C.4–C.6.

    account_label mode: 'short_id' (default), 'masked_email', or 'email'.
    """
    label = get_account_label(account, account_label)
    tier = account.subscription_tier or "—"
    markers = ""
    if account.disabled:
        markers += " [已禁用]"
    if account.is_forbidden:
        markers += " [已封禁]"
    if account.validation_blocked:
        markers += " [校验阻塞]"
    if account.qos_status:
        qos_label = QOS_LABELS.get(account.qos_status)
        if qos_label:
            remaining = account.qos_reset_seconds
            if remaining is not None:
                cd = _format_seconds(remaining)
                markers += f" [{qos_label} {cd}后解除]" if cd else f" [{qos_label}]"
            else:
                markers += f" [{qos_label}]"
    lines = [f"{label} ({tier}) ：{markers}"]
    if account.quota is None or not account.quota_groups:
        lines.append("额度数据不可用")
        return lines
    for group in account.quota_groups:
        lines.append(_group_label(group.display_name))
        buckets = sorted(
            enumerate(group.buckets),
            key=lambda pair: (_WINDOW_ORDER.get(pair[1].window, 2), pair[0]),
        )
        for _, bucket in buckets:
            if bucket.remaining_fraction is None:
                continue
            pct = round(bucket.remaining_fraction * 100)
            cd = format_countdown(bucket.reset_time, now)
            if bucket.window == "5h":
                lines.append(f"5小时剩余 {pct}%（{cd}后重置）")
            elif bucket.window == "weekly":
                lines.append(f"本周剩余 {pct}%（{cd}后重置）")
            else:
                lines.append(f"{bucket.window}剩余 {pct}%（{cd}后重置）")
    return lines


MODEL_HEADER = "模型消耗（近 24 小时 / 近一周）"


def merge_model_windows(models24, models168):
    """Merge the two rolling windows into (model, t24, t168) rows.

    Display set = union of both windows; a model is hidden only when BOTH
    windows are 0. Sort: 24h desc -> 7d desc -> model asc.
    """
    t24 = dict(models24 or ())
    t168 = dict(models168 or ())
    union = sorted(set(t24) | set(t168))
    rows = [(m, t24.get(m, 0.0), t168.get(m, 0.0)) for m in union]
    rows = [r for r in rows if r[1] > 0 or r[2] > 0]
    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    return tuple(rows)


def format_report(report: UsageReport, account_label: str = "short_id", show_models: bool = True) -> str:
    """Render the /agm report text per contract C."""
    lines = [
        "Antigravity Tools 消耗情况",
        report.window.day.isoformat(),
        f"当日已消耗 Token：{report.today:,.0f}" if report.today is not None else "当日已消耗 Token：—",
        f"本周已消耗 Token：{report.week:,.0f}" if report.week is not None else "本周已消耗 Token：—",
        f"累计消耗 Token：{report.all_time:,.0f}" if report.all_time is not None else "累计消耗 Token：—",
    ]
    if report.accounts is None:
        raise AGMSchemaError("accounts unavailable")
    lines.append(f"账号（{len(report.accounts)}）：")
    for account in report.accounts:
        lines.extend(format_account_section(account, report.window.as_of, account_label))
    if show_models:
        if report.models is None:
            lines.append(f"{MODEL_HEADER}：—")
        else:
            if not report.models:
                lines.append(f"{MODEL_HEADER}：无")
            else:
                lines.append(f"{MODEL_HEADER}：")
                for model, t24, t168 in report.models:
                    lines.append(f"{model}：{t24:,.0f} / {t168:,.0f} Token")
    return "\n".join(lines)


def paginate(lines: Sequence[str], max_lines: int, max_chars: int, label: str = "AGM") -> list[str]:
    """Split lines into pages of at most max_lines physical lines and
    max_message_chars characters. Prefix only added when total pages > 1.
    Nothing is dropped or truncated."""
    max_lines = max(1, int(max_lines))
    max_chars = max(1, int(max_chars))
    # Reserved width for the prefix "[{label} i/N] " in the worst case
    # (both numbers up to 3 digits) so a page can never exceed max_chars.
    reserve = len(f"[{label} 888/888] ")
    budget = max(1, max_chars - reserve)

    physical: list[str] = []
    for line in lines:
        while len(line) > budget:
            physical.append(line[:budget])
            line = line[budget:]
        physical.append(line)

    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in physical:
        line_len = len(line)
        would_lines = len(current) + 1
        would_chars = current_len + line_len + (1 if current else 0)
        if current and (would_lines > max_lines or would_chars > budget):
            pages.append(current)
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len = would_chars
    if current:
        pages.append(current)

    total = len(pages)
    # Only add prefix if there are multiple pages
    if total == 1:
        return ["\n".join(pages[0])]
    else:
        return [f"[{label} {i}/{total}] " + "\n".join(page) for i, page in enumerate(pages, start=1)]


def render_messages(report: UsageReport, account_label: str = "short_id", show_models: bool = True,
                    max_lines: int = 30, max_chars: int = 1800,
                    notice: str = "") -> list[str]:
    """One-shot helper: format the report, append the update notice, paginate.

    ``notice`` is "" (no notice) unless a strictly newer AGM release is known;
    it is appended AFTER format_report so it lands on the report's last line.
    """
    text = append_update_notice(format_report(report, account_label, show_models), notice)
    return paginate(text.split("\n"), max_lines, max_chars)


# ---- upstream AGM update notice --------------------------------------------

_UPDATE_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
# Exact rendered AGM-update notice; {version} is the bare X.Y.Z (the plugin
# adds the single leading v — never doubled when the source had one).
UPDATE_NOTICE_FORMAT = "AGM 有更新，最新版本号v{version}"


def parse_version_tuple(value: object) -> tuple[int, int, int] | None:
    """Parse an X.Y.Z version string (tolerating one leading v/V) into a
    comparable tuple. A bare ``X`` or ``X.Y`` is tolerated (missing parts are
    0), because AGM's updater.json is not guaranteed to be three-part. A
    pre-release suffix (``5.0.0-beta.2``) is dropped and only its numeric core
    is kept, so a pre-release never claims to be newer than its own stable
    core. Returns None for anything unparseable — never raises."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "-" in text:
        text = text.split("-", 1)[0].strip()
    m = _UPDATE_VERSION_RE.match(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def notice_version(notice: str) -> str:
    """Extract the bare version from a rendered notice ("" when it is not one)."""
    prefix = UPDATE_NOTICE_FORMAT.format(version="")
    if not notice or not notice.startswith(prefix):
        return ""
    rest = notice[len(prefix):].strip()
    return rest[1:] if rest.startswith("v") else rest


def derive_update_notice(updates: Mapping | None) -> str:
    """Decide the AGM-update notice line from a POST /api/system/updates/check
    payload (AGM's UpdateInfo struct).

    Returns the notice string ONLY when a genuinely newer release is known:
    both ``current_version`` and ``latest_version`` must parse and the latter
    must be strictly greater (tuple compare). AGM's own ``has_update`` boolean
    is deliberately NOT trusted alone — it is computed by AGM's semver helper,
    which we do not mirror (and must not depend on). Any other shape yields ""
    (no notice), never an exception.

    AGM versions may carry a pre-release suffix (``5.0.0-beta.2``). A
    pre-release is parsed as its numeric core and therefore never claims to be
    newer than the same stable core; a newer core always wins.
    """
    if not isinstance(updates, Mapping):
        return ""
    candidate = parse_version_tuple(updates.get("latest_version"))
    current = parse_version_tuple(updates.get("current_version"))
    if candidate is None or current is None:
        return ""
    if candidate <= current:
        return ""
    return UPDATE_NOTICE_FORMAT.format(version=".".join(str(n) for n in candidate))


def append_update_notice(report_text: str, notice: str) -> str:
    """Append the update notice as the VERY LAST line of the report.

    The notice goes after the 模型消耗 block (it is always last). Empty notice
    → the text is returned unchanged (byte-identical to the no-notice
    baseline).
    """
    if not notice:
        return report_text
    return report_text + "\n" + notice
