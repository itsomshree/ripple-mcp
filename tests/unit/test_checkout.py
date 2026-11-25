import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ripple.scanner.checkout import CheckoutError, RepoCheckout

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **IDENT},
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def commit(repo: Path, files: dict[str, str], amend: bool = False) -> None:
    for rel, text in files.items():
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", *(["--amend"] if amend else []), "-m", "c")


def origin(path: Path, files: dict[str, str], branch: str = "main") -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    commit(path, files)
    return path


def checkout(url: Path, dest: Path, **kw) -> RepoCheckout:
    return RepoCheckout(url=url.as_uri(), dest=dest, **kw)


def test_first_sync_clones_and_returns_the_sha(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "x = 1\n"})
    dest = tmp_path / "data" / "repo"
    sha = checkout(src, dest).sync()
    assert (dest / "a.py").read_text() == "x = 1\n"
    assert sha == git(src, "rev-parse", "HEAD")


def test_later_sync_follows_new_commits(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "x = 1\n"})
    c = checkout(src, tmp_path / "repo")
    c.sync()
    commit(src, {"b.py": "y = 2\n"})
    assert c.sync() == git(src, "rev-parse", "HEAD")
    assert (tmp_path / "repo" / "b.py").exists()


def test_rewritten_history_is_followed(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "v1\n"})
    c = checkout(src, tmp_path / "repo")
    c.sync()
    commit(src, {"a.py": "v2\n"}, amend=True)
    assert c.sync() == git(src, "rev-parse", "HEAD")
    assert (tmp_path / "repo" / "a.py").read_text() == "v2\n"


def test_local_edits_and_stray_files_are_discarded(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "x = 1\n"})
    dest = tmp_path / "repo"
    c = checkout(src, dest)
    c.sync()
    (dest / "a.py").write_text("tampered\n")
    (dest / "junk.py").write_text("z\n")
    c.sync()
    assert (dest / "a.py").read_text() == "x = 1\n"
    assert not (dest / "junk.py").exists()


def test_a_branch_can_be_selected_and_switched(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "main\n"})
    git(src, "checkout", "-q", "-b", "dev")
    commit(src, {"a.py": "dev\n"})
    git(src, "checkout", "-q", "main")
    dest = tmp_path / "repo"
    checkout(src, dest).sync()
    assert (dest / "a.py").read_text() == "main\n"
    checkout(src, dest, branch="dev").sync()
    assert (dest / "a.py").read_text() == "dev\n"


def test_a_changed_url_reclones(tmp_path):
    one = origin(tmp_path / "one", {"a.py": "one\n"})
    two = origin(tmp_path / "two", {"a.py": "two\n"})
    dest = tmp_path / "repo"
    checkout(one, dest).sync()
    checkout(two, dest).sync()
    assert (dest / "a.py").read_text() == "two\n"


def test_failure_is_a_checkout_error_and_leaves_no_half_clone(tmp_path):
    dest = tmp_path / "repo"
    with pytest.raises(CheckoutError):
        checkout(tmp_path / "missing", dest).sync()
    assert not dest.exists()


def test_an_unreachable_remote_keeps_the_existing_checkout(tmp_path):
    src = origin(tmp_path / "o", {"a.py": "x = 1\n"})
    dest = tmp_path / "repo"
    c = checkout(src, dest)
    c.sync()
    shutil.rmtree(src)
    with pytest.raises(CheckoutError):
        c.sync()
    assert (dest / "a.py").exists()


def test_token_is_sent_by_environment_and_never_in_errors(tmp_path):
    c = RepoCheckout(
        url="https://127.0.0.1:1/x.git", dest=tmp_path / "repo", token="s3cret"
    )
    env = c._env()
    assert "s3cret" not in "".join(env.values())
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "s3cret" not in c._redact(f"fatal: s3cret {c._basic()} boom")
    with pytest.raises(CheckoutError) as exc:
        c.sync()
    assert "s3cret" not in str(exc.value) and c._basic() not in str(exc.value)


@pytest.mark.parametrize(
    "kw",
    [
        {"url": "--upload-pack=x"},
        {"url": "https://h/r.git", "branch": "-x"},
        {"url": "file:///r", "token": "t"},
        {"url": "git@github.com:o/r.git", "token": "t"},
    ],
)
def test_unsafe_settings_are_rejected(tmp_path, kw):
    with pytest.raises(ValueError):
        RepoCheckout(dest=tmp_path / "repo", **kw)
