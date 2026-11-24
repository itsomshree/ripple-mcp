import os
import socket
import subprocess
import sys
import time

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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path):
    port = free_port()
    env = {
        **os.environ,
        "NEO4J_PASSWORD": "pw-12345",
        "MCP_TRANSPORT": "http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    for key in ("REPO_PATH", "PORT"):
        env.pop(key, None)

    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ripple.server"],
        cwd=tmp_path,  # no .env here
        env=env,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail(
                    f"server exited early:\n{(tmp_path / 'server.log').read_text()}"
                )
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("server did not become healthy within 30s")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()


def test_healthz_does_not_need_neo4j(http_server):
    r = httpx.get(f"{http_server}/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_the_six_tools_are_served_over_http(http_server):
    async with Client(f"{http_server}/mcp") as c:
        assert {t.name for t in await c.list_tools()} == TOOLS
