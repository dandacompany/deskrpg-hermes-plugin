"""아티팩트 저장소 — 플러그인이 소유하는 SQLite(`registry.db`) 와 blob 폴더.

정본이다. Hermes 코어 DB 를 건드리지 않고 `api` 에서는 `get_hermes_home` 만 쓴다.
blob 을 쓰는 코드는 `store_artifact_version` 하나뿐이다 — 크기 상한 → 안전한 이름 → 정체성 판정 →
sha256 비교 → 충돌 없는 경로 → write_bytes → 한 트랜잭션으로 행 → 실패 시 blob 회수. Hermes
`hermes_cli/kanban_db.py:store_attachment_bytes` 의 순서를 그대로 옮겼다(0.21.x).

전부 블로킹이다. 라우트 핸들러는 `run_blocking` 안에서, 도구·훅은 Hermes 가 주는 워커 스레드에서 부른다.

레지스트리는 게이트웨이당 하나다. Hermes 는 프로필 세션·칸반 워커를
`HERMES_HOME=<root>/profiles/<name>` 으로 띄우므로, `artifacts_root` 는 그 경우 조상
두 단계를 올라가 기본(게이트웨이) 홈을 찾는다. `HERMES_DESKRPG_ARTIFACTS_ROOT` 를 지정하면
그 판정을 건너뛰고 그 경로를 그대로 쓴다.
"""

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

SCHEMA_VERSION = 1

_TITLE_WS = re.compile(r"\s+")
_TITLE_PUNCT = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-À-￿]+")


def artifacts_root(api) -> Path:
    """`HERMES_DESKRPG_ARTIFACTS_ROOT` 가 이기고, 없으면 기본(게이트웨이) 홈의 `deskrpg/artifacts/`.

    Hermes 는 프로필 세션·칸반 워커를 `HERMES_HOME=<root>/profiles/<name>` 으로 띄우므로,
    `api.get_hermes_home()` 이 `profiles/<name>` 아래를 가리키면 조상 두 단계를 올라가
    게이트웨이 루트를 찾는다 — 레지스트리는 게이트웨이당 정확히 하나여야 하기 때문이다.
    """
    override = os.environ.get("HERMES_DESKRPG_ARTIFACTS_ROOT")
    if override:
        return Path(override).expanduser()
    home = Path(api.get_hermes_home())
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home / "deskrpg" / "artifacts"


def registry_path(api) -> Path:
    return artifacts_root(api) / "registry.db"


