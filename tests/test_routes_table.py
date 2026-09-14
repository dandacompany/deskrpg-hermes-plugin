

def test_보고하는_버전이_plugin_yaml_과_일치한다():
    """같은 사실이 두 곳에 적히면 반드시 갈라진다.

    실제로 갈렸다: 버전을 0.2.0·0.3.0 으로 두 번 올리는 동안 `routes.py` 의 하드코딩된
    문자열을 두 번 다 놓쳐 `/deskrpg/info` 가 계속 `0.1.0` 을 보고했고, 스테이징에서
    DeskRPG 가 그 값을 캐시하는 것을 보고서야 드러났다.
    """
    import pathlib
    import re

    from deskrpg_plugin.routes import PLUGIN_VERSION

    raw = (pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    declared = re.search(r"^version:\s*(.+)$", raw, re.M).group(1).strip().strip("\"'")
    assert PLUGIN_VERSION == declared
    assert PLUGIN_VERSION != "unknown"


def test_plugin_yaml_이_requires_hermes_를_최상위에_선언하고_버전은_0_6_0_이다():
    """`requires: {hermes: …}` 처럼 중첩하면 Hermes 는 모르는 키로 무시한다 — 실제 필드는
    최상위 `requires_hermes` 다(hermes_cli/plugin_validate.py). 무시되면 0.20.x 에 설치돼도
    경고 없이 로드된 뒤 칸반 심볼이 없어 죽는다.
    """
    import pathlib

    import yaml

    raw = (pathlib.Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(raw)
    assert manifest["version"] == "0.6.0"
    assert manifest["requires_hermes"] == ">=0.21.1"
    assert "requires" not in manifest
