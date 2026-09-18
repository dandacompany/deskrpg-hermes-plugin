"""아티팩트 원본 경로 정책과 kind 완결성 — 순수 함수.

경로 규칙은 Hermes 게이트웨이 파일 API 의 것을 **옮겨 적었다**(`hermes_cli/web_routers/files.py`
`_SENSITIVE_MANAGED_FILE_BASENAMES`·`_SENSITIVE_MANAGED_DIR_NAMES`·`_is_sensitive_filename`, 0.21.x).
그 모듈은 FastAPI 를 import 하므로 가져올 수 없다(standards: 대시보드 모듈 금지). Hermes 를 올릴 때
목록을 다시 대조한다.

그 위에 이 플러그인만의 규칙이 셋 있다(R20): 상대 경로는 받지 않고(게이트웨이 작업 디렉터리에 따라 뜻이
바뀐다), SQLite 류 DB 파일(`kanban.db`·`state.db` 와 WAL/SHM/저널)은 저장하지 않으며, 아티팩트 저장소
자신(`artifacts_store.artifacts_root`) 안의 파일은 다시 아티팩트로 만들지 않는다.
"""

import mimetypes
import os
from pathlib import Path

from . import artifacts_store as store

KINDS = ("document", "image", "media", "web", "react", "data", "file")

_SENSITIVE_BASENAMES = frozenset({
    "auth.json", "auth.lock", "credentials", "config.yaml", ".anthropic_oauth.json",
    "google_token.json", "google_oauth_pending.json", "google_oauth.json",
    "webhook_subscriptions.json", "bws_cache.json", "bws_cache.enc.json", ".git-credentials",
})
_SENSITIVE_DIRS = frozenset({"mcp-tokens", "pairing", ".ssh", ".gnupg", ".aws"})
_DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")

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


def is_database_file(path: Path) -> bool:
    return path.name.lower().endswith(_DATABASE_SUFFIXES)


def resolve_source_path(api, raw: str) -> Path:
    """허용 루트 아래의 **실제 파일**만 돌려준다. 심볼릭 링크는 resolve 뒤 판정한다."""
    try:
        is_absolute = Path(raw).expanduser().is_absolute()
    except (RuntimeError, ValueError):
        is_absolute = False
    if not is_absolute:
        raise PolicyError("artifact_path_outside_root", "절대 경로가 필요하다 — 상대 경로는 받지 않는다")
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
    if is_database_file(target) or is_database_file(Path(raw)):
        raise PolicyError("artifact_path_sensitive", "데이터베이스 파일(.db·.sqlite 와 WAL/SHM/저널)은 아티팩트로 저장할 수 없다")
    own = store.artifacts_root(api).resolve()
    if target == own or own in target.parents:
        raise PolicyError("artifact_path_sensitive", "아티팩트 저장소 안의 파일은 다시 저장할 수 없다")
    return target


# 프로젝트·도구 설정 파일 — 결과물이 아니라 작업 도구의 부산물이다. 훅만 거르고, 명시 저장은 막지 않는다.
_CONFIG_BASENAMES = frozenset({
    "package.json", "package-lock.json", "composer.json", "deno.json", "deno.jsonc", "biome.json",
    "turbo.json", "renovate.json", "lerna.json", "nx.json", "vercel.json", "netlify.json",
    "firebase.json", "angular.json", "manifest.json", "components.json", "pyrightconfig.json",
})
_CONFIG_PREFIXES = ("tsconfig", "jsconfig", ".eslintrc", ".prettierrc", ".babelrc", ".swcrc", ".stylelintrc")


def is_config_file(path: Path) -> bool:
    name = path.name.lower()
    return name in _CONFIG_BASENAMES or name.startswith(_CONFIG_PREFIXES) or ".config." in name


def is_workspace_file(path: Path, roots) -> bool:
    """관리 루트(`roots`) **아래에 있는** git 저장소(워크트리 포함 — `.git` 이 파일일 수 있다) 안의 파일인가.

    루트 자신과 그 위는 보지 않는다 — Hermes 홈이 dotfiles 저장소 안에 있으면 모든 산출물이
    "저장소 안" 으로 판정돼 자동 승격이 통째로 꺼진다."""
    path = Path(path)
    root = next((r for r in roots if r in path.parents), None)
    if root is None:
        return False
    for parent in path.parents:
        if parent == root:
            return False
        try:
            if (parent / ".git").exists():
                return True
        except OSError:
            return False
    return False


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
