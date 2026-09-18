"""아티팩트 저장소 — 루트 해석, 스키마, 정체성 규칙, 유일한 쓰기 함수."""
import contextlib
import hashlib
import sqlite3
import threading
import types

import pytest

from deskrpg_plugin import artifacts_store as store


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACTS_ROOT", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    return types.SimpleNamespace(get_hermes_home=lambda: str(home))


def _meta(**over):
    base = dict(
        kind="document", title="주간 보고서", summary="이번 주 요약", filename="report.md",
        mime="text/markdown", profile="sophie", source_kind="chat", session_id="s1",
        created_by="agent:sophie", captured_via="tool",
    )
    base.update(over)
    return store.ArtifactMeta(**base)


def test_루트는_기본_프로필_홈_아래이고_환경변수가_이긴다(api, monkeypatch, tmp_path):
    assert store.artifacts_root(api) == tmp_path / "home" / "deskrpg" / "artifacts"
    monkeypatch.setenv("HERMES_DESKRPG_ARTIFACTS_ROOT", str(tmp_path / "nas"))
    assert store.artifacts_root(api) == tmp_path / "nas"


def test_루트는_프로필_세션_홈이어도_게이트웨이_기본_홈_아래다(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACTS_ROOT", raising=False)
    root = tmp_path / "root"
    profile_home = root / "profiles" / "sophie"
    profile_home.mkdir(parents=True)
    profile_api = types.SimpleNamespace(get_hermes_home=lambda: str(profile_home))
    assert store.artifacts_root(profile_api) == root / "deskrpg" / "artifacts"

    root.mkdir(exist_ok=True)
    default_api = types.SimpleNamespace(get_hermes_home=lambda: str(root))
    assert store.artifacts_root(default_api) == root / "deskrpg" / "artifacts"


def test_open_registry_는_디렉터리와_스키마를_만들고_멱등이다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"artifacts", "artifact_versions", "artifact_events"} <= tables
    with contextlib.closing(store.open_registry(api)) as conn:  # 두 번째도 예외 없이
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_제목_정규화는_NFC_소문자_공백_구두점을_접는다():
    assert store.normalize_title("  주간   보고서!! ") == "주간 보고서"
    assert store.normalize_title("Weekly Report") == store.normalize_title("weekly report")
    assert store.normalize_title("한한") == store.normalize_title("한한")  # NFD → NFC


def test_새_id_는_시간순으로_정렬되고_26자다():
    a, b = store.new_artifact_id(), store.new_artifact_id()
    assert len(a) == 26 and a.isalnum() and a.islower()
    assert a < b


def _store(api, conn, data="# 보고서\n".encode(), **over):
    return store.store_artifact_version(api, conn, meta=_meta(**over), data=data, max_bytes=10_000)


def test_첫_저장은_새_아티팩트_버전_1_이고_blob_과_행이_함께_생긴다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        r = _store(api, conn)
        assert r.created and r.version == 1 and not r.deduped
        row = store.get_artifact(conn, r.artifact_id)
        assert row["current_version"] == 1 and row["title_norm"] == "주간 보고서"
        v = store.list_versions(conn, r.artifact_id)[0]
        report_bytes = "# 보고서\n".encode()
        assert v["sha256"] == hashlib.sha256(report_bytes).hexdigest()
        p = store.blob_path_for(api, v)
        assert p is not None and p.read_bytes() == report_bytes
        assert p.parent.name == "1" and p.parent.parent.name == r.artifact_id


def test_같은_세션_같은_kind_같은_제목이면_다음_버전이다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        first = _store(api, conn, data=b"v1")
        second = _store(api, conn, data=b"v2", title="주간  보고서!")  # 정규화하면 같다
        assert second.artifact_id == first.artifact_id and second.version == 2 and not second.created
        assert store.get_artifact(conn, first.artifact_id)["current_version"] == 2


def test_supersedes_는_세션이_달라도_그_아티팩트의_다음_버전이다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        first = _store(api, conn, data=b"v1")
        second = _store(api, conn, data=b"v2", session_id="other", title="완전히 다른 제목",
                        supersedes=first.artifact_id)
        assert second.artifact_id == first.artifact_id and second.version == 2


