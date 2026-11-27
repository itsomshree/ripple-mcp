from contextlib import asynccontextmanager

import httpx
import pytest
from fastmcp import FastMCP
from pydantic import SecretStr
from starlette.responses import JSONResponse

from ripple.auth import (
    MIN_TOKEN_LENGTH,
    AuthConfigError,
    StaticBearerVerifier,
    verifier_for,
)
from ripple.config import Settings

TOKEN = "t" * MIN_TOKEN_LENGTH
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-test", "version": "0"},
    },
}
BASE = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


# the verifier on its own
@pytest.mark.parametrize("bad", ["", "short", "x" * (MIN_TOKEN_LENGTH - 1)])
def test_a_short_or_empty_token_is_refused_at_construction(bad):
    with pytest.raises(AuthConfigError, match="at least"):
        StaticBearerVerifier(bad)


@pytest.mark.parametrize("bad", [TOKEN + "\n", " " + TOKEN, TOKEN[:10] + " " + TOKEN])
def test_whitespace_in_the_token_is_refused(bad):
    with pytest.raises(AuthConfigError, match="whitespace"):
        StaticBearerVerifier(bad)


async def test_only_the_exact_token_verifies():
    v = StaticBearerVerifier(TOKEN)
    ok = await v.verify_token(TOKEN)
    assert ok is not None and ok.client_id == "ripple-mcp"
    for wrong in ("", "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        assert await v.verify_token(wrong) is None


# the verifier behind FastMCP's HTTP app
@asynccontextmanager
async def http_client():
    """Scoped per test: FastMCP's lifespan is an anyio task group, which must be
    entered and exited in the same task, so this is not a pytest fixture"""
    mcp = FastMCP("auth-test", auth=StaticBearerVerifier(TOKEN))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_):
        return JSONResponse({"status": "ok"})

    app = mcp.http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def post_init(authorization: str | None):
    headers = dict(BASE)
    if authorization is not None:
        headers["Authorization"] = authorization
    async with http_client() as client:
        return await client.post("/mcp", json=INIT, headers=headers)


async def test_healthz_is_open():
    async with http_client() as client:
        r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "Bearer wrong",
        "Bearer",
        f"Basic {TOKEN}",
        f"Token {TOKEN}",
        f"Bearer {TOKEN[:-1]}",
        f"Bearer {TOKEN}x",
    ],
)
async def test_mcp_refuses_anything_but_the_exact_token(auth):
    r = await post_init(auth)
    assert r.status_code == 401
    assert "Bearer" in r.headers["www-authenticate"]


async def test_a_rejected_request_never_echoes_the_token():
    r = await post_init("Bearer sekrit-guess")
    assert r.status_code == 401
    assert "sekrit-guess" not in r.text and TOKEN not in r.text


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
async def test_mcp_accepts_the_token_whatever_the_scheme_case(scheme):
    r = await post_init(f"{scheme} {TOKEN}")
    assert r.status_code == 200


# which settings need a verifier
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("MCP_AUTH_TOKEN", "MCP_TRANSPORT", "MCP_PORT", "PORT"):
        monkeypatch.delenv(key, raising=False)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, neo4j_password=SecretStr("pw"), **kw)


def test_stdio_needs_no_token_and_ignores_one():
    assert verifier_for(settings()) is None
    given = settings(mcp_transport="stdio", mcp_auth_token=SecretStr(TOKEN))
    assert verifier_for(given) is None


def test_http_without_a_token_is_refused():
    with pytest.raises(AuthConfigError, match="MCP_AUTH_TOKEN is required"):
        verifier_for(settings(mcp_transport="http"))


@pytest.mark.parametrize("weak", ["", "short", "x" * (MIN_TOKEN_LENGTH - 1)])
def test_http_with_a_weak_token_is_refused(weak):
    with pytest.raises(AuthConfigError):
        verifier_for(settings(mcp_transport="http", mcp_auth_token=SecretStr(weak)))


async def test_http_with_a_good_token_builds_a_working_verifier():
    v = verifier_for(settings(mcp_transport="http", mcp_auth_token=SecretStr(TOKEN)))
    assert v is not None
    assert await v.verify_token(TOKEN) is not None
    assert await v.verify_token("wrong") is None
