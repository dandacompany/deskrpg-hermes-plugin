"""HTTP transport rejects oversized uploads and never publishes execution paths to downloads."""
import pytest
from aiohttp import FormData, web
from deskrpg_plugin import routes
from tests.test_conversation_files_prepare import runtime


@pytest.fixture
def file_api(tmp_path, fake_api):
    fake_api.__dict__.update(runtime(tmp_path)[0].api.__dict__)
    return fake_api


async def client_for(aiohttp_client, api):
    class Adapter:
        def _check_auth(self, request):
            expected = 'profile-test' if request.path.startswith('/p/') else 'owner-test'
            if request.headers.get('Authorization') != expected:
                return web.json_response({'error': 'unauthorized'}, status=401)
    app = web.Application(client_max_size=6 * 1024 * 1024)
    routes.attach(app, Adapter(), api)
    return await aiohttp_client(app)


def form(data=b'pdf', extra=False):
    body = FormData()
    body.add_field('conversationId', 'dm')
    body.add_field('requestId', 'upload')
    body.add_field('file', data, filename='a.pdf', content_type='application/pdf')
    if extra:
        body.add_field('file', b'other', filename='b.pdf')
    return body


async def test_upload_download_link_and_delete(aiohttp_client, file_api):
    client = await client_for(aiohttp_client, file_api)
    headers = {'Authorization': 'owner-test'}
    response = await client.post('/deskrpg/conversation-files', data=form(), headers=headers)
    assert response.status == 201
    f = await response.json()
    assert set(f) == {'id', 'name', 'mimeType', 'bytes', 'sha256'}
    content = await client.get(f"/deskrpg/conversation-files/{f['id']}/content", headers=headers)
    assert await content.read() == b'pdf'
    grant = await client.post(f"/deskrpg/conversation-files/{f['id']}/grant", headers=headers,
        json={'conversationId': 'dm', 'profileName': 'alice', 'sessionId': 'sid', 'sessionKey': 'key'})
    assert grant.status == 200
    assert set(await grant.json()) == {'grant', 'expiresAt'}
    linked = await client.post(f"/deskrpg/conversation-files/{f['id']}/link", headers=headers,
        json={'conversationId': 'dm', 'messageId': 'm'})
    assert linked.status == 200
    deleted = await client.delete(f"/deskrpg/conversation-files/{f['id']}", headers=headers)
    assert deleted.status == 200


async def test_auth_size_and_duplicate_file_parts(aiohttp_client, file_api):
    client = await client_for(aiohttp_client, file_api)
    assert (await client.post('/deskrpg/conversation-files', data=form(), headers={'Authorization':'profile-test'})).status == 401
    for body, expected in [(form(extra=True),400), (form(b'x'*(5*1024*1024+1)),413)]:
        result = await client.post('/deskrpg/conversation-files',data=body,headers={'Authorization':'owner-test'})
        assert result.status == expected


def test_missing_public_helper_disables_only_attachment_routes(file_api):
    from deskrpg_plugin.contract_fields import capabilities
    assert 'conversation_files_v1' in capabilities(file_api)
    file_api.install_profile_terminal_scope = None
    assert 'conversation_files_v1' not in capabilities(file_api)
    assert not any('/conversation-files' in p for _,p,_,_ in routes.routes_for(file_api))
    assert any(p == '/deskrpg/info' for _,p,_,_ in routes.routes_for(file_api))
