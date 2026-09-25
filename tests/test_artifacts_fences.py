"""응답 속 코드 블록 감지 — Hermes desktop `apps/desktop/src/lib/artifact-detect.ts`(2026-09 main) 기준의 이식.

경계값은 그 파일의 상수에서 왔다: HTML 문서 160자, HTML 조각 1200자, SVG 2000자, 코드 3000자 또는 48줄.
"""

import pytest

from deskrpg_plugin import artifacts_fences as fences


def _fence(lang: str, body: str, tick: str = "```") -> str:
    return f"{tick}{lang}\n{body}\n{tick}"


def _doc(title: str = "대시보드", pad: int = 200) -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body>{'x' * pad}</body></html>"


# ---------------------------------------------------------------------------
# 블록 추출
# ---------------------------------------------------------------------------


def test_닫힌_블록만_뽑고_언어_표기를_소문자로_정리한다():
    text = "앞말\n" + _fence("Python", "print(1)") + "\n중간\n" + _fence("", "plain") + "\n끝"
    blocks = fences.extract_blocks(text)
    assert [(b.language, b.content) for b in blocks] == [("python", "print(1)"), ("", "plain")]


def test_물결표_블록과_더_긴_닫는_표시를_받는다():
    text = "~~~ts\nconst a = 1\n~~~~\n"
    assert [(b.language, b.content) for b in fences.extract_blocks(text)] == [("ts", "const a = 1")]


def test_닫히지_않은_블록은_버린다():
    assert fences.extract_blocks("```html\n<html></html>\n") == []


def test_짧은_닫는_표시는_블록을_닫지_않는다():
    text = "````md\n```\n안쪽\n```\n````"
    blocks = fences.extract_blocks(text)
    assert len(blocks) == 1 and blocks[0].content == "```\n안쪽\n```"


def test_언어_표기의_속성은_떼어낸다():
    blocks = fences.extract_blocks('```tsx title="App.tsx" {1,3}\nexport default 1\n```')
    assert blocks[0].language == "tsx"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_HTML_문서는_160자_이상이면_web_이고_title_을_제목으로_쓴다():
    body = _doc("매출 대시보드")
    got = fences.detect("html", body)
    assert got is not None and got.kind == "web" and got.title == "매출 대시보드"
    assert got.filename == "매출-대시보드.html" and got.content == body


def test_HTML_문서가_160자_미만이면_잡지_않는다():
    short = "<html><body>짧다</body></html>"
    assert len(short) < 160 and fences.detect("html", short) is None


def test_HTML_조각은_1200자_이상이고_태그가_있어야_하며_문서_틀로_감싼다():
    frag = "<div>" + "y" * 1195 + "</div>"
    assert len(frag) >= 1200
    got = fences.detect("html", frag)
    assert got is not None and got.kind == "web"
    assert got.content.lower().startswith("<!doctype html>") and frag in got.content
    assert fences.detect("html", "<div>" + "y" * 1000 + "</div>") is None


def test_HTML_제목은_title_다음_h1_다음_기본값_순이다():
    h1_only = "<html><body><h1>분기 보고</h1>" + "z" * 200 + "</body></html>"
    assert fences.detect("html", h1_only).title == "분기 보고"
    bare = "<html><body>" + "z" * 200 + "</body></html>"
    assert fences.detect("html", bare).title == "HTML"


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------


def test_SVG_는_2000자부터_image_다():
    def svg(n):
        head = '<svg xmlns="http://www.w3.org/2000/svg"><title>로고</title><path d="'
        tail = '"/></svg>'
        return head + "M" * (n - len(head) - len(tail)) + tail

    assert fences.detect("svg", svg(1999)) is None
    got = fences.detect("svg", svg(2000))
    assert got is not None and got.kind == "image" and got.title == "로고" and got.filename == "로고.svg"


# ---------------------------------------------------------------------------
# 코드
# ---------------------------------------------------------------------------


def _lines(n: int, first: str = "x = 0") -> str:
    return "\n".join([first] + [f"x = {i}" for i in range(1, n)])


def test_코드는_48줄부터_잡고_47줄은_잡지_않는다():
    assert fences.detect("python", _lines(47)) is None
    got = fences.detect("python", _lines(48))
    assert got is not None and got.kind == "file" and got.filename.endswith(".py")


def test_코드는_줄이_적어도_3000자면_잡는다():
    assert fences.detect("sql", "SELECT " + "a, " * 1000 + "1") is not None


def test_제외_언어와_언어_표기_없는_블록과_모르는_언어는_잡지_않는다():
    long = _lines(60)
    for lang in ("", "text", "md", "markdown", "diff", "log", "mermaid", "console", "plaintext"):
        assert fences.detect(lang, long) is None, lang
    assert fences.detect("brainfuck", long) is None  # 확장자를 모르는 언어


