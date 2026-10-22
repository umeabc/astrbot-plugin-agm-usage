"""Diagnostic tests: endpoints, redaction, differences, cooldown."""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import pytest
from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_agm_usage.main import AGMUsagePlugin

from conftest import make_account, make_accounts_payload, make_quota


async def start(stub, **over):
    from astrbot.api import AstrBotConfig
    cfg = AstrBotConfig({"base_url": stub.base_url, "admin_password": "dummy-password",
                         "cooldown_seconds": 0, "enable_diag": True})
    cfg.update(over)
    plugin = AGMUsagePlugin(context=None, config=cfg)
    await plugin.initialize()
    return plugin


async def collect(agen):
    return [x async for x in agen]


async def test_diag_all_endpoints_and_differences(stub_agm):
    stub_agm.accounts_payload = make_accounts_payload([make_account("a1")])
    stub_agm.daily_payload = [{"period": "2026-10-22", "total_tokens": 500},
                              {"period": "2026-10-21", "total_tokens": 100}]
    plugin = await start(stub_agm)
    lines = await plugin.collect_diagnostics(datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc))
    text = "\n".join(lines)
    assert "/api/health" in text
    assert "认证: 通过" in text
    assert "H（当日小时数）" in text and "HW（本周小时数）" in text
    assert "daily 交叉核验差值" in text
    assert "本机时间" in text
    await plugin.terminate()


async def test_diag_daily_missing_bucket(stub_agm):
    stub_agm.daily_payload = [{"period": "2026-10-20", "total_tokens": 100}]
    plugin = await start(stub_agm)
    lines = await plugin.collect_diagnostics(datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc))
    assert any("桶缺失" in l and "回退" in l for l in lines)
    await plugin.terminate()


async def test_diag_auth_and_secret_redaction(stub_agm):
    plugin = await start(stub_agm, admin_password="wrong-password")
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agmdiag(ev))
    text = outs[0][1]
    assert "wrong-password" not in text
    assert "dummy-password" not in text
    assert "Authorization" not in text
    assert "认证: 失败" in text
    assert ev.stopped
    await plugin.terminate()


async def test_diag_cooldown_applies(stub_agm):
    plugin = await start(stub_agm, cooldown_seconds=30)
    ev = AstrMessageEvent(sender="u1", group=None, admin=False)
    await collect(plugin.agmdiag(ev))
    outs = await collect(plugin.agmdiag(ev))
    assert outs == [("plain", "查询过于频繁，请稍后重试。")]
    await plugin.terminate()


async def test_diag_health_ok_does_not_mean_auth_ok(stub_agm):
    # health needs no auth; with a wrong password health succeeds but auth fails
    plugin = await start(stub_agm, admin_password="nope")
    lines = await plugin.collect_diagnostics(datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc))
    text = "\n".join(lines)
    assert "/api/health: 200" in text
    assert "认证: 失败" in text
    await plugin.terminate()


async def test_diag_disabled_returns_fixed_text(stub_agm):
    """enable_diag=false: fixed reply, zero HTTP requests, stop_event called."""
    from astrbot_plugin_agm_usage.reporting import ERR_COOLDOWN
    plugin = await start(stub_agm, enable_diag=False, cooldown_seconds=30)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    calls_before = len(stub_agm.calls)
    outs = await collect(plugin.agmdiag(ev))
    assert outs == [("plain", "诊断命令已关闭。")]
    assert len(stub_agm.calls) == calls_before  # zero HTTP requests
    assert ev.stopped
    # still respects cooldown (consumed by the disabled call above)
    ev2 = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs2 = await collect(plugin.agmdiag(ev2))
    assert outs2 == [("plain", ERR_COOLDOWN)]
    await plugin.terminate()


async def test_diag_enabled_by_default(stub_agm):
    plugin = await start(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agmdiag(ev))
    assert outs[0][1].startswith("[AGM 诊断]")
    assert len(stub_agm.calls) > 0
    await plugin.terminate()
