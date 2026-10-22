"""Shared test fixtures: astrbot stub, FakeEvent, aiohttp StubAGM server, builders.

Fully offline; the stub server binds a random local port.
"""
from __future__ import annotations

import pathlib
import sys
import types
from typing import Any, Mapping

import pytest
import pytest_asyncio
import aiohttp
from aiohttp import web

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_PARENT = _PLUGIN_DIR.parent
for _entry in (str(_PACKAGE_PARENT), str(_PLUGIN_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def _install_astrbot_stub() -> None:
    if "astrbot.api" in sys.modules:
        return

    def _module(name):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    api = _module("astrbot")
    api_mod = _module("astrbot.api")
    star_mod = _module("astrbot.api.star")
    event_mod = _module("astrbot.api.event")

    import logging

    class AstrBotConfig(dict):
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class AstrMessageEvent:
        def __init__(self, sender="u1", group=None, admin=False, platform="aiocqhttp"):
            self.stopped = False
            self._sender = sender
            self._group = group
            self._admin = admin
            self._platform = platform

        def stop_event(self):
            self.stopped = True

        def get_sender_id(self):
            return self._sender

        def get_group_id(self):
            return self._group

        def is_admin(self):
            return self._admin

        def get_platform_name(self):
            return self._platform

        def plain_result(self, text):
            return ("plain", text)

    def filter_command(name):
        def deco(fn):
            fn._command_name = name
            return fn
        return deco

    class _Filter:
        command = staticmethod(filter_command)

    api_mod.AstrBotConfig = AstrBotConfig
    api_mod.logger = logging.getLogger("astrbot.stub")
    star_mod.Context = Context
    star_mod.Star = Star
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter
    star_mod.filter = _Filter


_install_astrbot_stub()


def make_bucket(window="5h", fraction=0.9, reset="2026-10-24T05:53:26Z"):
    return {"bucket_id": f"x-{window}", "window": window, "remaining_fraction": fraction,
            "reset_time": reset, "cycle_tokens": None, "display_name": "d", "description": ""}


def make_group(display_name="Gemini Models", buckets=None):
    return {"display_name": display_name, "description": "", "buckets": list(buckets or [])}


def make_quota(groups=None, tier="PRO", forbidden=False):
    return {
        "models": [],
        "last_updated": 1792682289,
        "subscription_tier": tier,
        "is_forbidden": forbidden,
        "quota_groups": list(groups or []),
    }


def make_account(acct_id="a1", email="someone@example.com", name="acct",
                 disabled=False, validation_blocked=False, quota=None):
    entry = {
        "id": acct_id, "email": email, "name": name,
        "priority": 1, "is_current": True,
        "disabled": disabled, "disabled_reason": None,
        "proxy_disabled": False, "proxy_disabled_reason": None,
        "validation_blocked": validation_blocked,
        "validation_blocked_reason": None,
        "quota": quota,
    }
    return entry


def make_accounts_payload(accounts=None):
    return {"accounts": list(accounts or [])}


def make_summary(total_tokens=1000, **extra):
    base = {
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cached_tokens": 0, "total_reasoning_tokens": 0,
        "total_tokens": total_tokens, "total_requests": 0,
        "unique_accounts": 1,
    }
    base.update(extra)
    return base


class StubAGM:
    """Offline AGM admin API stub."""

    def __init__(self) -> None:
        self.password = "dummy-password"
        self.health_payload = {"status": "ok", "version": "4.9.8"}
        self.accounts_payload = make_accounts_payload()
        self.summary_payload = make_summary()
        self.models_payload = []
        self.daily_payload = []
        self.weekly_payload = []
        self.calls: list[dict] = []
        self.summary_calls: list[dict] = []
        # knobs
        self.status_map: dict[str, int] = {}
        self.body_map: dict[str, Any] = {}
        self.raw_body_map: dict[str, bytes] = {}
        self.delay_map: dict[str, float] = {}
        self.fail_connect = False

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/api/accounts", self._accounts)
        app.router.add_get("/api/stats/summary", self._summary)
        app.router.add_get("/api/stats/models", self._models)
        app.router.add_get("/api/stats/daily", self._daily)
        app.router.add_get("/api/stats/weekly", self._weekly)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        host, port = self._runner.addresses[0][:2]
        self.base_url = f"http://{host}:{port}"

    async def stop(self) -> None:
        await self._runner.cleanup()

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.password}"

    async def _serve(self, request: web.Request, name: str, payload: Any):
        status = self.status_map.get(name)
        self.calls.append({"name": name, "auth": request.headers.get("Authorization"),
                           "query": dict(request.query)})
        delay = self.delay_map.get(name)
        if delay:
            import asyncio as _aio
            await _aio.sleep(delay)
        if name == "health":
            pass  # no auth required
        elif not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if name in self.status_map:
            return web.json_response({"error": "x"}, status=self.status_map[name])
        if name in self.raw_body_map:
            return web.Response(body=self.raw_body_map[name], content_type="application/json")
        if name in self.body_map:
            return web.json_response(self.body_map[name])
        return web.json_response(payload)

    async def _health(self, request):
        return await self._serve(request, "health", self.health_payload)

    async def _accounts(self, request):
        return await self._serve(request, "accounts", self.accounts_payload)

    async def _summary(self, request):
        self.summary_calls.append({"hours": request.query.get("hours"),
                                   "auth": request.headers.get("Authorization")})
        return await self._serve(request, "summary", self.summary_payload)

    async def _models(self, request):
        # models payloads may be keyed by the requested hours window so tests
        # can distinguish the 24h and 168h dual-window requests.
        hours = request.query.get("hours", "24")
        if isinstance(self.models_payload, dict):
            payload = self.models_payload.get(hours, [])
        else:
            payload = self.models_payload
        return await self._serve(request, "models", payload)

    async def _daily(self, request):
        return await self._serve(request, "daily", self.daily_payload)

    async def _weekly(self, request):
        return await self._serve(request, "weekly", self.weekly_payload)


@pytest_asyncio.fixture()
async def stub_agm():
    s = StubAGM()
    await s.start()
    yield s
    await s.stop()


@pytest_asyncio.fixture()
async def session():
    async with aiohttp.ClientSession() as s:
        yield s
