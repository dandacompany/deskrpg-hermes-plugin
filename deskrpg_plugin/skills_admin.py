"""NPC 스킬 관리(0.15.0) — 목록 확장·조회·쓰기·생애주기 라우트.

**스킬 정본은 Hermes 다.** 목록·사용량·보관 상태를 여기서 따로 저장하지 않는다. 분류·위치·경로 검사·
쓰기 컨텍스트는 `skills_common` 에 있고, 쓰기는 Hermes 의 사용자 쓰기 경로
(`_create_skill`·`_edit_skill`·`_write_file`)를 거친다 — 대시보드와 같은 가드·ledger 가 적용된다.

모든 Hermes 호출은 요청 프로필 홈 스코프(`picker._home_scope`) 안의 워커 스레드에서 한다.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
from pathlib import Path

from aiohttp import web

from . import config as _config
from .common import (
    RequestError,
    guarded,
    log_event,
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
    actor_of,
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


# ---- 생애주기 — 고정·보관·보관함·복원·영구 삭제 ---------------------------------------------

# 보관함 안의 폴더 이름. 경로 구분자·`..` 로 시작하는 이름을 막는다(복원·영구 삭제 공용).
_ARCHIVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_local(ref: SkillRef) -> None:
    if ref.source != "local":
        raise RequestError(400, "skill_not_local", ref.source)


def _require_archive_name(name: str) -> None:
    if not _ARCHIVE_NAME_RE.fullmatch(name):
        raise RequestError(400, "invalid_name", name)


def _pin(api, home, name, pinned):
    with _home_scope(api, home):
        _require_local(require_skill(api, name))
        if not api.set_pinned(name, pinned):
            raise RequestError(400, "lifecycle_rejected", "pin did not land")
        return {"name": name, "pinned": pinned}


def _archive(api, home, name, actor):
    with _home_scope(api, home):
        _require_local(require_skill(api, name))
        with user_write(api):
            ok, message = api.archive_skill(name)
        if not ok:
            raise RequestError(400, "lifecycle_rejected", str(message)[:500])
    # Hermes `archive_skill` 이 자체 ledger 를 남기므로 evidence 로 넘길 자리가 없다 — 누가 했는지는 로그로만.
    log_event("skill_archive", skill_id=name, actor_id=actor)
    return {"name": name}


def _archived(api, home):
    with _home_scope(api, home):
        root = Path(api._archive_dir())
        out = []
        for name in api.list_archived_skill_names():
            p = root / name
            ts = p.stat().st_mtime if p.exists() else None
            out.append({"name": name,
                        "archivedAt": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat() if ts else None})
        return {"archived": out}


def _restore(api, home, name):
    _require_archive_name(name)
    with _home_scope(api, home):
        with user_write(api):
            ok, message = api.restore_skill(name)
        if not ok:
            if "not found" in str(message):
                raise RequestError(404, "archived_not_found", name)
            raise RequestError(400, "lifecycle_rejected", str(message)[:500])
        return {"name": name}


def _purge(api, home, name, actor):
    """보관된 스킬 하나를 지운다 — Hermes `curator purge`(TTL 일괄)와 같은 부품: capture_before → rmtree → append_entry."""
    _require_archive_name(name)
    with _home_scope(api, home):
        root = Path(api._archive_dir()).resolve()
        target = root / name
        if not target.is_dir() or target.is_symlink() or target.resolve().parent != root:
            raise RequestError(404, "archived_not_found", name)
        before = api.capture_before(target, complete_package=True, skill=name)
        shutil.rmtree(target)
        entry = api.append_entry("purge", name, before=before or [], after=[], actor="user",
                                 evidence={"reason": "deskrpg_single_purge", "deskrpgUserId": actor})
        return {"name": name, "ledgerId": entry}


def pinned_handler(api):
    """`PUT /p/{profile}/deskrpg/skills/{name}/pinned`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        pinned = require_bool(await read_json_object(request), "pinned")
        return web.json_response(await run_blocking(_pin, api, home, request.match_info["name"], pinned))

    return handler


def archive_handler(api):
    """`POST /p/{profile}/deskrpg/skills/{name}/archive`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(
            await run_blocking(_archive, api, home, request.match_info["name"], actor_of(request)))

    return handler


def archive_list_handler(api):
    """`GET /p/{profile}/deskrpg/skills/archive`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(_archived, api, home))

    return handler


def restore_handler(api):
    """`POST /p/{profile}/deskrpg/skills/archive/{name}/restore`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(_restore, api, home, request.match_info["name"]))

    return handler


def purge_handler(api):
    """`DELETE /p/{profile}/deskrpg/skills/archive/{name}`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(
            await run_blocking(_purge, api, home, request.match_info["name"], actor_of(request)))

    return handler
