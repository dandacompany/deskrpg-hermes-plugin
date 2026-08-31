"""Hermes 내부 API 를 부르는 **유일한** 지점.

여기 말고 어디서도 `hermes_cli.*` 를 임포트하지 않는다. Hermes 가 함수를 옮기거나
이름을 바꾸면 이 파일 하나만 고치면 되고, 무엇이 없어졌는지도 한눈에 보인다.

전부 아니면 전무다. 심볼이 하나라도 없으면 로드를 포기한다 — 목록은 되는데
삭제만 조용히 실패하는 반쯤 동작하는 상태가 최악이다.
"""

import types

REQUIRED = (
    "get_profile_dir",
    "profile_exists",
    "validate_profile_name",
    "list_profiles",
    "create_profile",
    "delete_profile",
    "remove_wrapper_script",
    "read_profile_meta",
)

SOUL_REQUIRED = ("DEFAULT_SOUL_MD", "is_legacy_template_soul")


class MissingHermesApi(RuntimeError):
    """이 Hermes 빌드에 필요한 내부 API 가 없다."""


def load() -> types.SimpleNamespace:
    try:
        from hermes_cli import profiles as _profiles
        from hermes_cli import default_soul as _soul
    except Exception as exc:  # ImportError 뿐 아니라 초기화 실패도 잡는다
        raise MissingHermesApi(f"hermes_cli 를 임포트할 수 없다: {exc!r}") from exc

    resolved = {}
    missing = []
    for name in REQUIRED:
        value = getattr(_profiles, name, None)
        if value is None:
            missing.append(f"hermes_cli.profiles.{name}")
        resolved[name] = value
    for name in SOUL_REQUIRED:
        value = getattr(_soul, name, None)
        if value is None:
            missing.append(f"hermes_cli.default_soul.{name}")
        resolved[name] = value

    if missing:
        raise MissingHermesApi("없는 심볼: " + ", ".join(missing))

    return types.SimpleNamespace(**resolved)
