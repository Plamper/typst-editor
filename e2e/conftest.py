"""Browser regression fixtures using a temporary database and self-hosted assets.

Playwright and Chromium must be installed; missing browser dependencies fail
instead of silently skipping CI coverage. No production or dev-project data is used.
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

expect.set_options(timeout=15_000)  # assertion waits match the ctx action timeout (expect default is only 5s)


def pytest_addoption(parser):
    parser.addoption("--shard", default="", help="Run shard i/N (1-based), e.g. --shard=1/3")


def pytest_collection_modifyitems(config, items):
    spec = config.getoption("--shard")
    if not spec:
        return
    idx_s, total_s = spec.split("/", 1)
    idx, total = int(idx_s), int(total_s)
    if not (1 <= idx <= total):
        raise pytest.UsageError(f"invalid --shard={spec!r}")
    items[:] = [item for i, item in enumerate(items) if i % total == idx - 1]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, 'rep_' + report.when, report)
    if report.when == "call" and report.failed:
        report.sections.append(("server log tail", _server_tail(100)))

ROOT = Path(__file__).resolve().parents[1]

_SERVER_LOG_PATH: Path | None = None


def _server_tail(n: int = 100) -> str:
    try:
        if _SERVER_LOG_PATH is None or not _SERVER_LOG_PATH.exists():
            return "<no server log>"
        lines = _SERVER_LOG_PATH.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError as e:
        return f"<server log unreadable: {e}>"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    global _SERVER_LOG_PATH
    from backend.config import _RATE_KEYS

    data = tmp_path_factory.mktemp("e2e-data")
    port = _free_port()
    env = {**os.environ, "DATA_DIR": str(data), "REGISTRATION": "open", "PYTHONUNBUFFERED": "1",
           # All smoke tests share one egress IP, hence one per-IP rate-limit bucket
           # (production clients have distinct IPs). Raise every rate bucket
           # so the suite can't 429 itself; app limits stay untouched.
           "RATE_REGISTER_PER_HOUR": "1000",
           **{f"RATE_{k}_PER_MIN": "1000" for k in _RATE_KEYS}}
    _SERVER_LOG_PATH = data / "server.log"
    # NOTE: never stdout=PIPE here without a reader: once the pipe buffer
    # (64KB on GH runners) fills, uvicorn blocks on its next access-log
    # write and the server hangs (late-test page.goto timeouts). A file
    # never blocks and keeps the log for the failure tail below.
    logf = open(_SERVER_LOG_PATH, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            try:
                logf.flush()
                out = _SERVER_LOG_PATH.read_text(errors="replace") if _SERVER_LOG_PATH else ""
            except OSError:
                out = ""
            proc.kill()
            logf.close()
            raise RuntimeError("server did not start:\n" + out[-2000:])
        time.sleep(0.2)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    finally:
        logf.close()


_BROWSER_RECYCLE = 10


@pytest.fixture(scope="session")
def _playwright_browser():
    # Recycle Chromium every _BROWSER_RECYCLE tests instead of per-test cold starts:
    # avoids WASM/canvas memory creep across a full session while cutting ~90% of
    # browser launch overhead.
    state = {"browser": None, "uses": 0}
    with sync_playwright() as p:
        def get():
            if state["browser"] is None or state["uses"] >= _BROWSER_RECYCLE or not state["browser"].is_connected():
                if state["browser"] is not None:
                    state["browser"].close()
                try:
                    state["browser"] = p.chromium.launch()
                except Exception as e:  # browser binary missing
                    pytest.fail(f"no Playwright browser available: {e}")
                state["uses"] = 0
            state["uses"] += 1
            return state["browser"]
        yield get
        if state["browser"] is not None:
            state["browser"].close()


@pytest.fixture()
def browser(_playwright_browser):
    return _playwright_browser()


@pytest.fixture()
def ctx(browser, base_url, request):
    c = browser.new_context(base_url=base_url, accept_downloads=True, locale="en-US")
    c.set_default_timeout(15_000)
    # DOM snapshots are enough to debug a failure; per-action screenshots and
    # JS source maps nearly double trace overhead. Failure screenshots are
    # captured separately below, so keep the trace itself lean.
    c.tracing.start(screenshots=False, snapshots=True, sources=False)
    yield c
    if getattr(request.node, 'rep_call', None) and request.node.rep_call.failed:
        dest = ROOT / 'test-results' / re.sub(r'[^A-Za-z0-9_-]', '_', request.node.nodeid)
        dest.mkdir(parents=True, exist_ok=True)
        c.tracing.stop(path=str(dest / 'trace.zip'))
        for i, page in enumerate(c.pages):
            try:
                page.screenshot(path=str(dest / f'page-{i}.png'))
            except Exception:
                pass
    else:
        c.tracing.stop()
    c.close()


@pytest.fixture()
def page(ctx):
    return ctx.new_page()
