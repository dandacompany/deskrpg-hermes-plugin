"""`/deskrpg/info` `install.commit` — the plugin commit that is running, read from `.git` files only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from deskrpg_plugin import install_info

# Hooks export GIT_DIR and friends; a repository built here must not inherit them.
CLEAN_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, env=CLEAN_ENV, capture_output=True, text=True)
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "deskrpg"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "plugin.yaml").write_text("version: 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "plugin.yaml").write_text("version: 2\n")
    _git(repo, "commit", "-qam", "two")
    return repo, first, _git(repo, "rev-parse", "HEAD")


def test_a_branch_checkout_reports_its_head(tmp_path):
    repo, _first, head = _repo(tmp_path)
    assert install_info.commit_of(repo) == head


def test_a_pinned_install_reports_the_detached_commit(tmp_path):
    repo, first, _head = _repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach", first)
    assert install_info.commit_of(repo) == first


def test_a_packed_ref_is_found(tmp_path):
    repo, _first, head = _repo(tmp_path)
    _git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "master").exists()
    assert install_info.commit_of(repo) == head


def test_a_linked_worktree_resolves_through_its_gitdir_file(tmp_path):
    repo, first, _head = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked), first)
    assert (linked / ".git").is_file()
    assert install_info.commit_of(linked) == first


def test_no_git_directory_or_a_broken_head_is_unknown(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert install_info.commit_of(plain) is None
    repo, _first, _head = _repo(tmp_path)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/missing\n")
    assert install_info.commit_of(repo) is None
    (repo / ".git" / "HEAD").write_text("not a commit\n")
    assert install_info.commit_of(repo) is None


def test_the_report_names_the_commit_read_at_import(monkeypatch):
    monkeypatch.setattr(install_info, "INSTALLED_COMMIT", "a" * 40)
    assert install_info.report() == {"commit": "a" * 40}
