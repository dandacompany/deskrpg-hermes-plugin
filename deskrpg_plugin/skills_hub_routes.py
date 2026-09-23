"""Hub 검색·미리보기·설치·삭제·업데이트(0.15.0). Hermes 대시보드의 `/api/skills/hub/*` 와 같은 함수를 쓴다.

설치 전 판정(`should_allow_install`)을 여기서도 한 번 본다 — `block` 이면 하위 프로세스를 띄우지 않는다.
최종 차단은 여전히 Hermes 설치 명령이 한다(우리는 우회하지 않는다).

판정은 Hermes 함수에 두 번 묻는다. `force=False` 로 허용이면 `allow`, 아니면 `force=True` 로 다시 물어 허용이면
`ask`(확인 뒤 `--force` 로 설치), 그래도 막히면 `block`(community·trusted 의 dangerous — `--force` 도 안 통한다).
Hermes 정책표에서 hub 출처는 "ask" 를 돌려주지 않는다 — community+caution 은 `False` 지만 `--force` 로 풀린다.
"""

from __future__ import annotations

import re
import shutil

from aiohttp import web

from . import skill_jobs
from .common import RequestError, guarded, read_json_object, require_bool, require_str, run_blocking
from .cron import resolve_profile_home
from .picker import _home_scope
from .skills_common import locate

# 첫 글자 `-` 금지 — 하위 프로세스 argv 의 위치 인자라 `--force`·`--category=…` 같은 플래그로 읽히면 안 된다.
_IDENT_RE = re.compile(r"^[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]{0,511}$")


def _ident(value) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise RequestError(400, "invalid_identifier", "identifier")
    return value


def _skill_name(value) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise RequestError(400, "invalid_name", "name")
    return value


def _search(api, home, q, source):
    if not q.strip():
        return {"results": [], "timedOut": []}
    with _home_scope(api, home):
        sources = api.create_source_router()
        results, _counts, timed_out = api.parallel_search_sources(
            sources, query=q.strip(), source_filter=source or "all", overall_timeout=30)
    rank = {"builtin": 2, "trusted": 1, "community": 0}
    seen = {}
    for r in results:
        prev = seen.get(r.identifier)
        if prev is None or rank.get(r.trust_level, 0) > rank.get(prev.trust_level, 0):
            seen[r.identifier] = r
    return {"results": [{"identifier": r.identifier, "name": r.name, "description": r.description or "",
                         "source": r.source, "trustLevel": r.trust_level} for r in list(seen.values())[:50]],
            "timedOut": list(timed_out)}


def _policy(api, result) -> tuple[str, str]:
    allowed, reason = api.should_allow_install(result, force=False)
    if allowed is True:
        return "allow", reason or ""
    forced, _ = api.should_allow_install(result, force=True)
    return ("ask" if allowed is None or forced is True else "block"), reason or ""


def _preview(api, home, ident):
    """(응답 본문, Hermes 가 설치할 폴더 이름 `bundle.name`)."""
    with _home_scope(api, home):
        sources = api.create_source_router()
        meta, bundle, _src = api._resolve_source_meta_and_bundle(ident, sources)
        if not bundle:
            raise RequestError(404, "hub_skill_not_found", ident)
        files = {}
        for rel, content in (bundle.files or {}).items():
            if isinstance(content, bytes):
                try:
                    files[rel] = content.decode("utf-8")
                except UnicodeDecodeError:
                    files[rel] = "(binary file)"
            else:
                files[rel] = content
        scan_source = "official" if bundle.source == "official" else (getattr(bundle, "identifier", "") or ident)
        q_path = api.quarantine_bundle(bundle)
        try:
            result = api.scan_skill(q_path, source=scan_source)
        finally:
            shutil.rmtree(q_path, ignore_errors=True)
        policy, reason = _policy(api, result)
    m = meta or bundle
    body = {
        "name": getattr(m, "name", ident), "identifier": getattr(m, "identifier", ident) or ident,
        "description": getattr(m, "description", "") or "", "source": getattr(m, "source", "") or "",
        "trustLevel": getattr(m, "trust_level", "community") or "community",
        "skillMd": files.get("SKILL.md", ""), "files": sorted(files),
        "hasScripts": any(f.startswith("scripts/") for f in files),
        "verdict": result.verdict, "policy": policy, "policyReason": reason,
    }
    return body, bundle.name


def _hub_installed(api, home, name) -> bool:
    with _home_scope(api, home):
        return bool(api.is_hub_installed(name))


def search_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(
            _search, api, home, request.query.get("q", ""), request.query.get("source", "all")))
    return handler


def preview_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        ident = _ident(request.query.get("identifier", ""))
        body, _name = await run_blocking(_preview, api, home, ident)
        return web.json_response(body)
    return handler


def install_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        home = resolve_profile_home(api, profile)
        body = await read_json_object(request)
        ident = _ident(body.get("identifier"))
        force = require_bool(body, "force", required=False, default=False)
        preview, name = await run_blocking(_preview, api, home, ident)
        if preview["policy"] == "block":
            raise RequestError(400, "install_blocked", preview["policyReason"][:500])
        argv = ["skills", "install", ident, "--yes"] + (["--force"] if force and preview["policy"] == "ask" else [])
        job = skill_jobs.TABLE.start(api, api.normalize_profile_name(profile), "hub_install", argv,
                                     verify=lambda: _hub_installed(api, home, name))
        return web.json_response({"jobId": job}, status=202)
    return handler


def job_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        resolve_profile_home(api, profile)
        return web.json_response(skill_jobs.TABLE.get(api.normalize_profile_name(profile), request.match_info["job_id"]))
    return handler


def _require_hub(api, home, name):
    with _home_scope(api, home):
        ref = locate(api, name)
        if ref is None or ref.source != "hub":
            raise RequestError(400, "skill_not_hub", name)


def uninstall_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        home = resolve_profile_home(api, profile)
        name = _skill_name(require_str(await read_json_object(request), "name"))
        await run_blocking(_require_hub, api, home, name)
        job = skill_jobs.TABLE.start(api, api.normalize_profile_name(profile), "hub_update",
                                     ["skills", "uninstall", name, "--yes"],
                                     verify=lambda: not _hub_installed(api, home, name))
        return web.json_response({"jobId": job}, status=202)
    return handler


def update_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        home = resolve_profile_home(api, profile)
        name = require_str(await read_json_object(request), "name", required=False, default=None)
        if name is not None:
            await run_blocking(_require_hub, api, home, _skill_name(name))
        argv = ["skills", "update"] + ([name] if name else [])
        job = skill_jobs.TABLE.start(api, api.normalize_profile_name(profile), "hub_update", argv)
        return web.json_response({"jobId": job}, status=202)
    return handler
