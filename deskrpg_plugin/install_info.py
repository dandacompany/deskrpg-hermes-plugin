"""Which commit of this plugin is running — `/deskrpg/info` `install.commit`.

DeskRPG pins the plugin commit each release was tested with. A container deployment set up with an older compose
file keeps installing the plugin's main branch, whose version string usually equals the latest release, so only the
commit tells DeskRPG that it is talking to an unreleased build.

The commit is read once, when the plugin is imported, so it names the code that is running even if the checkout is
replaced before the gateway restarts. Only files under `.git` are read — no git binary, no Hermes internals. A
checkout without `.git` (catalog or subdirectory installs) or anything unreadable reports None; this never raises.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git_dir(root: Path) -> Path | None:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():  # a linked worktree or submodule: "gitdir: <path>"
        text = dot.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            path = Path(text[len("gitdir:"):].strip())
            return path if path.is_absolute() else (root / path).resolve()
    return None


def _sha(value: str) -> str | None:
    value = value.strip()
    return value if _SHA.match(value) else None


def commit_of(root: Path) -> str | None:
    """The commit checked out at `root`, or None."""
    try:
        git = _git_dir(root)
        if git is None:
            return None
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return _sha(head)  # detached, as an install made with --ref leaves it
        ref = head[len("ref: "):].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            loose = base / ref
            if loose.is_file():
                return _sha(loose.read_text(encoding="utf-8"))
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return _sha(sha)
        return None
    except Exception:  # noqa: BLE001 — an unreadable checkout is "unknown", never an error
        return None


INSTALLED_COMMIT = commit_of(PLUGIN_ROOT)


def report() -> dict:
    """`{"commit": str | None}`."""
    return {"commit": INSTALLED_COMMIT}
