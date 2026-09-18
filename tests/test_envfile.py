import os
import stat

from deskrpg_plugin import envfile


def test_기존_줄을_보존하고_같은_키는_한_줄만_남긴다(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# 주석\nA=1\nB=old\n\nB=older\nC=3\n", encoding="utf-8")
    envfile.upsert_lines(p, {"B": "B=new", "D": "D='x y'"})
    assert p.read_text(encoding="utf-8") == "# 주석\nA=1\nB=new\n\nC=3\nD='x y'\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_없는_파일이면_만든다(tmp_path):
    p = tmp_path / ".env"
    envfile.upsert_lines(p, {"A": "A=1"})
    assert p.read_text(encoding="utf-8") == "A=1\n"


def test_export_접두와_공백을_읽고_마지막_정의가_이긴다(tmp_path):
    p = tmp_path / ".env"
    p.write_text("export A=1\n B = 2 \nA=3\n#C=9\nnot a line\n", encoding="utf-8")
    got = envfile.read_assignments(p)
    assert set(got) == {"A", "B"}
    assert got["A"] == "A=3"


def test_지우면_지운_이름을_돌려준다(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\nA=3\n", encoding="utf-8")
    assert envfile.remove_keys(p, ["A", "Z"]) == ["A"]
    assert p.read_text(encoding="utf-8") == "B=2\n"
