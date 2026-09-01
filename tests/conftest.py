import re
import types
import pytest
from aiohttp import web

# 실제 Hermes 0.20.6 의 validate_profile_name 정규식(앵커 있음). fake 가 이보다
# 느슨하면 대문자·점·유니코드 이름이 테스트에서만 통과해 회귀를 못 잡는다(M-7).
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class FakeAdapter:
    """api_server 어댑터를 흉내 낸다. _check_auth 만 쓴다."""

    def __init__(self, *, authorized: bool):
        self.authorized = authorized
        self.checked = []

    def _check_auth(self, request):
        self.checked.append(request.path)
        if self.authorized:
            return None
        return web.json_response({"error": "unauthorized"}, status=401)


@pytest.fixture
def fake_api(tmp_path):
    """Hermes 내부 API 를 흉내 낸다. 프로필은 tmp_path 아래 디렉토리다."""

    def get_profile_dir(name):
        return tmp_path / "profiles" / name

    def profile_exists(name):
        return get_profile_dir(name).is_dir()

    def validate_profile_name(name):
        if not name or not _PROFILE_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid profile name: {name!r}")

    def list_profiles():
        root = tmp_path / "profiles"
        if not root.is_dir():
            return []
        return [types.SimpleNamespace(name=p.name) for p in sorted(root.iterdir())]

    def get_wrapper_path(name):
        return tmp_path / "wrappers" / name

    def create_profile(name, **kwargs):
        d = get_profile_dir(name)
        d.mkdir(parents=True, exist_ok=False)
        (d / "SOUL.md").write_text("You are Hermes Agent", encoding="utf-8")
        # 실제 CLI 는 프로필 생성과 함께 wrapper 스크립트도 만든다 — 여기서도
        # 만들어 둬야 delete_profile/remove_wrapper_script 의 순서 상호작용을
        # 진실되게 재현할 수 있다(I-3).
        wrapper = get_wrapper_path(name)
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        return d

    def remove_wrapper_script(name):
        wrapper = get_wrapper_path(name)
        if wrapper.is_file():
            wrapper.unlink()
            return True
        return False

    def delete_profile(name, yes=False):
        import shutil

        d = get_profile_dir(name)
        shutil.rmtree(d)
        # 0.20.6 의 delete_profile 은 wrapper 가 남아 있으면 스스로 지운다 —
        # 그 부수효과를 재현해야 "delete_profile 뒤에 부르면 항상 False" 라는
        # 실제 버그를 fake 위에서도 관찰할 수 있다.
        wrapper = get_wrapper_path(name)
        if wrapper.is_file():
            wrapper.unlink()
        return d

    return types.SimpleNamespace(
        get_profile_dir=get_profile_dir,
        get_wrapper_path=get_wrapper_path,
        profile_exists=profile_exists,
        validate_profile_name=validate_profile_name,
        list_profiles=list_profiles,
        create_profile=create_profile,
        delete_profile=delete_profile,
        remove_wrapper_script=remove_wrapper_script,
        read_profile_meta=lambda d: {"description": ""},
        DEFAULT_SOUL_MD="You are Hermes Agent",
        is_legacy_template_soul=lambda text: False,
    )

@pytest.fixture(autouse=True)
def _isolated_user_home(tmp_path, monkeypatch):
    """유닛 파일 탐색이 **실제 홈을 절대 보지 않게** 한다.

    없으면 테스트 결과가 실행하는 사람의 머신에 달린다 — 실제로
    `~/Library/LaunchAgents/ai.hermes.gateway-noah.plist` 가 있는 Mac 에서
    삭제 테스트가 409 로 떨어졌다. 가드가 제대로 문 것이지만, 테스트가
    환경에 좌우되면 회귀를 잡는 그물이 못 된다.
    """
    from deskrpg_plugin import safedelete

    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setattr(safedelete, "user_home", lambda: home)
    return home
