"""프로필 설정 읽기·쓰기.

쓸 수 있는 키를 허용목록으로 제한한다. 임의 YAML 을 쓰지 않는다 — 잘못된 키
하나가 프로필을 못 뜨게 만들 수 있다.
"""

import logging
import time

import yaml
from aiohttp import web

from . import contract_fields, envfile
from .catalog import REASONING_EFFORTS
from .common import run_blocking

logger = logging.getLogger(__name__)

ALLOWED_KEYS = frozenset(
    {
        "model",
        "provider",
        "toolsets",
        "reasoning_effort",
        "enabledToolsets",
        "disabledSkills",
        "clearBaseUrl",
    }
)

# `reasoning_effort` 는 **config 최상위 키**다(`model` 블록 안이 아니다) —
# `agent/auxiliary_client.py:5555` 가 `config.get("reasoning_effort")` 로 읽는다.
# 허용값은 catalog.REASONING_EFFORTS 와 같은 출처를 쓴다.
#
# `enabledToolsets`/`disabledSkills` 는 피커 화면(0.9.0)이 쓰는 파생 키다 —
# 실제로는 `platform_toolsets`·`skills.disabled` 를 쓴다(아래 참조).

# 대화(api_server)·크론(cron)·칸반 워커(cli)가 각자 다른 목록을 읽는다
# (api_server.py:2159 · cron/scheduler.py:405 · kanban_db_dispatch.py:2355). 직원은 어디서 일하든
# 같은 도구를 써야 하므로 셋에 같은 목록을 쓴다(2026-09-18 결정).
TOOLSET_PLATFORMS = ("api_server", "cron", "cli")

CONFIG_FILENAME = "config.yaml"


class ConfigUnreadable(Exception):
    """config.yaml 이 존재하지만 읽거나 해석할 수 없다.

    파일이 아예 없는 것과는 다르다 — 없으면 "아직 설정 안 함"이지만, 이건
    "설정이 있는데 망가졌다"이다. GET/PUT 이 이 둘을 구분해서 다룬다.
    """


