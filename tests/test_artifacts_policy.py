"""원본 경로 정책과 kind 완결성 — 임의 파일 읽기 도구가 되지 않게, 모델이 형태를 맞추게."""
import types
from pathlib import Path

import pytest

from deskrpg_plugin import artifacts_policy as policy


@pytest.fixture
def api(tmp_path):
    home, kanban = tmp_path / "home", tmp_path / "kanban"
    home.mkdir(); kanban.mkdir()
    return types.SimpleNamespace(get_hermes_home=lambda: str(home), kanban_home=lambda: str(kanban))


def test_허용_루트는_홈과_칸반_홈과_환경변수_추가분이다(api, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACT_SOURCE_ROOTS", str(tmp_path / "ws"))
    roots = policy.allowed_source_roots(api)
    assert (tmp_path / "home").resolve() in roots and (tmp_path / "ws").resolve() in roots


def test_루트_밖_경로는_outside_root_다(api, tmp_path):
    outside = tmp_path / "elsewhere.md"; outside.write_text("x")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(outside))
    assert e.value.code == "artifact_path_outside_root"


def test_심볼릭_링크로_루트를_벗어나도_막힌다(api, tmp_path):
    outside = tmp_path / "secret.md"; outside.write_text("x")
    link = tmp_path / "home" / "link.md"; link.symlink_to(outside)
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(link))
    assert e.value.code == "artifact_path_outside_root"


@pytest.mark.parametrize("name", [".env", ".env.local", ".envrc", "auth.json", "config.yaml", ".git-credentials", "Auth.JSON"])
def test_민감_파일_이름은_거부된다(api, tmp_path, name):
    p = tmp_path / "home" / name; p.write_text("k")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(p))
    assert e.value.code == "artifact_path_sensitive"


def test_민감_디렉터리_아래는_어디든_거부된다(api, tmp_path):
    p = tmp_path / "home" / "mcp-tokens" / "srv.json"; p.parent.mkdir(); p.write_text("t")
    assert policy.is_sensitive_path(p)


def test_없는_파일과_디렉터리는_outside_root_로_같이_거절한다(api, tmp_path):
    with pytest.raises(policy.PolicyError):
        policy.resolve_source_path(api, str(tmp_path / "home"))  # 디렉터리
    with pytest.raises(policy.PolicyError):
        policy.resolve_source_path(api, str(tmp_path / "home" / "nope.md"))


@pytest.mark.parametrize("name,kind", [("a.md", "document"), ("a.pdf", "document"), ("a.png", "image"),
                                       ("a.mp4", "media"), ("a.wav", "media"), ("a.html", "web"),
                                       ("a.tsx", "react"), ("a.csv", "data"), ("a.json", "data"), ("a.zip", "file")])
def test_확장자로_kind_를_정한다(name, kind):
    assert policy.kind_for_filename(name) == kind


def test_mime_은_md_와_tsx_를_텍스트로_다룬다():
    assert policy.mime_for_filename("x.md") == "text/markdown"
    assert policy.mime_for_filename("x.tsx") == "text/plain"
    assert policy.mime_for_filename("x.unknownext") == "application/octet-stream"


def test_web_은_완전한_HTML_문서여야_한다():
    with pytest.raises(policy.PolicyError) as e:
        policy.validate_completeness("web", filename="a.html", text="<div>hi</div>", from_path=False)
    assert e.value.code == "artifact_incomplete" and "html" in e.value.detail.lower()
    policy.validate_completeness("web", filename="a.html", text="<!doctype html><html></html>", from_path=False)


def test_react_는_기본_내보내기가_있는_tsx_여야_한다():
    with pytest.raises(policy.PolicyError):
        policy.validate_completeness("react", filename="A.tsx", text="const A = () => null", from_path=False)
    with pytest.raises(policy.PolicyError):
        policy.validate_completeness("react", filename="A.ts", text="export default 1", from_path=False)
    policy.validate_completeness("react", filename="A.tsx", text="export default function A(){}", from_path=False)


