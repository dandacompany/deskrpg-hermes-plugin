"""NPC 스킬 관리(0.15.0) — 목록 확장·조회·쓰기·생애주기 라우트.

**스킬 정본은 Hermes 다.** 목록·사용량·보관 상태를 여기서 따로 저장하지 않는다. 분류·위치·경로 검사·
쓰기 컨텍스트는 `skills_common` 에 있고, 쓰기는 Hermes 의 사용자 쓰기 경로
(`_create_skill`·`_edit_skill`·`_write_file`)를 거친다 — 대시보드와 같은 가드·ledger 가 적용된다.

모든 Hermes 호출은 요청 프로필 홈 스코프(`picker._home_scope`) 안의 워커 스레드에서 한다.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from . import config as _config
from .common import (
    RequestError,
    guarded,
    read_json_object,
    require_bool,
    require_str,
    require_str_list,
    run_blocking,
)
from .cron import resolve_profile_home
from .picker import _disabled, _home_scope, _load_config
from .skills_common import (
    MAX_EDIT_BYTES,
    SkillRef,
    classify,
    is_editable,
    require_skill,
    resolve_editable_path,
    sha256_text,
    user_write,
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


# ---- 쓰기 — 파일 저장·새 스킬·켜기/끄기 ----------------------------------------------------


def _check_hermes(result: dict) -> None:
    if not result.get("success"):
        raise RequestError(400, "skill_write_rejected", str(result.get("error") or "rejected")[:500])


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise RequestError(413, "payload_too_large", f"max {MAX_EDIT_BYTES} bytes")


def _write_file(api, home, name, rel, content, base_hash):
    """`baseHash` 가 null 이면 새 파일(있으면 409 `file_exists`), 아니면 현재 내용 해시와 같아야 쓴다."""
    _check_size(content)
    with _home_scope(api, home):
        ref = require_skill(api, name)
        target = resolve_editable_path(ref, rel, for_write=True)
        exists = target.is_file()
        if base_hash is None:
            if exists:
                raise RequestError(409, "file_exists", rel)
        else:
            current = target.read_text(encoding="utf-8") if exists else None
            if current is None or sha256_text(current) != base_hash:
                raise RequestError(409, "skill_changed", rel)
        # Hermes 는 스킬을 **폴더 이름**으로 찾는다(`_find_skill`) — 프론트매터 이름이 아니다.
        with user_write(api):
            if rel == "SKILL.md":
                result = api._edit_skill(ref.path.name, content)
            else:
                result = api._write_file(ref.path.name, rel, content)
        _check_hermes(result)
        return {"path": rel, "hash": sha256_text(content)}


def _create(api, home, name, category, content):
    _check_size(content)
    with _home_scope(api, home):
        with user_write(api):
            result = api._create_skill(name, content, category or None)
        _check_hermes(result)
        return {"name": name}


def _set_disabled(api, home, enable: list[str], disable: list[str]):
    """`skills.disabled` 를 한 번 읽고 한 번 쓴다. 하나라도 거절이면 아무것도 쓰지 않는다."""
    essential = set(getattr(api, "ESSENTIAL_SKILLS", None) or ())
    bad = sorted(set(disable) & essential)
    if bad:
        raise RequestError(400, "essential_skill", ",".join(bad))
    with _home_scope(api, home):
        known = {s["name"] for s in api._find_all_skills(skip_disabled=True)}
    unknown = sorted((set(enable) | set(disable)) - known)
    if unknown:
        raise RequestError(400, "unknown_skill", ",".join(unknown))
    disabled = (_disabled(_load_config(home)) - set(enable)) | set(disable)
    _config.write_skills_disabled(home, sorted(disabled))
    return {"disabled": sorted(disabled)}


def file_put_handler(api):
    """`PUT /p/{profile}/deskrpg/skills/{name}/file`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        rel = require_str(body, "path")
        content = require_str(body, "content", allow_empty=True)
        base_hash = body.get("baseHash")
        if base_hash is not None and not isinstance(base_hash, str):
            raise RequestError(400, "invalid_field", "baseHash")
        return web.json_response(
            await run_blocking(_write_file, api, home, request.match_info["name"], rel, content, base_hash))

    return handler


def create_handler(api):
    """`POST /p/{profile}/deskrpg/skills`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        name = require_str(body, "name")
        category = require_str(body, "category", required=False, default=None)
        content = require_str(body, "content")
        return web.json_response(await run_blocking(_create, api, home, name, category, content), status=201)

    return handler


def enabled_handler(api):
    """`PUT /p/{profile}/deskrpg/skills/{name}/enabled`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        name = request.match_info["name"]
        enabled = require_bool(await read_json_object(request), "enabled")
        await run_blocking(_set_disabled, api, home, [name] if enabled else [], [] if enabled else [name])
        return web.json_response({"name": name, "enabled": enabled})

    return handler


def bulk_enabled_handler(api):
    """`PUT /p/{profile}/deskrpg/skills/enabled`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        enable = require_str_list(body, "enable", required=False, default=[])
        disable = require_str_list(body, "disable", required=False, default=[])
        if set(enable) & set(disable):
            raise RequestError(400, "invalid_field", "enable/disable overlap")
        return web.json_response(await run_blocking(_set_disabled, api, home, enable, disable))

    return handler