def open_registry(api) -> sqlite3.Connection:
    root = artifacts_root(api)
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(registry_path(api), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_registry(conn)
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, title_norm TEXT NOT NULL, summary TEXT,
  profile TEXT NOT NULL, source_kind TEXT NOT NULL, session_id TEXT NOT NULL,
  board TEXT, task_id TEXT, job_id TEXT, run_id TEXT,
  current_version INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  deleted_at INTEGER, deleted_by TEXT
);
CREATE INDEX IF NOT EXISTS artifacts_profile_updated ON artifacts(profile, updated_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_board_task ON artifacts(board, task_id);
CREATE INDEX IF NOT EXISTS artifacts_session_kind ON artifacts(session_id, kind, title_norm);
CREATE TABLE IF NOT EXISTS artifact_versions (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id), version INTEGER NOT NULL,
  filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
  stored_path TEXT NOT NULL, origin_path TEXT, created_by TEXT NOT NULL, captured_via TEXT NOT NULL,
  note TEXT, created_at INTEGER NOT NULL, PRIMARY KEY (artifact_id, version)
);
CREATE TABLE IF NOT EXISTS artifact_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def init_registry(conn: sqlite3.Connection) -> None:
    """멱등. `user_version` 이 0 이면 만들고 1 로 올린다. 앞으로의 마이그레이션은 여기 번호로 잇는다."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    with conn:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFC", title or "").lower()
    text = _TITLE_PUNCT.sub(" ", text)
    return _TITLE_WS.sub(" ", text).strip()


_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_id_lock = threading.Lock()
_last_ms = 0


def new_artifact_id() -> str:
    """앞 10자는 밀리초 시각의 36진수(시간순 정렬), 뒤 16자는 난수. 표준 라이브러리만 쓴다.

    같은 밀리초(또는 시계가 뒤로 가는 경우)에 연속 호출돼도 프로세스 안에서는 엄격히 증가하도록
    `_last_ms` 로 단조성을 강제한다. 도구·훅 핸들러가 워커 스레드에서 동시에 부를 수 있어 락을 건다.
    """
    global _last_ms
    with _id_lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            ms = _last_ms + 1
        _last_ms = ms
    stamp = ms
    head = ""
    for _ in range(10):
        stamp, rem = divmod(stamp, 36)
        head = _ID_ALPHABET[rem] + head
    tail = "".join(secrets.choice(_ID_ALPHABET) for _ in range(16))
    return head + tail


class ArtifactTooLarge(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ArtifactMeta:
    kind: str
    title: str
    summary: str
    filename: str
    mime: str
    profile: str
    source_kind: str
    session_id: str
    created_by: str
    captured_via: str
    board: str | None = None
    task_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    origin_path: str | None = None
    note: str | None = None
    supersedes: str | None = None


@dataclasses.dataclass(frozen=True)
class StoreResult:
    artifact_id: str
    version: int
    deduped: bool
    created: bool


def _safe_filename(name: str) -> str:
    """경로 구분자·제어문자를 떼고 basename 만 남긴다. 비면 `artifact`. 확장자는 유지된다."""
    base = Path(name.replace("\\", "/")).name
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f")
    base = _SAFE_NAME.sub("_", base).strip("._ ") or "artifact"
    return base[:180]


def _collision_free(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(2, 10_000):
        candidate = dest_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(name)


def _resolve_target(conn, meta: ArtifactMeta) -> str | None:
    """정체성 규칙 1·2. 살아 있는 아티팩트 id 또는 None(새 아티팩트)."""
    if meta.supersedes:
        row = conn.execute(
            "SELECT id FROM artifacts WHERE id=? AND deleted_at IS NULL", (meta.supersedes,)
        ).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        "SELECT id FROM artifacts WHERE session_id=? AND kind=? AND title_norm=? AND deleted_at IS NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (meta.session_id, meta.kind, normalize_title(meta.title)),
    ).fetchone()
    return row["id"] if row else None


def store_artifact_version(api, conn, *, meta: ArtifactMeta, data: bytes, max_bytes: int) -> StoreResult:
    if len(data) > max_bytes:
        raise ArtifactTooLarge(f"artifact exceeds {max_bytes} bytes")
    digest = hashlib.sha256(data).hexdigest()
    now = int(time.time())
    target = _resolve_target(conn, meta)
    if target is not None:
        head = conn.execute(
            "SELECT version, sha256 FROM artifact_versions WHERE artifact_id=? ORDER BY version DESC LIMIT 1",
            (target,),
        ).fetchone()
        if head and head["sha256"] == digest:
            return StoreResult(target, head["version"], deduped=True, created=False)
        version = (head["version"] if head else 0) + 1
        artifact_id, created = target, False
    else:
        artifact_id, version, created = new_artifact_id(), 1, True

    dest_dir = artifacts_root(api) / "blobs" / artifact_id / str(version)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _collision_free(dest_dir, _safe_filename(meta.filename))
    dest.write_bytes(data)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if created:
                conn.execute(
                    "INSERT INTO artifacts (id, kind, title, title_norm, summary, profile, source_kind, session_id,"
                    " board, task_id, job_id, run_id, current_version, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (artifact_id, meta.kind, meta.title, normalize_title(meta.title), meta.summary, meta.profile,
                     meta.source_kind, meta.session_id, meta.board, meta.task_id, meta.job_id, meta.run_id,
                     version, now, now),
                )
            else:
                conn.execute(
                    "UPDATE artifacts SET current_version=?, updated_at=?, summary=COALESCE(?, summary) WHERE id=?",
                    (version, now, meta.summary or None, artifact_id),
                )
            conn.execute(
                "INSERT INTO artifact_versions (artifact_id, version, filename, mime, size, sha256, stored_path,"
                " origin_path, created_by, captured_via, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, version, dest.name, meta.mime, len(data), digest, str(dest.resolve()),
                 meta.origin_path, meta.created_by, meta.captured_via, meta.note, now),
            )
            _append_event(conn, now, "artifact.created" if created else "artifact.versioned", {
                "artifact_id": artifact_id, "version": version, "kind": meta.kind, "title": meta.title,
                "profile": meta.profile, "source_kind": meta.source_kind, "board": meta.board,
                "task_id": meta.task_id, "captured_via": meta.captured_via,
                **({"supersedes": meta.supersedes} if meta.supersedes and not created else {}),
            })
    except Exception:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            dest_dir.rmdir()
        raise
    return StoreResult(artifact_id, version, deduped=False, created=created)


def _append_event(conn, ts: int, kind: str, payload: dict) -> None:
    conn.execute("INSERT INTO artifact_events (ts, kind, payload) VALUES (?,?,?)",
                 (ts, kind, json.dumps({k: v for k, v in payload.items() if v is not None}, ensure_ascii=False)))


def get_artifact(conn, artifact_id: str):
    return conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()


def list_versions(conn, artifact_id: str) -> list:
    return conn.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id=? ORDER BY version ASC", (artifact_id,)
    ).fetchall()


def list_artifacts(conn, *, profiles=None, board=None, kind=None, source=None, q=None, before=None, limit=50) -> list:
    """살아 있는 것만, `updated_at DESC, id DESC`. `profiles` 와 `board` 는 OR."""
    where, params = ["deleted_at IS NULL"], []
    scope = []
    if profiles:
        scope.append(f"profile IN ({','.join('?' * len(profiles))})")
        params.extend(profiles)
    if board:
        scope.append("board=?")
        params.append(board)
    if scope:
        where.append("(" + " OR ".join(scope) + ")")
    if kind:
        where.append("kind=?"); params.append(kind)
    if source:
        where.append("source_kind=?"); params.append(source)
    if q:
        like = f"%{q}%"
        where.append("(title LIKE ? OR summary LIKE ? OR id IN (SELECT artifact_id FROM artifact_versions WHERE filename LIKE ?))")
        params.extend([like, like, like])
    if before:
        where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
        params.extend([before[0], before[0], before[1]])
    params.append(int(limit))
    return conn.execute(
        f"SELECT * FROM artifacts WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id DESC LIMIT ?", params
    ).fetchall()


def blob_path_for(api, version_row) -> Path | None:
    """`stored_path` 가 루트 아래일 때만 경로를 준다 — 행이 변조되면 임의 파일 읽기가 되기 때문."""
    root = artifacts_root(api).resolve()
    try:
        stored = Path(version_row["stored_path"]).resolve()
        stored.relative_to(root)
    except (ValueError, OSError):
        return None
    return stored


def soft_delete(api, conn, artifact_id: str, *, deleted_by: str, now: int) -> list:
    failed = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE artifacts SET deleted_at=?, deleted_by=? WHERE id=? AND deleted_at IS NULL",
                     (now, deleted_by, artifact_id))
        _append_event(conn, now, "artifact.deleted", {"artifact_id": artifact_id, "deleted_by": deleted_by})
    for row in list_versions(conn, artifact_id):
        path = blob_path_for(api, row)
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(path)
    with contextlib.suppress(OSError):
        shutil.rmtree(artifacts_root(api) / "blobs" / artifact_id, ignore_errors=True)
    return failed
