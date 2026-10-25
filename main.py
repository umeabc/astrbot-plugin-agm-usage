"""AstrBot Star plugin: AGM usage report via /agm, diagnostics via /agmdiag.

Lifecycle mirrors astrbot_plugin_workbuddy_usage: one aiohttp ClientSession is
created in initialize() and closed in terminate(). Authorization runs BEFORE
any network request. /agmdiag bypasses the user/group allowlists but keeps the
cooldown.

Read-only against AGM state: every report request is a GET, plus at most one
POST /api/system/updates/check (AGM asks GitHub for its own latest version and
returns it without persisting anything). No AGM mutation endpoint is ever
called.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import AsyncIterator, Optional

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .client import (
    AGMClient,
    AGMError,
    AGMRateLimitError,
    AGMSchemaError,
    AGMServerError,
    AGMTimeoutError,
    AGMAuthError,
    AGMUnavailableError,
    AGMJsonError,
    AGMTooLargeError,
)
from .models import UsageReport, compute_window
from .reporting import (
    ERR_CONFIG,
    ERR_COOLDOWN,
    ERR_INTERNAL,
    ERR_UNAUTHORIZED,
    derive_update_notice,
    merge_model_windows,
    notice_version,
    parse_config,
    pick_error_text,
    render_messages,
)

class AGMReportError(Exception):
    """Whole-report failure carrying one fixed user-facing text."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


ERR_DIAG_DISABLED = "诊断命令已关闭。"

# /agmdiag notice-state strings (offline; no extra request made for these).
DIAG_UPDATE_OFF = "AGM 更新提示: 已关闭"
DIAG_UPDATE_NONE = "AGM 更新提示: 无或未知（尚未拿到版本数据）"


ERROR_CLASS_TO_CODE = {
    AGMAuthError: "auth",
    AGMTimeoutError: "timeout",
    AGMSchemaError: "schema",
    AGMServerError: "server",
    AGMUnavailableError: "unavailable",
    AGMJsonError: "json",
    AGMTooLargeError: "too_large",
    AGMRateLimitError: "rate_limit",
    AGMError: "internal",
}


def _error_code(exc: BaseException) -> str:
    # isinstance checks walk the dict in insertion order; AGMServerError and
    # AGMUnavailableError are siblings of AGMError, but ordering is kept
    # explicit so subclass additions can never shadow their parents.
    for cls in ERROR_CLASS_TO_CODE:
        if isinstance(exc, cls):
            return ERROR_CLASS_TO_CODE[cls]
    return "internal"


def _id_set(value) -> set:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set, frozenset)):
        result = set()
        for item in value:
            result |= _id_set(item)
        return result
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
        return {p.strip() for p in parts if p.strip()}
    try:
        text = str(value).strip()
    except Exception:
        return set()
    return {text} if text else set()


