"""스킬 관리 공통 부품(0.15.0)."""

import pytest

from deskrpg_plugin.common import RequestError
from deskrpg_plugin.picker import _home_scope
from deskrpg_plugin.skills_common import locate, resolve_editable_path, sha256_text


def _home(fake_api):
    fake_api.create_profile("sophie")
    return fake_api.get_profile_dir("sophie")


def test_원산지_분류와_폴더_이름이_다른_스킬(fake_api):
    home = _home(fake_api)
    fake_api.skills.seed("sophie", "weekly-report", folder="wr", category="reports")
    fake_api.skills.seed("sophie", "pdf-tools", source="hub")
    fake_api.skills.seed("sophie", "web", source="bundled")
    with _home_scope(fake_api, home):
        ref = locate(fake_api, "weekly-report")
        assert ref.source == "local" and ref.path.name == "wr"
        assert locate(fake_api, "pdf-tools").source == "hub"
        assert locate(fake_api, "web").source == "bundled"
        assert locate(fake_api, "ghost") is None


def test_경로_검사(fake_api, tmp_path):
    home = _home(fake_api)
    base = fake_api.skills.seed("sophie", "weekly", files={"references/a.md": "a", "scripts/x.py": "1"})
    (tmp_path / "secret").write_text("s", encoding="utf-8")
    (base / "references" / "link.md").symlink_to(tmp_path / "secret")
    with _home_scope(fake_api, home):
        ref = locate(fake_api, "weekly")
        assert resolve_editable_path(ref, "references/a.md", for_write=True) == base / "references" / "a.md"
        assert resolve_editable_path(ref, "scripts/x.py") == base / "scripts" / "x.py"
        for bad, write in (("scripts/x.py", True), ("../x", False), ("/etc/passwd", False),
                           ("references/link.md", False), ("a\\b", False)):
            with pytest.raises(RequestError) as exc:
                resolve_editable_path(ref, bad, for_write=write)
            assert exc.value.code == "path_not_editable", bad


def test_sha256_text():
    import hashlib
    assert sha256_text("가") == hashlib.sha256("가".encode("utf-8")).hexdigest()
