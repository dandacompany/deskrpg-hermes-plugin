"""아티팩트 원본 경로 정책과 kind 완결성 — 순수 함수.

경로 규칙은 Hermes 게이트웨이 파일 API 의 것을 **옮겨 적었다**(`hermes_cli/web_routers/files.py`
`_SENSITIVE_MANAGED_FILE_BASENAMES`·`_SENSITIVE_MANAGED_DIR_NAMES`·`_is_sensitive_filename`, 0.21.x).
그 모듈은 FastAPI 를 import 하므로 가져올 수 없다(standards: 대시보드 모듈 금지). Hermes 를 올릴 때
목록을 다시 대조한다.
"""

import mimetypes
import os
from pathlib import Path

KINDS = ("document", "image", "media", "web", "react", "data", "file")

_SENSITIVE_BASENAMES = frozenset({
    "auth.json", "auth.lock", "credentials", "config.yaml", ".anthropic_oauth.json",
    "google_token.json", "google_oauth_pending.json", "google_oauth.json",
    "webhook_subscriptions.json", "bws_cache.json", "bws_cache.enc.json", ".git-credentials",
})
_SENSITIVE_DIRS = frozenset({"mcp-tokens", "pairing", ".ssh", ".gnupg", ".aws"})

_KIND_BY_EXT = {
    **dict.fromkeys((".md", ".txt", ".pdf", ".docx", ".doc", ".rtf"), "document"),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"), "image"),
    **dict.fromkeys((".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".mp4", ".webm", ".mov", ".mkv", ".avi"), "media"),
    **dict.fromkeys((".html", ".htm"), "web"),
    **dict.fromkeys((".tsx", ".jsx"), "react"),
    **dict.fromkeys((".csv", ".json", ".jsonl"), "data"),
}
HOOK_EXTENSIONS = frozenset(_KIND_BY_EXT) | {".zip", ".tar", ".gz"}
_TEXT_MIME = {".md": "text/markdown", ".tsx": "text/plain", ".jsx": "text/plain", ".jsonl": "application/x-ndjson"}


class PolicyError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def allowed_source_roots(api) -> list:
    roots = [Path(api.get_hermes_home()), Path(api.kanban_home())]
    extra = os.environ.get("HERMES_DESKRPG_ARTIFACT_SOURCE_ROOTS", "")
    roots += [Path(p).expanduser() for p in extra.split(os.pathsep) if p]
    return [r.resolve() for r in roots]


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env.") or name == ".envrc" or name in _SENSITIVE_BASENAMES:
        return True
    return any(part.lower() in _SENSITIVE_DIRS for part in path.parts)


def resolve_source_path(api, raw: str) -> Path:
    """허용 루트 아래의 **실제 파일**만 돌려준다. 심볼릭 링크는 resolve 뒤 판정한다."""
    try:
        target = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise PolicyError("artifact_path_outside_root", "파일이 없거나 읽을 수 없다")
    if not target.is_file():
        raise PolicyError("artifact_path_outside_root", "파일이 아니다")
    if not any(target == root or root in target.parents for root in allowed_source_roots(api)):
        raise PolicyError("artifact_path_outside_root", "게이트웨이 관리 루트 밖의 경로다")
    if is_sensitive_path(target) or is_sensitive_path(Path(raw)):
        raise PolicyError("artifact_path_sensitive", "자격증명 파일은 아티팩트로 저장할 수 없다")
    return target


def kind_for_filename(name: str) -> str:
    return _KIND_BY_EXT.get(Path(name).suffix.lower(), "file")


def mime_for_filename(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _TEXT_MIME:
        return _TEXT_MIME[suffix]
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def validate_completeness(kind: str, *, filename: str, text, from_path: bool) -> None:
    if kind not in KINDS:
        raise PolicyError("artifact_bad_kind", f"kind 는 {', '.join(KINDS)} 중 하나다")
    suffix = Path(filename).suffix.lower()
    body = (text or "").lower()
    if kind == "web" and "<html" not in body and "<!doctype html" not in body:
        raise PolicyError("artifact_incomplete", "web 은 완전한 HTML 문서여야 한다 — <html> 또는 <!doctype html> 이 없다")
    if kind == "react":
        if suffix not in (".tsx", ".jsx"):
            raise PolicyError("artifact_incomplete", "react 는 .tsx 또는 .jsx 파일이어야 한다")
        if "export default" not in body:
            raise PolicyError("artifact_incomplete", "react 는 기본 내보내기(export default)가 있는 단일 컴포넌트 파일이어야 한다")
    if kind == "data" and suffix not in (".csv", ".json", ".jsonl"):
        raise PolicyError("artifact_incomplete", "data 는 CSV 또는 JSON 이어야 한다")
    if kind == "document" and suffix not in (".md", ".txt", ".pdf", ".docx", ".doc", ".rtf", ".html"):
        raise PolicyError("artifact_incomplete", "document 는 md·txt·pdf·docx·html 이어야 한다")
    if kind in ("image", "media"):
        if not from_path:
            raise PolicyError("artifact_incomplete", f"{kind} 는 인라인 본문이 아니라 파일 경로로 넘겨야 한다")
        if _KIND_BY_EXT.get(suffix) != kind:
            if kind == "image":
                raise PolicyError("artifact_incomplete", "image 는 png·jpg·gif·webp·svg·bmp 파일이어야 한다")
            else:  # kind == "media"
                raise PolicyError("artifact_incomplete", "media 는 오디오·비디오 파일(mp3·wav·mp4·webm 등)이어야 한다")