def _yaml_fault_location(exc) -> str:
    """YAML 오류의 **위치만** 문자열로 — 줄·열, 없으면 빈 문자열.

    `str(yaml.YAMLError)` 는 문제가 난 줄을 그대로 인용한다. config.yaml 에는
    인라인 API 키가 들어갈 수 있으므로(`providers.*.api_key`, `custom_providers`)
    그 문장을 응답이나 로그에 실으면 키가 새어 나간다. 사용자가 파일을 고치려면
    위치는 알아야 하니, 숫자만 꺼내 쓴다 — `problem_mark.get_snippet()` 은
    원문을 담으므로 **절대 쓰지 않는다**.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" ({mark.line + 1}번째 줄 {mark.column + 1}번째 칸)"


def _value_error(key, value):
    """키가 허용목록에 있어도 값의 타입이 틀리면 거절 사유를 돌려준다.

    허용목록은 "잘못된 키 하나가 프로필을 못 뜨게 만든다"를 막으려는
    것인데, 키만 막고 값을 열어 두면 `model.default` 에 dict/list 가 그대로
    들어가 같은 문제가 재발한다 — 값도 목적에 맞는 타입인지 봐야 한다.

    model/provider 는 빈 문자열·공백만 있는 문자열도 거절한다. 둘 다 Hermes
    가 그대로 프로필 기동에 쓰는 식별자라, "일단 저장은 되지만 의미 없는
    값"을 허용해 봐야 다음 GET 이나 기동 시점에 더 알기 어려운 형태로
    터진다 — 여기서 바로 걸러 이유를 말해 주는 편이 낫다.
    """
    if key in ("model", "provider"):
        if not isinstance(value, str):
            return f"{key} must be a string"
        if not value.strip():
            return f"{key} must not be empty"
        return None
    if key == "clearBaseUrl":
        # 값이 있는 키가 아니라 "지워 달라" 는 신호다 — true 만 받는다.
        if value is not True:
            return "clearBaseUrl must be true"
        return None
    if key == "reasoning_effort":
        # 빈 문자열은 "지정 안 함" 이라는 뜻으로 Hermes 가 다루므로 허용한다
        # (model/provider 와 다르다 — 그 둘은 빈 값이 곧 고장이다).
        if not isinstance(value, str):
            return "reasoning_effort must be a string"
        if value and value not in REASONING_EFFORTS:
            return f"reasoning_effort must be one of: {', '.join(REASONING_EFFORTS)}"
        return None
    if key == "toolsets":
        if not isinstance(value, list):
            return "toolsets must be a list of strings"
        if not all(isinstance(v, str) for v in value):
            return "toolsets must be a list of strings"
        return None
    if key in ("enabledToolsets", "disabledSkills"):
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            return f"{key} must be a list of non-empty strings"
        return None
    return None


def _resolve(request, api):
    name = request.match_info["profile"]
    try:
        api.validate_profile_name(name)
    except Exception as exc:
        raise web.HTTPBadRequest(reason=f"invalid profile name: {exc}") from exc
    if not api.profile_exists(name):
        raise web.HTTPNotFound(reason=f"no such profile: {name}")
    return api.get_profile_dir(name) / CONFIG_FILENAME


def _load(path):
    """설정을 읽는다.

    파일이 없으면 빈 dict (= 아직 설정 없음). 있는데 읽거나 파싱할 수 없으면
    ConfigUnreadable 을 던진다 — is_file() 통과가 read_text() 성공을 보장하지
    않고(권한/인코딩), read_text() 성공이 yaml.safe_load() 성공을 보장하지
    않으며(문법 오류), safe_load() 성공이 dict 를 돌려준다는 보장도 없다
    (맨 문자열·리스트·None 도 유효한 YAML 이다).
    """
    if not path.is_file():
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # 사유에 예외 타입 이름만 싣는다 — OSError 의 메시지는 경로를,
        # UnicodeDecodeError 의 메시지는 깨진 바이트열을 담는다. `from None` 으로
        # 체인을 끊어 상위 트레이스백에도 원문이 실리지 않게 한다.
        raise ConfigUnreadable(
            f"config.yaml 을 읽을 수 없다 ({type(exc).__name__})"
        ) from None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigUnreadable(
            f"config.yaml 의 YAML 문법이 올바르지 않다{_yaml_fault_location(exc)}"
        ) from None

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigUnreadable("config.yaml 최상위 구조가 매핑(dict)이 아니다")
    return data


def _save(path, data: dict) -> None:
    """백업을 남기고 원자적으로 저장한다. `put_handler` 와 `write_skills_disabled` 가 같이 쓴다."""
    if path.is_file():
        # 같은 초에 두 번 써도 백업이 서로 덮어쓰지 않도록 마이크로초까지
        # 찍는다(identity.py 와 동일한 방식) — 백업의 존재 이유가 옛 내용
        # 보존인데 충돌로 지워지면 목적이 무색해진다.
        backup = path.with_name(
            f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            f"-{time.time_ns() % 1_000_000:06d}"
        )
        # 원자적으로, 0600 으로 쓴다 — 백업은 원본의 **완전한 사본**이라
        # config.yaml 에 인라인 키가 있으면 그 키가 그대로 들어간다.
        # `write_text` 로 쓰면 umask(보통 022)를 타 0644 가 되고, 원본이
        # 0600 이어도 사본만 세상에 열린다. 백업은 저장할 때마다 쌓인다.
        envfile.write_text_atomic(backup, path.read_text(encoding="utf-8"))
    # `write_text` 는 자리에서 잘라 쓴다 — 쓰는 도중 끊기면(디스크 가득·강제
    # 종료) 반쯤 쓴 YAML 이 남아 그 프로필이 뜨지 않는다. 임시 파일에 다 쓰고
    # `os.replace` 로 갈아 끼우면 실패해도 원본이 손대지 않은 채 남는다.
    envfile.write_text_atomic(
        path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    )


def write_skills_disabled(home, names: list[str]) -> None:
    """`skills.disabled` 만 바꿔 저장한다. 다른 키는 그대로. 읽기 실패는 `ConfigUnreadable` 로 올린다."""
    path = home / CONFIG_FILENAME
    data = _load(path)
    block = dict(data.get("skills") or {})
    block["disabled"] = sorted(set(names))
    data["skills"] = block
    _save(path, data)


def _apply_enabled_toolsets(api, data: dict, enabled: set) -> None:
    """`platform_toolsets.{api_server,cron,cli}` 에 켠 툴셋을 쓴다 — Hermes 의 `hermes tools` 저장과 같게.

    Hermes `hermes_cli/tools_config.py:_save_platform_tools`(0.21.3, 680-716)를 플랫폼마다 따라 한다.
    그 함수를 직접 부르지 않는 이유: 안에서 `save_config` 를 불러 파일 전체를 Hermes 식으로 정규화해
    다시 쓰는데(기본값 제거·전역 락·모듈 캐시), 이 PUT 은 같은 쓰기에 model·skills 등을 함께 싣고
    자기 백업·거절 규칙을 가진다 — 한 요청이 파일을 두 번, 서로 다른 방식으로 쓰게 된다.

    1. 켤 이름은 그 플랫폼에서 허용되는 것만(`_toolset_allowed_for_platform`, 원본 685행).
    2. 기존 항목 중 설정 가능한 키·플러그인 키·플랫폼 기본 합성명·`no_mcp` 가 **아닌 것**(MCP 서버 이름 등)은
       남긴다(원본 687-695행). 지우면 그 플랫폼의 MCP 허용목록이 사라져 "전부 허용" 으로 넓어진다.
    3. `known_plugin_toolsets`·`known_builtin_toolsets` 를 기록한다(원본 696-702행). 없으면 끈 툴셋과
       "저장 뒤 새로 나온 툴셋" 을 구분하지 못해 `_enable_recently_shipped_toolsets` 가 다시 켠다.
    4. 방금 켠 이름을 `agent.disabled_toolsets` 에서 뺀다(원본 703-715행) — 그 목록은 마지막에 덮어쓰는
       억제 목록이라, 빼지 않으면 체크한 툴셋이 켜지지 않는다.
    """
    block = data.get("platform_toolsets")
    block = dict(block) if isinstance(block, dict) else {}
    configurable = set(api._configurable_keys())
    plugin_keys = set(api._get_plugin_toolset_keys())
    drop = configurable | plugin_keys | set(api._platform_default_keys()) | {"no_mcp"}
    for platform in TOOLSET_PLATFORMS:
        allowed = {ts for ts in enabled if api._toolset_allowed_for_platform(ts, platform)}
        existing = block.get(platform)
        preserved = {str(e) for e in (existing if isinstance(existing, list) else []) if str(e) not in drop}
        block[platform] = sorted(allowed | preserved)
        if plugin_keys:
            _section(data, "known_plugin_toolsets")[platform] = sorted(plugin_keys)
        _section(data, "known_builtin_toolsets")[platform] = sorted(configurable)
        agent_cfg = data.get("agent")
        newly_enabled = allowed - preserved
        if isinstance(agent_cfg, dict) and agent_cfg.get("disabled_toolsets") and newly_enabled:
            parsed = api.parse_config_string_list(agent_cfg["disabled_toolsets"])
            remaining = [ts for ts in parsed if ts not in newly_enabled]
            if remaining != parsed:
                agent_cfg["disabled_toolsets"] = remaining
    data["platform_toolsets"] = block


def _section(data: dict, key: str) -> dict:
    """`tools_config._cfg_section` 과 같다 — 없거나 매핑이 아니면 `{}` 로 갈아 끼운다."""
    section = data.get(key)
    if not isinstance(section, dict):
        section = {}
        data[key] = section
    return section


def get_handler(api):
    async def handler(request):
        path = _resolve(request, api)

        try:
            data = _load(path)
        except ConfigUnreadable as exc:
            # 500 으로 새지 않는다 — 이 라우트는 설정 화면이 여는 첫 요청이라,
            # 파일이 망가졌다고 화면 자체가 못 뜨면 고칠 방법도 없어진다.
            # 값은 전부 null 로 두고 unreadable 플래그로 "비어있음"과 구분한다.
            logger.warning("[deskrpg] config 읽기 실패: %s", type(exc).__name__)
            return web.json_response(
                {
                    "model": None,
                    "provider": None,
                    "toolsets": None,
                    "reasoning_effort": None,
                    "enabledToolsets": None,
                    "disabledSkills": None,
                    "unreadable": True,
                }
            )

        model_block = data.get("model") or {}
        if not isinstance(model_block, dict):
            model_block = {}
        pt = data.get("platform_toolsets")
        api_server = pt.get("api_server") if isinstance(pt, dict) else None
        skills = data.get("skills")
        disabled = skills.get("disabled") if isinstance(skills, dict) else None
        return web.json_response(
            {
                "model": model_block.get("default"),
                "provider": model_block.get("provider"),
                # 런타임 리졸버가 실제로 읽는 요청 주소(hermes_cli/config.py 의 model.base_url).
                # 제공자를 바꿔도 이 값이 남아 있으면 요청은 옛 엔드포인트로 간다 — 화면이
                # 그 사실을 보여 주려면 값을 알아야 한다. 비밀이 아니라 주소다.
                "baseUrl": model_block.get("base_url"),
                "toolsets": data.get("toolsets"),
                # 최상위 키다 — model 블록 안에서 찾지 않는다.
                "reasoning_effort": data.get("reasoning_effort"),
                "enabledToolsets": [str(x) for x in api_server] if isinstance(api_server, list) else None,
                "disabledSkills": [str(x) for x in disabled] if isinstance(disabled, list) else [],
            }
        )

    return handler


def put_handler(api):
    async def handler(request):
        path = _resolve(request, api)

        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="body must be JSON")

        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(reason="body must be a JSON object")

        unknown = set(payload) - ALLOWED_KEYS
        if unknown:
            raise web.HTTPBadRequest(
                reason=f"keys not allowed: {', '.join(sorted(unknown))}"
            )
        if not payload:
            raise web.HTTPBadRequest(reason="nothing to apply")

        for key, value in payload.items():
            error = _value_error(key, value)
            if error:
                raise web.HTTPBadRequest(reason=f"invalid value for {key}: {error}")

        try:
            data = _load(path)
        except ConfigUnreadable as exc:
            # 망가진 파일 위에 백업하고 새로 쓰면 "성공"을 보고하면서 원본을
            # 영영 잃는다 — 여기서 거절하는 편이 백업-후-덮어쓰기보다 안전하다.
            # 호출자가 먼저 config.yaml 을 직접 고치거나 지워야 한다.
            logger.warning(
                "[deskrpg] config 쓰기 거부 — 기존 파일을 해석할 수 없다: %s",
                type(exc).__name__,
            )
            # 사유는 이제 한 줄 고정 문장이다(원문을 싣지 않으므로) — 그래도
            # HTTP reason 이 아니라 본문에 담는다. reason 은 개행을 못 담고
            # (aiohttp 가 \r\n 을 거절), 화면은 `error` 코드로 분기한다.
            return web.json_response(
                {"error": "config_unreadable", "reason": str(exc)}, status=409
            )

        existing_model = data.get("model")
        if existing_model is not None and not isinstance(existing_model, dict):
            # 기존 model: 이 스칼라/리스트면 dict(existing_model) 이 ValueError 를
            # 던진다 — 그 전에 걸러야 한다. get_handler(:110-112) 는 이미 같은
            # 상황을 방어하는데 put 만 놓치고 있었다(I-1). 백업보다 반드시
            # 먼저 거절해야 "거절 전 백업 찌꺼기 0개" 가 이 경로에서도 지켜진다.
            # 값을 %r 로 찍지 않는다 — 사용자가 그 자리에 무엇을 적어 두었는지
            # 알 수 없고, 이 파일에는 인라인 키가 들어갈 수 있다.
            logger.warning(
                "[deskrpg] config 쓰기 거부 — 기존 model 키가 매핑이 아니다: %s",
                type(existing_model).__name__,
            )
            return web.json_response(
                {
                    "error": "config_unreadable",
                    "reason": "existing 'model' key is not a mapping",
                },
                status=409,
            )

        from . import picker  # 지역 import — picker 가 config 를 import 한다(순환 방지)

        home = path.parent
        if "enabledToolsets" in payload:
            if not contract_fields.has_toolset_symbols(api):
                raise web.HTTPBadRequest(reason="enabledToolsets is not supported by this Hermes build")
            if data.get("platform_toolsets") is not None and not isinstance(data["platform_toolsets"], dict):
                return web.json_response(
                    {"error": "config_unreadable", "reason": "existing 'platform_toolsets' key is not a mapping"},
                    status=409,
                )
            known = await run_blocking(picker.known_toolset_names, api, home)
            unknown_names = sorted(set(payload["enabledToolsets"]) - known)
            if unknown_names:
                raise web.HTTPBadRequest(reason=f"unknown toolsets: {', '.join(unknown_names)}")
        if "disabledSkills" in payload:
            if not contract_fields.has_skill_symbols(api):
                raise web.HTTPBadRequest(reason="disabledSkills is not supported by this Hermes build")
            if data.get("skills") is not None and not isinstance(data["skills"], dict):
                return web.json_response(
                    {"error": "config_unreadable", "reason": "existing 'skills' key is not a mapping"},
                    status=409,
                )
            requested = set(payload["disabledSkills"])
            essential = sorted(requested & set(getattr(api, "ESSENTIAL_SKILLS", None) or ()))
            if essential:
                raise web.HTTPBadRequest(reason=f"essential skills cannot be disabled: {', '.join(essential)}")
            known = await run_blocking(picker.known_skill_names, api, home)
            unknown_names = sorted(requested - known)
            if unknown_names:
                raise web.HTTPBadRequest(reason=f"unknown skills: {', '.join(unknown_names)}")

        model_block = dict(existing_model or {})
        if payload.get("clearBaseUrl"):
            # Hermes 의 clear_model_endpoint_credentials(clear_base_url=True) 와 같은 결과다.
            # 사용자가 확인한 경우에만 호출부가 이 신호를 보낸다 — 말없이 지우지 않는다.
            model_block.pop("base_url", None)
            model_block.pop("api_base", None)
        if "model" in payload:
            model_block["default"] = payload["model"]
        if "provider" in payload:
            model_block["provider"] = payload["provider"]
        if model_block:
            data["model"] = model_block
        if "toolsets" in payload:
            data["toolsets"] = payload["toolsets"]
        if "enabledToolsets" in payload:
            _apply_enabled_toolsets(api, data, set(payload["enabledToolsets"]))
        if "disabledSkills" in payload:
            block = dict(data.get("skills") or {})
            block["disabled"] = sorted(set(payload["disabledSkills"]))
            data["skills"] = block
        if "reasoning_effort" in payload:
            # 최상위 키다. 빈 문자열이면 키를 지운다 — 빈 값을 남기면 Hermes 가
            # 그것을 "지정됨" 으로 읽을지 "미지정" 으로 읽을지 확실하지 않다.
            if payload["reasoning_effort"]:
                data["reasoning_effort"] = payload["reasoning_effort"]
            else:
                data.pop("reasoning_effort", None)

        _save(path, data)

        return web.json_response(
            {
                "applied": sorted(payload),
                # Hermes 는 설정 변경 시 실행 중 에이전트를 재시작할 수 있다.
                # 조용히 바뀌지 않도록 호출자에게 알린다.
                "restartMayBeRequired": True,
            }
        )

    return handler
