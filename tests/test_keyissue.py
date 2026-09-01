"""키 발급 — 갓 만든 프로필이 말을 걸 수 있는 상태로 태어나는가."""

import logging
import stat

import pytest

from deskrpg_plugin import keyissue



def _env(profile_dir):
    return (profile_dir / ".env").read_text(encoding="utf-8")


def test_발급된_키는_Hermes_최소길이_16을_넘는다():
    # has_usable_secret(key, min_length=16) 을 통과하지 못하면 인증이 fail-closed 로
    # 막혀 프로필이 태어나자마자 죽은 것과 같다.
    for _ in range(20):
        assert len(keyissue.generate_key()) >= 16


def test_키는_매번_다르다():
    assert len({keyissue.generate_key() for _ in range(50)}) == 50


def test_env_에_키를_쓰고_권한을_0600으로_조인다(tmp_path):
    d = tmp_path / "olivia"
    d.mkdir()
    key = keyissue.issue(d)
    assert f"API_SERVER_KEY={key}" in _env(d)
    mode = stat.S_IMODE((d / ".env").stat().st_mode)
    assert mode == 0o600, f"0600 이어야 하는데 {oct(mode)}"


def test_기존_줄을_한_줄도_잃지_않는다(tmp_path):
    d = tmp_path / "olivia"
    d.mkdir()
    (d / ".env").write_text(
        "# Per-profile secrets\nOPENAI_API_KEY=sk-keep-me\nSLACK_TOKEN=xoxb-keep\n",
        encoding="utf-8",
    )
    keyissue.issue(d)
    body = _env(d)
    assert "OPENAI_API_KEY=sk-keep-me" in body
    assert "SLACK_TOKEN=xoxb-keep" in body
    assert "# Per-profile secrets" in body


def test_이미_있는_키는_갈아끼우고_중복_줄을_남기지_않는다(tmp_path):
    # dotenv 는 나중 정의가 이긴다. 첫 줄만 고치고 뒷줄을 남기면 우리가 쓴 값이
    # 조용히 무시되어 "성공을 보고하는 실패" 가 된다.
    d = tmp_path / "olivia"
    d.mkdir()
    (d / ".env").write_text(
        "API_SERVER_KEY=old-one\nOTHER=1\nAPI_SERVER_KEY=old-two\n", encoding="utf-8"
    )
    key = keyissue.issue(d)
    lines = [l for l in _env(d).splitlines() if l.startswith("API_SERVER_KEY=")]
    assert lines == [f"API_SERVER_KEY={key}"]
    assert "OTHER=1" in _env(d)


def test_주석_처리된_정의는_건드리지_않는다(tmp_path):
    d = tmp_path / "olivia"
    d.mkdir()
    (d / ".env").write_text("# API_SERVER_KEY=example-placeholder\n", encoding="utf-8")
    key = keyissue.issue(d)
    body = _env(d)
    assert "# API_SERVER_KEY=example-placeholder" in body
    assert f"API_SERVER_KEY={key}" in body


def test_쓸_수_없으면_KeyIssueFailed_이고_키값이_새지_않는다(tmp_path):
    d = tmp_path / "olivia"
    d.mkdir()
    (d / ".env").mkdir()  # 디렉토리라 write_text 가 실패한다
    with pytest.raises(keyissue.KeyIssueFailed) as exc:
        keyissue.issue(d)
    assert "IsADirectoryError" in exc.value.reason or "OSError" in exc.value.reason


def test_로그에_키_값이_찍히지_않는다(tmp_path, caplog):
    d = tmp_path / "olivia"
    d.mkdir()
    with caplog.at_level(logging.DEBUG):
        key = keyissue.issue(d)
    assert key not in caplog.text
