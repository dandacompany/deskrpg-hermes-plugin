"""모델 응답 속 큰 코드 블록 감지 — Hermes desktop `apps/desktop/src/lib/artifact-detect.ts`(2026-09 main) 이식.

순수 함수. 응답 텍스트에서 닫힌 코드 블록을 뽑고, 데스크톱과 같은 경계로 "대화에 두기엔 큰 독립 산출물"만
고른다. HTML 문서(160자 이상), HTML 조각(태그가 있고 1200자 이상), SVG(2000자 이상), 코드(3000자 또는
48줄 이상). 언어 표기가 없거나 산문·로그 계열 표기면 제외한다.

데스크톱과 다른 점 둘:
- 산문 판별 휴리스틱(`isLikelyProseCodeBlock`)을 옮기지 않고, **확장자를 아는 언어만** 코드로 인정한다
  (데스크톱 `DOWNLOAD_EXTENSION_BY_LANGUAGE` 표). 표 밖의 언어 표기는 건너뛴다 — 산문이 섞인 블록을 거르는
  효과가 같고 판정이 결정적이다.
- 선언 이름에 Go 의 `func` 를 더했다(데스크톱 정규식에는 없다).
"""

import dataclasses
import re
from pathlib import Path

from . import artifacts_policy as policy

HTML_DOC_MIN_CHARS = 160
HTML_FRAGMENT_MIN_CHARS = 1200
SVG_MIN_CHARS = 2000
CODE_MIN_LINES = 48
CODE_MIN_CHARS = 3000

HTML_LANGUAGES = frozenset({"html", "htm", "xhtml"})
NON_ARTIFACT_LANGUAGES = frozenset({
    "", "console", "diff", "listing", "log", "logs", "markdown", "md", "mermaid", "output", "patch",
    "plain", "plaintext", "shell-session", "stdout", "text", "txt",
})
# 데스크톱 `DOWNLOAD_EXTENSION_BY_LANGUAGE` — html/svg 는 위에서 따로 다룬다.
EXTENSION_BY_LANGUAGE = {
    "bash": ".sh", "c": ".c", "cpp": ".cpp", "csharp": ".cs", "css": ".css", "go": ".go", "java": ".java",
    "javascript": ".js", "js": ".js", "json": ".json", "jsx": ".jsx", "kotlin": ".kt", "php": ".php",
    "py": ".py", "python": ".py", "rb": ".rb", "rs": ".rs", "ruby": ".rb", "rust": ".rs", "sh": ".sh",
    "sql": ".sql", "swift": ".swift", "toml": ".toml", "ts": ".ts", "tsx": ".tsx", "typescript": ".ts",
    "xml": ".xml", "yaml": ".yaml", "yml": ".yaml",
}

_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LANG_TOKEN_RE = re.compile(r"[^a-z0-9+#._-]")
HTML_DOC_RE = re.compile(r"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]", re.I)
HTML_TAG_RE = re.compile(r"<[a-z][a-z0-9-]*(\s[^>]*)?>", re.I)
_SVG_RE = re.compile(r"<svg[\s>]", re.I)
_TAGS_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
FILENAME_COMMENT_RE = re.compile(r"^\s*(?://|#|--|<!--|/\*)\s*([\w./-]+\.[a-z0-9]{1,8})\b", re.I)
DECLARATION_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|struct|interface|enum|trait|impl|def|fn|func)\s+([A-Za-z_$][\w$]*)"
)
_UNSAFE_NAME_RE = re.compile(r"[^\w._ -]+")


@dataclasses.dataclass(frozen=True)
class Block:
    language: str
    content: str


@dataclasses.dataclass(frozen=True)
class Detection:
    kind: str
    language: str
    title: str
    filename: str
    content: str


def _language(info: str) -> str:
    token = (info.strip().split() or [""])[0].lower()
    return _LANG_TOKEN_RE.sub("", token)


def extract_blocks(text) -> list:
    """닫힌 코드 블록만. 닫는 표시는 여는 표시와 같은 문자이고 길이가 같거나 길어야 한다."""
    if not isinstance(text, str) or not text:
        return []
    lines = text.split("\n")
    blocks, i = [], 0
    while i < len(lines):
        match = _OPEN_RE.match(lines[i])
        if not match or (match.group(1)[0] == "`" and "`" in match.group(2)):
            i += 1
            continue
        fence = match.group(1)
        close_re = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
        for j in range(i + 1, len(lines)):
            if close_re.match(lines[j]):
                blocks.append(Block(_language(match.group(2)), "\n".join(lines[i + 1:j])))
                i = j + 1
                break
        else:
            return blocks  # 닫히지 않은 블록 — 그 뒤는 볼 필요가 없다
    return blocks


def _strip_tags(value: str) -> str:
    return _WS_RE.sub(" ", _TAGS_RE.sub(" ", value)).strip()


def _title_from_tag(content: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", content, re.I)
    return _strip_tags(match.group(1))[:80] if match else ""


def _safe_base(title: str) -> str:
    return _WS_RE.sub("-", _UNSAFE_NAME_RE.sub("", title).strip())[:60] or "artifact"


def _filename(title: str, ext: str) -> str:
    base = _safe_base(title)
    return base if re.search(r"\.[a-z0-9]{1,8}$", base, re.I) else base + ext


def compose_html(fragment: str) -> str:
    """조각을 최소 문서 틀로 감싼다(데스크톱 `composeArtifactHtml`). 뷰어가 바로 열 수 있게."""
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>body{margin:0;font-family:system-ui,sans-serif}</style></head><body>\n"
        + fragment + "\n</body></html>\n"
    )


def _code_title(language: str, content: str) -> str:
    head = content[:2000]
    named = FILENAME_COMMENT_RE.search(head)
    if named:
        return Path(named.group(1)).name
    declared = DECLARATION_RE.search(head)
    if declared:
        return declared.group(1)
    return language


def detect(language, code) -> Detection | None:
    trimmed = (code or "").strip()
    if not trimmed:
        return None
    lang = _language(language or "")
    if lang in HTML_LANGUAGES:
        is_doc = bool(HTML_DOC_RE.search(trimmed))
        if is_doc and len(trimmed) >= HTML_DOC_MIN_CHARS:
            content = trimmed
        elif not is_doc and len(trimmed) >= HTML_FRAGMENT_MIN_CHARS and HTML_TAG_RE.search(trimmed):
            content = compose_html(trimmed)
        else:
            return None
        title = _title_from_tag(trimmed, "title") or _title_from_tag(trimmed, "h1") or "HTML"
        return Detection("web", lang, title, _filename(title, ".html"), content)
    if lang == "svg":
        if len(trimmed) < SVG_MIN_CHARS or not _SVG_RE.search(trimmed):
            return None
        title = _title_from_tag(trimmed, "title") or "SVG"
        return Detection("image", lang, title, _filename(title, ".svg"), trimmed)
    if lang in NON_ARTIFACT_LANGUAGES or lang not in EXTENSION_BY_LANGUAGE:
        return None
    if len(trimmed) < CODE_MIN_CHARS and trimmed.count("\n") + 1 < CODE_MIN_LINES:
        return None
    title = _code_title(lang, trimmed)
    filename = _filename(title, EXTENSION_BY_LANGUAGE[lang])
    return Detection(policy.kind_for_filename(filename), lang, title, filename, trimmed)


def detect_in_response(text) -> list:
    return [d for d in (detect(b.language, b.content) for b in extract_blocks(text)) if d is not None]
