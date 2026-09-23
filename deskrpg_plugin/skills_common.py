"""NPC 스킬 관리(0.15.0)의 공통 부품 — 분류·위치·경로 검사·쓰기 컨텍스트.

**스킬 정본은 Hermes 다.** 판정은 Hermes 함수(`is_hub_installed`·`is_bundled`·`is_external_skill_path`)를
그대로 쓰고, 쓰기는 Hermes 의 사용자 쓰기 경로를 거친다. 여기 함수는 전부 **요청 프로필 홈 스코프
(`picker._home_scope`) 안의 워커 스레드에서만** 부른다.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import re
from pathlib import Path

from .common import RequestError

EDITABLE_DIRS = ("references", "templates")
MAX_EDIT_BYTES = 262144
ACTOR_HEADER = "X-DeskRPG-Actor"
_ACTOR_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclasses.dataclass(frozen=True)
class SkillRef:
    name: str
    path: Path
    source: str  # "local" | "hub" | "bundled" | "external"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify(api, name: str, path: Path | None) -> str:
    if api.is_hub_installed(name):
        return "hub"
    if api.is_bundled(name):
        return "bundled"
    if path is not None and api.is_external_skill_path(path):
        return "external"
    return "local"


def locate(api, name: str) -> SkillRef | None:
    """프론트매터 이름 → 스킬 폴더. 로컬을 먼저, 없으면 외부 폴더."""
    path = api._find_skill_dir(name) or api._find_external_skill_dir(name)
    if path is None:
        return None
    path = Path(path)
    return SkillRef(name=name, path=path, source=classify(api, name, path))


def require_skill(api, name: str) -> SkillRef:
    ref = locate(api, name)
    if ref is None:
        raise RequestError(404, "skill_not_found", name)
    return ref


def require_unpinned(api, name: str) -> None:
    """고정된 스킬은 보관하지 않는다 — Hermes 에서 pin 은 사용자 삭제도 막는다(`learning_mutations._delete_skill`)."""
    if (api.load_usage() or {}).get(name, {}).get("pinned"):
        raise RequestError(409, "skill_pinned", name)


def is_editable(ref: SkillRef, rel: str) -> bool:
    if ref.source != "local":
        return False
    return rel == "SKILL.md" or rel.split("/", 1)[0] in EDITABLE_DIRS


def _has_symlink_between(root: Path, target: Path) -> bool:
    """root(포함 안 함)부터 target(포함)까지 경로 구성 요소 중 심볼릭 링크가 있는가."""
    cur = root
    for part in target.relative_to(root).parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def resolve_editable_path(ref: SkillRef, rel: str, *, for_write: bool = False) -> Path:
    """상대 경로 → 스킬 폴더 안의 실제 경로. 폴더 밖·절대 경로·`..`·심볼릭 링크는 403.

    읽기도 이 검사를 거친다 — 심볼릭 링크로 스킬 밖 파일을 읽히지 않게. 쓰기는 편집 가능 경로만.
    """
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        raise RequestError(403, "path_not_editable", rel)
    target = ref.path / rel
    if _has_symlink_between(ref.path, target):
        raise RequestError(403, "path_not_editable", rel)
    try:
        target.resolve().relative_to(ref.path.resolve())
    except (OSError, ValueError):
        raise RequestError(403, "path_not_editable", rel) from None
    if for_write and not is_editable(ref, rel):
        raise RequestError(403, "path_not_editable", rel)
    return target


def actor_of(request) -> str | None:
    raw = request.headers.get(ACTOR_HEADER)
    return raw if raw and _ACTOR_RE.fullmatch(raw) else None


@contextlib.contextmanager
def user_write(api):
    """사용자 쓰기 — ledger actor 를 user 로 묶고, 끝나면 스킬 프롬프트 캐시를 비운다(대시보드와 같다)."""
    token = api.set_ledger_actor("user")
    try:
        yield
    finally:
        api.reset_ledger_actor(token)
        try:
            api.clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:  # noqa: BLE001 — 캐시 비우기 실패가 저장 성공을 뒤집지 않는다
            pass
