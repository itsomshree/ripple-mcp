from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    from ripple.config import Settings

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
CLIENT_ID = "ripple-mcp"


class AuthConfigError(ValueError):
    """MCP_AUTH_TOKEN is missing or too weak for a network-facing server"""


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class StaticBearerVerifier(TokenVerifier):
    def __init__(self, token: str) -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise AuthConfigError(
                f"MCP_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
            )
        if any(ch.isspace() for ch in token):
            raise AuthConfigError("MCP_AUTH_TOKEN must not contain whitespace")
        super().__init__()
        self._expected = _digest(token)

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(_digest(token), self._expected):
            return AccessToken(token=token, client_id=CLIENT_ID, scopes=[])
        logger.warning("rejected a bearer token")
        return None


def verifier_for(settings: Settings) -> StaticBearerVerifier | None:
    if settings.mcp_transport != "http":
        return None
    token = settings.mcp_auth_token
    if token is None:
        raise AuthConfigError("MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http")
    return StaticBearerVerifier(token.get_secret_value())
