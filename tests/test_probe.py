"""Probe CLI tests: run against the stub, check exit codes and redaction."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_account, make_accounts_payload, make_quota

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PROBE = PLUGIN_DIR / "tools" / "probe_agm.py"


async def run_probe(args, env_password=None):
    """Run the probe as an async subprocess so the in-loop stub server keeps
    serving while the child runs (a blocking subprocess.run would starve it)."""
    env = os.environ.copy()
    env.pop("AGM_ADMIN_PASSWORD", None)
    if env_password is not None:
        env["AGM_ADMIN_PASSWORD"] = env_password
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(PROBE), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    return subprocess.CompletedProcess(
        args, proc.returncode,
        out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
    )


async def test_probe_offline_exit_codes(stub_agm):
    stub_agm.accounts_payload = make_accounts_payload([make_account("a1")])
    r = await run_probe(["--base-url", stub_agm.base_url], env_password="dummy-password")
    assert r.returncode == 0
    assert "health:" in r.stdout
    assert "dummy-password" not in r.stdout and "dummy-password" not in r.stderr
    # auth failure -> exit 3
    r = await run_probe(["--base-url", stub_agm.base_url], env_password="wrong")
    assert r.returncode == 3
    assert "wrong" not in r.stdout


async def test_probe_transport_exit_code():
    r = await run_probe(["--base-url", "http://127.0.0.1:1"], env_password="x")
    assert r.returncode == 2


async def test_probe_render_flag(stub_agm):
    stub_agm.accounts_payload = make_accounts_payload([make_account("a1")])
    stub_agm.summary_payload = {"total_tokens": 100}
    r = await run_probe(["--base-url", stub_agm.base_url, "--render"], env_password="dummy-password")
    assert r.returncode == 0
    assert "Antigravity Tools 消耗情况" in r.stdout
    assert "账号（1）：" in r.stdout
