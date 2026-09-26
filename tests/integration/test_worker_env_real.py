"""What a real kanban worker process knows about its card — the input the approval hooks rely on.

A worker spawned by the real dispatcher loads a probe plugin that records its environment. The approval hooks
need the card, the run, the worker's profile and the card's board (to fall back to the board's default policy),
plus the same shared kanban home as the gateway (where the approval store lives).
"""

import json
import os
from pathlib import Path

import pytest

from tests.integration.test_kanban_real import BOARD, B, _board
from tests.integration.worker_harness import (
    ScriptedModel, configure_profile, install_plugin, tool_results, wait_until,
)

pytestmark = pytest.mark.integration

PROBE = Path(__file__).parent / "probe_plugins" / "envprobe"


def _finish_immediately(model, messages):
    # Complete the card on the first turn so the worker exits cleanly (no retry loop).
    if not tool_results(messages):
        return ("tool", "kanban_complete", {"summary": "probe done"})
    return ("text", "done")


@pytest.fixture
def model():
    server = ScriptedModel(_finish_immediately)
    yield server
    server.close()


@pytest.mark.parametrize("explicit_kanban_home", [True, False], ids=["kanban-home-env", "default-root"])
async def test_worker_sees_its_card_board_profile_and_the_shared_kanban_home(
    client, api, make_profile, model, tmp_path, monkeypatch, explicit_kanban_home
):
    if not explicit_kanban_home:
        # A plain install sets no HERMES_KANBAN_HOME: gateway and workers both derive the shared root.
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    home = make_profile("impl")
    configure_profile(home, model="worker", base_url=model.base_url, plugins=["envprobe"])
    install_plugin(home, PROBE)
    out = tmp_path / "probe"
    out.mkdir()
    monkeypatch.setenv("ENVPROBE_OUT", str(out))
    monkeypatch.delenv("HERMES_BIN", raising=False)

    await _board(client)
    resp = await client.post(f"/deskrpg/kanban/tasks{B}", json={"title": "probe", "assignee": "impl"})
    assert resp.status == 201, await resp.text()
    task_id = (await resp.json())["task"]["id"]
    resp = await client.post(f"/deskrpg/kanban/dispatch{B}")
    assert resp.status == 200, await resp.text()
    assert task_id in json.dumps(await resp.json())

    records = wait_until(lambda: [json.loads(p.read_text()) for p in out.glob("*.json")], timeout=120)
    assert records, "the worker never loaded the probe plugin"
    probe = records[0]
    assert probe["task_id"] == task_id
    assert probe["board"] == BOARD
    assert probe["run_id"] and probe["run_id"].isdigit()
    assert "impl" in (probe["profile_env"], probe["profile_home"])
    assert probe["kanban_home"] == str(api.kanban_home())

    def settled():
        with api.connect_closing(board=BOARD) as conn:
            status = api.get_task(conn, task_id).status
        return status if status != "running" else None

    assert wait_until(settled, timeout=60) == "done"
    # Kept in the failure message on purpose: what else the dispatcher hands a worker.
    print("probe:", {k: probe[k] for k in ("board", "profile_env", "profile_home", "kanban_home", "kanban_env")})
