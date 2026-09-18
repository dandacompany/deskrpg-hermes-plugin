"""링크(URL) 아티팩트 — 정리·라벨·추출. 순수 함수이고 네트워크를 쓰지 않는다.

기준은 Hermes desktop `apps/desktop/src/app/artifacts/artifact-utils.ts`(2026-09-08): 답변의 맨 URL 과
마크다운 링크 전부, 도구 결과는 `artifacts_detect` 의 키 규칙. 데스크톱과 다른 점 — 코드 블록·인라인 코드
안의 URL 은 뺀다(예시 주소가 대부분이다). 정규식은 모두 선형 시간이다 — 마크다운 대상은 공백·짝 없는 괄호에서,
맨 URL 은 공백·ASCII 밖 글자에서 끊기고, 역추적할 겹치는 선택지가 없다.

맨 URL 의 끝(0.8.3): 본문 속 URL 은 ASCII 다(비ASCII 는 퍼센트 인코딩된다). 그래서 한글·CJK·「」 같은 글자가 나오면
거기서 끝낸다(`…/report에 올렸습니다`). 그 뒤 끝 구두점·마크다운 강조(`*_~!?:'"` 와 `,.;`)를 떼고, `)` 는
괄호 짝이 안 맞을 때만 뗀다(`Foo_(bar)` 는 지킨다).
"""

import dataclasses
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from . import artifacts_detect as detect
from . import artifacts_fences as fences

LINK_MIME = "text/uri-list"
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 200
_TRAILING = ",.;*_~!?:'\""  # 끝에서 떼는 구두점·강조. `)` 는 짝이 안 맞을 때만 따로 뗀다
_SCHEMES = ("http", "https")
# 대상은 괄호 한 단계까지(`Foo_(bar)`). 두 선택지의 첫 글자가 겹치지 않아 역추적이 선형이다.
_MD_LINK_RE = re.compile(r"!?\[([^\[\]\n]*)\]\(((?:[^()\s]|\([^()\s]{0,2048}\)){1,2048})\)")
# 공백·`<>"'`·백틱을 뺀 인쇄 가능 ASCII 만 — 첫 비ASCII 글자에서 끝난다.
_URL_RE = re.compile(r"https?://[!#-&(-;=?-_a-~]+", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_UNSAFE_NAME_RE = re.compile(r"[^\w.-]+")


@dataclasses.dataclass(frozen=True)
class Link:
    url: str
    title: str


def _strip_trailing(value: str) -> str:
    """끝 구두점·강조를 떼고, `)` 는 여는 괄호보다 닫는 괄호가 많을 때만 뗀다. 괄호 수는 한 번만 센다(선형)."""
    opens, closes, end = value.count("("), value.count(")"), len(value)
    while end:
        ch = value[end - 1]
        if ch in _TRAILING:
            end -= 1
        elif ch == ")" and closes > opens:
            closes -= 1
            end -= 1
        else:
            break
    return value[:end]


def canonical_url(raw) -> str | None:
    """http(s) 만. 끝 구두점을 떼고 스킴·호스트를 소문자로, 사용자 정보는 버린다. 경로·쿼리·조각은 그대로.

    역슬래시가 있으면 버린다 — 브라우저는 `\\` 를 `/` 로 읽어 `https://good.com\\@evil.com` 의 호스트가 달라진다."""
    if not isinstance(raw, str):
        return None
    value = _strip_trailing(raw.strip())
    if not value or len(value) > MAX_URL_CHARS or "\\" in value \
            or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme.lower() not in _SCHEMES or not host:
        return None
    if ":" in host:
        host = f"[{host}]"  # IPv6
    netloc = host if port is None else f"{host}:{port}"
    out = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    return out if len(out) <= MAX_URL_CHARS else None


def label_for(url: str, text: str | None = None) -> str:
    """마크다운 텍스트 → 경로 마지막 조각(퍼센트 디코딩) → 호스트. 제어문자는 공백으로 바꾸고 공백을 합친다."""
    named = _clean_label(text or "")
    if named:
        return named[:MAX_TITLE_CHARS]
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    last = _clean_label(unquote(segments[-1])) if segments else ""
    return (last or parts.hostname or url)[:MAX_TITLE_CHARS]


def _clean_label(text: str) -> str:
    return " ".join(_CONTROL_RE.sub(" ", text).split())


def link_filename(title: str) -> str:
    base = _UNSAFE_NAME_RE.sub("-", title).strip(".-")[:60].strip(".-")
    return (base or "link") + ".url"


def url_blob(url: str) -> bytes:
    return (url + "\n").encode("utf-8")


def _collect(pairs) -> list:
    """(raw, text) 순서대로 정리·중복 합침. 같은 URL 은 처음 자리를 지키고 텍스트가 있는 제목을 우선한다."""
    titles: dict = {}
    for raw, text in pairs:
        url = canonical_url(raw)
        if url is None:
            continue
        if url not in titles:
            titles[url] = text or ""
        elif text and not titles[url]:
            titles[url] = text
    return [Link(url, label_for(url, text)) for url, text in titles.items()]


def links_in_response(text) -> list:
    """답변의 마크다운 링크(이미지 포함)와 맨 URL. 코드 블록·인라인 코드 안은 뺀다."""
    if not isinstance(text, str) or not text:
        return []
    prose = _INLINE_CODE_RE.sub(" ", fences.prose_outside_blocks(text))
    pairs = [(m.group(2), m.group(1)) for m in _MD_LINK_RE.finditer(prose)]
    rest = _MD_LINK_RE.sub(" ", prose)
    pairs += [(m.group(0), "") for m in _URL_RE.finditer(rest)]
    return _collect(pairs)


def links_in_payload(payloads: list, *, producer: bool) -> list:
    """도구 결과의 키 규칙(`artifacts_detect.candidate_paths`)으로 태그된 값 중 http(s) URL 인 것."""
    return _collect((value, "") for value in detect.candidate_paths(payloads, producer=producer))
