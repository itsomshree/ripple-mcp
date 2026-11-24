import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

TOOLS = {
    "what_breaks_if_i_change",
    "trace_request_flow",
    "affected_tests",
    "scan_repo",
    "status",
    "rescan",
}
TIMEOUT = 30


class StdioServer:
    """`python -m ripple.server` as a subprocess, speaking JSON-RPC over stdio"""

    def __init__(self, cwd: Path) -> None:
        env = {
            **os.environ,
            "NEO4J_PASSWORD": "pw-12345",
            "MCP_TRANSPORT": "stdio",
            "LOG_LEVEL": "DEBUG",
            "PYTHONUNBUFFERED": "1",
        }
        env.pop("REPO_PATH", None)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ripple.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=env,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._inbox: queue.Queue[str] = queue.Queue()
        self._threads = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.strip():
                self.stdout_lines.append(line.rstrip("\n"))
                self._inbox.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(
        self, request_id: int, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            try:
                line = self._inbox.get(timeout=TIMEOUT)
            except queue.Empty:
                stderr = "\n".join(self.stderr_lines[-20:])
                pytest.fail(f"no response to {method} within {TIMEOUT}s\n{stderr}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-protocol output on stdout: {line!r}")
            if message.get("id") == request_id and "method" not in message:
                return message

    def close(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for t in self._threads:
            t.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    s = StdioServer(tmp_path)
    try:
        yield s
    finally:
        s.close()


def test_stdout_carries_only_protocol_messages(server):
    init = server.request(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "stdout-check", "version": "0"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "ripple-mcp"
    server.notify("notifications/initialized")

    listed = server.request(2, "tools/list")
    assert {t["name"] for t in listed["result"]["tools"]} == TOOLS

    server.close()

    assert server.stdout_lines
    for line in server.stdout_lines:
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0", line
