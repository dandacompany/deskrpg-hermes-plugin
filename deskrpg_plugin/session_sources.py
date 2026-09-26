"""What a session actually read — `GET /p/{profile}/deskrpg/sessions/{session_id}/sources`.

Derived on every call from the profile's own `state.db` (Hermes stays the source of truth; nothing is
stored here). Only the tool calls that mean "the agent read this" are used:

- `web_extract`   → each extracted page's `url` and `title` (URLs from the arguments when the result is
                    not readable JSON, e.g. a large result that Hermes replaced with a preview)
- `browser_navigate` → the final `url` and `title` from the result
- `read_file`     → the `path` argument, relative to the session's working folder. Kanban worker
                    sessions record no working folder (`cwd` is empty), so a file in a card workspace
                    (`<kanban root>/kanban/boards/<board>/{workspaces,attachments}/<card>/…`) is named `<card>/…`
- `delegate_task` → each child's `files_read`, and one level into a child session it names

Search results (`web_search`, `search_files`) are candidates, not reads, and are left out.

Nothing from a page or file body leaves this module — only addresses, paths and titles. URLs lose
credentials, fragments and token-like query parameters. A file outside the working folder is never
named; it only adds to `outside_workdir_files`. When the session is gone (Hermes deletes ended
sessions after its retention period) the route answers 404 `session_not_found`.
"""

from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web

from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home
from .cron_results import open_session_db

MAX_SOURCES = 200
MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 2000
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

_SECRET_PARAM = re.compile(
    r"token|key|secret|sig|signature|auth|password|passwd|pass|session|sid|code|credential|access|jwt",
    re.IGNORECASE,
)
# An opaque value is dropped whatever its parameter is called.
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_\-.~+/=%]{24,}$")
_SECRET_TEXT = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"[A-Za-z0-9+/_=-]{32,}"),
)
_CHILD_SESSION_KEYS = ("child_session_id", "session_id")


def _iso(ts) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def clean_url(raw) -> str | None:
    """An http(s) URL without credentials, fragment or token-like query parameters; None otherwise."""
    if not isinstance(raw, str):
        return None
    try:
        parts = urlsplit(raw.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    netloc = host if port is None else f"{host}:{port}"
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _SECRET_PARAM.search(k) and not _OPAQUE_VALUE.match(v)
    ]
    url = urlunsplit((parts.scheme.lower(), netloc, parts.path, urlencode(kept), ""))
    return url if len(url) <= MAX_URL_CHARS else None


