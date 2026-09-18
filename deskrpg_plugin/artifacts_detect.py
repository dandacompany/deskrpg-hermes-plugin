"""툴 결과에서 산출물 경로를 찾는 휴리스틱 — Hermes desktop `artifact-utils.ts`(2026-09 main) 의 이식.

순수 함수. 파일시스템을 보지 않는다(그건 `artifacts_policy.resolve_source_path`). 본문 정규식 스캔은 하지
않는다 — 데스크톱은 하지만 오탐이 많고, 여기서는 사용자가 `artifact_save` 도구를 갖고 있다.
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
_UNTRUSTED_OPEN = re.compile(r"^<untrusted_tool_result\b[^>]*>\s*")
_UNTRUSTED_CLOSE = "</untrusted_tool_result>"
MAX_DEPTH = 6


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

    def walk(node, depth):
        if depth > MAX_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key)
                if STRONG_KEY_RE.match(k) or (producer and WEAK_KEY_RE.match(k)):
                    push(value)
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    for payload in payloads:
        walk(payload, 0)
    return found
