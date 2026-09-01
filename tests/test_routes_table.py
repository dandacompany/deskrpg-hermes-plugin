

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
