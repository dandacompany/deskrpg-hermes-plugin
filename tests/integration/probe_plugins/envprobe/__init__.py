"""Test only: at load time inside a kanban worker, write what the process can see about its card."""

import json
import os


def register(ctx):
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    out = os.environ.get("ENVPROBE_OUT")
    if not task_id or not out:
        return
    from hermes_cli import kanban_db
    from hermes_cli.profiles import get_active_profile_name

    try:
        from hermes_cli.kanban_db_connect import connect_closing
    except ImportError:  # older builds
        connect_closing = kanban_db.connect_closing

    fields = {}
    try:
        with connect_closing(board=os.environ.get("HERMES_KANBAN_BOARD")) as conn:
            task = kanban_db.get_task(conn, task_id)
            fields = sorted(vars(task)) if task is not None and hasattr(task, "__dict__") else []
    except Exception as exc:  # noqa: BLE001
        fields = [f"error: {type(exc).__name__}"]
    record = {
        "task_id": task_id,
        "run_id": os.environ.get("HERMES_KANBAN_RUN_ID"),
        "board": os.environ.get("HERMES_KANBAN_BOARD"),
        "profile_env": os.environ.get("HERMES_PROFILE_NAME"),
        "profile_home": get_active_profile_name(),
        "kanban_home": str(kanban_db.kanban_home()),
        "task_fields": fields,
        "kanban_env": sorted(k for k in os.environ if k.startswith("HERMES_KANBAN_")),
    }
    with open(os.path.join(out, f"{os.getpid()}.json"), "w") as f:
        json.dump(record, f)
