import pytest

HERMETIC_KEYS = (
    "REPO_URL",
    "REPO_TOKEN",
    "REPO_BRANCH",
    "REPO_CHECKOUT_DIR",
    "REPO_PATH",
    "SCAN_EXCLUDE",
    "MCP_AUTH_TOKEN",
    "MCP_TRANSPORT",
    "MCP_HOST",
    "MCP_PORT",
    "PORT",
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for key in HERMETIC_KEYS:
        monkeypatch.delenv(key, raising=False)
