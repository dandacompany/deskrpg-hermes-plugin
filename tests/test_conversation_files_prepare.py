"""Preparation must prove backend bytes and retain only authorized session lineage."""
import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deskrpg_plugin.common import RequestError
from deskrpg_plugin.conversation_files_store import FileStore


def runtime(tmp_path):
    from deskrpg_plugin.conversation_files_prepare import FileRuntime
    home = tmp_path / 'profiles' / 'alice'; home.mkdir(parents=True)
    (home / 'state.db').touch()
    class DB:
        def __init__(self, *_a, **_kw): pass
        def close(self): pass
        def get_session(self, sid): return {'id': sid} if sid in {'sid', 'child', 'branch'} else None
        def get_compression_chain(self, sid): return ['sid', 'child'] if sid == 'sid' else [sid]
        def get_compression_lineage(self, sid): return ['sid', 'child'] if sid in {'sid', 'child'} else [sid]
        def resolve_resume_session_id(self, sid): return 'child' if sid == 'sid' else sid
    calls = []
    def dispatch(_name, args, **kw):
        calls.append(kw)
        path = next((home / 'cache').rglob('*.pdf'))
        return json.dumps({'exit_code': 0, 'output': hashlib.sha256(path.read_bytes()).hexdigest()})
    api = SimpleNamespace(get_profile_dir=lambda p: tmp_path if p == 'default' else tmp_path / 'profiles' / p,
        profile_exists=lambda p: p == 'alice', get_hermes_home=lambda: home,
        validate_profile_name=lambda p: None, SessionDB=DB,
        set_hermes_home_override=lambda p: None, reset_hermes_home_override=lambda t: None,
        install_profile_terminal_scope=lambda p: None, reset_terminal_scope=lambda t: None,
        terminal_env=lambda name, default='': 'local' if name == 'TERMINAL_ENV' else default,
        set_session_vars=lambda **kw: None, clear_session_vars=lambda t: None,
        to_agent_visible_cache_path=str, conversation_dispatch_tool=dispatch)
    return FileRuntime(api), calls


def test_prepare_reads_original_and_checks_actual_dispatch_bytes(tmp_path):
    rt, calls = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    grant = rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    result = rt.prepare(grant['grant'], 'request', 'alice')
    assert Path(result['agentPath']).read_bytes() == b'original'
    assert calls[-1] == {'task_id': 'child', 'session_id': 'child'}
    Path(result['agentPath']).write_bytes(b'changed')
    again = rt.prepare(grant['grant'], 'request', 'alice')
    assert Path(again['agentPath']).read_bytes() == b'original'
    assert rt.store.read_file(f['id']) == b'original'
    assert result['leaseId'] == again['leaseId']
    rt.store.delete_file(f['id'])
    rt.store.release_lease(result['leaseId'], 'alice', 'sid', 'request')
    assert not Path(result['agentPath']).exists()


def test_prepare_refuses_bad_backend_probe_and_unknown_session(tmp_path):
    rt, _ = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    with pytest.raises(RequestError, match='attachment_not_found'):
        rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'missing')
    g = rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    rt.api.conversation_dispatch_tool = lambda *a, **kw: json.dumps({'exit_code': 0, 'output': 'wrong-hash'})
    with pytest.raises(RequestError, match='attachment_prepare_failed'):
        rt.prepare(g['grant'], 'request', 'alice')
    rt.api.terminal_env = lambda *a: 'ssh'
    with pytest.raises(RequestError, match='attachment_backend_unsupported'):
        rt.prepare(g['grant'], 'other', 'alice')


def test_tool_uses_trusted_identity_and_excludes_branches(tmp_path):
    from deskrpg_plugin.conversation_files_tool import make_handler
    rt, calls = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    handler = make_handler(rt.api)
    result = json.loads(handler({'action': 'list'}, session_id='child', task_id='running-task'))
    assert [row['id'] for row in result['files']] == [f['id']]
    assert json.loads(handler({'action': 'list'}, session_id='branch'))['files'] == []
    assert json.loads(handler({'action': 'prepare', 'fileId': f['id'], 'sessionId': 'child'}, session_id='branch'))['error']
    prepared = json.loads(handler({'action': 'prepare', 'fileId': f['id']}, session_id='child', task_id='running-task'))
    assert prepared['sha256'] == f['sha256']
    assert calls[-1] == {'task_id': 'running-task', 'session_id': 'child'}
    rt.store.revoke_binding('dm', 'alice', 'sid')
    assert json.loads(handler({'action': 'list'}, session_id='child'))['files'] == []


def test_prepare_request_reuses_fresh_grant_but_rejects_different_file(tmp_path):
    rt, _ = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    g = rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    first = rt.prepare(g['grant'], 'request', 'alice')
    g2 = rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    assert rt.prepare(g2['grant'], 'request', 'alice')['leaseId'] == first['leaseId']
    other, _ = rt.store.store_file('dm', 'r2', 'b.pdf', 'application/pdf', [b'original'])
    other_grant = rt.issue_grant(other['id'], 'dm', 'alice', 'key', 'sid')
    with pytest.raises(RequestError, match='attachment_conflict'):
        rt.prepare(other_grant['grant'], 'request', 'alice')


def test_revoking_compressed_session_revokes_original_binding(tmp_path):
    rt, _ = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    rt.revoke_binding('dm', 'alice', 'child')
    assert rt.store.list_bound('alice', ['sid', 'child']) == []


def test_failed_retry_cannot_release_an_already_ready_execution_lease(tmp_path):
    rt, _ = runtime(tmp_path)
    f, _ = rt.store.store_file('dm', 'r', 'a.pdf', 'application/pdf', [b'original'])
    g = rt.issue_grant(f['id'], 'dm', 'alice', 'key', 'sid')
    rt.prepare(g['grant'], 'request', 'alice')
    rt.api.conversation_dispatch_tool = lambda *a, **kw: json.dumps({'exit_code': 1, 'output': ''})
    with pytest.raises(RequestError):
        rt.prepare(g['grant'], 'request', 'alice')
    assert rt.store.delete_file(f['id'])['state'] == 'pending'
