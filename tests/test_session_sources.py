"""What a session read — `GET /p/{profile}/deskrpg/sessions/{id}/sources`."""

import json

import pytest
from aiohttp import web

from deskrpg_plugin import contract_fields, routes, session_sources
from tests.conftest import FakeAdapter

WORKDIR = "/home/u/.hermes/kanban/boards/b/workspaces/t_1"
BASE = "/p/sophie/deskrpg/sessions"


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _assistant(ts, *calls):
    return {"role": "assistant", "content": "", "timestamp": ts, "tool_calls": list(calls)}


def _tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content if isinstance(content, str) else json.dumps(content)}


class _Store:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.closed = 0


def _session_db(store, *, compacted_kwarg=True):
    class _SessionDB:
        def __init__(self, path, read_only=False):
            assert read_only is True
            self.path = path

        def get_session(self, sid):
            return store.sessions.get(sid)

        if compacted_kwarg:
            def get_messages(self, sid, include_compacted=False):
                assert include_compacted is True
                return list(store.messages.get(sid, []))
        else:
            def get_messages(self, sid):
                return list(store.messages.get(sid, []))

        def close(self):
            store.closed += 1

    return _SessionDB


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
async def client(aiohttp_client, fake_api, store):
    fake_api.create_profile("sophie")
    (fake_api.get_profile_dir("sophie") / "state.db").write_bytes(b"")
    fake_api.SessionDB = _session_db(store)
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    return await aiohttp_client(app)


def test_capability_is_advertised(fake_api):
    assert "session_sources" in contract_fields.capabilities(fake_api)


async def test_extracts_pages_and_workdir_files(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1_790_000_000, _call("c1", "web_extract", {"urls": ["https://a.example/post", "https://b.example/x"]})),
        _tool("c1", "[untrusted]\n" + json.dumps({"results": [
            {"url": "https://a.example/post", "title": "A post", "content": "BODY-A"},
            {"url": "https://b.example/x", "title": "", "content": "", "error": "blocked"},
        ]})),
        _assistant(1_790_000_060, _call("c2", "browser_navigate", {"url": "https://c.example"}),
                   _call("c3", "read_file", {"path": f"{WORKDIR}/notes/plan.md"}),
                   _call("c4", "read_file", {"path": "draft.md"}),
                   _call("c5", "web_search", {"query": "q"})),
        _tool("c2", {"success": True, "url": "https://c.example/final", "title": "C final", "snapshot": "BODY-C"}),
        _tool("c3", {"content": "BODY-FILE", "total_lines": 3}),
        _tool("c4", {"content": "x"}),
        _tool("c5", {"success": True, "data": {"web": [{"url": "https://candidate.example", "title": "cand"}]}}),
    ]
    res = await client.get(f"{BASE}/s1/sources")
    assert res.status == 200
    body = await res.json()
    assert set(body) == contract_fields.SESSION_SOURCES_KEYS
    assert all(set(s) == contract_fields.SESSION_SOURCE_KEYS for s in body["sources"])
    assert [(s["kind"], s["ref"], s["title"], s["via"]) for s in body["sources"]] == [
        ("web", "https://a.example/post", "A post", "web_extract"),
        ("web", "https://c.example/final", "C final", "browser_navigate"),
        ("file", "notes/plan.md", None, "read_file"),
        ("file", "draft.md", None, "read_file"),
    ]
    assert body["sources"][0]["at"] == "2026-09-21T14:13:20Z"
    assert body["outside_workdir_files"] == 0 and body["truncated"] is False
    assert "BODY" not in json.dumps(body) and "candidate" not in json.dumps(body)


async def test_files_outside_the_workdir_are_counted_not_named(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1, _call("a", "read_file", {"path": "/home/u/.hermes/.env"}),
                   _call("b", "read_file", {"path": "../t_2/other.md"}),
                   _call("c", "read_file", {"path": "~/secrets.txt"}),
                   _call("d", "read_file", {"path": f"{WORKDIR}-evil/x.md"})),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert body["sources"] == [] and body["outside_workdir_files"] == 4
    assert "/home" not in json.dumps(body) and ".env" not in json.dumps(body)


async def test_kanban_worker_files_are_named_by_card(client, store, fake_api):
    # Kanban worker sessions record no cwd; card workspaces live under the kanban home.
    kanban = "/srv/hermes/kanban"
    fake_api.kanban_home = lambda: kanban
    store.sessions["w"] = {"id": "w", "source": "kanban", "cwd": ""}
    store.messages["w"] = [
        _assistant(1, _call("a", "read_file", {"path": f"{kanban}/boards/b1/workspaces/t_aa/draft.md"}),
                   _call("b", "read_file", {"path": f"{kanban}/boards/b1/workspaces/t_parent/research/notes.md"}),
                   _call("c", "read_file", {"path": f"{kanban}/boards/b1/kanban.db"}),
                   _call("d", "read_file", {"path": f"{kanban}/boards/b1/workspaces/t_aa"}),
                   _call("e", "read_file", {"path": f"{kanban}/boards/b1/workspaces/../../../etc/passwd"}),
                   _call("f", "read_file", {"path": "relative.md"})),
    ]
    body = await (await client.get(f"{BASE}/w/sources")).json()
    assert [s["ref"] for s in body["sources"]] == ["t_aa/draft.md", "t_parent/research/notes.md"]
    assert body["outside_workdir_files"] == 4
    assert "/srv" not in json.dumps(body)


def test_relative_path_rules():
    rp = session_sources.relative_path
    assert rp("a/b.md", WORKDIR) == "a/b.md"
    assert rp(f"{WORKDIR}/../t_2/x.md", WORKDIR, "/home/u/.hermes/kanban") == "t_2/x.md"
    assert rp("/etc/hosts", WORKDIR, "/home/u/.hermes/kanban") is None
    assert rp("/x/boards/b/workspaces/t_1/f.md", None, None) is None
    assert rp("", WORKDIR) is None


