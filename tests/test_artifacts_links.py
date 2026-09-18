"""링크 아티팩트의 순수 함수 — 정리·라벨·추출. 네트워크를 쓰지 않는다."""
import time

import pytest

from deskrpg_plugin import artifacts_fences as fences
from deskrpg_plugin import artifacts_links as links


@pytest.mark.parametrize("raw,expected", [
    ("https://Example.COM/A/b?q=1#frag", "https://example.com/A/b?q=1#frag"),
    ("  http://x.io/path).  ", "http://x.io/path"),
    ("https://x.io/a,;.", "https://x.io/a"),
    ("HTTPS://X.IO", "https://x.io"),
    ("https://user:pw@x.io/p", "https://x.io/p"),
    ("https://x.io:8443/p", "https://x.io:8443/p"),
    ("https://[::1]:8080/p", "https://[::1]:8080/p"),
])
def test_정리는_스킴_호스트를_소문자로_끝_구두점과_사용자_정보를_뗀다(raw, expected):
    assert links.canonical_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "mailto:a@b.c", "ftp://x.io",
    "https://", "http:///nohost", "https://x.io/a b", "https://x.io/\nb", "", None, 42,
    "https://x.io/" + "a" * 2100, "https://x.io:99999/",
    "https://good.com\\@evil.com/x", "https://x.io/a\\b",
])
def test_http_s_가_아니거나_호스트가_없거나_길면_None(raw):
    assert links.canonical_url(raw) is None


def test_라벨은_텍스트_다음_경로_마지막_조각_다음_호스트다():
    assert links.label_for("https://x.io/a/b", "  분기   보고서 ") == "분기 보고서"
    assert links.label_for("https://x.io/docs/%EB%B3%B4%EA%B3%A0.pdf") == "보고.pdf"
    assert links.label_for("https://x.io/") == "x.io"
    assert len(links.label_for("https://x.io/", "가" * 300)) == 200


def test_라벨은_제어문자를_지우고_공백을_합친다():
    assert links.label_for("https://x.io/%0Aevil%0D%00") == "evil"
    assert links.label_for("https://x.io/a%09b%7F%0Ac") == "a b c"
    assert links.label_for("https://x.io/p", "이\x00름\x7f\n  둘") == "이 름 둘"


def test_파일명은_항상_url_로_끝나고_비면_link():
    assert links.link_filename("분기 보고서") == "분기-보고서.url"
    assert links.link_filename("a/b\\c..") == "a-b-c.url"
    assert links.link_filename("...") == "link.url"


def test_blob_은_URL_한_줄이다():
    assert links.url_blob("https://x.io/a") == b"https://x.io/a\n"


def test_코드_블록_밖_본문만_남긴다():
    text = "앞 https://a.io\n```bash\ncurl https://b.io\n```\n뒤 https://c.io"
    prose = fences.prose_outside_blocks(text)
    assert "a.io" in prose and "c.io" in prose and "b.io" not in prose


def test_닫히지_않은_코드_블록은_본문으로_둔다():
    assert "b.io" in fences.prose_outside_blocks("```\nhttps://b.io\n")


def test_답변에서_마크다운_이미지_맨_URL_을_순서대로_뽑고_코드는_뺀다():
    text = (
        "보고서는 [9월 보고](https://x.io/r.pdf) 입니다. 그림 ![차트](https://x.io/c.png)\n"
        "원문: https://news.io/a).\n"
        "예시 `https://api.example.com` 무시\n"
        "```\nhttps://code.io\n```\n"
    )
    got = links.links_in_response(text)
    assert [(l.url, l.title) for l in got] == [
        ("https://x.io/r.pdf", "9월 보고"), ("https://x.io/c.png", "차트"), ("https://news.io/a", "a"),
    ]


def test_같은_URL_은_하나로_합치고_텍스트가_있는_제목을_쓴다():
    text = "먼저 https://x.io/p 그리고 [정식 이름](https://X.io/p)"
    got = links.links_in_response(text)
    assert [(l.url, l.title) for l in got] == [("https://x.io/p", "정식 이름")]


@pytest.mark.parametrize("text", [None, 42, "", "링크 없음", "javascript:alert(1) [x](file:///a)"])
def test_링크가_없거나_문자열이_아니면_빈_목록(text):
    assert links.links_in_response(text) == []


def test_악성_입력에도_선형_시간이다():
    started = time.perf_counter()
    for evil in ("[" * 200_000, "](" * 200_000, "[a](" + "b" * 400_000, "https://" * 50_000,
                 "(" * 200_000, "https://x.io/" + "(" * 200_000, "https://x.io/" + ")" * 200_000,
                 "[a](" + "(" * 200_000, "[a](https://x.io/(" + "b" * 400_000):
        links.links_in_response(evil)
    assert time.perf_counter() - started < 1.0


def test_도구_결과는_강한_키면_어느_도구든_약한_키는_산출_도구만():
    payload = [{"output_url": "https://cdn.io/out.mp4", "download_url": "https://cdn.io/d.zip",
                "url": "https://search.io/hit", "note": "https://ignored.io"}]
    assert [l.url for l in links.links_in_payload(payload, producer=False)] == ["https://cdn.io/out.mp4"]
    assert [l.url for l in links.links_in_payload(payload, producer=True)] == [
        "https://cdn.io/out.mp4", "https://cdn.io/d.zip"]


def test_도구_결과의_경로_값과_중복_URL_은_버린다():
    payload = [{"output_url": "https://cdn.io/a", "result_url": "https://CDN.io/a", "output_path": "/tmp/x.md"}]
    got = links.links_in_payload(payload, producer=False)
    assert [(l.url, l.title) for l in got] == [("https://cdn.io/a", "a")]


@pytest.mark.parametrize("text,expected", [
    ("https://example.com/report에 올렸습니다", "https://example.com/report"),
    ("**https://x.io/a**", "https://x.io/a"),
    ("「https://x.io/a」입니다", "https://x.io/a"),
    ("https://x.io/a!", "https://x.io/a"),
    ("정말요? https://x.io/a?", "https://x.io/a"),
    ("_https://x.io/a_ 와 ~https://x.io/a~", "https://x.io/a"),
    ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("(출처 https://en.wikipedia.org/wiki/Foo_(bar))", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("[위키](https://en.wikipedia.org/wiki/Foo_(bar))", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("(https://x.io/a)", "https://x.io/a"),
])
def test_맨_URL_은_한글_마크다운_강조_끝_구두점에서_끝난다(text, expected):
    assert [l.url for l in links.links_in_response(text)] == [expected]


def test_마크다운_괄호_URL_의_제목은_링크_텍스트다():
    got = links.links_in_response("[위키](https://en.wikipedia.org/wiki/Foo_(bar)) 참고")
    assert [(l.url, l.title) for l in got] == [("https://en.wikipedia.org/wiki/Foo_(bar)", "위키")]