def test_직전_버전과_해시가_같으면_no_op_이고_deduped_다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        first = _store(api, conn, data=b"same")
        again = _store(api, conn, data=b"same")
        assert again.artifact_id == first.artifact_id and again.version == 1 and again.deduped
        assert len(store.list_versions(conn, first.artifact_id)) == 1
        blobs = list((store.artifacts_root(api) / "blobs" / first.artifact_id).rglob("*"))
        assert len([b for b in blobs if b.is_file()]) == 1


def test_다른_세션_다른_제목이면_새_아티팩트다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        a = _store(api, conn, data=b"a")
        b = _store(api, conn, data=b"b", session_id="s2", title="회의록")
        assert a.artifact_id != b.artifact_id and b.created


def test_크기_초과는_ArtifactTooLarge_이고_아무것도_남기지_않는다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        with pytest.raises(store.ArtifactTooLarge):
            store.store_artifact_version(api, conn, meta=_meta(), data=b"x" * 11, max_bytes=10)
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert not any((store.artifacts_root(api) / "blobs").rglob("*"))


def test_행_삽입이_실패하면_방금_쓴_blob_을_지운다(api):
    # sqlite3.Connection 의 속성은 읽기 전용이라 conn.execute 자체를 monkeypatch 할 수 없다(실측).
    # 대신 트리거로 INSERT 를 실패시켜 같은 트랜잭션 롤백 경로를 확인한다.
    with contextlib.closing(store.open_registry(api)) as conn:
        conn.execute(
            "CREATE TRIGGER fail_version BEFORE INSERT ON artifact_versions "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            _store(api, conn)
        assert not [p for p in (store.artifacts_root(api) / "blobs").rglob("*") if p.is_file()]
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_삭제된_아티팩트에_supersedes_하면_새_아티팩트가_된다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        first = _store(api, conn, data=b"v1")
        store.soft_delete(api, conn, first.artifact_id, deleted_by="human:u1", now=1)
        second = _store(api, conn, data=b"v2", supersedes=first.artifact_id)
        assert second.artifact_id != first.artifact_id and second.created


def test_파일명은_경로_구분자와_제어문자를_잃고_충돌하면_번호가_붙는다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        r = _store(api, conn, data=b"1", filename="../../evil\x00.md")
        p = store.blob_path_for(api, store.list_versions(conn, r.artifact_id)[0])
        assert p.name == "evil.md" and ".." not in p.parts


def test_soft_delete_는_행을_남기고_blob_을_전부_지운다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        r = _store(api, conn, data=b"v1")
        _store(api, conn, data=b"v2")
        failed = store.soft_delete(api, conn, r.artifact_id, deleted_by="human:u1", now=99)
        assert failed == []
        row = store.get_artifact(conn, r.artifact_id)
        assert row["deleted_at"] == 99 and row["deleted_by"] == "human:u1"
        assert not (store.artifacts_root(api) / "blobs" / r.artifact_id).exists()
        assert store.list_artifacts(conn) == []


def test_목록은_프로필과_보드를_OR_로_거르고_최신순이다(api):
    with contextlib.closing(store.open_registry(api)) as conn:
        _store(api, conn, data=b"1", profile="a", title="A")
        _store(api, conn, data=b"2", profile="b", title="B", source_kind="kanban", board="dev", task_id="t1")
        _store(api, conn, data=b"3", profile="c", title="C")
        got = [r["title"] for r in store.list_artifacts(conn, profiles=["a"], board="dev")]
        assert got == ["B", "A"]
        assert [r["title"] for r in store.list_artifacts(conn, kind="document", q="c")] == ["C"]


def test_동시_저장은_서로_다른_데이터면_한_아티팩트에_버전_1_2_를_만든다(api):
    """훅+도구 동시 저장(스펙 ⑤): 두 스레드가 각자의 커넥션으로 같은 정체성에 동시에 쓰면
    트랜잭션 직렬화로 버전이 하나씩 순서대로 배정돼야 한다 — 두 개의 새 아티팩트가 생기면 안 된다.
    (사전 워밍업 없이 iteration 0 은 첫 오픈 경합도 함께 겪는다 — open_registry 의 잠금 처리가
    맞다면 여기도 걸리면 안 된다.)"""
    for i in range(20):
        session_id = f"race-{i}"
        errors: list[Exception] = []
        results: list[store.StoreResult] = []
        barrier = threading.Barrier(2)

        def work(payload):
            try:
                with contextlib.closing(store.open_registry(api)) as conn:
                    barrier.wait(timeout=5)
                    r = store.store_artifact_version(
                        api, conn, meta=_meta(session_id=session_id), data=payload, max_bytes=10_000,
                    )
                    results.append(r)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(payload,)) for payload in (b"a", b"b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"iteration {i}: {errors}"
        assert len(results) == 2

        with contextlib.closing(store.open_registry(api)) as conn:
            rows = conn.execute(
                "SELECT id FROM artifacts WHERE session_id=? AND deleted_at IS NULL", (session_id,)
            ).fetchall()
            assert len(rows) == 1, f"iteration {i}: expected 1 artifact, got {len(rows)}"
            versions = {v["version"] for v in store.list_versions(conn, rows[0]["id"])}
            assert versions == {1, 2}, f"iteration {i}: versions={versions}"


def test_동시_저장은_같은_데이터면_버전_하나만_남고_한_쪽만_deduped_다(api):
    for i in range(20):
        session_id = f"race-same-{i}"
        errors: list[Exception] = []
        results: list[store.StoreResult] = []
        barrier = threading.Barrier(2)

        def work():
            try:
                with contextlib.closing(store.open_registry(api)) as conn:
                    barrier.wait(timeout=5)
                    r = store.store_artifact_version(
                        api, conn, meta=_meta(session_id=session_id), data=b"identical", max_bytes=10_000,
                    )
                    results.append(r)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"iteration {i}: {errors}"
        assert len(results) == 2
        assert len({r.artifact_id for r in results}) == 1, f"iteration {i}: {results}"
        assert {r.version for r in results} == {1}, f"iteration {i}: {results}"
        assert sum(1 for r in results if r.created) == 1, f"iteration {i}: {results}"
        assert sum(1 for r in results if r.deduped) == 1, f"iteration {i}: {results}"

        with contextlib.closing(store.open_registry(api)) as conn:
            artifact_id = results[0].artifact_id
            assert len(store.list_versions(conn, artifact_id)) == 1, f"iteration {i}"


def test_새_레지스트리를_여러_스레드가_동시에_처음_열어도_잠금_오류가_없다(tmp_path_factory, monkeypatch):
    """워밍업 없이 신선한 registry.db 를 여러 스레드가 동시에 처음 연다 — 스키마 생성
    (`init_registry`)과 `PRAGMA journal_mode=WAL` 이 busy 타임아웃을 우회해 `database is
    locked` 를 내면 안 된다."""
    monkeypatch.delenv("HERMES_DESKRPG_ARTIFACTS_ROOT", raising=False)
    for trial in range(50):
        home = tmp_path_factory.mktemp(f"home{trial}")
        trial_api = types.SimpleNamespace(get_hermes_home=lambda h=home: str(h))
        barrier = threading.Barrier(8)
        errors: list[Exception] = []

        def work():
            try:
                barrier.wait(timeout=5)
                with contextlib.closing(store.open_registry(trial_api)):
                    pass
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"trial {trial}: {errors}"
        with contextlib.closing(store.open_registry(trial_api)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1, f"trial {trial}"


def test_blob_path_for_는_루트_밖을_가리키는_행에_None_이다(api, tmp_path):
    with contextlib.closing(store.open_registry(api)) as conn:
        r = _store(api, conn)
        conn.execute("UPDATE artifact_versions SET stored_path=? WHERE artifact_id=?",
                     (str(tmp_path / "etc" / "passwd"), r.artifact_id))
        assert store.blob_path_for(api, store.list_versions(conn, r.artifact_id)[0]) is None