async def test_without_a_workdir_no_file_is_named(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": None}
    store.messages["s1"] = [_assistant(1, _call("a", "read_file", {"path": "/tmp/x.md"}))]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert body["sources"] == [] and body["outside_workdir_files"] == 1


async def test_failed_reads_are_left_out(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1, _call("a", "read_file", {"path": "missing.md"}),
                   _call("b", "browser_navigate", {"url": "https://down.example"}),
                   _call("c", "web_extract", {"urls": ["https://refused.example"]})),
        _tool("a", {"error": "File not found"}),
        _tool("b", {"success": False, "error": "timeout"}),
        _tool("c", {"success": False, "error": "blocked"}),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert body["sources"] == [] and body["outside_workdir_files"] == 0


async def test_unreadable_extract_result_falls_back_to_argument_urls(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1, _call("a", "web_extract", {"urls": ["https://big.example/page", "ftp://x.example/f"]})),
        _tool("a", "Result too large; preview only: ..."),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert [(s["ref"], s["title"]) for s in body["sources"]] == [("https://big.example/page", None)]


async def test_urls_lose_credentials_fragments_and_token_params(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    opaque = "A" * 30
    store.messages["s1"] = [
        _assistant(1, _call("a", "web_extract", {"urls": [
            f"https://user:pw@docs.example:8443/p?id=7&access_token=abc&x={opaque}&lang=ko#frag",
        ]})),
        _tool("a", {"results": [{"url": f"https://user:pw@docs.example:8443/p?id=7&access_token=abc&x={opaque}&lang=ko#frag",
                                 "title": "Doc sk-live-abcdefghijklmnop"}]}),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    (src,) = body["sources"]
    assert src["ref"] == "https://docs.example:8443/p?id=7&lang=ko"
    assert src["title"] == "Doc …"


async def test_duplicates_merge_and_keep_a_title(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1, _call("a", "web_extract", {"urls": ["https://a.example"]}),
                   _call("b", "browser_navigate", {"url": "https://a.example"}),
                   _call("c", "read_file", {"path": "x.md"}), _call("d", "read_file", {"path": f"{WORKDIR}/x.md"})),
        _tool("a", "not json"),
        _tool("b", {"success": True, "url": "https://a.example", "title": "A"}),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert [(s["ref"], s["title"], s["via"]) for s in body["sources"]] == [
        ("https://a.example", "A", "web_extract"), ("x.md", None, "read_file"),
    ]


async def test_list_is_capped(client, store, monkeypatch):
    monkeypatch.setattr(session_sources, "MAX_SOURCES", 2)
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [_assistant(1, *[_call(f"c{i}", "read_file", {"path": f"f{i}.md"}) for i in range(3)])]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert [s["ref"] for s in body["sources"]] == ["f0.md", "f1.md"] and body["truncated"] is True


async def test_delegate_children_add_their_reads(client, store):
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.sessions["child"] = {"id": "child", "cwd": WORKDIR}
    store.messages["s1"] = [
        _assistant(1, _call("a", "delegate_task", {"goal": "g"})),
        _tool("a", {"results": [{"status": "completed", "files_read": [f"{WORKDIR}/brief.md", "/etc/hosts"],
                                 "session_id": "child"},
                                {"status": "failed", "child_session_id": "gone"}]}),
    ]
    store.messages["child"] = [
        _assistant(2, _call("x", "web_extract", {"urls": ["https://child.example"]})),
        _tool("x", {"results": [{"url": "https://child.example", "title": "From child"}]}),
    ]
    body = await (await client.get(f"{BASE}/s1/sources")).json()
    assert [(s["ref"], s["via"]) for s in body["sources"]] == [
        ("brief.md", "delegate_task"), ("https://child.example", "delegate_task"),
    ]
    assert body["outside_workdir_files"] == 1


async def test_missing_session_is_404(client, store):
    res = await client.get(f"{BASE}/nope/sources")
    assert res.status == 404 and (await res.json())["error"] == "session_not_found"
    assert store.closed == 1


async def test_profile_without_state_db_is_404(aiohttp_client, fake_api, store):
    fake_api.create_profile("sophie")
    fake_api.SessionDB = _session_db(store)
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=True), fake_api)
    res = await (await aiohttp_client(app)).get(f"{BASE}/s1/sources")
    assert res.status == 404 and store.closed == 0


async def test_bad_session_id_is_400(client):
    res = await client.get(f"{BASE}/..%2Fx/sources")
    assert res.status in (400, 404)
    res = await client.get(f"{BASE}/-bad/sources")
    assert res.status == 400 and (await res.json())["error"] == "invalid_session_id"


async def test_hermes_without_compaction_archives(client, store, fake_api):
    fake_api.SessionDB = _session_db(store, compacted_kwarg=False)
    store.sessions["s1"] = {"id": "s1", "cwd": WORKDIR}
    store.messages["s1"] = [_assistant(1, _call("a", "read_file", {"path": "x.md"}))]
    # The handler was built with the old class; rebuild through the module directly.
    sdb = fake_api.SessionDB("state.db", read_only=True)
    assert session_sources.read_sources(sdb, "s1")["sources"][0]["ref"] == "x.md"


async def test_unauthorized_is_rejected(aiohttp_client, fake_api):
    fake_api.create_profile("sophie")
    app = web.Application()
    routes.attach(app, FakeAdapter(authorized=False), fake_api)
    res = await (await aiohttp_client(app)).get(f"{BASE}/s1/sources")
    assert res.status == 401
