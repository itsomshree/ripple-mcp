from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr

    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(
        default=8000, validation_alias=AliasChoices("MCP_PORT", "PORT")
    )

    mcp_auth_token: SecretStr | None = None
    repo_path: Path | None = None
    scan_exclude: str = ""
    log_level: str = "INFO"

    @property
    def exclude_globs(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.scan_exclude.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]
