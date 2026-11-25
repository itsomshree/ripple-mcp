from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ripple.config import Settings

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 300.0


class CheckoutError(Exception):
    """The remote repository could not be cloned or updated"""


@dataclass(frozen=True)
class RepoCheckout:
    """A shallow clone of one branch, kept in step with the remote"""

    url: str
    dest: Path
    branch: str | None = None
    token: str | None = None
    timeout: float = GIT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.url.startswith("-") or (self.branch or "").startswith("-"):
            raise ValueError("the repo url and branch must not start with '-'")
        if self.token and not self.url.startswith("https://"):
            raise ValueError("REPO_TOKEN needs an https:// REPO_URL")

    def sync(self) -> str:
        """Make the checkout match the remote branch tip. Returns the commit sha"""
        if self._origin() == self.url:
            self._update()
        else:
            self._clone()
        return self._git("rev-parse", "HEAD", cwd=self.dest)

    # steps
    def _origin(self) -> str | None:
        if not (self.dest / ".git").is_dir():
            return None
        try:
            return self._git("remote", "get-url", "origin", cwd=self.dest)
        except CheckoutError:
            return None

    def _clone(self) -> None:
        if self.dest.exists():
            shutil.rmtree(self.dest)
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1"]
        if self.branch:
            args += ["--branch", self.branch]
        try:
            self._git(*args, "--", self.url, str(self.dest), cwd=self.dest.parent)
        except CheckoutError:
            shutil.rmtree(self.dest, ignore_errors=True)
            raise

    def _update(self) -> None:
        branch = self.branch or self._git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=self.dest
        )
        self._git("fetch", "--depth", "1", "origin", branch, cwd=self.dest)
        self._git("checkout", "--force", "-B", branch, "FETCH_HEAD", cwd=self.dest)
        self._git("clean", "-ffdx", cwd=self.dest)

    # git
    def _env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if self.token:
            env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {self._basic()}",
            }
        return env

    def _basic(self) -> str:
        return base64.b64encode(f"x-access-token:{self.token}".encode()).decode()

    def _redact(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "***").replace(self._basic(), "***")
        return text

    def _git(self, *args: str, cwd: Path) -> str:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CheckoutError(
                f"git {args[0]} timed out after {self.timeout:.0f}s"
            ) from exc
        except OSError as exc:
            raise CheckoutError(f"cannot run git: {exc}") from exc
        if done.returncode != 0:
            lines = self._redact(done.stderr).strip().splitlines()
            raise CheckoutError(
                f"git {args[0]} failed: {lines[-1] if lines else done.returncode}"
            )
        return done.stdout.strip()


def checkout_from_settings(settings: Settings) -> RepoCheckout | None:
    if not settings.repo_url:
        return None
    token = settings.repo_token.get_secret_value() if settings.repo_token else None
    return RepoCheckout(
        url=settings.repo_url,
        dest=settings.repo_checkout_dir.expanduser(),
        branch=settings.repo_branch or None,
        token=token or None,
    )
