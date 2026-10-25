"""Read-only CLI probe for the AGM admin API — no AstrBot dependency.

Reads the admin password from AGM_ADMIN_PASSWORD (never a CLI argument, never
printed). Usage:

  AGM_ADMIN_PASSWORD=... python tools/probe_agm.py --base-url http://host:port
  AGM_ADMIN_PASSWORD=... python tools/probe_agm.py --base-url http://host:port --render
  AGM_ADMIN_PASSWORD=... python tools/probe_agm.py --base-url http://host:port --check-updates

--check-updates asks AGM to compare its own version against GitHub (one
read-only POST /api/system/updates/check); it writes nothing on either side.

Exit codes: 0 ok; 2 transport; 3 auth; 4 schema/json; 5 other.
"""
from __future__ import annotations

import argparse
import asyncio
import io as _io
import os
import pathlib
import sys

# Windows consoles default to a legacy code page; force UTF-8 so the report
# text survives redirection/pipes regardless of the console's code page.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

if __package__ in (None, ""):
    _PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
    _PACKAGE_PARENT = _PLUGIN_DIR.parent
    for _entry in (str(_PACKAGE_PARENT), str(_PLUGIN_DIR)):
        if _entry not in sys.path:
            sys.path.insert(0, _entry)

from astrbot_plugin_agm_usage.client import (  # noqa: E402
    AGMAuthError,
    AGMError,
    AGMJsonError,
    AGMSchemaError,
    AGMClient,
    AGMUnavailableError,
)
from astrbot_plugin_agm_usage.models import UsageReport, compute_window  # noqa: E402
from astrbot_plugin_agm_usage.reporting import (  # noqa: E402
    derive_update_notice,
    format_report,
    merge_model_windows,
)

EXIT_OK = 0
EXIT_TRANSPORT = 2
EXIT_AUTH = 3
EXIT_SCHEMA = 4
EXIT_OTHER = 5


def exit_code_for(exc: AGMError) -> int:
    if isinstance(exc, AGMUnavailableError):
        return EXIT_TRANSPORT
    if isinstance(exc, AGMAuthError):
        return EXIT_AUTH
    if isinstance(exc, (AGMSchemaError, AGMJsonError)):
        return EXIT_SCHEMA
    return EXIT_OTHER


async def run(base_url: str, password: str, timezone: str, render: bool,
              check_updates: bool = False) -> int:
    async with aiohttp.ClientSession(trust_env=False) as session:
        client = AGMClient(base_url=base_url, admin_password=password,
                           session=session, timeout_seconds=15.0)
        try:
            h = await client.health()
            print(f"health: status={h.value[0]} version={h.value[1]}")
            acc = await client.accounts()
            print(f"accounts: n={len(acc.value)}")
            now = datetime.now(ZoneInfo(timezone))
            window = compute_window(now, timezone)
            print(f"H={window.hours} HW={window.week_hours} day={window.day.isoformat()}")
            # 当日: natural-day bucket (rolling summary leaks yesterday's tail)
            today_str = window.day.isoformat()
            daily = await client.daily(2)
            today = next((t for period, t in daily.value if period == today_str), None)
            if today is not None:
                src_today = f"daily[{today_str}]"
            else:
                today = (await client.summary(window.hours)).value
                src_today = f"summary(hours={window.hours}) FALLBACK"
            # 本周: natural-week bucket (AGM strftime('%Y-W%W', localtime), Monday start)
            week_label = f"{window.day.year}-W{int(window.day.strftime('%W')):02d}"
            weekly = await client.weekly(1)
            week = next((t for period, t in weekly.value if period == week_label), None)
            if week is not None:
                src_week = f"weekly[{week_label}]"
            else:
                week = (await client.summary(window.week_hours)).value
                src_week = f"summary(hours={window.week_hours}) FALLBACK"
            all_time = await client.summary(0)
            print(f"today={today:,.0f} (src={src_today})")
            print(f"week={week:,.0f} (src={src_week})")
            print(f"all={all_time.value:,.0f}")
            m24 = await client.model_stats(24)
            m168 = await client.model_stats(168)
            models = merge_model_windows(m24.value, m168.value)
            print(f"models: 24h n={len(m24.value)}, 7d n={len(m168.value)}")
            notice = ""
            if check_updates:
                # AGM asks GitHub itself; a failure here is reported but never
                # fatal for the rest of the probe.
                try:
                    upd = await client.check_updates()
                except AGMError as exc:
                    print(f"updates: 失败 ({exc.code})")
                else:
                    payload = upd.value if isinstance(upd.value, dict) else {}
                    print(f"updates: current={payload.get('current_version')} "
                          f"latest={payload.get('latest_version')} "
                          f"has_update={payload.get('has_update')}")
                    notice = derive_update_notice(payload)
                    print(f"updates: notice={notice or '（无）'}")
            if render:
                report = UsageReport(
                    instance="AGM", window=window,
                    today=today, week=week, all_time=all_time.value,
                    accounts=acc.value, models=models,
                )
                print("---- /agm render ----")
                text = format_report(report)
                if notice:
                    text = text + "\n" + notice
                print(text)
            return EXIT_OK
        except AGMError as exc:
            print(f"ERROR: {type(exc).__name__} ({exc.code})", file=sys.stderr)
            return exit_code_for(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only AGM admin API probe")
    parser.add_argument("--base-url", required=True, help="AGM admin API origin")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--render", action="store_true",
                        help="also render the full /agm report text")
    parser.add_argument("--check-updates", action="store_true",
                        help="also ask AGM to check GitHub for its latest version "
                             "(one read-only POST /api/system/updates/check)")
    args = parser.parse_args()
    password = os.environ.get("AGM_ADMIN_PASSWORD") or ""
    if not password:
        import getpass

        password = getpass.getpass("AGM admin password: ")
    try:
        return asyncio.run(run(args.base_url, password, args.timezone, args.render,
                               args.check_updates))
    except KeyboardInterrupt:
        return EXIT_OTHER


if __name__ == "__main__":
    sys.exit(main())
