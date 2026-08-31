import types
import pytest
from aiohttp import web


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
        if not name or "/" in name or ".." in name or name.strip() != name:
            raise ValueError(f"invalid profile name: {name!r}")

    def list_profiles():
        root = tmp_path / "profiles"
        if not root.is_dir():
            return []
        return [types.SimpleNamespace(name=p.name) for p in sorted(root.iterdir())]

    def create_profile(name, **kwargs):
        d = get_profile_dir(name)
        d.mkdir(parents=True, exist_ok=False)
        (d / "SOUL.md").write_text("You are Hermes Agent", encoding="utf-8")
        return d

    def delete_profile(name, yes=False):
        import shutil

        d = get_profile_dir(name)
        shutil.rmtree(d)
        return d

    return types.SimpleNamespace(
        get_profile_dir=get_profile_dir,
        profile_exists=profile_exists,
        validate_profile_name=validate_profile_name,
        list_profiles=list_profiles,
        create_profile=create_profile,
        delete_profile=delete_profile,
        remove_wrapper_script=lambda name: True,
        read_profile_meta=lambda d: {"description": ""},
        DEFAULT_SOUL_MD="You are Hermes Agent",
        is_legacy_template_soul=lambda text: False,
    )
