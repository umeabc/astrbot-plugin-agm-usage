"""Reporting tests: windows, masking, countdown, template, pagination."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone, date
from zoneinfo import ZoneInfo

import pytest
import re

from astrbot_plugin_agm_usage.client import AGMSchemaError
from astrbot_plugin_agm_usage.models import AccountInfo, QuotaBucket, QuotaGroup, UsageReport, compute_window
from astrbot_plugin_agm_usage.reporting import (
    ERR_AUTH,
    ERR_CONFIG,
    ERR_COOLDOWN,
    ERR_INTERNAL,
    ERR_JSON,
    ERR_RATE_LIMIT,
    ERR_SCHEMA,
    ERR_SERVER,
    ERR_TIMEOUT,
    ERR_TOO_LARGE,
    ERR_UNAVAILABLE,
    ERR_UNAUTHORIZED,
    ERROR_PRIORITY,
    error_text_for_code,
    format_account_section,
    format_countdown,
    format_report,
    mask_email,
    merge_model_windows,
    paginate,
    parse_config,
    pick_error_text,
)


# -- windows ---------------------------------------------------------------

def _aware(y, mo, d, h, mi=0, s=0, tz="Asia/Shanghai"):
    return datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz))


def test_hours_midnight_exact_hour_and_second():
    assert compute_window(_aware(2026, 10, 22, 0, 0, 0), "Asia/Shanghai").hours == 1
    assert compute_window(_aware(2026, 10, 22, 0, 1, 0), "Asia/Shanghai").hours == 1
    assert compute_window(_aware(2026, 10, 22, 10, 0, 0), "Asia/Shanghai").hours == 10
    assert compute_window(_aware(2026, 10, 22, 10, 0, 1), "Asia/Shanghai").hours == 11
    assert compute_window(_aware(2026, 10, 22, 23, 59, 0), "Asia/Shanghai").hours == 24


def test_week_hours_monday_boundary():
    # 2026-10-19 is a Monday
    assert compute_window(_aware(2026, 10, 19, 0, 0, 0), "Asia/Shanghai").week_hours == 1
    # 2026-10-25 is a Sunday
    assert compute_window(_aware(2026, 10, 25, 23, 59, 0), "Asia/Shanghai").week_hours == 168


def test_timezone_utc_and_dst_and_naive_rejected():
    utc_now = datetime(2026, 10, 22, 20, 0, tzinfo=dt_timezone.utc)
    w = compute_window(utc_now, "Asia/Shanghai")
    assert w.day == date(2026, 10, 23)  # 20:00 UTC = next day 04:00 CST
    with pytest.raises(ValueError):
        compute_window(datetime(2026, 10, 22, 10, 0), "Asia/Shanghai")


# -- masking ---------------------------------------------------------------

def test_email_mask_rules():
    assert mask_email("someone@example.com") == "s***e@***.com"
    assert mask_email("ab@x.com") == "a***@***.com"  # short local part
    assert mask_email("a@x.com") == "a***@***.com"
    assert mask_email("user@example.com", mask=False) == "user@example.com"
    assert mask_email("") == "—"


# -- countdown ---------------------------------------------------------------

def test_countdown_formats():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
    assert format_countdown(now + timedelta(hours=4, minutes=42), now) == "4小时42分"
    assert format_countdown(now + timedelta(days=6, hours=22, minutes=42), now) == "6天22小时42分"
    assert format_countdown(now + timedelta(minutes=3), now) == "3分"
    assert format_countdown(now - timedelta(seconds=1), now) == "即将"
    assert format_countdown(None, now) == "即将"


# -- account rendering -------------------------------------------------------

def _acct(groups=None, disabled=False, forbidden=False, blocked=False,
          tier="PRO", email="someone@example.com", quota=None):
    return AccountInfo(
        id="a1", email=email, name="acct",
        disabled=disabled, validation_blocked=blocked,
        quota=quota if quota is not None else ({} if groups is not None else None),
        quota_groups=tuple(groups or ()),
        subscription_tier=tier,
        is_forbidden=forbidden,
    )


def test_quota_groups_render_and_countdown():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
    gem = QuotaGroup(display_name="Gemini Models", buckets=(
        QuotaBucket("weekly", 0.87334895, now + timedelta(days=6, hours=22, minutes=42)),
        QuotaBucket("5h", 0.977411, now + timedelta(hours=4, minutes=42)),
    ))
    claude = QuotaGroup(display_name="Claude and GPT models", buckets=(
        QuotaBucket("5h", 0.5, now + timedelta(hours=1)),
        QuotaBucket("weekly", 0.25, now + timedelta(days=1)),
    ))
    lines = format_account_section(_acct(groups=[gem, claude]), now)
    assert lines[0] == "s***e@***.com (PRO) ："
    assert lines[1] == "Gemini:"
    assert lines[2] == "5小时剩余 98%（4小时42分后重置）"   # 5h before weekly
    assert lines[3] == "本周剩余 87%（6天22小时42分后重置）"
    assert lines[4] == "Claude:"
    assert "5小时剩余 50%" in lines[5]


def test_account_status_markers_and_no_quota():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
    lines = format_account_section(_acct(disabled=True, forbidden=True, blocked=True), now)
    assert lines[0] == "s***e@***.com (PRO) ： [已禁用] [已封禁] [校验阻塞]"
    lines = format_account_section(_acct(quota=None), now)
    assert lines == ["s***e@***.com (PRO) ：", "额度数据不可用"]
    lines = format_account_section(_acct(groups=[], quota={}), now)
    assert lines == ["s***e@***.com (PRO) ：", "额度数据不可用"]


def test_tier_missing_dash_and_custom_group():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
    acct = AccountInfo(id="a", email="", name="", quota={}, quota_groups=(
        QuotaGroup("Other Models", (QuotaBucket("daily", 0.5, now + timedelta(hours=2)),)),
    ), subscription_tier=None)
    lines = format_account_section(acct, now)
    assert lines[0] == "— (—) ："
    assert lines[1] == "Other Models:"
    assert lines[2].startswith("daily剩余 50%")


# -- full template snapshot ----------------------------------------------------

def _report(accounts, today=48952193, week=1000000, all_time=99999999, models=(("m1", 100, 200),)):
    now = _aware(2026, 10, 22, 12, 0, 0)
    w = compute_window(now, "Asia/Shanghai")
    return UsageReport(instance="AGM", window=w, today=today, week=week,
                       all_time=all_time, accounts=accounts, models=models)


def test_exact_template_snapshot():
    now = _aware(2026, 10, 22, 12, 0, 0)
    gem = QuotaGroup("Gemini Models", (
        QuotaBucket("weekly", 0.87334895, now + timedelta(days=6, hours=22, minutes=42)),
        QuotaBucket("5h", 0.977411, now + timedelta(hours=4, minutes=42)),
    ))
    claude = QuotaGroup("Claude and GPT models", (
        QuotaBucket("5h", 0.9, now + timedelta(hours=2)),
        QuotaBucket("weekly", 0.8, now + timedelta(days=2)),
    ))
    acct = _acct(groups=[gem, claude])
    text = format_report(_report([acct], models=(("gemini-3.8-flash-high", 782939, 12345678),)))
    expected = "\n".join([
        "Antigravity Tools 消耗情况",
        "2026-10-22",
        "当日已消耗 Token：48,952,193",
        "本周已消耗 Token：1,000,000",
        "累计消耗 Token：99,999,999",
        "账号（1）：",
        "s***e@***.com (PRO) ：",
        "Gemini:",
        "5小时剩余 98%（4小时42分后重置）",
        "本周剩余 87%（6天22小时42分后重置）",
        "Claude:",
        "5小时剩余 90%（2小时后重置）",
        "本周剩余 80%（2天后重置）",
        "模型消耗（近 24 小时 / 近一周）：",
        "gemini-3.8-flash-high：782,939 / 12,345,678 Token",
    ])
    assert text == expected


def test_model_zero_hidden_sort_exact_names():
    rows = merge_model_windows(
        [("b-model", 50), ("a-model", 50), ("zero", 0)],
        [("b-model", 60), ("a-model", 40), ("zero", 0)],
    )
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], models=rows))
    lines = text.split("\n")
    assert "zero：0" not in text
    body = lines[lines.index("模型消耗（近 24 小时 / 近一周）：") + 1:lines.index("模型消耗（近 24 小时 / 近一周）：") + 3]
    # sort: 24h desc (tie 50/50) -> 7d desc (60 > 40) -> model asc
    assert body == ["b-model：50 / 60 Token", "a-model：50 / 40 Token"]
    assert "zero" not in text


def test_models_empty_and_failed_states():
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], models=()))
    assert "模型消耗（近 24 小时 / 近一周）：无" in text
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], models=None))
    assert "模型消耗（近 24 小时 / 近一周）：—" in text
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], today=None))
    assert "当日已消耗 Token：—" in text


def test_accounts_unavailable_raises():
    with pytest.raises(AGMSchemaError):
        format_report(_report(None))


# -- pagination ---------------------------------------------------------------

def test_pagination_no_loss():
    lines = [f"line-{i}" for i in range(10)]
    pages = paginate(lines, max_lines=3, max_chars=1800)
    assert len(pages) == 4
    assert pages[0].startswith("[AGM 1/4] ")
    joined = "".join(pages)
    for i in range(10):
        assert f"line-{i}" in joined
    # prefix always present even for single page
    one = paginate(["a", "b"], 30, 1800)
    assert len(one) == 1 and one[0].startswith("[AGM 1/1] ")


def test_pagination_long_line_and_limits():
    long_line = "x" * 500
    pages = paginate([long_line], 30, 200)
    assert "".join(p.split("] ", 1)[1] for p in pages) == long_line  # no loss
    for p in pages:
        assert len(p) <= 200
    # line limit includes prefix
    pages = paginate(["a"] * 40, 5, 1800)
    assert len(pages) == 8


# -- config / error text -------------------------------------------------------

def test_parse_config_clamps_and_invalid():
    p = parse_config({"base_url": "http://x:1/", "admin_password": "dummy-password"})
    assert p.valid and p.base_url == "http://x:1"
    p = parse_config({"base_url": "ftp://x", "admin_password": ""})
    assert not p.valid
    p = parse_config({"base_url": "http://x", "admin_password": "p",
                      "timeout_seconds": 999, "max_message_chars": 10})
    assert p.timeout_seconds == 60.0 and p.max_message_chars == 300 and p.warnings
    p = parse_config({"base_url": "http://x", "admin_password": "p",
                      "default_timezone": "Not/AZone"})
    assert p.default_timezone == "Asia/Shanghai"


def test_error_texts_and_priority():
    assert error_text_for_code("auth") == ERR_AUTH
    assert error_text_for_code("timeout") == ERR_TIMEOUT
    assert error_text_for_code("server") == ERR_SERVER
    assert error_text_for_code("unavailable") == ERR_UNAVAILABLE
    assert error_text_for_code("json") == ERR_JSON
    assert error_text_for_code("schema") == ERR_SCHEMA
    assert error_text_for_code("too_large") == ERR_TOO_LARGE
    assert error_text_for_code("rate_limit") == ERR_RATE_LIMIT
    assert error_text_for_code("internal") == ERR_INTERNAL
    assert pick_error_text(["server", "auth"]) == ERR_AUTH
    assert pick_error_text(["timeout", "server"]) == ERR_TIMEOUT
    assert pick_error_text(["json", "schema", "too_large"]) == ERR_JSON
    assert pick_error_text(["rate_limit", "too_large"]) == ERR_TOO_LARGE
    assert ERROR_PRIORITY[0] == "auth"
    # contract strings used verbatim
    assert ERR_UNAUTHORIZED == "无权查询 AGM 用量。"
    assert ERR_COOLDOWN == "查询过于频繁，请稍后重试。"
    assert ERR_CONFIG == "AGM 查询配置无效，请联系管理员检查配置。"


# ---- dual-window model section (24h / 7d) -----------------------------------

def test_models_two_windows_union_and_sort():
    """24h has A/B, 7d has B/C -> all three rows shown, sorted by 24h desc,
    then 7d desc, then model asc."""
    rows = merge_model_windows(
        [("A", 100), ("B", 200)],
        [("B", 300), ("C", 400)],
    )
    # sort: 24h desc dominates -> B(200), A(100), C(0); 7d only breaks ties
    assert rows == (("B", 200.0, 300.0), ("A", 100.0, 0.0), ("C", 0.0, 400.0))
    assert [m for m, _, _ in rows] == ["B", "A", "C"]
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], models=rows))
    body = text.split("模型消耗（近 24 小时 / 近一周）：\n")[1].split("\n")
    assert body[0] == "B：200 / 300 Token"
    assert body[1] == "A：100 / 0 Token"
    assert body[2] == "C：0 / 400 Token"


def test_models_both_zero_hidden():
    """Models zero in BOTH windows are hidden; single-window models get 0
    in the other column."""
    rows = merge_model_windows(
        [("A", 100), ("zero1", 0)],
        [("B", 300), ("zero2", 0)],
    )
    names = [m for m, _, _ in rows]
    assert "zero1" not in names and "zero2" not in names
    assert ("A", 100.0, 0.0) in rows and ("B", 0.0, 300.0) in rows


def test_models_zero_displays_plain_zero():
    rows = merge_model_windows([("A", 0)], [("A", 0)])
    text = format_report(_report([_acct(groups=[QuotaGroup("G", ())])], models=rows))
    assert "A：0 / 0 Token" not in text  # both zero -> hidden entirely


def test_models_sort_tie_7d_then_name():
    rows = merge_model_windows(
        [("b", 100), ("a", 100), ("c", 50)],
        [("c", 500), ("a", 200), ("b", 200)],
    )
    assert [m for m, _, _ in rows] == ["a", "b", "c"]


def test_models_empty_both_windows():
    assert merge_model_windows([], []) == ()
