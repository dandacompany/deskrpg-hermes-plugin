"""`.githooks/pre-push` and `scripts/ci-local.sh`: what runs when a push is checked.

Two ways the gate went wrong (2026-09-27, plugin releases 0.28.0 to 0.29.1):

- The hook checks the working tree, not the commit being pushed. A tag pushed from a checkout that sat on an old
  commit was "verified" by that old commit's tests.
- `ci-local.sh --full` reuses `.ci-venv`. Its first step, the CI test job, ran in that venv, which the first full run
  had given Hermes. From the second run on, that step collected the integration tests the pinned job deselects
  (`upstream_only`) and failed — the tag push that follows a master push in a fresh worktree always failed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-push"
SCRIPT = ROOT / "scripts" / "ci-local.sh"
CLEAN_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
ZERO = "0" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, env=CLEAN_ENV, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with the real hook and a stub ci-local.sh that records that it ran."""
    repo = tmp_path / "repo"
    (repo / ".githooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy(HOOK, repo / ".githooks" / "pre-push")
    ran = tmp_path / "ran"
    stub = repo / "scripts" / "ci-local.sh"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{ran}"\n')
    stub.chmod(0o755)
    _git(repo, "init", "-q")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        _git(repo, "config", key, value)
    (repo / "f.txt").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    return repo, ran


def _push(repo: Path, lines: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(repo / ".githooks" / "pre-push"), "origin", "https://example.invalid/r.git"],
        cwd=repo, input="".join(line + "\n" for line in lines), env=CLEAN_ENV,
        capture_output=True, text=True,
    )


def test_a_push_of_the_checked_out_commit_runs_the_checks(tmp_path: Path) -> None:
    repo, ran = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    done = _push(repo, [f"refs/heads/master {head} refs/heads/master {ZERO}"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert ran.read_text() == "--full\n"


def test_an_annotated_tag_on_the_checked_out_commit_is_accepted(tmp_path: Path) -> None:
    repo, ran = _repo(tmp_path)
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    tag_object = _git(repo, "rev-parse", "v1.0.0")
    done = _push(repo, [f"refs/tags/v1.0.0 {tag_object} refs/tags/v1.0.0 {ZERO}"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert ran.exists()


def test_a_push_of_another_commit_is_refused_before_any_check(tmp_path: Path) -> None:
    repo, ran = _repo(tmp_path)
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    (repo / "f.txt").write_text("2\n")
    _git(repo, "commit", "-qam", "two")
    old_tag = _git(repo, "rev-parse", "v1.0.0")
    done = _push(repo, [f"refs/tags/v1.0.0 {old_tag} refs/tags/v1.0.0 {ZERO}"])
    assert done.returncode == 1
    assert not ran.exists(), "the checks would have tested the wrong commit"
    assert "refs/tags/v1.0.0" in done.stdout + done.stderr


def test_uncommitted_changes_to_tracked_files_are_refused(tmp_path: Path) -> None:
    repo, ran = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("dirty\n")
    done = _push(repo, [f"refs/heads/master {head} refs/heads/master {ZERO}"])
    assert done.returncode == 1
    assert not ran.exists()


def test_a_delete_only_push_checks_nothing(tmp_path: Path) -> None:
    repo, ran = _repo(tmp_path)
    done = _push(repo, [f"(delete) {ZERO} refs/tags/v0 {'1' * 40}"])
    assert done.returncode == 0
    assert not ran.exists()


def test_the_ci_test_job_step_runs_in_a_venv_that_never_gets_hermes() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert 'UNIT_VENV="$ROOT/.ci-venv-unit"' in body
    unit_run = '"$UNIT_PY" -m pytest -q'
    assert unit_run in body
    # Hermes is installed only into the other venv, and the unit venv is checked to be without it.
    assert '"$PY" -m pip install -q -e "$HERMES_SRC[mcp]"' in body
    assert '"$UNIT_PY" -m pip install -q -e' not in body
    guard = "find_spec('hermes_cli')"
    assert guard in body and body.index(guard) < body.index(unit_run)


def test_the_unit_venv_is_ignored_by_git() -> None:
    assert ".ci-venv-unit/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
