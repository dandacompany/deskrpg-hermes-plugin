"""No route coroutine calls a Hermes API that asks the gateway's own control pipe.

`list_profiles`, `delete_profile` and `rename_profile` check whether a profile's gateway runs through the
gateway's control socket/pipe; `_check_dispatcher_presence` does the same. The plugin's routes run on that same
gateway's event loop, which is the one that must answer — so called there, they stall the loop for the socket
timeout (POSIX) or freeze it for good (Windows, where the pipe read has no timeout) until the loop watchdog kills
the gateway. They may only run in a worker thread (`run_blocking`, `asyncio.to_thread`, a nested `def work()`).
"""

import ast
from pathlib import Path

SELF_QUERYING = {"list_profiles", "delete_profile", "rename_profile", "_check_dispatcher_presence"}
PLUGIN = Path(__file__).resolve().parent.parent / "deskrpg_plugin"


def _direct_calls(fn: ast.AsyncFunctionDef):
    """Calls made by the coroutine itself — nested functions and lambdas run elsewhere (threads) and are skipped."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in SELF_QUERYING:
            yield node.lineno, ast.unparse(node.func)
        stack.extend(ast.iter_child_nodes(node))


def test_no_route_coroutine_calls_a_self_querying_hermes_api_on_the_loop():
    found = []
    for path in sorted(PLUGIN.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef):
                found += [f"{path.name}:{line} {name}" for line, name in _direct_calls(node)]
    assert found == []
