"""Expand `/skill … instruction` into the message Hermes' TUI and messaging gateway send.

The API server does not expand skill slash commands, so DeskRPG asks the plugin to build the same
message with Hermes' public builders (`agent.skill_commands`), scoped to the requesting profile's home.
A failed expansion is an error, never a plain message: DeskRPG must not send the chat without the skill.
"""

from __future__ import annotations

import re

from aiohttp import web

from .common import RequestError, guarded, read_json_object, run_blocking
from .cron import resolve_profile_home

# Hermes stacks at most five leading `/skill` tokens (`agent.skill_commands`); DeskRPG uses the same cap.
MAX_SKILLS = 5
# DeskRPG chat runs on the API server, so that platform's `skills.platform_disabled` list applies.
CHAT_PLATFORM = "api_server"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _same(a: str, b: str) -> bool:
    # Hermes treats `_` and `-` alike in slash commands.
    return a.lower().replace("_", "-") == b.lower().replace("_", "-")


def parse(body: dict) -> tuple[list[str], str]:
    skills = body.get("skills")
    instruction = body.get("instruction", "")
    if not isinstance(skills, list) or not skills or not isinstance(instruction, str):
        raise RequestError(400, "invalid_skills", "skills must be a non-empty list and instruction a string")
    if len(skills) > MAX_SKILLS:
        raise RequestError(400, "too_many_skills", f"at most {MAX_SKILLS} skills")
    names = [s.strip().removeprefix("/") if isinstance(s, str) else "" for s in skills]
    # Names are only lookup keys into Hermes' slash-command map, never paths.
    if any(not _NAME.match(n) for n in names):
        raise RequestError(400, "invalid_skills", "invalid skill name")
    if len({n.lower().replace("_", "-") for n in names}) != len(names):
        raise RequestError(400, "invalid_skills", "duplicate skill name")
    return names, instruction.strip()


def expand(api, home, names: list[str], instruction: str) -> dict:
    """Build the message under the profile's home. Must run on one worker thread from start to end."""
    token = api.set_hermes_home_override(str(home))
    try:
        disabled_names = api.get_disabled_skill_names(platform=CHAT_PLATFORM) or set()
        commands = api.get_skill_commands() or {}
        keys, missing, disabled = [], [], []
        for name in names:
            key = api.resolve_skill_command_key(name)
            skill_name = (commands.get(key) or {}).get("name", name) if key else name
            if any(_same(skill_name, d) or _same(name, d) for d in disabled_names):
                disabled.append(name)
            elif key is None:
                missing.append(name)
            keys.append(key)
        if missing:
            raise RequestError(404, "skill_not_found", "unknown skill").with_extra(missing=missing)
        if disabled:
            raise RequestError(409, "skill_disabled", "skill is disabled").with_extra(disabled=disabled)

        if len(keys) == 1:
            message = api.build_skill_invocation_message(keys[0], instruction)
            lost = [] if message else list(names)
        else:
            result = api.build_stacked_skill_invocation_message(keys, instruction)
            message, _loaded, lost = result if result else (None, [], list(names))
        if not message or lost:
            raise RequestError(422, "skill_load_failed", "skill could not be loaded").with_extra(missing=list(lost))
        return {"message": message, "loaded": list(names), "missing": []}
    finally:
        api.reset_hermes_home_override(token)


def post_handler(api):
    """`POST /p/{profile}/deskrpg/skill-invocation`"""

    @guarded
    async def handler(request):
        home = resolve_profile_home(api, request.match_info["profile"])
        names, instruction = parse(await read_json_object(request))
        return web.json_response(await run_blocking(expand, api, home, names, instruction))

    return handler
