"""실제 Hermes 로 실증한다 — 프로필 홈으로 뜬 프로세스(칸반 워커·크론 실행과 같은 조건)가 이 플러그인을
로드하는가. 루트에만 설치하면 안 뜨고(결함 재현), `worker_plugin.ensure` 뒤에는 뜨며 훅과 도구가 등록된다.

별도 프로세스로 돌린다: Hermes 는 플러그인 매니저를 홈별로 캐시하고 모듈을 `sys.modules` 에 남긴다 — 같은
프로세스에서 홈만 바꾸면 앞의 로드 결과가 섞인다. 워커도 실제로 별도 프로세스(`hermes -p <프로필>`)다.
"""
import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("hermes_cli")

from deskrpg_plugin import worker_plugin  # noqa: E402

_PROBE = """
import json
from hermes_cli.plugins import PluginManager
m = PluginManager()
m.discover_and_load()
print(json.dumps({p["key"]: p for p in m.list_plugins()}))
"""


def _load_in(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HERMES_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _install_at_root(home):
    """게이트웨이 설치를 흉내 낸다 — 루트 홈의 plugins/ 에 두고 루트 config 에서 켠다."""
    (home / "plugins").mkdir(exist_ok=True)
    os.symlink(worker_plugin.plugin_root(), home / "plugins" / worker_plugin.PLUGIN_KEY, target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - deskrpg\n", encoding="utf-8")


def test_루트에만_설치하면_프로필_홈으로_뜬_프로세스에는_플러그인이_없다(hermes_env, api):
    _install_at_root(hermes_env["home"])
    api.create_profile("sophie", no_alias=True)

    # 대조군: 루트 홈에서는 뜬다.
    root = _load_in(hermes_env["home"])
    assert root[worker_plugin.PLUGIN_KEY]["enabled"] is True, root.get(worker_plugin.PLUGIN_KEY)

    loaded = _load_in(api.get_profile_dir("sophie"))
    entry = loaded.get(worker_plugin.PLUGIN_KEY)
    assert entry is None or entry["enabled"] is False, entry


def test_ensure_뒤에는_프로필_홈에서도_뜨고_훅과_도구가_등록된다(hermes_env, api):
    _install_at_root(hermes_env["home"])
    api.create_profile("sophie", no_alias=True)

    assert worker_plugin.ensure(api, "sophie") == {"profile": "sophie", "link": "created", "enabled": "added"}

    loaded = _load_in(api.get_profile_dir("sophie"))
    entry = loaded[worker_plugin.PLUGIN_KEY]
    assert entry["enabled"] is True and entry["error"] is None, entry
    # post_tool_call·post_llm_call 두 훅, artifact_save 와 카드 제안 두 도구.
    assert entry["hooks"] >= 2 and entry["tools"] >= 2, entry
