"""Authenticated transport for originals and profile-scoped execution preparation."""
from urllib.parse import quote

from aiohttp import web

from .common import RequestError, guarded, read_json_object, require_str, run_blocking
from .conversation_files_prepare import FileRuntime
from .conversation_files_types import MAX_BYTES


def handler(api, action):
    @guarded
    async def route(request):
        if action == 'upload':
            try:
                reader = await request.multipart()
            except Exception:
                raise RequestError(400, 'attachment_invalid_file') from None
            fields = {}; chunks = []; name = None; mime = None; size = 0
            async for part in reader:
                if part.name == 'file':
                    if name is not None or not part.filename:
                        raise RequestError(400, 'attachment_invalid_file')
                    name = part.filename; mime = part.headers.get('Content-Type', 'application/octet-stream')
                    while chunk := await part.read_chunk(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise RequestError(413, 'attachment_too_large')
                        chunks.append(chunk)
                elif part.name in ('conversationId', 'requestId') and part.name not in fields:
                    raw = await part.read_chunk(4096)
                    if len(raw) > 2048 or not part.at_eof():
                        raise RequestError(400, 'attachment_invalid_selection')
                    fields[part.name] = raw.decode('utf-8')
                else:
                    raise RequestError(400, 'attachment_invalid_selection')
            if name is None or set(fields) != {'conversationId', 'requestId'}:
                raise RequestError(400, 'attachment_invalid_file')
            def upload():
                return FileRuntime(api).store.store_file(fields['conversationId'], fields['requestId'], name, mime, chunks)
            meta, created = await run_blocking(upload)
            return web.json_response(meta, status=201 if created else 200)

        if action == 'content':
            def content():
                store = FileRuntime(api).store
                fid = request.match_info['id']
                return store.metadata(fid), store.read_file(fid)
            meta, data = await run_blocking(content)
            return web.Response(body=data, headers={
                'Content-Type': 'application/octet-stream', 'X-Content-Type-Options': 'nosniff',
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(meta['name'], safe=''),
                'Cache-Control': 'private, no-store',
            })
        body = {} if action == 'delete' else await read_json_object(request)
        allowed = {
            'grant': {'conversationId', 'profileName', 'sessionKey', 'sessionId'},
            'link': {'conversationId', 'messageId'}, 'prepare': {'grant', 'requestId'},
            'renew': {'sessionId', 'requestId'}, 'release': {'sessionId', 'requestId'},
            'revoke': {'conversationId', 'profileName', 'sessionId'}, 'delete': set(),
        }[action]
        if set(body) != allowed:
            raise RequestError(400, 'attachment_invalid_selection')
        for key in allowed:
            require_str(body, key)
        def work():
            rt = FileRuntime(api); store = rt.store
            if action == 'grant':
                return rt.issue_grant(request.match_info['id'], body['conversationId'], body['profileName'], body['sessionKey'], body['sessionId'])
            if action == 'link':
                return store.link_file(request.match_info['id'], body['conversationId'], body['messageId'])
            if action == 'delete':
                return store.delete_file(request.match_info['id'])
            if action == 'revoke':
                return rt.revoke_binding(body['conversationId'], body['profileName'], body['sessionId'])
            profile = request.match_info['profile']
            if action == 'prepare':
                return rt.prepare(body['grant'], body['requestId'], profile)
            return getattr(store, action + '_lease')(request.match_info['id'], profile, body['sessionId'], body['requestId'])
        result = await run_blocking(work)
        status = 202 if result.get('state') == 'pending' else 200
        if action == 'delete':
            result = {'deleted': result['state'] == 'deleted', 'pending': result['state'] == 'pending'}
        return web.json_response(result, status=status)
    return route
