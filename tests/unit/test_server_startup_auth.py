import os
import socket
import subprocess
import sys

import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31])
def test_http_refuses_to_start_without_a_strong_token(tmp_path, token):
    env = {
        **os.environ,
        "NEO4J_PASSWORD": "pw-12345",
        "MCP_TRANSPORT": "http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(free_port()),
        "PYTHONUNBUFFERED": "1",
    }
    for key in ("REPO_PATH", "PORT", "MCP_AUTH_TOKEN"):
        env.pop(key, None)
    if token is not None:
        env["MCP_AUTH_TOKEN"] = token

    try:
        done = subprocess.run(
            [sys.executable, "-m", "ripple.server"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the server started in HTTP mode without a valid MCP_AUTH_TOKEN")

    assert done.returncode == 2
    assert "MCP_AUTH_TOKEN" in done.stderr
    assert done.stdout == ""
    if token:
        assert token not in done.stderr
