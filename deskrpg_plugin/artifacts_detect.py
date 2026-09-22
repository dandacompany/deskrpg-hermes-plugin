"""툴 결과에서 산출물 경로를 찾는 휴리스틱 — Hermes desktop `artifact-utils.ts`(2026-09 main) 의 이식.

순수 함수. 파일시스템을 보지 않는다(그건 `artifacts_policy.resolve_source_path`). 본문 정규식 스캔은 하지
않는다 — 데스크톱은 하지만 오탐이 많고, 여기서는 사용자가 `artifact_save` 도구를 갖고 있다.

조상 키가 일치하면 그 전체 하위 트리가 산출물 후보로 태그된다. 예: {"generated_image": {"url": "/w/x.png"}}
에서 "generated_image"(강한 키)가 일치하면 하위의 모든 문자열 값 "/w/x.png"가 후보가 된다.
"""

import json
import re

PRODUCER_TOOL_RE = re.compile(
    r"(?:^|_)(?:creat(?:e|ion)|download|export|generat(?:e|ion)|render|save|speech|tts|write)(?:_|$)", re.I
)
STRONG_KEY_RE = re.compile(
    r"^(?:artifact_(?:file|image|path|url)|files?_(?:created|modified|written)|generated_(?:file|image|path|url)"
    r"|media_tag|output_(?:file|path|url)|result_(?:file|path|url)|saved_to|screenshot_path)$", re.I
)
WEAK_KEY_RE = re.compile(
    r"^(?:artifact(?:s|_(?:file|image|path|url))?|attachment(?:s|_(?:file|image|path|url))?"
    r"|download(?:s|_(?:file|path|url))?|(?:audio|image|video)(?:_(?:file|path|url))?|file_path|local_path"
    r"|media(?:_(?:file|path|url))?|path)$", re.I
)
# 비산출 도구의 결과를 JSON 으로 파싱하기 전 싼 사전 검사 — 강한 키가 JSON 키 자리에 글자로 나오는가.
_STRONG_KEY_TEXT_RE = re.compile(r'"(?:' + STRONG_KEY_RE.pattern[4:-2] + r')"\s*:', re.I)

_UNTRUSTED_OPEN = re.compile(r"^<untrusted_tool_result\b[^>]*>\s*")
_UNTRUSTED_CLOSE = "</untrusted_tool_result>"
MAX_DEPTH = 6


def mentions_strong_key(result) -> bool:
    if isinstance(result, dict):
        return True  # 이미 구조체 — 파싱 비용이 없다
    return isinstance(result, str) and bool(_STRONG_KEY_TEXT_RE.search(result))


def is_producer_tool(name: str) -> bool:
    return bool(PRODUCER_TOOL_RE.search((name or "").strip()))


def unwrap_untrusted(text: str):
    trimmed = (text or "").strip()
    match = _UNTRUSTED_OPEN.match(trimmed)
    if not match:
        return None
    close = trimmed.rfind(_UNTRUSTED_CLOSE)
    if close <= match.end():
        return None
    wrapped = trimmed[match.end():close].strip()
    blank = wrapped.find("\n\n")
    return (wrapped if blank == -1 else wrapped[blank + 2:]).strip()


def _maybe_json(text: str):
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def parse_tool_result(result) -> list:
    if isinstance(result, dict):
        return [result]
    if not isinstance(result, str):
        return []
    out = []
    for candidate in (result, unwrap_untrusted(result)):
        parsed = _maybe_json(candidate) if candidate else None
        if isinstance(parsed, (dict, list)):
            out.append(parsed)
    return out


def candidate_paths(payloads: list, *, producer: bool) -> list:
    found, seen = [], set()

    def push(value):
        if isinstance(value, str):
            v = value.strip()
            if v and v not in seen:
                seen.add(v)
                found.append(v)
        elif isinstance(value, list):
            for item in value:
                push(item)

    def walk(node, depth, tagged=False):
        if depth > MAX_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key)
                matched = STRONG_KEY_RE.match(k) or (producer and WEAK_KEY_RE.match(k))
                # 이 부분트리가 태그되어야 하는지 결정
                new_tagged = tagged or matched

                # 이 키가 일치하면 값을 push한다
                if matched:
                    push(value)
                # 태그된 부분트리에서 문자열 값이면 push한다
                elif tagged and isinstance(value, str):
                    push(value)

                # 컨테이너 값으로 재귀, 갱신된 태그 상태로 넘긴다
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1, new_tagged)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1, tagged)

    for payload in payloads:
        walk(payload, 0)
    return found
