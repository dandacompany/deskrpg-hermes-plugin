"""프로필 설정 읽기·쓰기.

쓸 수 있는 키를 허용목록으로 제한한다. 임의 YAML 을 쓰지 않는다 — 잘못된 키
하나가 프로필을 못 뜨게 만들 수 있다.
"""

import logging
import time

import yaml
from aiohttp import web

from .catalog import REASONING_EFFORTS

logger = logging.getLogger(__name__)

ALLOWED_KEYS = frozenset({"model", "provider", "toolsets", "reasoning_effort"})

# `reasoning_effort` 는 **config 최상위 키**다(`model` 블록 안이 아니다) —
# `agent/auxiliary_client.py:5555` 가 `config.get("reasoning_effort")` 로 읽는다.
# 허용값은 catalog.REASONING_EFFORTS 와 같은 출처를 쓴다.

CONFIG_FILENAME = "config.yaml"


class ConfigUnreadable(Exception):
    """config.yaml 이 존재하지만 읽거나 해석할 수 없다.

    파일이 아예 없는 것과는 다르다 — 없으면 "아직 설정 안 함"이지만, 이건
    "설정이 있는데 망가졌다"이다. GET/PUT 이 이 둘을 구분해서 다룬다.
    """


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
        raise ConfigUnreadable(f"config.yaml 읽기 실패: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigUnreadable(f"config.yaml 파싱 실패: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigUnreadable("config.yaml 최상위 구조가 매핑(dict)이 아니다")
    return data


def get_handler(api):
    async def handler(request):
        path = _resolve(request, api)

        try:
            data = _load(path)
        except ConfigUnreadable as exc:
            # 500 으로 새지 않는다 — 이 라우트는 설정 화면이 여는 첫 요청이라,
            # 파일이 망가졌다고 화면 자체가 못 뜨면 고칠 방법도 없어진다.
            # 값은 전부 null 로 두고 unreadable 플래그로 "비어있음"과 구분한다.
            logger.warning("[deskrpg] config 읽기 실패: %s", exc)
            return web.json_response(
                {
                    "model": None,
                    "provider": None,
                    "toolsets": None,
                    "reasoning_effort": None,
                    "unreadable": True,
                }
            )

        model_block = data.get("model") or {}
        if not isinstance(model_block, dict):
            model_block = {}
        return web.json_response(
            {
                "model": model_block.get("default"),
                "provider": model_block.get("provider"),
                "toolsets": data.get("toolsets"),
                # 최상위 키다 — model 블록 안에서 찾지 않는다.
                "reasoning_effort": data.get("reasoning_effort"),
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
            logger.warning("[deskrpg] config 쓰기 거부 — 기존 파일을 해석할 수 없다: %s", exc)
            # HTTP reason 은 개행을 못 담는다(aiohttp 가 \r\n 을 거절) — YAML
            # 파서 에러는 여러 줄이라 reason 에는 한 줄 요약만, 자세한 사유는
            # 응답 본문에 담는다.
            return web.json_response(
                {"error": "config_unreadable", "reason": str(exc)}, status=409
            )

        existing_model = data.get("model")
        if existing_model is not None and not isinstance(existing_model, dict):
            # 기존 model: 이 스칼라/리스트면 dict(existing_model) 이 ValueError 를
            # 던진다 — 그 전에 걸러야 한다. get_handler(:110-112) 는 이미 같은
            # 상황을 방어하는데 put 만 놓치고 있었다(I-1). 백업보다 반드시
            # 먼저 거절해야 "거절 전 백업 찌꺼기 0개" 가 이 경로에서도 지켜진다.
            logger.warning(
                "[deskrpg] config 쓰기 거부 — 기존 model 키가 매핑이 아니다: %r", existing_model
            )
            return web.json_response(
                {
                    "error": "config_unreadable",
                    "reason": "existing 'model' key is not a mapping",
                },
                status=409,
            )

        if path.is_file():
            # 같은 초에 두 번 써도 백업이 서로 덮어쓰지 않도록 마이크로초까지
            # 찍는다(identity.py 와 동일한 방식) — 백업의 존재 이유가 옛 내용
            # 보존인데 충돌로 지워지면 목적이 무색해진다.
            backup = path.with_name(
                f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
                f"-{time.time_ns() % 1_000_000:06d}"
            )
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        model_block = dict(existing_model or {})
        if "model" in payload:
            model_block["default"] = payload["model"]
        if "provider" in payload:
            model_block["provider"] = payload["provider"]
        if model_block:
            data["model"] = model_block
        if "toolsets" in payload:
            data["toolsets"] = payload["toolsets"]
        if "reasoning_effort" in payload:
            # 최상위 키다. 빈 문자열이면 키를 지운다 — 빈 값을 남기면 Hermes 가
            # 그것을 "지정됨" 으로 읽을지 "미지정" 으로 읽을지 확실하지 않다.
            if payload["reasoning_effort"]:
                data["reasoning_effort"] = payload["reasoning_effort"]
            else:
                data.pop("reasoning_effort", None)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        return web.json_response(
            {
                "applied": sorted(payload),
                # Hermes 는 설정 변경 시 실행 중 에이전트를 재시작할 수 있다.
                # 조용히 바뀌지 않도록 호출자에게 알린다.
                "restartMayBeRequired": True,
            }
        )

    return handler
