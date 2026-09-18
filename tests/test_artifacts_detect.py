"""Hermes desktop `apps/desktop/src/app/artifacts/artifact-utils.ts` 규칙의 이식. 케이스는 그 파일의 정규식에서 뽑았다."""
import json

import pytest

from deskrpg_plugin import artifacts_detect as detect


@pytest.mark.parametrize("name,ok", [
    ("create_file", True), ("image_generate", True), ("render_chart", True), ("save_report", True),
    ("text_to_speech", True), ("tts", True), ("export_pdf", True), ("download_file", True), ("write_file", True),
    ("read_file", False), ("web_search", False), ("terminal", False), ("recreate", False), ("artifact_save", True),
])
def test_산출_도구_이름_판정(name, ok):
    assert detect.is_producer_tool(name) is ok


def test_untrusted_래퍼를_벗기면_빈_줄_뒤의_페이로드가_남는다():
    text = '<untrusted_tool_result source="x">\n주의문\n\n{"saved_to": "/w/a.png"}\n</untrusted_tool_result>'
    assert detect.unwrap_untrusted(text) == '{"saved_to": "/w/a.png"}'
    assert detect.unwrap_untrusted("그냥 텍스트") is None


def test_parse_tool_result_는_dict_문자열_래핑_셋을_다_받는다():
    assert detect.parse_tool_result({"a": 1}) == [{"a": 1}]
    assert detect.parse_tool_result('{"a": 1}') == [{"a": 1}]
    wrapped = '<untrusted_tool_result>\nx\n\n{"b": 2}\n</untrusted_tool_result>'
    assert detect.parse_tool_result(wrapped) == [{"b": 2}]
    assert detect.parse_tool_result("not json") == []


def test_강한_키는_산출_도구가_아니어도_잡고_약한_키는_산출_도구일_때만_잡는다():
    payload = {"saved_to": "/w/report.pdf", "path": "/w/tmp.txt", "nested": {"files_created": ["/w/a.md", "/w/b.md"]}}
    assert detect.candidate_paths([payload], producer=False) == ["/w/report.pdf", "/w/a.md", "/w/b.md"]
    assert detect.candidate_paths([payload], producer=True) == ["/w/report.pdf", "/w/tmp.txt", "/w/a.md", "/w/b.md"]


def test_중첩_깊이는_6_까지만_보고_중복은_한_번만():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"saved_to": "/deep"}}}}}}}}
    assert detect.candidate_paths([deep], producer=True) == []
    dup = [{"saved_to": "/w/x"}, {"output_file": "/w/x"}]
    assert detect.candidate_paths(dup, producer=False) == ["/w/x"]


def test_문자열이_아닌_값과_빈_문자열은_무시한다():
    assert detect.candidate_paths([{"saved_to": 3, "output_path": "", "screenshot_path": "  /w/s.png  "}], producer=False) == ["/w/s.png"]


def test_조상_키가_하위_트리_전체를_산출물_후보로_표시한다():
    # 강한 키가 하위 트리의 모든 문자열 값을 태그한다
    assert detect.candidate_paths([{"generated_image": {"url": "/w/x.png"}}], producer=False) == ["/w/x.png"]
    assert detect.candidate_paths([{"artifact_file": {"path": "/w/a.pdf", "mime": "application/pdf"}}], producer=False) == ["/w/a.pdf", "application/pdf"]


def test_약한_키는_producer일_때만_하위_트리를_태그한다():
    # 약한 키 "result"는 producer=False일 때 태그하지 않으므로 하위의 "image", "src"도 매칭 안 됨
    assert detect.candidate_paths([{"result": {"image": {"src": "/w/i.png"}}}], producer=False) == []
    # producer=True일 때만 "result"가 태그되고 하위 문자열 "/w/i.png"가 후보가 됨
    assert detect.candidate_paths([{"result": {"image": {"src": "/w/i.png"}}}], producer=True) == ["/w/i.png"]


def test_태그는_리스트를_통해서도_전파된다():
    # "files_created" 강한 키가 태그되면, 리스트 내 객체의 문자열 값도 후보가 됨
    assert detect.candidate_paths([{"files_created": [{"name": "/w/n.md"}]}], producer=False) == ["/w/n.md"]
