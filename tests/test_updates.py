"""v0.3.0 — AGM 5.0.0 support + upstream update notice.

Covers: notice decision (pure), report rendering, the AGM-side update check
request, the time-throttled background task, terminate cleanup, and the AGM
5.0.0 additive `qos_status` / `qos_reset_seconds` account fields.

All offline (aiohttp stub server + pure functions).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_agm_usage.client import (
    AGMClient,
    AGMServerError,
    validate_accounts,
)
from astrbot_plugin_agm_usage.main import (
    DIAG_UPDATE_NONE,
    DIAG_UPDATE_OFF,
    AGMUsagePlugin,
)
from astrbot_plugin_agm_usage.reporting import (
    UPDATE_NOTICE_FORMAT,
    append_update_notice,
    derive_update_notice,
    format_account_section,
    format_report,
    notice_version,
    parse_config,
    parse_version_tuple,
    render_messages,
)
from astrbot_plugin_agm_usage.models import (
    AccountInfo,
    QuotaBucket,
    QuotaGroup,
    compute_window,
)

from conftest import make_account, make_accounts_payload, make_group, make_quota


NOW = None  # set per-test from compute_window for portability


def _aware():
    from datetime import datetime, timezone as dt_timezone
    return datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc)


def _notice_payload(current="5.0.0", latest="5.0.1", **extra):
    payload = {
        "current_version": current,
        "latest_version": latest,
        "has_update": True,
        "download_url": "https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v5.0.1",
        "release_notes": "",
        "published_at": "",
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Pure: version parsing / notice decision
# ---------------------------------------------------------------------------


class TestParseVersionTuple:
    def test_three_part(self):
        assert parse_version_tuple("5.0.0") == (5, 0, 0)
        assert parse_version_tuple("v5.0.0") == (5, 0, 0)
        assert parse_version_tuple(" 5.0.0 ") == (5, 0, 0)

    def test_two_and_one_part_tolerated(self):
        # AGM's updater.json is not guaranteed to be three-part.
        assert parse_version_tuple("5.0") == (5, 0, 0)
        assert parse_version_tuple("5") == (5, 0, 0)

    def test_prerelease_uses_numeric_core(self):
        # AGM ships prereleases (5.0.0-beta.2): only the numeric core is kept,
        # so a prerelease never claims to be newer than its own stable core.
        assert parse_version_tuple("5.0.0-beta.2") == (5, 0, 0)

    def test_prerelease_never_beats_same_stable_core(self):
        payload = _notice_payload(current="5.0.0", latest="5.0.0-beta.2")
        assert derive_update_notice(payload) == ""
        # ...and a newer core still wins over a prerelease current version.
        payload = _notice_payload(current="5.0.0-beta.2", latest="5.0.1")
        assert derive_update_notice(payload) == "AGM 有更新，最新版本号v5.0.1"

    def test_unparseable(self):
        for bad in ("", "  ", "dev", "v", "5.0.0.1", None, 500, 5.0):
            assert parse_version_tuple(bad) is None


class TestDeriveUpdateNotice:
    def test_newer_shown(self):
        assert derive_update_notice(_notice_payload()) == "AGM 有更新，最新版本号v5.0.1"

    def test_equal_no_notice(self):
        assert derive_update_notice(_notice_payload(latest="5.0.0")) == ""

    def test_older_no_notice(self):
        assert derive_update_notice(_notice_payload(current="5.0.1", latest="5.0.0")) == ""

    def test_leading_v_not_doubled(self):
        text = derive_update_notice(_notice_payload(current="v5.0.0", latest="v5.0.1"))
        assert text == "AGM 有更新，最新版本号v5.0.1"
        assert "vv" not in text

    def test_missing_versions(self):
        assert derive_update_notice({"latest_version": "5.0.1"}) == ""
        assert derive_update_notice({"current_version": "5.0.0"}) == ""
        assert derive_update_notice({}) == ""

    def test_unparseable(self):
        assert derive_update_notice(_notice_payload(current="dev")) == ""
        assert derive_update_notice(_notice_payload(latest="not-a-version")) == ""

    def test_has_update_alone_not_trusted(self):
        # AGM's own boolean says "update" but the versions are equal.
        payload = _notice_payload(latest="5.0.0", has_update=True)
        assert derive_update_notice(payload) == ""

    def test_has_update_false_but_newer_still_shown(self):
        payload = _notice_payload(latest="5.0.1", has_update=False)
        assert derive_update_notice(payload) == "AGM 有更新，最新版本号v5.0.1"

    def test_not_a_mapping(self):
        assert derive_update_notice(None) == ""
        assert derive_update_notice("oops") == ""  # type: ignore[arg-type]

    def test_notice_version_roundtrip(self):
        notice = derive_update_notice(_notice_payload())
        assert notice_version(notice) == "5.0.1"
        assert notice_version("") == ""
        assert notice_version("完全不是提示") == ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _report(accounts=(), models=(), today=1.0):
    from astrbot_plugin_agm_usage.models import UsageReport
    now = _aware()
    return UsageReport(instance="AGM", window=compute_window(now, "Asia/Shanghai"),
                       today=today, week=2.0, all_time=3.0,
                       accounts=tuple(accounts), models=tuple(models))


def test_append_notice_is_last_line_and_empty_is_identity():
    base = "a\nb"
    assert append_update_notice(base, "") == base
    assert append_update_notice(base, "提示") == "a\nb\n提示"


def test_notice_lands_on_report_last_line():
    report = _report([AccountInfo(id="a1")], models=(("m", 1.0, 2.0),))
    plain = render_messages(report, max_lines=50, max_chars=5000)[0]
    assert plain.split("\n")[-1] == "m：1 / 2 Token"
    with_notice = render_messages(report, max_lines=50, max_chars=5000,
                                  notice=UPDATE_NOTICE_FORMAT.format(version="5.0.1"))[0]
    lines = with_notice.split("\n")
    assert lines[-1] == "AGM 有更新，最新版本号v5.0.1"
    assert lines[:-1] == plain.split("\n")
    # Still one page: the page prefix must NOT appear for a single page.
    assert not with_notice.startswith("[AGM ")


def test_notice_survives_pagination_as_last_line():
    accounts = [AccountInfo(id=f"acct{i:03d}") for i in range(40)]
    report = _report(accounts, models=(("m", 1.0, 2.0),))
    notice = UPDATE_NOTICE_FORMAT.format(version="5.0.1")
    pages = render_messages(report, max_lines=10, max_chars=400, notice=notice)
    assert len(pages) > 1
    assert pages[-1].split("\n")[-1] == notice
    # Prefix only appears because there really are multiple pages.
    assert pages[0].startswith("[AGM 1/")


# ---------------------------------------------------------------------------
# AGM 5.0.0 additive account fields (qos_status / qos_reset_seconds)
# ---------------------------------------------------------------------------


def test_validate_accounts_without_qos_fields_is_unchanged():
    result = validate_accounts(make_accounts_payload([make_account("a1")]))
    assert result[0].qos_status is None
    assert result[0].qos_reset_seconds is None


def test_validate_accounts_reads_qos_fields():
    entry = make_account("a1")
    entry["qos_status"] = "jailed"
    entry["qos_reset_seconds"] = 420
    result = validate_accounts(make_accounts_payload([entry]))
    assert result[0].qos_status == "jailed"
    assert result[0].qos_reset_seconds == 420


def test_validate_accounts_tolerates_bad_qos_values():
    entry = make_account("a1")
    entry["qos_status"] = 123          # wrong type -> ignored
    entry["qos_reset_seconds"] = -5    # negative -> ignored
    result = validate_accounts(make_accounts_payload([entry]))
    assert result[0].qos_status is None
    assert result[0].qos_reset_seconds is None


def _acct(**kw):
    base = dict(id="a1", email="", name="", quota={}, quota_groups=(), subscription_tier="PRO")
    base.update(kw)
    return AccountInfo(**base)


def test_qos_marker_rendered_with_countdown():
    acct = _acct(qos_status="jailed", qos_reset_seconds=420)
    lines = format_account_section(acct, _aware(), account_label="short_id")
    assert lines[0] == "a1 (PRO) ： [QoS隔离 7分后解除]"


def test_qos_marker_without_remaining_seconds():
    acct = _acct(qos_status="probing")
    lines = format_account_section(acct, _aware(), account_label="short_id")
    # probing is transient: no reset time, just the label
    assert lines[0] == "a1 (PRO) ： [QoS探针]"


def test_qos_zero_remaining_renders_bare_label():
    acct = _acct(qos_status="jailed", qos_reset_seconds=0)
    lines = format_account_section(acct, _aware(), account_label="short_id")
    assert lines[0] == "a1 (PRO) ： [QoS隔离]"


def test_healthy_qos_is_null_so_no_marker():
    lines = format_account_section(_acct(qos_status=None), _aware(), account_label="short_id")
    assert lines[0] == "a1 (PRO) ："


def test_unknown_qos_state_ignored():
    lines = format_account_section(_acct(qos_status="brand_new_state"), _aware(),
                                   account_label="short_id")
    assert lines[0] == "a1 (PRO) ："


def test_qos_combines_with_existing_markers():
    acct = _acct(disabled=True, validation_blocked=True,
                 qos_status="half_open", qos_reset_seconds=60)
    lines = format_account_section(acct, _aware(), account_label="short_id")
    assert lines[0] == "a1 (PRO) ： [已禁用] [校验阻塞] [QoS缓刑 1分后解除]"


def test_qos_marker_has_no_markdown_triggers():
    acct = _acct(qos_status="suspicious", qos_reset_seconds=90)
    text = format_report(_report([acct]), account_label="short_id")
    for token in ("*", "_", "`", "~~"):
        assert token not in text
    assert "a1 (PRO) ： [QoS可疑 1分后解除]" in text


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults_for_update_fields():
    parsed = parse_config({"base_url": "http://x", "admin_password": "p"})
    assert parsed.update_notice is True
    assert parsed.update_check_interval_hours == 6


def test_config_interval_clamped():
    parsed = parse_config({"base_url": "http://x", "admin_password": "p",
                           "update_check_interval_hours": 9999})
    assert parsed.update_check_interval_hours == 168
    assert any("update_check_interval_hours" in w for w in parsed.warnings)
    parsed = parse_config({"base_url": "http://x", "admin_password": "p",
                           "update_check_interval_hours": 0})
    assert parsed.update_check_interval_hours == 1


def test_config_notice_can_be_disabled():
    parsed = parse_config({"base_url": "http://x", "admin_password": "p",
                           "update_notice": False})
    assert parsed.update_notice is False


# ---------------------------------------------------------------------------
# Client: the update-check request
# ---------------------------------------------------------------------------


def make_client(stub, session, **kw):
    return AGMClient(base_url=stub.base_url, admin_password=stub.password,
                     session=session, **kw)


async def test_check_updates_posts_and_authenticates(stub_agm, session):
    stub_agm.updates_payload = _notice_payload()
    client = make_client(stub_agm, session)
    result = await client.check_updates()
    call = next(c for c in stub_agm.calls if c["name"] == "updates")
    assert call["method"] == "POST"
    assert call["auth"] == "Bearer dummy-password"
    assert result.value["latest_version"] == "5.0.1"


async def test_check_updates_maps_errors(stub_agm, session):
    stub_agm.status_map["updates"] = 500
    client = make_client(stub_agm, session)
    with pytest.raises(AGMServerError):
        await client.check_updates()


# ---------------------------------------------------------------------------
# Plugin: background check + notice wiring
# ---------------------------------------------------------------------------


def make_plugin(base_url="", **overrides):
    cfg = AstrBotConfig({
        "base_url": base_url,
        "admin_password": "dummy-password",
        "cooldown_seconds": 0,
    })
    cfg.update(overrides)
    return AGMUsagePlugin(context=None, config=cfg)


async def start_plugin(stub, **overrides):
    plugin = make_plugin(base_url=stub.base_url, **overrides)
    await plugin.initialize()
    return plugin


async def collect(agen):
    return [x async for x in agen]


async def _settle(plugin, timeout=5.0):
    """Wait for the background update task (if any) to finish."""
    task = plugin._update_check_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)


async def test_first_report_has_no_notice_then_next_reports_it(stub_agm):
    stub_agm.updates_payload = _notice_payload(current="5.0.0", latest="5.0.1")
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    text = outs[0][1]
    # The check was scheduled, but the notice is never retroactive.
    assert "AGM 有更新" not in text
    await _settle(plugin)

    ev2 = AstrMessageEvent(sender="u2", group=None, admin=True)
    outs2 = await collect(plugin.agm(ev2))
    assert outs2[0][1].split("\n")[-1] == "AGM 有更新，最新版本号v5.0.1"
    await plugin.terminate()


async def test_up_to_date_produces_no_notice(stub_agm):
    stub_agm.updates_payload = _notice_payload(current="5.0.0", latest="5.0.0")
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    await _settle(plugin)
    assert plugin.report_notice() == ""
    ev2 = AstrMessageEvent(sender="u2", group=None, admin=True)
    outs = await collect(plugin.agm(ev2))
    assert "AGM 有更新" not in outs[0][1]
    await plugin.terminate()


async def test_check_failure_is_silent_and_does_not_break_report(stub_agm):
    stub_agm.status_map["updates"] = 500
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    outs = await collect(plugin.agm(ev))
    assert "Antigravity Tools 消耗情况" in outs[0][1]
    await _settle(plugin)
    assert plugin.report_notice() == ""
    ev2 = AstrMessageEvent(sender="u2", group=None, admin=True)
    outs2 = await collect(plugin.agm(ev2))
    assert "AGM 有更新" not in outs2[0][1]
    await plugin.terminate()


async def test_notice_disabled_makes_no_updates_request(stub_agm):
    plugin = await start_plugin(stub_agm, update_notice=False)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    assert not [c for c in stub_agm.calls if c["name"] == "updates"]
    await plugin.terminate()


async def test_check_is_time_throttled(stub_agm):
    plugin = await start_plugin(stub_agm, update_check_interval_hours=6)
    ev1 = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev1))
    await _settle(plugin)
    assert len([c for c in stub_agm.calls if c["name"] == "updates"]) == 1
    # A second /agm within the interval must not fire another check.
    ev2 = AstrMessageEvent(sender="u2", group=None, admin=True)
    await collect(plugin.agm(ev2))
    await _settle(plugin)
    assert len([c for c in stub_agm.calls if c["name"] == "updates"]) == 1
    # Rewinding our own throttle timestamp fires exactly one more.
    plugin._last_update_check_ts = time.monotonic() - 7 * 3600
    ev3 = AstrMessageEvent(sender="u3", group=None, admin=True)
    await collect(plugin.agm(ev3))
    await _settle(plugin)
    assert len([c for c in stub_agm.calls if c["name"] == "updates"]) == 2
    await plugin.terminate()


async def test_single_in_flight_check(stub_agm):
    stub_agm.delay_map["updates"] = 0.3
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    plugin._last_update_check_ts = None  # pretend the throttle expired
    ev2 = AstrMessageEvent(sender="u2", group=None, admin=True)
    await collect(plugin.agm(ev2))
    await _settle(plugin)
    assert len([c for c in stub_agm.calls if c["name"] == "updates"]) == 1
    await plugin.terminate()


async def test_terminate_cancels_in_flight_check(stub_agm):
    stub_agm.delay_map["updates"] = 5.0
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    task = plugin._update_check_task
    assert task is not None and not task.done()
    await plugin.terminate()  # must cancel and await it before closing
    assert task.done()
    assert plugin._update_check_task is None
    assert plugin._session is None


async def test_report_failure_does_not_schedule_check(stub_agm):
    stub_agm.status_map["accounts"] = 503
    plugin = await start_plugin(stub_agm)
    ev = AstrMessageEvent(sender="u1", group=None, admin=True)
    await collect(plugin.agm(ev))
    assert plugin._update_check_task is None
    assert not [c for c in stub_agm.calls if c["name"] == "updates"]
    await plugin.terminate()


async def test_diag_reports_notice_state_offline(stub_agm):
    from datetime import datetime, timezone as dt_timezone
    plugin = await start_plugin(stub_agm)
    lines = await plugin.collect_diagnostics(
        datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc))
    assert DIAG_UPDATE_NONE in lines
    plugin._update_notice = "AGM 有更新，最新版本号v5.0.1"
    lines = await plugin.collect_diagnostics(
        datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc))
    assert "AGM 更新提示: 有 v5.0.1" in lines
    assert not [c for c in stub_agm.calls if c["name"] == "updates"]
    await plugin.terminate()


async def test_diag_reports_notice_off_when_disabled(stub_agm):
    from datetime import datetime, timezone as dt_timezone
    plugin = await start_plugin(stub_agm, update_notice=False)
    lines = await plugin.collect_diagnostics(
        datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc))
    assert DIAG_UPDATE_OFF in lines
    await plugin.terminate()
