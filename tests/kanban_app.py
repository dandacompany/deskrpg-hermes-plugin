"""칸반 핸들러 테스트용 작은 aiohttp 앱.

`routes.py` 는 다른 태스크가 배선한다. 여기서는 `routes.attach` 가 하는 것과 같은 방식
(`require_auth` 로 감싸 등록)으로 칸반 보드·카드 핸들러만 계약 경로에 붙인다. 경로 문자열은
부록 A 의 것과 같다 — 나중에 routes 테이블이 붙을 때 그대로 옮겨 적으면 된다.
"""

from aiohttp import web

from deskrpg_plugin import kanban_board as kb
from deskrpg_plugin import kanban_views as kv
from deskrpg_plugin.auth import Scope, require_auth
from tests.conftest import FakeAdapter


def make_app(api, *, authorized: bool = True) -> web.Application:
    adapter = FakeAdapter(authorized=authorized)
    app = web.Application()
    table = [
        ("GET", "/deskrpg/kanban/boards", kb.list_boards_handler(api)),
        ("POST", "/deskrpg/kanban/boards", kb.create_board_handler(api)),
        ("PATCH", "/deskrpg/kanban/boards/{slug}", kb.patch_board_handler(api)),
        ("GET", "/deskrpg/kanban/board", kb.get_board_handler(api)),
        ("GET", "/deskrpg/kanban/tasks/{task_id}", kb.get_task_handler(api)),
        ("POST", "/deskrpg/kanban/tasks", kb.create_task_handler(api)),
        ("PATCH", "/deskrpg/kanban/tasks/{task_id}", kb.patch_task_handler(api)),
        ("DELETE", "/deskrpg/kanban/tasks/{task_id}", kb.delete_task_handler(api)),
        ("POST", "/deskrpg/kanban/tasks/{task_id}/comments", kb.add_comment_handler(api)),
        ("GET", "/deskrpg/kanban/links", kv.links_handler(api)),
        ("GET", "/deskrpg/kanban/runs", kv.runs_handler(api)),
        ("GET", "/deskrpg/kanban/events", kv.task_events_handler(api)),
        ("POST", "/deskrpg/kanban/links", kb.link_handler(api, "add")),
        ("DELETE", "/deskrpg/kanban/links", kb.link_handler(api, "remove")),
    ]
    for method, path, handler in table:
        app.router.add_route(method, path, require_auth(adapter, Scope.DEFAULT, handler))
    return app
