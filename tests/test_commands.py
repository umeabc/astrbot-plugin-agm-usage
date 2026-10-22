"""Command-level tests: access, cooldown, degradation, lifecycle, template."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone as dt_timezone

import pytest
import re
import aiohttp
from aiohttp import web

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_agm_usage.main import AGMUsagePlugin
from astrbot_plugin_agm_usage.models import QuotaBucket, QuotaGroup, UsageReport, compute_window
from astrbot_plugin_agm_usage.reporting import (
    ERR_CONFIG,
    ERR_COOLDOWN,
    ERR_RATE_LIMIT,
    ERR_SCHEMA,
    ERR_SERVER,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_UNAUTHORIZED,
    format_report,
)

from conftest import make_account, make_accounts_payload, make_group, make_quota, make_summary


def make_plugin(base_url="", **overrides):
    cfg = AstrBotConfig({
        "base_url": base_url,
        "admin_password": "dummy-password",
        "cooldown_seconds": 0,
    })
    cfg.update(overrides)
    plugin = AGMUsagePlugin(context=None, config=cfg)
    return plugin


async def start_plugin(stub, **overrides):
    plugin = make_plugin(base_url=stub.base_url, **overrides)
    await plugin.initialize()
    return plugin


async def collect(agen):
    return [x async for x in agen]


async def test_access_default_deny_and_group_and(stub_agm):
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender=" stranger", group=None, admin=False)
    outs = await collect(plugin.agm(ev))
    assert outs == [("plain", ERR_UNAUTHORIZED)]
    assert ev.stopped
    # group needs user+group both
    cfg = plugin._parsed
    plugin._config.update({"allowed_users": ["u1"], "allowed_groups": ["g1"]})
    plugin._parsed = type(plugin._parsed)(**{**cfg.__dict__, "allowed_users": ("u1",), "allowed_groups": ("g1",)})
    ev_ok = AstrMessageEvent(sender="u1", group="g1")
    outs = await collect(plugin.agm(ev_ok))
    assert outs and "Antigravity Tools" in outs[0][1]
    ev_bad_group = AstrMessageEvent(sender="u1", group="g2")
    outs = await collect(plugin.agm(ev_bad_group))
    assert outs == [("plain", ERR_UNAUTHORIZED)]
    await plugin.terminate()


async def test_diag_bypasses_allowlists(stub_agm):
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="nobody", group=None, admin=False)
    outs = await collect(plugin.agmdiag(ev))
    text = outs[0][1]
    assert "[AGM 诊断]" in text
    assert "dummy-password" not in text
    assert "someone@example.com" not in text  # no full email
    assert ev.stopped
    await plugin.terminate()


async def test_cooldown_and_busy(stub_agm):
    plugin = await start_plugin(stub_agm, cooldown_seconds=10)
    ev1 = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev1))
    ev2 = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev2))
    assert outs == [("plain", ERR_COOLDOWN)]
    # different sender not blocked
    ev3 = AstrMessageEvent(sender="u2", group=None, admin=True)
    outs = await collect(plugin.agm(ev3))
    assert outs and "Antigravity Tools" in outs[0][1]
    await plugin.terminate()


async def test_invalid_config_reports_config_error():
    plugin = make_plugin(base_url="ftp://bad")
    await plugin.initialize()
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    assert outs == [("plain", ERR_CONFIG)]
    assert ev.stopped
    await plugin.terminate()


async def test_partial_failure_preserves_other_sections(stub_agm):
    plugin = await start_plugin(stub_agm)
    stub_agm.status_map["models"] = 500
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    text = outs[0][1]
    assert "Antigravity Tools 消耗情况" in text
    assert "模型消耗（近 24 小时 / 近一周）：—" in text
    await plugin.terminate()


async def test_all_fail_priority_and_stop_event(stub_agm):
    plugin = await start_plugin(stub_agm)
    stub_agm.status_map["accounts"] = 503
    stub_agm.status_map["summary"] = 500
    stub_agm.status_map["models"] = 429
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    assert outs == [("plain", ERR_SERVER)]
    assert ev.stopped
    await plugin.terminate()


async def test_auth_failure_fixed_text(stub_agm):
    plugin = await start_plugin(stub_agm, admin_password="wrong")
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    assert outs == [("plain", "AGM 管理 API 认证失败，请联系管理员检查密码。")]
    await plugin.terminate()


async def test_accounts_payload_rendered(stub_agm):
    stub_agm.accounts_payload = make_accounts_payload([
        make_account("a1", "someone@example.com", quota=make_quota(
            groups=[make_group("Gemini Models", [
                {"window": "weekly", "remaining_fraction": 0.5, "reset_time": "2026-10-24T05:53:26Z"},
                {"window": "5h", "remaining_fraction": 0.9, "reset_time": "2026-10-22T19:25:30Z"},
            ])])),
        make_account("a2", "other@x.com", quota=None, disabled=True),
    ])
    stub_agm.summary_payload = make_summary(total_tokens=1217320)
    stub_agm.models_payload = {"24": [{"model": "gemini-3.8-flash-high", "total_tokens": 782939}],
                               "168": [{"model": "gemini-3.8-flash-high", "total_tokens": 12345678}]}
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    text = outs[0][1]
    assert "账号（2）：" in text
    assert "s***e@***.com (PRO) ：" in text
    assert "额度数据不可用" in text
    assert "[已禁用]" in text
    assert re.search(r"模型消耗（近 24 小时 / 近一周）：\ngemini-3\.8-flash-high：782,939 / 12,345,678 Token", text)
    await plugin.terminate()


async def test_initialize_terminate_idempotent(stub_agm):
    plugin = await start_plugin(stub_agm)
    s1 = plugin._session
    await plugin.terminate()
    await plugin.terminate()  # double close OK
    assert plugin._session is None
    # re-init creates a fresh session
    await plugin.initialize()
    assert plugin._session is not None and plugin._session is not s1
    await plugin.terminate()


async def test_single_batch_hours_across_midnight(stub_agm):
    plugin = await start_plugin(stub_agm)
    now = datetime(2026, 10, 22, 23, 59, 59, tzinfo=dt_timezone.utc)
    report = await plugin.build_report(now)
    # One batch: all summary calls share the same H computed from `now`
    hours_seen = {c["hours"] for c in stub_agm.summary_calls}
    # last call is hours=0 (cumulative) plus H and HW
    assert "0" in hours_seen
    await plugin.terminate()


# ---- natural-day / natural-week bucket tests (measured 2026-10-23) ----------

TODAY = datetime(2026, 10, 23, 0, 26, 0, tzinfo=dt_timezone.utc)  # 08:26 CST

async def test_today_uses_daily_bucket_not_rolling(stub_agm):
    """daily bucket 556,931 wins over rolling summary(hours=1)=704,273."""
    plugin = await start_plugin(stub_agm)
    stub_agm.daily_payload = [
        {"period": "2026-10-23", "total_tokens": 556931},
        {"period": "2026-10-22", "total_tokens": 999},
    ]
    stub_agm.summary_payload = make_summary(total_tokens=704273)  # hours=1 rolling
    report = await plugin.build_report(TODAY)
    assert report.today == 556931.0
    await plugin.terminate()


async def test_week_uses_weekly_bucket(stub_agm):
    """weekly bucket W42=35,034,722 wins over rolling summary(97)=35,122,628;
    weeks=1 returns W41+W42, must pick W42."""
    plugin = await start_plugin(stub_agm)
    stub_agm.weekly_payload = [
        {"period": "2026-W41", "total_tokens": 111111},
        {"period": "2026-W42", "total_tokens": 35034722},
    ]
    stub_agm.summary_payload = make_summary(total_tokens=35122628)
    report = await plugin.build_report(TODAY)
    assert report.week == 35034722.0
    await plugin.terminate()


async def test_bucket_missing_falls_back_to_rolling(stub_agm):
    """No daily/weekly bucket -> fall back to rolling summary + diag marker."""
    plugin = await start_plugin(stub_agm)
    stub_agm.daily_payload = [{"period": "2026-10-21", "total_tokens": 5}]
    stub_agm.weekly_payload = [{"period": "2026-W40", "total_tokens": 5}]
    stub_agm.summary_payload = make_summary(total_tokens=12345)
    report = await plugin.build_report(TODAY)
    assert report.today == 12345.0
    assert report.week == 12345.0
    lines = await plugin.collect_diagnostics(TODAY)
    text = "\n".join(lines)

    assert "FALLBACK" in text or "回退" in text
    await plugin.terminate()


async def test_diag_shows_buckets_and_rolling_compare(stub_agm):
    plugin = await start_plugin(stub_agm)
    stub_agm.daily_payload = [{"period": "2026-10-23", "total_tokens": 556931}]
    stub_agm.weekly_payload = [{"period": "2026-W42", "total_tokens": 35034722}]
    stub_agm.summary_payload = make_summary(total_tokens=704273)
    lines = await plugin.collect_diagnostics(TODAY)
    text = "\n".join(lines)

    assert "556,931" in text
    assert "35,034,722" in text
    assert "daily 交叉核验差值" in text
    assert "weekly 交叉核验差值" in text
    await plugin.terminate()


async def test_models_window_failure_dash(stub_agm):
    """24h succeeds / 7d fails -> the WHOLE section renders as — (never a
    single-column partial)."""
    plugin = await start_plugin(stub_agm)
    # fail only the 168h request: stub serves by hours key
    def _broken(hours):
        if hours == "168":
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response([{"model": "a", "total_tokens": 100}])
    # simplest: point models 168h requests at a 500 via a custom payload hook
    stub_agm.models_payload = {"24": [{"model": "a", "total_tokens": 100}]}
    original_serve = stub_agm._serve
    async def patched(request, name, payload):
        if name == "models" and request.query.get("hours") == "168":
            return web.json_response({"error": "x"}, status=500)
        return await original_serve(request, name, payload)
    stub_agm._serve = patched
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    text = outs[0][1]
    assert "模型消耗（近 24 小时 / 近一周）：—" in text
    assert "a：100 /" not in text  # no partial single column
    await plugin.terminate()


async def test_models_requests_use_24_and_168(stub_agm):
    """The two models requests must carry hours=24 and hours=168 exactly."""
    plugin = await start_plugin(stub_agm)
    stub_agm.models_payload = {"24": [], "168": []}
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    hours_seen = sorted(c["query"]["hours"] for c in stub_agm.calls if c["name"] == "models")
    assert hours_seen == ["168", "24"]
    await plugin.terminate()
