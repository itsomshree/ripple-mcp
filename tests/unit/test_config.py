import pytest
from pydantic import ValidationError

from ripple.config import Settings

ENV_KEYS = (
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "MCP_AUTH_TOKEN",
    "REPO_PATH",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_defaults_with_only_a_password(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "pw-12345")
    s = make()
    assert s.neo4j_uri == "bolt://localhost:7687"
    assert s.neo4j_user == "neo4j"
    assert s.neo4j_password.get_secret_value() == "pw-12345"
    assert s.mcp_auth_token is None
    assert s.repo_path is None
    assert s.log_level == "INFO"


def test_missing_password_fails_fast():
    with pytest.raises(ValidationError) as exc:
        make()
    assert "neo4j_password" in str(exc.value)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "pw-12345")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://abc.databases.neo4j.io")
    monkeypatch.setenv("REPO_PATH", "/srv/target")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = make()
    assert s.neo4j_uri == "neo4j+s://abc.databases.neo4j.io"
    assert str(s.repo_path) == "/srv/target"
    assert s.log_level == "DEBUG"


def test_password_is_not_leaked_in_repr(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "pw-12345")
    assert "pw-12345" not in repr(make())