class AGMUsagePlugin(Star):
    """Star plugin exposing /agm and /agmdiag."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[AGMClient] = None
        self._parsed = None  # ParsedConfig
        self._last_query: dict = {}  # (platform, sender) -> monotonic ts
        self._busy = False
        # AGM update-notice state (in-memory only, never persisted): the last
        # derived notice ("" = none known), the monotonic timestamp of the last
        # background check we fired, and the single in-flight task handle.
        self._update_notice: str = ""
        self._last_update_check_ts: float | None = None
        self._update_check_task: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------------

    async def initialize(self) -> None:
        self._parsed = parse_config(self._config)
        self._enable_diag = bool(self._config.get("enable_diag", True))
        for w in self._parsed.warnings:
            logger.warning("agm_usage config: %s", w)
        if not self._parsed.valid:
            logger.warning("agm_usage: config invalid; /agm will report ERR_CONFIG")
            return
        self._session = aiohttp.ClientSession(
            trust_env=False,
            connector=aiohttp.TCPConnector(limit=4),
        )
        self._client = AGMClient(
            base_url=self._parsed.base_url,
            admin_password=self._parsed.admin_password,
            session=self._session,
            timeout_seconds=self._parsed.timeout_seconds,
            max_response_bytes=self._parsed.max_response_bytes,
        )
        logger.info("agm_usage initialized")

    async def terminate(self) -> None:
        # Cancel/await a pending background update check FIRST so it never
        # touches a closed session; safe when none was ever created.
        task = self._update_check_task
        self._update_check_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        session = self._session
        self._session = None
        self._client = None
        if session is not None and not session.closed:
            await session.close()

    # -- access control ---------------------------------------------------------

    def _event_ids(self, event: AstrMessageEvent):
        try:
            sender = event.get_sender_id()
        except Exception:
            sender = None
        try:
            group = event.get_group_id()
        except Exception:
            group = None
        return str(sender) if sender else None, str(group) if group else None

    def _deny_reason(self, event: AstrMessageEvent) -> str:
        """Return "" when allowed, else the user-facing denial text."""
        assert self._parsed is not None
        sender, group = self._event_ids(event)
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        users = _id_set(self._parsed.allowed_users)
        groups = _id_set(self._parsed.allowed_groups)
        private_chat = not group
        if private_chat:
            if not self._parsed.allow_private:
                return ERR_UNAUTHORIZED
            if is_admin and self._parsed.allow_admin:
                return ""
            if sender in users:
                return ""
            return ERR_UNAUTHORIZED
        if is_admin and self._parsed.allow_admin:
            return ""
        if sender not in users or group not in groups:
            return ERR_UNAUTHORIZED
        return ""

    def _cooldown_ok(self, event: AstrMessageEvent) -> bool:
        """Per (platform, sender) monotonic cooldown; failures also count."""
        assert self._parsed is not None
        try:
            sender = str(event.get_sender_id() or "unknown")
        except Exception:
            sender = "unknown"
        try:
            platform = str(getattr(event, "get_platform_name", lambda: "")() or "")
        except Exception:
            platform = ""
        key = (platform, sender)
        cooldown = self._parsed.cooldown_seconds
        now = time.monotonic()
        if cooldown > 0:
            self._last_query = {k: t for k, t in self._last_query.items() if (now - t) < cooldown}
            last = self._last_query.get(key)
            if last is not None and (now - last) < cooldown:
                return False
        self._last_query[key] = now
        return True

    # -- report ----------------------------------------------------------------

    # -- AGM update notice -------------------------------------------------------

    def _cfg_update_notice_enabled(self) -> bool:
        return bool(self._parsed.update_notice) if self._parsed is not None else False

    def _maybe_schedule_update_check(self) -> None:
        """Fire-and-forget background POST /api/system/updates/check.

        Time-throttled on a monotonic clock (not gated on "no data yet"): a
        failing or unreachable GitHub lookup must never cause a request storm,
        so the timestamp is stamped the moment we fire. Conditions, ALL needed:
        - update_notice config is true;
        - no check is currently in flight in this process;
        - our own monotonic timestamp is absent or older than the interval.
        The task swallows ALL exceptions: a failed update check must never
        surface to the user (it is logged at info once).
        """
        if not self._cfg_update_notice_enabled():
            return
        if self._update_check_task is not None and not self._update_check_task.done():
            return
        assert self._parsed is not None
        interval_hours = self._parsed.update_check_interval_hours
        now_mono = time.monotonic()
        if (
            self._last_update_check_ts is not None
            and (now_mono - self._last_update_check_ts) < interval_hours * 3600
        ):
            return
        client = self._client
        if client is None:
            return
        self._last_update_check_ts = now_mono
        self._update_check_task = asyncio.get_running_loop().create_task(
            self._background_update_check(client)
        )

    async def _background_update_check(self, client: AGMClient) -> None:
        """Run one AGM-side update check; on success remember the derived
        notice for the NEXT /agm. Never raises — every failure is logged and
        dropped. Always discards its own task handle so a later call can
        schedule again."""
        try:
            result = await client.check_updates()
            self._update_notice = derive_update_notice(result.value)
            logger.info(
                "agm_usage: background AGM update check done (notice=%s)",
                bool(self._update_notice),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never fatal, never shown
            logger.info(
                "agm_usage: background AGM update check failed (ignored): %s",
                type(exc).__name__,
            )
        finally:
            # Drop the handle so the next throttled window can schedule again;
            # the identity check leaves a newer task's handle untouched.
            if self._update_check_task is asyncio.current_task():
                self._update_check_task = None

    def report_notice(self) -> str:
        """The current in-memory AGM-update notice ("" = none).

        The background check sets it between /agm calls; it appears on the
        NEXT report, never retroactively. Kept as a method for clean test
        injection.
        """
        return self._update_notice

    async def build_report(self, now: datetime) -> UsageReport:
        """Concurrent read-only GETs; independent degradation per plan §4.5."""
        assert self._parsed is not None
        window = compute_window(now, self._parsed.default_timezone)
        client = self._client
        if client is None:
            raise AGMError("client unavailable")

        results = await asyncio.gather(
            client.accounts(),
            client.daily(2),
            client.weekly(1),
            client.summary(0),
            client.model_stats(24),
            client.model_stats(168),
            return_exceptions=True,
        )
        accounts_r, daily_r, weekly_r, all_r, models24_r, models168_r = results

        errors = []
        accounts = None
        if isinstance(accounts_r, BaseException):
            errors.append(_error_code(accounts_r))
        else:
            accounts = accounts_r.value
        all_time = None if isinstance(all_r, BaseException) else all_r.value
        if isinstance(all_r, BaseException):
            errors.append(_error_code(all_r))
        # 模型小节: fixed dual rolling windows (24h / 7d). H is NOT used for
        # models anymore — H only remains for the "today" fallback + diag.
        # Any window failing -> models None (renders the whole section as —)
        # rather than fabricating a two-column row from partial data.
        models = None
        if isinstance(models24_r, BaseException):
            errors.append(_error_code(models24_r))
        elif isinstance(models168_r, BaseException):
            errors.append(_error_code(models168_r))
        else:
            models = merge_model_windows(models24_r.value, models168_r.value)

        if accounts is None:
            raise AGMReportError(pick_error_text(errors) if errors else ERR_INTERNAL)

        # 当日: natural-day bucket from /api/stats/daily (AGM aggregates by
        # localtime periods). A rolling summary(hours=H) window would leak
        # yesterday's tail into "today", so it is only a FALLBACK when the
        # bucket is missing (usually means zero usage today).
        today = None
        if isinstance(daily_r, BaseException):
            errors.append(_error_code(daily_r))
        else:
            today_str = window.day.isoformat()
            today = next((t for period, t in daily_r.value if period == today_str), None)
            if today is None:
                # bucket missing -> fall back to the rolling window
                try:
                    today = (await client.summary(window.hours)).value
                except AGMError as exc:
                    errors.append(_error_code(exc))

        # 本周: natural-week bucket from /api/stats/weekly. AGM labels weeks
        # with strftime('%Y-W%W', localtime), Monday as the week start.
        week = None
        if isinstance(weekly_r, BaseException):
            errors.append(_error_code(weekly_r))
        else:
            week_label = f"{window.day.year}-W{int(window.day.strftime('%W')):02d}"
            week = next((t for period, t in weekly_r.value if period == week_label), None)
            if week is None:
                try:
                    week = (await client.summary(window.week_hours)).value
                except AGMError as exc:
                    errors.append(_error_code(exc))

        return UsageReport(
            instance=self._parsed.instance_name,
            window=window,
            today=today,
            week=week,
            all_time=all_time,
            accounts=accounts,
            models=models,
        )

    # -- commands --------------------------------------------------------------

    @filter.command("agm")
    async def agm(self, event: AstrMessageEvent) -> AsyncIterator:
        """/agm — AGM 用量报表（白名单受限）。"""
        if not self._cooldown_ok(event):
            yield event.plain_result(ERR_COOLDOWN)
            self._stop(event)
            return
        if self._parsed is None or not self._parsed.valid:
            yield event.plain_result(ERR_CONFIG)
            self._stop(event)
            return
        reason = self._deny_reason(event)
        if reason:
            yield event.plain_result(reason)
            self._stop(event)
            return
        if self._client is None:
            yield event.plain_result(ERR_CONFIG)
            self._stop(event)
            return
        if self._busy:
            yield event.plain_result(ERR_COOLDOWN)
            self._stop(event)
            return
        self._busy = True
        try:
            try:
                report = await self.build_report(datetime.now().astimezone())
            except AGMReportError as exc:
                yield event.plain_result(exc.text)
            except Exception:
                logger.exception("agm_usage: unexpected error")
                yield event.plain_result(ERR_INTERNAL)
            else:
                # Schedule (throttled, background) the AGM update check AFTER
                # this report was built: its result shows on the NEXT /agm,
                # so one reply is never delayed by the GitHub lookup.
                self._maybe_schedule_update_check()
                for page in render_messages(
                    report,
                    account_label=self._parsed.account_label,
                    show_models=self._parsed.show_model_breakdown,
                    max_lines=self._parsed.max_lines_per_message,
                    max_chars=self._parsed.max_message_chars,
                    notice=self.report_notice(),
                ):
                    yield event.plain_result(page)
        finally:
            self._busy = False
        self._stop(event)

    @filter.command("agmdiag")
    async def agmdiag(self, event: AstrMessageEvent) -> AsyncIterator:
        """/agmdiag — 诊断（不受白名单限制，受冷却限制；可被 enable_diag 关闭）。"""
        if not self._cooldown_ok(event):
            yield event.plain_result(ERR_COOLDOWN)
            self._stop(event)
            return
        if not getattr(self, "_enable_diag", True):
            # Cooldown is consumed first (anti-spam), but NO HTTP request is
            # made when diagnostics are disabled.
            yield event.plain_result(ERR_DIAG_DISABLED)
            self._stop(event)
            return
        lines = ["[AGM 诊断]"]
        try:
            diag_lines = await self.collect_diagnostics(datetime.now().astimezone())
        except Exception:
            logger.exception("agm_usage: diagnostics failed")
            diag_lines = ["诊断执行失败，请联系管理员查看日志。"]
        lines.extend(diag_lines)
        yield event.plain_result("\n".join(lines))
        self._stop(event)

    async def collect_diagnostics(self, now: datetime) -> list:
        """Offline + online diagnostics; never prints secrets or full emails."""
        assert self._parsed is not None
        lines = []
        p = self._parsed
        lines.append(f"配置解析: {'有效' if p.valid else '无效'}")
        if p.warnings:
            lines.append(f"配置警告: {len(p.warnings)} 项")
        lines.append(f"base_url: {p.base_url or '（未配置）'}")
        lines.append(f"default_timezone: {p.default_timezone}")
        client = self._client
        if client is None:
            lines.append("客户端: 未初始化（配置无效）")
            return lines
        window = compute_window(now, p.default_timezone)
        lines.append(f"H（当日小时数）: {window.hours}")
        lines.append(f"HW（本周小时数）: {window.week_hours}")
        # health (no auth header sent)
        try:
            r = await client.health()
            lines.append(f"/api/health: 200 status={r.value[0]} version={r.value[1]} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/health: 失败 ({exc.code})")
        # auth probe via accounts
        try:
            r = await client.accounts()
            lines.append(f"认证: 通过 (accounts n={len(r.value)}, {r.observation.elapsed_ms}ms)")
        except AGMAuthError:
            lines.append("认证: 失败 (401/403)")
        except AGMError as exc:
            lines.append(f"认证: 未知 ({exc.code})")
        # summary & models observations
        try:
            r = await client.summary(window.hours)
            lines.append(f"/api/stats/summary hours={window.hours}: 200 total={r.value:,.0f} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/stats/summary hours={window.hours}: 失败 ({exc.code})")
        try:
            r = await client.summary(window.week_hours)
            lines.append(f"/api/stats/summary hours={window.week_hours}: 200 total={r.value:,.0f} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/stats/summary hours={window.week_hours}: 失败 ({exc.code})")
        try:
            r = await client.summary(0)
            lines.append(f"/api/stats/summary hours=0: 200 total={r.value:,.0f} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/stats/summary hours=0: 失败 ({exc.code})")
        try:
            r = await client.model_stats(24)
            s24 = sum(t for _, t in r.value)
            lines.append(f"/api/stats/models hours=24: 200 models={len(r.value)} sum={s24:,.0f} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/stats/models hours=24: 失败 ({exc.code})")
            s24 = None
        try:
            r = await client.model_stats(168)
            s168 = sum(t for _, t in r.value)
            lines.append(f"/api/stats/models hours=168: 200 models={len(r.value)} sum={s168:,.0f} ({r.observation.elapsed_ms}ms)")
        except AGMError as exc:
            lines.append(f"/api/stats/models hours=168: 失败 ({exc.code})")
            s168 = None
        # daily bucket (source of 今日 in the report)
        today_str = window.day.isoformat()
        daily_bucket = None
        try:
            r = await client.daily(2)
            daily_bucket = next((t for period, t in r.value if period == today_str), None)
            if daily_bucket is None:
                lines.append(f"daily[{today_str}]: 桶缺失 → 当日回退滚动 summary({window.hours})")
            else:
                lines.append(f"daily[{today_str}]: {daily_bucket:,.0f}")
        except AGMError as exc:
            lines.append(f"/api/stats/daily: 失败 ({exc.code})")
        # weekly bucket (source of 本周)
        week_label = f"{window.day.year}-W{int(window.day.strftime('%W')):02d}"
        weekly_bucket = None
        try:
            r = await client.weekly(1)
            weekly_bucket = next((t for period, t in r.value if period == week_label), None)
            if weekly_bucket is None:
                lines.append(f"weekly[{week_label}]: 桶缺失 → 本周回退滚动 summary({window.week_hours})")
            else:
                lines.append(f"weekly[{week_label}]: {weekly_bucket:,.0f}")
        except AGMError as exc:
            lines.append(f"/api/stats/weekly: 失败 ({exc.code})")
        # rolling vs natural-bucket cross-check (also surfaces fallback usage)
        if daily_bucket is not None:
            try:
                sr = (await client.summary(window.hours)).value
                diff = sr - daily_bucket
                sign = "+" if diff >= 0 else ""
                lines.append(f"daily 交叉核验差值 (滚动H − 自然日): {sign}{diff:,.0f}")
            except AGMError:
                lines.append("daily 交叉核验: summary 不可用 → —")
            if s24 is not None:
                sign = "+" if s24 - daily_bucket >= 0 else ""
                lines.append(f"models(24h) 合计 − daily[today]: {sign}{s24 - daily_bucket:,.0f}"
                             "（模型为滚动窗口，与自然日/自然周不可直接对账）")
        if weekly_bucket is not None:
            try:
                sr = (await client.summary(window.week_hours)).value
                diff = sr - weekly_bucket
                sign = "+" if diff >= 0 else ""
                lines.append(f"weekly 交叉核验差值 (滚动HW − 自然周): {sign}{diff:,.0f}")
            except AGMError:
                lines.append("weekly 交叉核验: summary 不可用 → —")
            if s168 is not None:
                sign = "+" if s168 - weekly_bucket >= 0 else ""
                lines.append(f"models(168h) 合计 − weekly[label]: {sign}{s168 - weekly_bucket:,.0f}"
                             "（模型为滚动窗口，与自然日/自然周不可直接对账）")
        # clock skew
        import email.utils as _eu
        lines.append(f"本机时间: {now.isoformat()}")
        # AGM update notice state — OFFLINE (no extra request is made here; the
        # value was captured by a previous /agm's background check).
        if not self._cfg_update_notice_enabled():
            lines.append(DIAG_UPDATE_OFF)
        elif self._update_notice:
            version = notice_version(self._update_notice)
            lines.append(f"AGM 更新提示: 有 v{version}" if version else DIAG_UPDATE_NONE)
        else:
            lines.append(DIAG_UPDATE_NONE)
        return lines

    @staticmethod
    def _stop(event: AstrMessageEvent) -> None:
        try:
            event.stop_event()
        except Exception:
            pass