def clean_title(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    for pattern in _SECRET_TEXT:
        text = pattern.sub("…", text)
    text = text.strip()
    if not text.replace("…", "").strip():
        return None
    return text if len(text) <= MAX_TITLE_CHARS else text[: MAX_TITLE_CHARS - 1] + "…"


def _inside(full: str, root: str) -> str | None:
    root = posixpath.normpath(root)
    if full == root or not full.startswith(root.rstrip("/") + "/"):
        return None
    return posixpath.relpath(full, root)


# Card folders under a board: a card's workspace, and the files attached to it (a parent card's output
# is often read from there).
_CARD_DIRS = ("workspaces", "attachments")


def relative_path(raw, cwd, boards_root=None) -> str | None:
    """`raw` relative to the working folder — or `<card>/…` inside a kanban card workspace — or None
    when it is anywhere else (or cannot be placed)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw.strip()
    if path.startswith("~"):
        return None
    has_cwd = isinstance(cwd, str) and cwd.startswith("/")
    if not path.startswith("/"):
        if not has_cwd:
            return None
        path = posixpath.join(cwd, path)
    full = posixpath.normpath(path)
    if has_cwd:
        inside = _inside(full, cwd)
        if inside is not None:
            return inside
    if isinstance(boards_root, str) and boards_root.startswith("/"):
        inside = _inside(full, boards_root)
        parts = inside.split("/") if inside else []
        # <board>/{workspaces,attachments}/<card>/<file…>
        if len(parts) >= 4 and parts[1] in _CARD_DIRS:
            return "/".join(parts[2:])
    return None


def _json(text):
    """A tool result as JSON. Hermes may put a line of text in front of the JSON, so fall back to the
    first object in the text."""
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            return None


def _calls(message):
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = fn.get("name") or call.get("name")
        args = _json(fn.get("arguments") if "arguments" in fn else call.get("arguments"))
        yield call.get("id") or call.get("call_id"), name, args if isinstance(args, dict) else {}


class _Collector:
    def __init__(self, boards_root=None):
        self.boards_root = boards_root
        self.sources: list[dict] = []
        self.index: dict[tuple[str, str], dict] = {}
        self.outside = 0
        self.truncated = False

    def web(self, url, title, via, at):
        ref = clean_url(url)
        if ref:
            self._add("web", ref, clean_title(title), via, at)

    def file(self, path, cwd, via, at):
        ref = relative_path(path, cwd, self.boards_root)
        if ref is None:
            self.outside += 1
        else:
            self._add("file", ref, None, via, at)

    def _add(self, kind, ref, title, via, at):
        seen = self.index.get((kind, ref))
        if seen is not None:
            if not seen["title"] and title:
                seen["title"] = title
            return
        if len(self.sources) >= MAX_SOURCES:
            self.truncated = True
            return
        entry = {"kind": kind, "ref": ref, "title": title, "via": via, "at": at}
        self.index[(kind, ref)] = entry
        self.sources.append(entry)


def collect(messages, cwd, out: _Collector, *, via_override=None, children=None) -> None:
    """Adds what `messages` read to `out`. Child session ids named by `delegate_task` go to `children`."""
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        at = _iso(message.get("timestamp"))
        for call_id, name, args in _calls(message):
            via = via_override or name
            result = _json(results.get(call_id)) if call_id in results else None
            if name == "web_extract":
                pages = result.get("results") if isinstance(result, dict) else None
                if isinstance(pages, list):
                    for page in pages:
                        if isinstance(page, dict) and not page.get("error"):
                            out.web(page.get("url"), page.get("title"), via, at)
                elif not (isinstance(result, dict) and result.get("success") is False):
                    for url in args.get("urls") or []:
                        out.web(url, None, via, at)
            elif name == "browser_navigate":
                if isinstance(result, dict) and result.get("success") is not False and result.get("url"):
                    out.web(result.get("url"), result.get("title"), via, at)
            elif name == "read_file":
                if not (isinstance(result, dict) and result.get("error")):
                    out.file(args.get("path"), cwd, via, at)
            elif name == "delegate_task" and isinstance(result, dict):
                for child in result.get("results") or []:
                    if not isinstance(child, dict):
                        continue
                    for path in child.get("files_read") or []:
                        out.file(path, cwd, "delegate_task", at)
                    if children is not None:
                        for key in _CHILD_SESSION_KEYS:
                            if isinstance(child.get(key), str) and child[key]:
                                children.append(child[key])
                                break


def _messages(sdb, session_id):
    try:
        return sdb.get_messages(session_id, include_compacted=True)
    except TypeError:  # a Hermes build without compaction archives
        return sdb.get_messages(session_id)


def _cwd(row):
    value = row.get("cwd") if isinstance(row, dict) else getattr(row, "cwd", None)
    return value if isinstance(value, str) and value else None


def read_sources(sdb, session_id, boards_root=None) -> dict | None:
    row = sdb.get_session(session_id)
    if not row:
        return None
    cwd = _cwd(row)
    out = _Collector(boards_root)
    children: list[str] = []
    collect(_messages(sdb, session_id), cwd, out, children=children)
    for child_id in dict.fromkeys(children):
        if child_id == session_id:
            continue
        child = sdb.get_session(child_id)
        if child:
            collect(_messages(sdb, child_id), _cwd(child) or cwd, out, via_override="delegate_task")
    return {
        "session_id": session_id,
        "sources": out.sources,
        "outside_workdir_files": out.outside,
        "truncated": out.truncated,
    }


def _boards_root(api):
    """Hermes `kanban_db.boards_root()`: `kanban_home()` is the shared Hermes root, not the kanban folder."""
    try:
        return str(Path(api.kanban_home()) / "kanban" / "boards")
    except Exception:  # noqa: BLE001 — without it, card files are only counted
        return None


def get_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        session_id = request.match_info["session_id"]
        if not _SESSION_ID.match(session_id):
            raise RequestError(400, "invalid_session_id", "session id has an unexpected shape")

        def read():
            if not (Path(home) / "state.db").is_file():
                return None
            sdb = open_session_db(api, home)
            try:
                return read_sources(sdb, session_id, _boards_root(api))
            finally:
                sdb.close()

        body = await run_blocking(read)
        if body is None:
            raise RequestError(404, "session_not_found", "the session is gone or never existed")
        return web.json_response(body)

    return handler
