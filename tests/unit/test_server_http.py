import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from fastmcp import Client

TOOLS = {
    "what_breaks_if_i_change",
    "trace_request_flow",
    "affected_tests",
    "scan_repo",
    "status",
    "rescan",
}
TOKEN = secrets.token_urlsafe(32)
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "http-test", "version": "0"},
    },
}
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


class Server(NamedTuple):
    url: str
    log: Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post_init(url: str, authorization: str | None = None) -> httpx.Response:
    headers = dict(HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    return httpx.post(f"{url}/mcp", json=INIT, headers=headers, timeout=5)


@pytest.fixture
def http_server(tmp_path):
    port = free_port()
    env = {
        **os.environ,
        "NEO4J_PASSWORD": "pw-12345",
        "MCP_TRANSPORT": "http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_AUTH_TOKEN": TOKEN,
        "PYTHONUNBUFFERED": "1",
    }
    for key in ("REPO_PATH", "PORT"):
        env.pop(key, None)

    log_path = tmp_path / "server.log"
    log = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ripple.server"],
        cwd=tmp_path,
        env=env,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail(f"server exited early:\n{log_path.read_text()}")
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("server did not become healthy within 30s")
            time.sleep(0.2)
        yield Server(url, log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


def test_healthz_is_open_and_does_not_need_neo4j(http_server):
    r = httpx.get(f"{http_server.url}/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("auth", [None, "Bearer wrong", f"Basic {TOKEN}"])
def test_mcp_without_the_token_is_refused(http_server, auth):
    r = post_init(http_server.url, auth)
    assert r.status_code == 401
    assert "Bearer" in r.headers["www-authenticate"]


def test_mcp_with_the_token_initializes(http_server):
    assert post_init(http_server.url, f"Bearer {TOKEN}").status_code == 200


async def test_the_six_tools_are_served_with_the_token(http_server):
    async with Client(f"{http_server.url}/mcp", auth=TOKEN) as c:
        assert {t.name for t in await c.list_tools()} == TOOLS


def test_the_token_never_reaches_the_server_log(http_server):
    post_init(http_server.url, "Bearer sekrit-guess")
    post_init(http_server.url, f"Bearer {TOKEN}")

    deadline = time.monotonic() + 5
    log = http_server.log.read_text()
    while log.count("POST /mcp") < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
        log = http_server.log.read_text()

    assert log.count("POST /mcp") >= 2
    assert TOKEN not in log and "sekrit-guess" not in log
