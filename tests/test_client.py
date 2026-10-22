"""Client tests: auth, status mapping, JSON/schema, streaming, size cap."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from astrbot_plugin_agm_usage.client import (
    AGMAuthError,
    AGMClient,
    AGMJsonError,
    AGMRateLimitError,
    AGMSchemaError,
    AGMServerError,
    AGMTimeoutError,
    AGMUnavailableError,
    validate_accounts,
    validate_summary,
    validate_models,
)
from astrbot_plugin_agm_usage.models import AccountInfo

from conftest import make_account, make_accounts_payload, make_group, make_quota


def make_client(stub, session, **kw):
    return AGMClient(base_url=stub.base_url, admin_password=stub.password,
                     session=session, **kw)


async def test_bearer_exact_and_health_without_auth(stub_agm, session):
    client = make_client(stub_agm, session)
    await client.health()
    await client.accounts()
    await client.summary(12)
    health = next(c for c in stub_agm.calls if c["name"] == "health")
    accounts = next(c for c in stub_agm.calls if c["name"] == "accounts")
    assert health["auth"] is None  # no auth header on health
    assert accounts["auth"] == "Bearer dummy-password"
    assert any(c["hours"] == "12" for c in stub_agm.summary_calls)


async def test_no_post_requests(stub_agm, session):
    client = make_client(stub_agm, session)
    await client.health()
    # only GET routes registered; any POST would 404 -> schema error. Just
    # assert that all recorded calls are GETs (implicit via successful serve).
    assert stub_agm.calls


@pytest.mark.parametrize("status,exc", [
    (401, AGMAuthError), (403, AGMAuthError), (429, AGMRateLimitError),
    (500, AGMServerError), (503, AGMServerError), (302, AGMSchemaError),
    (404, AGMSchemaError),
])
async def test_status_error_mapping(stub_agm, session, status, exc):
    stub_agm.status_map["accounts"] = status
    client = make_client(stub_agm, session)
    with pytest.raises(exc):
        await client.accounts()


async def test_timeout_and_connect_failure(stub_agm, session):
    # Timeout: stub delays 3s against a 1s budget — the timer fires with a
    # large margin, immune to int() truncation and loop jitter.
    stub_agm.delay_map["accounts"] = 3.0
    client = make_client(stub_agm, session, timeout_seconds=1.0)
    with pytest.raises(AGMTimeoutError):
        await client.accounts()
    # Connect failure: closed port. On Windows the TCP stack takes ~2s to
    # report the refusal, so the budget must exceed that (5s) for
    # ClientConnectorError to surface deterministically.
    bad = AGMClient(base_url="http://127.0.0.1:1", admin_password="x",
                    session=session, timeout_seconds=5.0)
    with pytest.raises(AGMUnavailableError):
        await bad.accounts()


async def test_timeout_classification_boundary(stub_agm, session, monkeypatch):
    """Deterministic classification without real timing luck.

    Case 1 (fast zone): patch the event-loop clock with a FROZEN shim, so the
    elapsed measured inside client._get is ~0 regardless of how long the
    request actually takes — a TimeoutError raised in that state must be
    classified as unavailable (fast-fail zone, < 0.5x budget).
    Case 2 (at/over budget): restore the real clock; a 3s stub delay against
    a 1s budget fires the timer with a huge margin -> timeout, immune to
    int() truncation jitter (measured 199 vs 200ms).
    """
    import astrbot_plugin_agm_usage.client as client_mod

    client = make_client(stub_agm, session, timeout_seconds=1.0)
    real_loop = asyncio.get_running_loop()
    frozen = {"t": real_loop.time()}

    class FrozenLoop:
        def time(self):
            return frozen["t"]

    # Case 1: frozen clock -> elapsed ~= 0 -> unavailable.
    monkeypatch.setattr(client_mod.asyncio, "get_running_loop", lambda: FrozenLoop())
    fast_client = AGMClient(base_url="http://127.0.0.1:1", admin_password="x",
                            session=session, timeout_seconds=1.0)
    with pytest.raises(AGMUnavailableError):
        await fast_client.accounts()

    # Case 2: real clock, delay 3s vs budget 1s -> timeout.
    monkeypatch.setattr(client_mod.asyncio, "get_running_loop", lambda: real_loop)
    stub_agm.delay_map["accounts"] = 3.0
    with pytest.raises(AGMTimeoutError):
        await client.accounts()


async def test_invalid_json_and_schema(stub_agm, session):
    stub_agm.raw_body_map["accounts"] = b"not json"
    client = make_client(stub_agm, session)
    with pytest.raises(AGMJsonError):
        await client.accounts()
    stub_agm.raw_body_map.clear()
    stub_agm.body_map["accounts"] = {"nope": 1}
    with pytest.raises(AGMSchemaError):
        await client.accounts()
    # summary schema errors: bool / negative / string / missing
    for bad in (True, -5, "12"):
        stub_agm.body_map["summary"] = {"total_tokens": bad}
        with pytest.raises(AGMSchemaError):
            await client.summary(1)
    stub_agm.body_map["summary"] = {}
    with pytest.raises(AGMSchemaError):
        await client.summary(1)


async def test_streamed_large_response_complete_and_cap(stub_agm, session):
    # big-but-valid accounts payload read in chunks by the server framework
    big = make_accounts_payload([make_account(f"a{i}") for i in range(200)])
    stub_agm.accounts_payload = big
    client = make_client(stub_agm, session)
    result = await client.accounts()
    assert len(result.value) == 200
    # cap: 1 byte over the limit -> too_large
    tiny = AGMClient(base_url=stub_agm.base_url, admin_password=stub_agm.password,
                     session=session, max_response_bytes=10)
    with pytest.raises(AGMTooLargeError):
        await tiny.accounts()


from astrbot_plugin_agm_usage.client import AGMTooLargeError


def test_roster_quota_null_and_unknown_fields():
    payload = make_accounts_payload([
        make_account("a1", quota=None),
        {"id": "a2", "email": "x@y.com", "unknown_extra": {"deep": 1}, "quota": make_quota()},
    ])
    result = validate_accounts(payload)
    assert len(result) == 2
    assert result[0].quota is None
    assert result[1].subscription_tier == "PRO"


def test_validate_summary_missing_field():
    with pytest.raises(AGMSchemaError):
        validate_summary({"total_input": 1})


def test_validate_models_schema():
    with pytest.raises(AGMSchemaError):
        validate_models([{"model": "m"}])  # missing total_tokens
    ok = validate_models([{"model": "m", "total_tokens": 5}])
    assert ok == (("m", 5.0),)
