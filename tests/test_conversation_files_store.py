"""Original bytes, idempotency, grants and deletion boundaries."""
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from deskrpg_plugin.common import RequestError


def store(tmp_path, **kwargs):
    from deskrpg_plugin.conversation_files_store import FileStore
    return FileStore(tmp_path / 'files', **kwargs)


def upload(s, data=b'%PDF-1.4\noriginal', request='upload-1', conversation='dm-1', name='reference.pdf'):
    return s.store_file(conversation, request, name, 'application/pdf', [data])


def test_original_roundtrip_and_idempotent_request(tmp_path):
    s = store(tmp_path)
    meta, created = upload(s)
    assert created and meta['bytes'] == 17
    assert s.read_file(meta['id']) == b'%PDF-1.4\noriginal'
    assert meta['sha256'] == hashlib.sha256(b'%PDF-1.4\noriginal').hexdigest()
    assert upload(s) == (meta, False)
    with pytest.raises(RequestError, match='attachment_conflict'):
        upload(s, b'changed')
    other, _ = upload(s, request='upload-2')
    assert other['id'] != meta['id']


def test_upload_size_quota_and_partial_failure_leave_no_blob(tmp_path):
    s = store(tmp_path, quota=8)
    with pytest.raises(RequestError, match='attachment_quota_exceeded'):
        upload(s)
    def broken():
        yield b'hi'
        raise OSError('connection lost')
    with pytest.raises(OSError):
        s.store_file('dm', 'r', 'a.pdf', 'application/pdf', broken())
    assert not list((tmp_path / 'files' / 'blobs').iterdir())
    with pytest.raises(RequestError, match='attachment_too_large'):
        upload(store(tmp_path / 'large'), b'x' * (5 * 1024 * 1024 + 1))


@pytest.mark.parametrize('name', ['../escape.pdf', '/tmp/escape.pdf', 'a/b.pdf', 'a\\b.pdf', 'bad\x00.pdf'])
def test_reject_unsafe_filename(tmp_path, name):
    with pytest.raises(RequestError, match='attachment_invalid_file'):
        upload(store(tmp_path), name=name)


def test_stream_and_quota_are_atomic_under_concurrent_writers(tmp_path):
    s = store(tmp_path, quota=5)
    def put(i):
        try:
            return upload(s, b'abc', request=str(i))[0]['id']
        except RequestError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(put, range(2)))
    assert results.count('attachment_quota_exceeded') == 1


def test_grant_scope_expiry_revoke_and_delete_lease(tmp_path):
    s = store(tmp_path)
    meta, _ = upload(s)
    grant = s.issue_grant(meta['id'], 'dm-1', 'alice', 'chat-key', 'sid', now=10)
    assert s.authorize_grant(grant['grant'], 'alice', now=11)['session_id'] == 'sid'
    for profile, now in [('bob', 11), ('alice', 311)]:
        with pytest.raises(RequestError, match='attachment_not_found'):
            s.authorize_grant(grant['grant'], profile, now=now)
    assert [f['id'] for f in s.list_bound('alice', ['sid'])] == [meta['id']]
    lease = s.acquire_lease(meta['id'], 'alice', 'sid', 'prepare-1', now=20)
    assert s.delete_file(meta['id'], now=21)['state'] == 'pending'
    with pytest.raises(RequestError, match='attachment_not_found'):
        s.read_file(meta['id'])
    assert s.release_lease(lease['leaseId'], 'alice', 'sid', 'prepare-1', now=22)['state'] == 'deleted'
    assert not list((tmp_path / 'files' / 'blobs').iterdir())


def test_only_unlinked_old_originals_are_swept(tmp_path):
    s = store(tmp_path)
    a, _ = s.store_file('dm', 'a', 'a.pdf', 'application/pdf', [b'a'], now=1)
    b, _ = s.store_file('dm', 'b', 'b.pdf', 'application/pdf', [b'b'], now=1)
    s.link_file(b['id'], 'dm', 'message')
    s.sweep_files(now=90000, grace=False)
    with pytest.raises(RequestError):
        s.read_file(a['id'])
    assert s.read_file(b['id']) == b'b'


def test_binding_revocation_does_not_delete_original(tmp_path):
    s = store(tmp_path)
    f, _ = upload(s)
    g = s.issue_grant(f['id'], 'dm-1', 'alice', 'key', 'sid')
    s.revoke_binding('dm-1', 'alice', 'sid')
    assert s.list_bound('alice', ['sid']) == []
    with pytest.raises(RequestError):
        s.authorize_grant(g['grant'], 'alice')
    assert s.read_file(f['id'])


def test_active_unlinked_file_is_not_tombstoned_by_sweep(tmp_path):
    s = store(tmp_path)
    f, _ = s.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'a'], now=1)
    s.acquire_lease(f['id'], 'alice', 'sid', 'req', now=90000)
    s.sweep_files(now=90001, grace=False)
    assert s.read_file(f['id']) == b'a'


def test_message_cannot_link_more_than_three_files(tmp_path):
    s = store(tmp_path)
    for i in range(3):
        f, _ = upload(s, request=str(i))
        s.link_file(f['id'], 'dm-1', 'm')
    f, _ = upload(s, request='four')
    with pytest.raises(RequestError, match='attachment_count_exceeded'):
        s.link_file(f['id'], 'dm-1', 'm')


def test_blob_symlink_is_never_read(tmp_path):
    s = store(tmp_path)
    f, _ = upload(s)
    original = tmp_path/'private.pdf';original.write_bytes(b'private')
    path = s.blob_path(f['id']);path.unlink();path.symlink_to(original)
    with pytest.raises(RequestError, match='attachment_not_found'):
        s.read_file(f['id'])
