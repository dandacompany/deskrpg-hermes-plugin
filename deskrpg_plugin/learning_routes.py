"""curator 상태·제어와 학습 관계도(0.15.0).

관계도는 Hermes `build_learning_graph()` 를 그대로 옮긴다. 메모리·USER 조각은 `includeMemory=1` 일 때만 싣는다 —
DeskRPG 서버가 게이트웨이 소유자에게만 1 로 부른다. 걸러내기는 여기서(서버에서) 한다.

메모리 노드 id(`memory:<source>:<index>`)는 번호라 파일이 바뀌면 다른 조각을 가리킨다. 편집·삭제는 baseHash 로
현재 조각을 확인한다. Hermes 의 메모리 삭제는 백업이 없으므로 지우기 전에 원문을 `plugin-data/deskrpg/memory_deleted.jsonl` 에 남긴다.

스킬 노드 id 는 프론트매터 이름이다(`build_skill_nodes`). Hermes `node_detail`·`edit_node` 는 스킬을 **폴더 이름**
(`_find_skill`)으로 찾아 둘이 다른 스킬에서 어긋나므로, 스킬 노드는 `locate`(프론트매터 이름)로 찾아 직접 다룬다.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path

from aiohttp import web

from . import skill_jobs
from .common import RequestError, guarded, read_json_object, require_bool, require_str, run_blocking
from .cron import resolve_profile_home
from .picker import _home_scope
from .skills_common import actor_of, locate, require_unpinned, sha256_text, user_write

_LOG_LOCK = threading.Lock()
MEMORY_LOG = ("plugin-data", "deskrpg", "memory_deleted.jsonl")
_MEMORY_FILES = {"memory": "MEMORY.md", "profile": "USER.md"}  # Hermes learning_mutations 와 같은 표


def _curator(api, home):
    with _home_scope(api, home):
        state = api.load_state() or {}
        return {"enabled": bool(api.is_enabled()), "paused": bool(api.is_paused()),
                "intervalHours": api.get_interval_hours(), "lastRunAt": state.get("last_run_at"),
                "minIdleHours": api.get_min_idle_hours(), "staleAfterDays": api.get_stale_after_days(),
                "archiveAfterDays": api.get_archive_after_days()}


def _set_paused(api, home, paused):
    with _home_scope(api, home):
        api.set_paused(bool(paused))
    return {"paused": bool(paused)}


def _graph(api, home, include_memory):
    with _home_scope(api, home):
        g = api.build_learning_graph() or {}
    nodes = g.get("nodes") or []
    edges = g.get("edges") or []
    if not include_memory:
        memory_ids = {n["id"] for n in nodes if n.get("kind") == "memory"}
        nodes = [n for n in nodes if n.get("kind") != "memory"]
        edges = [e for e in edges if e.get("source") not in memory_ids and e.get("target") not in memory_ids]
        # Hermes stats·clusters 에는 메모리 조각 수가 섞여 있어 싣지 않는다.
        return {"nodes": nodes, "edges": edges, "stats": {"skill_nodes": len(nodes)}}
    return {"nodes": nodes, "edges": edges, "memory": g.get("memory") or [], "stats": g.get("stats") or {}}


def _node_id(raw) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 256:
        raise RequestError(400, "invalid_node_id", "id")
    return raw


def _is_memory(api, node_id: str) -> bool:
    return api.parse_node_kind(node_id) == "memory"


def _skill_ref(api, node_id):
    ref = locate(api, node_id)
    if ref is None or not (ref.path / "SKILL.md").is_file():
        raise RequestError(404, "node_not_found", node_id)
    return ref


def _current(api, node_id) -> tuple[str, str, object]:
    """(kind, 현재 내용, 스킬이면 SkillRef). 없으면 404."""
    if _is_memory(api, node_id):
        detail = api.node_detail(node_id)
        if not detail.get("ok"):
            raise RequestError(404, "node_not_found", str(detail.get("message") or "")[:200])
        return "memory", detail["content"], None
    ref = _skill_ref(api, node_id)
    return "skill", (ref.path / "SKILL.md").read_text(encoding="utf-8"), ref


def _require_local(ref) -> None:
    if ref.source != "local":
        raise RequestError(400, "skill_not_local", ref.source)


def _node_get(api, home, node_id):
    with _home_scope(api, home):
        kind, content, _ref = _current(api, node_id)
        return {"id": node_id, "kind": kind, "content": content, "hash": sha256_text(content)}


def _node_put(api, home, node_id, content, base_hash):
    with _home_scope(api, home):
        kind, current, ref = _current(api, node_id)
        if sha256_text(current) != base_hash:
            raise RequestError(409, "node_changed", node_id)
        if kind == "skill":
            _require_local(ref)
            with user_write(api):
                result = api._edit_skill(ref.path.name, content)
            if not result.get("success"):
                raise RequestError(400, "skill_write_rejected", str(result.get("error"))[:500])
        else:
            result = api.edit_node(node_id, content)
            if not result.get("ok"):
                raise RequestError(400, "node_write_rejected", str(result.get("message"))[:500])
        return {"id": node_id, "hash": sha256_text(_current(api, node_id)[1])}


def _append_memory_log(home, row: dict) -> None:
    path = Path(home).joinpath(*MEMORY_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 처음부터 0600 으로 만든다 — 지운 메모리 원문이 담긴다.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with _LOG_LOCK, os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _node_delete(api, home, node_id, base_hash, actor):
    with _home_scope(api, home):
        kind, current, ref = _current(api, node_id)
        if sha256_text(current) != base_hash:
            raise RequestError(409, "node_changed", node_id)
        if kind == "skill":
            _require_local(ref)
            require_unpinned(api, node_id)
            with user_write(api):
                ok, message = api.archive_skill(node_id)
            if not ok:
                raise RequestError(400, "lifecycle_rejected", str(message)[:500])
            return {"id": node_id, "kind": "skill", "result": "archived"}
        source = node_id.split(":", 2)[1] if node_id.count(":") >= 2 else ""
        _append_memory_log(home, {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "nodeId": node_id,
                                  "sourceFile": _MEMORY_FILES.get(source, ""), "content": current,
                                  "deskrpgUserId": actor})
        result = api.delete_node(node_id)
        if not result.get("ok"):
            raise RequestError(400, "node_write_rejected", str(result.get("message"))[:500])
        return {"id": node_id, "kind": "memory", "result": "deleted"}


def curator_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(_curator, api, home))
    return handler


def curator_paused_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        paused = require_bool(await read_json_object(request), "paused")
        return web.json_response(await run_blocking(_set_paused, api, home, paused))
    return handler


def curator_run_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        resolve_profile_home(api, profile)
        job = skill_jobs.TABLE.start(api, api.normalize_profile_name(profile), "curator_run", ["curator", "run"])
        return web.json_response({"jobId": job}, status=202)
    return handler


def curator_job_handler(api):
    @guarded
    async def handler(request):
        profile = request.match_info["profile"]
        resolve_profile_home(api, profile)
        return web.json_response(skill_jobs.TABLE.get(api.normalize_profile_name(profile), request.match_info["job_id"]))
    return handler


def graph_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        include = request.query.get("includeMemory") == "1"
        return web.json_response(await run_blocking(_graph, api, home, include))
    return handler


def node_get_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        return web.json_response(await run_blocking(_node_get, api, home, _node_id(request.query.get("id"))))
    return handler


def node_put_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        node_id = _node_id(body.get("id"))
        content = require_str(body, "content")
        base_hash = require_str(body, "baseHash")
        return web.json_response(await run_blocking(_node_put, api, home, node_id, content, base_hash))
    return handler


def node_delete_handler(api):
    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        body = await read_json_object(request)
        node_id = _node_id(body.get("id"))
        base_hash = require_str(body, "baseHash")
        return web.json_response(
            await run_blocking(_node_delete, api, home, node_id, base_hash, actor_of(request)))
    return handler
