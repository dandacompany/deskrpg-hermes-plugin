"""NPC 스킬 관리(0.15.0) — 목록 확장·조회·쓰기·생애주기 라우트.

**스킬 정본은 Hermes 다.** 목록·사용량·보관 상태를 여기서 따로 저장하지 않는다. 분류·위치·경로 검사·
쓰기 컨텍스트는 `skills_common` 에 있고, 쓰기는 Hermes 의 사용자 쓰기 경로
(`_create_skill`·`_edit_skill`·`_write_file`)를 거친다 — 대시보드와 같은 가드·ledger 가 적용된다.

모든 Hermes 호출은 요청 프로필 홈 스코프(`picker._home_scope`) 안의 워커 스레드에서 한다.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .common import RequestError, guarded, run_blocking
from .cron import resolve_profile_home
from .picker import _home_scope
from .skills_common import (
    SkillRef,
    classify,
    is_editable,
    require_skill,
    resolve_editable_path,
    sha256_text,
)


def _row_extra(api, name: str, usage: dict) -> dict:
    rec = usage.get(name) or {}
    path = api._find_skill_dir(name) or api._find_external_skill_dir(name)
    return {
        "source": classify(api, name, Path(path) if path else None),
        "curatorManaged": bool(api.is_curator_managed(name)),
        "state": rec.get("state") if rec.get("state") in ("active", "stale") else "active",
        "pinned": bool(rec.get("pinned")),
        "useCount": int(rec.get("use_count") or 0),
        "viewCount": int(rec.get("view_count") or 0),
        "lastUsedAt": api.latest_activity_at(rec) if rec else None,
    }


def enrich_rows(api, rows: list[dict]) -> list[dict]:
    """`picker.skill_rows` 의 행에 0.15.0 필드를 덧붙인다. **홈 스코프 안에서만** 부른다."""
    usage = api.load_usage() or {}
    return [{**row, **_row_extra(api, row["name"], usage)} for row in rows]


def file_tree(ref: SkillRef) -> list[dict]:
    out = []
    for p in sorted(ref.path.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(ref.path).as_posix()
        out.append({"path": rel, "size": p.stat().st_size, "editable": is_editable(ref, rel)})
    return out


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    out = {}
    for line in head.splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _detail(api, home, name):
    with _home_scope(api, home):
        ref = require_skill(api, name)
        rec = (api.load_usage() or {}).get(name) or {}
        text = (ref.path / "SKILL.md").read_text(encoding="utf-8")
        return {
            "skill": {"name": name, "source": ref.source,
                      "curatorManaged": bool(api.is_curator_managed(name)),
                      "pinned": bool(rec.get("pinned")), "frontmatter": _frontmatter(text)},
            "files": file_tree(ref),
        }


def _read_file(api, home, name, rel):
    with _home_scope(api, home):
        ref = require_skill(api, name)
        target = resolve_editable_path(ref, rel)
        if not target.is_file():
            raise RequestError(404, "file_not_found", rel)
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise RequestError(415, "binary_file", rel) from None
        return {"path": rel, "content": content, "hash": sha256_text(content)}


def detail_handler(api):
    """`GET /p/{profile}/deskrpg/skills/{name}`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(_detail, api, home, request.match_info["name"]))

    return handler


def file_get_handler(api):
    """`GET /p/{profile}/deskrpg/skills/{name}/file?path=`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        rel = request.query.get("path", "")
        return web.json_response(await run_blocking(_read_file, api, home, request.match_info["name"], rel))

    return handler