def test_data_는_csv_json_이어야_하고_image_media_는_경로여야_한다():
    with pytest.raises(policy.PolicyError):
        policy.validate_completeness("data", filename="a.txt", text="1,2", from_path=False)
    with pytest.raises(policy.PolicyError) as e:
        policy.validate_completeness("image", filename="a.png", text="....", from_path=False)
    assert "file path" in e.value.detail
    policy.validate_completeness("image", filename="a.png", text=None, from_path=True)


def test_모르는_kind_는_bad_kind_다():
    with pytest.raises(policy.PolicyError) as e:
        policy.validate_completeness("sticker", filename="a", text="", from_path=False)
    assert e.value.code == "artifact_bad_kind"


def test_image_는_확장자_허용_목록을_확인한다():
    with pytest.raises(policy.PolicyError) as e:
        policy.validate_completeness("image", filename="payload.exe", text=None, from_path=True)
    assert e.value.code == "artifact_incomplete"
    policy.validate_completeness("image", filename="A.PNG", text=None, from_path=True)


def test_media_는_확장자_허용_목록을_확인한다():
    with pytest.raises(policy.PolicyError) as e:
        policy.validate_completeness("media", filename="a.png", text=None, from_path=True)
    assert e.value.code == "artifact_incomplete"
    policy.validate_completeness("media", filename="a.mp4", text=None, from_path=True)


# ---------------------------------------------------------------------------
# R20 — 상대 경로·DB 파일·아티팩트 저장소 자신
# ---------------------------------------------------------------------------


def test_상대_경로는_작업_디렉터리와_무관하게_거부되고_절대_경로를_요구한다(api, tmp_path, monkeypatch):
    (tmp_path / "home" / "rel.md").write_text("x")
    monkeypatch.chdir(tmp_path / "home")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, "rel.md")
    assert e.value.code == "artifact_path_outside_root"
    assert "absolute path" in e.value.detail


@pytest.mark.parametrize("name", ["state.db", "kanban.db-wal", "kanban.db-shm", "x.db-journal", "a.sqlite", "b.SQLITE3", "C.DB"])
def test_데이터베이스_파일은_민감_경로로_거부된다(api, tmp_path, name):
    p = tmp_path / "kanban" / name; p.write_bytes(b"SQLite format 3\x00")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(p))
    assert e.value.code == "artifact_path_sensitive"


def test_아티팩트_저장소_안의_파일은_민감_경로로_거부된다(api, tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACTS_ROOT", raising=False)
    blob = tmp_path / "home" / "deskrpg" / "artifacts" / "blobs" / "01x" / "v1" / "r.md"
    blob.parent.mkdir(parents=True); blob.write_text("x")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(blob))
    assert e.value.code == "artifact_path_sensitive"


def test_환경변수로_옮긴_아티팩트_저장소도_거부된다(api, tmp_path, monkeypatch):
    root = tmp_path / "kanban" / "arts"
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACTS_ROOT", str(root))
    blob = root / "blobs" / "r.md"; blob.parent.mkdir(parents=True); blob.write_text("x")
    with pytest.raises(policy.PolicyError) as e:
        policy.resolve_source_path(api, str(blob))
    assert e.value.code == "artifact_path_sensitive"
    ok = tmp_path / "kanban" / "ok.md"; ok.write_text("x")
    assert policy.resolve_source_path(api, str(ok)) == ok.resolve()


def test_link_종류와_url_MIME_이_있고_path_content_로는_저장할_수_없다():
    from deskrpg_plugin import contract_fields as cf
    assert policy.KINDS[-1] == "link" and cf.ARTIFACT_KINDS == policy.KINDS
    assert policy.mime_for_filename("a.url") == "text/uri-list"
    for from_path in (True, False):
        with pytest.raises(policy.PolicyError) as err:
            policy.validate_completeness("link", filename="a.url", text="https://x.io", from_path=from_path)
        assert err.value.code == "artifact_incomplete"
