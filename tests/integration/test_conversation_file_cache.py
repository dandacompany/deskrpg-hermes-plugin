"""Optional latest-Hermes contract: authenticated HTTP prepare reaches the actual tool backend.

Run with an isolated HOME and env -i. Docker is opt-in, never pulls an image.
The pinned compatibility job may lack these public helpers and must still load the plugin.
"""
import contextvars
import hashlib
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.upstream_only]


def attach_runtime(api):
    import model_tools
    from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
    from deskrpg_plugin.conversation_files_prepare import available
    ctx = PluginContext(PluginManifest(name='conversation-cache-test'), PluginManager())
    def dispatch(*args, **kwargs):
        result = ctx.dispatch_tool(*args, **kwargs)
        parsed = json.loads(result)
        assert parsed.get('exit_code') == 0, parsed
        return result
    api.conversation_dispatch_tool = dispatch
    if not available(api):
        if os.environ.get('DESKRPG_ATTACHMENTS_GATE') == '1':
            pytest.fail('Required public attachment runtime APIs are absent')
        pytest.skip('Optional latest-Hermes attachment APIs are absent on the pinned build')
    import tools.terminal_tool  # Registers the actual handler, no provider/model call.
    return ctx


@pytest.mark.parametrize('backend', ['local', 'docker'] if os.environ.get('DESKRPG_ATTACHMENTS_DOCKER') == '1' else ['local'])
def test_profile_concurrent_prepare_reads_and_rechecks_original(api, make_profile, backend):
    if backend == 'docker' and os.environ.get('DESKRPG_ATTACHMENTS_DOCKER') != '1':
        pytest.skip('Docker integration is explicitly opt-in')
    attach_runtime(api)
    from deskrpg_plugin.conversation_files_prepare import FileRuntime
    rt = FileRuntime(api)
    for profile in ('alice', 'bob'):
        home = make_profile(profile)
        (home / 'config.yaml').write_text('terminal:\n  backend: '+backend+'\n  cwd: /tmp\n'
            '  docker_image: python:3.12-slim\n  container_persistent: false\n'
            '  docker_persist_across_processes: false\n  docker_orphan_reaper: false\n')
        db = api.SessionDB(home/'state.db')
        db.create_session('sid-'+profile, 'api_server')
        db.close()

    def work(profile):
        data = ('%PDF-1.4\n'+profile).encode()
        file, _ = rt.store.store_file('dm-'+profile, 'upload', 'original.pdf', 'application/pdf', [data])
        grant = rt.issue_grant(file['id'], 'dm-'+profile, profile, 'key-'+profile, 'sid-'+profile)
        prepared = rt.prepare(grant['grant'], 'prepare', profile)
        assert prepared['sha256'] == hashlib.sha256(data).hexdigest()
        # Delete the execution copy after creating a container. Preparing again must restore live mounted bytes.
        with rt.store.connection() as db:
            copy = db.execute('SELECT path FROM copies WHERE file=? AND profile=?', (file['id'], profile)).fetchone()[0]
        Path(copy).unlink()
        again = rt.prepare(grant['grant'], 'prepare', profile)
        assert again['leaseId'] == prepared['leaseId']
        assert Path(copy).read_bytes() == data
        assert rt.store.read_file(file['id']) == data
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(work, ('alice','bob')))
    from tools.terminal_tool import cleanup_all_environments
    cleanup_all_environments()


def test_public_session_lineage_excludes_branches_and_delegates(api, make_profile):
    attach_runtime(api)
    from deskrpg_plugin.conversation_files_prepare import FileRuntime
    rt = FileRuntime(api)
    home = make_profile('alice')
    db = api.SessionDB(home/'state.db')
    db.create_session('root', 'api_server')
    db.end_session('root', 'compression')
    db.create_session('child', 'api_server', parent_session_id='root')
    db.create_session('branch', 'api_server', parent_session_id='root', model_config={'_branched_from':'root'})
    db.create_session('delegate', 'api_server', parent_session_id='root', model_config={'_delegate_from':'root'})
    db.close()
    assert rt.resolve_session('alice','root') == 'child'
    assert rt.authorized_sessions('alice','child') == ['root','child']
    assert rt.authorized_sessions('alice','branch') == ['branch']
    assert rt.authorized_sessions('alice','delegate') == ['delegate']


async def test_http_session_create_prepare_and_trusted_tool_identity(api, make_profile, aiohttp_client):
    from aiohttp import web
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from deskrpg_plugin import routes
    from deskrpg_plugin.conversation_files_prepare import FileRuntime
    from deskrpg_plugin.conversation_files_tool import make_handler, TOOL_SCHEMA
    ctx = attach_runtime(api)
    home = make_profile('alice')
    (home/'config.yaml').write_text('terminal:\n  backend: local\n  cwd: '+str(home)+'\n')
    db = api.SessionDB(home/'state.db')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key':'attachment-test-key'}))
    adapter._session_db = db
    app = web.Application()
    app.router.add_post('/api/sessions', adapter._handle_create_session)
    app.router.add_get('/api/sessions/{session_id}', adapter._handle_get_session)
    routes.attach(app,adapter,api)
    client = await aiohttp_client(app)
    headers={'Authorization':'Bearer attachment-test-key'}
    response = await client.post('/api/sessions',json={'id':'trusted-session'},headers=headers)
    assert response.status == 201
    assert (await response.json())['session']['id'] == 'trusted-session'
    assert (await client.get('/api/sessions/trusted-session',headers=headers)).status == 200
    rt=FileRuntime(api)
    file,_=rt.store.store_file('dm','upload','original.pdf','application/pdf',[b'%PDF-1.4\noriginal'])
    grant_response=await client.post('/deskrpg/conversation-files/'+file['id']+'/grant',headers=headers,
        json={'conversationId':'dm','profileName':'alice','sessionKey':'chat-key','sessionId':'trusted-session'})
    assert grant_response.status == 200
    grant=await grant_response.json()
    response=await client.post('/p/alice/deskrpg/conversation-files/prepare',headers=headers,
        json={'grant':grant['grant'],'requestId':'prepare'})
    assert response.status == 200, await response.text()
    receipt=await response.json()

    def actual_tool_call():
        import model_tools
        ht=api.set_hermes_home_override(str(home));tt=api.install_profile_terminal_scope(home)
        tokens=api.set_session_vars(platform='api_server',profile='alice',session_key='chat-key',session_id='trusted-session')
        from hermes_cli.plugins import PluginContext, PluginManifest, PluginManager
        profile_ctx=PluginContext(PluginManifest(name='conversation-cache-test'),PluginManager(scope_key=str(home)))
        profile_ctx.register_tool('conversation_files','conversation_files',TOOL_SCHEMA,make_handler(api))
        try:
            result=model_tools.handle_function_call('conversation_files', {'action':'prepare','fileId':file['id']},
                task_id='trusted-session',session_id='trusted-session')
            result=json.loads(result)
            assert 'agentPath' in result, result
            assert result['agentPath'] == receipt['agentPath']
            assert result['sha256'] == file['sha256']
            forged=json.loads(model_tools.handle_function_call('conversation_files',
                {'action':'prepare','fileId':file['id'],'sessionId':'trusted-session'},task_id='foreign',session_id='foreign'))
            assert forged['error'] == 'attachment_invalid_selection'
        finally:
            api.clear_session_vars(tokens);api.reset_terminal_scope(tt);api.reset_hermes_home_override(ht)
    contextvars.copy_context().run(actual_tool_call)
    db.close()
