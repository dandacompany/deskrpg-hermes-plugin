import sys
import types
import pytest

from deskrpg_plugin import _hermes_api


def _fake_profiles_module(missing=()):
    """hermes_cli.profiles 를 흉내 낸다. missing 에 든 이름만 빠뜨린다."""
    mod = types.ModuleType("hermes_cli.profiles")
    for name in (
        "get_profile_dir", "profile_exists", "validate_profile_name",
        "list_profiles", "create_profile", "delete_profile",
        "remove_wrapper_script", "read_profile_meta",
    ):
        if name not in missing:
            setattr(mod, name, lambda *a, **k: None)
    return mod


def _fake_soul_module():
    mod = types.ModuleType("hermes_cli.default_soul")
    mod.DEFAULT_SOUL_MD = "You are Hermes Agent"
    mod.is_legacy_template_soul = lambda text: False
    return mod


def _install(monkeypatch, missing=()):
    pkg = types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", _fake_profiles_module(missing))
    monkeypatch.setitem(sys.modules, "hermes_cli.default_soul", _fake_soul_module())


def test_모든_심볼이_있으면_로드된다(monkeypatch):
    _install(monkeypatch)
    api = _hermes_api.load()
    for name in _hermes_api.REQUIRED:
        assert getattr(api, name) is not None


def test_심볼이_하나라도_없으면_던진다(monkeypatch):
    # 반쯤 동작하는 상태를 만들지 않는다 — 목록은 되는데 삭제만 조용히 실패하는 식.
    _install(monkeypatch, missing=("delete_profile",))
    with pytest.raises(_hermes_api.MissingHermesApi) as excinfo:
        _hermes_api.load()
    assert "delete_profile" in str(excinfo.value)


def test_hermes_가_아예_없으면_던진다(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    with pytest.raises(_hermes_api.MissingHermesApi):
        _hermes_api.load()