def test_코드_제목은_파일명_주석_다음_선언_다음_언어_순이다():
    assert fences.detect("python", _lines(48, "# report_builder.py")).title == "report_builder.py"
    assert fences.detect("python", _lines(48, "def build_report():")).title == "build_report"
    assert fences.detect("python", _lines(48)).title == "python"


def test_파일명_주석이_있으면_그_이름과_확장자를_그대로_쓴다():
    got = fences.detect("typescript", _lines(48, "// src/App.tsx"))
    assert got.filename == "App.tsx" and got.kind == "react"


@pytest.mark.parametrize("lang,ext,kind", [
    ("tsx", ".tsx", "react"), ("jsx", ".jsx", "react"), ("json", ".json", "data"),
    ("ts", ".ts", "file"), ("go", ".go", "file"), ("yaml", ".yaml", "file"), ("css", ".css", "file"),
])
def test_코드_kind_는_확장자로_정한다(lang, ext, kind):
    got = fences.detect(lang, _lines(48))
    assert got.filename.endswith(ext) and got.kind == kind


# ---------------------------------------------------------------------------
# 응답 전체
# ---------------------------------------------------------------------------


def test_응답에서_조건을_넘는_블록만_순서대로_고른다():
    text = "\n\n".join([
        _fence("python", "print(1)"),          # 작다
        _fence("html", _doc("첫 페이지")),       # 잡힌다
        _fence("text", _lines(60)),            # 제외 언어
        _fence("go", _lines(50, "func Main() {")),  # 잡힌다
    ])
    got = fences.detect_in_response(text)
    assert [(d.kind, d.title) for d in got] == [("web", "첫 페이지"), ("file", "Main")]


def test_응답이_비었거나_문자열이_아니면_빈_목록이다():
    assert fences.detect_in_response("") == [] and fences.detect_in_response(None) == []


# ---------------------------------------------------------------------------
# 리뷰 수정 (0.8.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("Example.com", "Example.com.html"),
    ("My App v2.0", "My-App-v2.0.html"),
    ("report.PDF", "report.PDF.html"),
    ("..", "artifact.html"),
])
def test_HTML_파일명은_제목과_무관하게_항상_html_로_끝난다(title, expected):
    got = fences.detect("html", _doc(title))
    assert got.filename == expected and got.filename.endswith(".html")


def test_SVG_파일명은_항상_svg_로_끝난다():
    head = '<svg xmlns="http://www.w3.org/2000/svg"><title>logo.png</title><path d="'
    body = head + "M" * 2100 + '"/></svg>'
    assert fences.detect("svg", body).filename == "logo.png.svg"


def test_코드의_파일명_주석_확장자가_언어와_다르면_주석을_무시한다():
    got = fences.detect("python", _lines(48, "# App.tsx"))
    assert got.kind == "file" and got.filename.endswith(".py")


def test_ts_와_tsx_js_와_jsx_는_서로_호환되는_확장자로_본다():
    assert fences.detect("typescript", _lines(48, "// src/App.tsx")).filename == "App.tsx"
    assert fences.detect("javascript", _lines(48, "// Widget.jsx")).filename == "Widget.jsx"


def test_head_나_body_만_있는_HTML_은_문서_틀로_감싼다():
    body_only = "<body><h1>보고</h1>" + "b" * 200 + "</body>"
    got = fences.detect("html", body_only)
    assert got.kind == "web" and got.content.lower().startswith("<!doctype html>")


def test_제목의_HTML_엔티티를_되돌린다():
    got = fences.detect("html", _doc("Tom &amp; Jerry"))
    assert got.title == "Tom & Jerry"


def _cpu_시간(text):
    """세 번 중 가장 짧은 CPU 시간. 벽시계가 아니라 이 프로세스의 CPU 만 재서 경합에 둔감하다."""
    import time

    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        fences.detect("html", text)
        best = min(best, time.process_time() - started)
    return best


@pytest.mark.parametrize(
    "make",
    [lambda n: "<x " * n, lambda n: "<title" * n + "<html>" + "y" * 200],
)
def test_닫히지_않은_태그가_반복돼도_선형_시간에_끝난다(make):
    # 벽시계 1초 예산은 머신 부하를 함께 잰다. 입력을 4배로 늘렸을 때의 증가율을 본다 —
    # 선형이면 약 4배, 제곱이면 약 16배다.
    assert fences.detect("html", "<x " * 40_000) is None
    assert fences.detect("html", "<title" * 20_000 + "<html>" + "y" * 200) is not None
    small = _cpu_시간(make(10_000))
    large = _cpu_시간(make(40_000))
    assert large < 8 * max(small, 0.001), f"4배 입력에 {large / max(small, 1e-9):.1f}배 걸렸다"
