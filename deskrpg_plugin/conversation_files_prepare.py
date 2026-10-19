"""Prepare originals in the exact public Hermes execution scope and probe their bytes."""
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import shlex
import uuid

from .common import RequestError
from .conversation_files_store import FileStore, identifier, safe_directory, store_root
from .conversation_files_types import MAX_BYTES

PUBLIC_SYMBOLS = (
    'install_profile_terminal_scope', 'reset_terminal_scope', 'terminal_env',
    'set_session_vars', 'clear_session_vars', 'to_agent_visible_cache_path',
    'conversation_dispatch_tool',
)


def available(api):
    return all(callable(getattr(api, name, None)) for name in PUBLIC_SYMBOLS) and all(
        callable(getattr(getattr(api, 'SessionDB', None), name, None))
        for name in ('get_session', 'resolve_resume_session_id', 'get_compression_chain', 'get_compression_lineage'))


class FileRuntime:
    def __init__(self, api):
        if not available(api):
            raise RequestError(428, 'attachment_feature_unavailable')
        self.api = api
        self.store = FileStore(store_root(api))

    def home(self, profile):
        try:
            self.api.validate_profile_name(profile)
        except (ValueError, TypeError):
            raise RequestError(404, 'attachment_not_found') from None
        if profile != 'default' and not self.api.profile_exists(profile):
            raise RequestError(404, 'attachment_not_found')
        return Path(self.api.get_profile_dir(profile))

    @contextlib.contextmanager
    def session_db(self, profile):
        path = self.home(profile) / 'state.db'
        if not path.is_file() or path.is_symlink():
            raise RequestError(404, 'attachment_not_found')
        try:
            db = self.api.SessionDB(path, read_only=True)
        except Exception:
            raise RequestError(502, 'attachment_prepare_failed') from None
        try:
            yield db
        finally:
            db.close()

    def resolve_session(self, profile, session_id):
        identifier(session_id)
        with self.session_db(profile) as db:
            if not db.get_session(session_id):
                raise RequestError(404, 'attachment_not_found')
            resolved = db.resolve_resume_session_id(session_id)
            # The resume resolver can also walk non-compression descendants. They never inherit a grant.
            if resolved not in db.get_compression_chain(session_id):
                raise RequestError(404, 'attachment_not_found')
            return resolved

    def authorized_sessions(self, profile, session_id):
        identifier(session_id)
        with self.session_db(profile) as db:
            if not db.get_session(session_id):
                raise RequestError(404, 'attachment_not_found')
            return [sid for sid in db.get_compression_lineage(session_id)
                    if session_id in db.get_compression_chain(sid)]

    def issue_grant(self, file_id, conversation, profile, session_key, session_id):
        self.resolve_session(profile, session_id)
        return self.store.issue_grant(file_id, conversation, profile, session_key, session_id)

    def revoke_binding(self, conversation, profile, session_id):
        identifier(session_id)
        with self.session_db(profile) as db:
            if db.get_session(session_id):
                chain = db.get_compression_chain(session_id)
                sessions = [sid for sid in db.get_compression_lineage(session_id)
                            if sid in chain or session_id in db.get_compression_chain(sid)]
            else:
                sessions = [session_id]
        for sid in sessions:
            self.store.revoke_binding(conversation, profile, sid)
        return {'state': 'revoked'}

    def prepare(self, grant, request_id, profile):
        binding = self.store.authorize_grant(grant, profile)
        sid = self.resolve_session(profile, binding['session_id'])
        # Do not clear or mutate the caller's session context when preparing from an HTTP request.
        return contextvars.copy_context().run(self._http_prepare, binding, request_id, profile, sid)

    def _http_prepare(self, binding, request_id, profile, session_id):
        home_token = self.api.set_hermes_home_override(str(self.home(profile)))
        terminal_token = None; tokens = None
        try:
            terminal_token = self.api.install_profile_terminal_scope(self.home(profile))
            tokens = self.api.set_session_vars(platform='api_server', profile=profile,
                session_key=binding['session_key'], session_id=session_id, async_delivery=False)
            return self.prepare_bound(binding['file'], request_id, profile, session_id, session_id,
                                      lease_session_id=binding['session_id'])
        finally:
            if tokens is not None:
                self.api.clear_session_vars(tokens)
            if terminal_token is not None:
                self.api.reset_terminal_scope(terminal_token)
            self.api.reset_hermes_home_override(home_token)

    def prepare_bound(self, file_id, request_id, profile, session_id, task_id, *, lease_session_id=None):
        identifier(request_id); identifier(task_id)
        sessions = self.authorized_sessions(profile, session_id)
        # HTTP renew/release carries the exact session in the grant, even after compression.
        binding = self.store.bound_file(file_id, profile,
                                        [lease_session_id] if lease_session_id in sessions else sessions)
        backend = self.api.terminal_env('TERMINAL_ENV', 'local')
        if backend not in ('local', 'docker'):
            raise RequestError(422, 'attachment_backend_unsupported')
        meta = self.store.metadata(file_id)
        lease = self.store.acquire_lease(file_id, profile, binding['session_id'], request_id)
        try:
            folder = safe_directory(self.home(profile) / 'cache' / 'documents' / 'deskrpg' /
                                    hashlib.sha256(binding['conversation'].encode()).hexdigest())
            extension = Path(meta['name']).suffix
            path = folder / (file_id + extension)
            if path.is_symlink():
                raise RequestError(400, 'attachment_invalid_file')
            data = self.store.read_file(file_id)
            quota = int(os.environ.get('DESKRPG_CONVERSATION_CACHE_QUOTA_BYTES', 268435456))
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                self.store._row(db, file_id)
                used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM copies WHERE profile=? AND file!=?', (profile, file_id)).fetchone()[0]
                if used + len(data) > quota:
                    raise RequestError(413, 'attachment_quota_exceeded')
                temp = folder / (str(uuid.uuid4()) + '.part')
                try:
                    with temp.open('xb') as stream:
                        stream.write(data); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temp, path)
                    db.execute('INSERT INTO copies VALUES(?,?,?,?) ON CONFLICT(file,profile) DO UPDATE SET path=excluded.path,bytes=excluded.bytes', (file_id, profile, str(path), len(data)))
                finally:
                    temp.unlink(missing_ok=True)
            visible = self.api.to_agent_visible_cache_path(str(path))
            # Only a server-generated absolute path reaches a quoted command. No user text is executable.
            if not isinstance(visible, str) or not visible.startswith('/'):
                raise RequestError(422, 'attachment_backend_unsupported')
            command = ('sha256sum ' if backend == 'docker' else 'shasum -a 256 ') + shlex.quote(visible)
            result = self.api.conversation_dispatch_tool('terminal', {'command': command, 'timeout': 30, 'workdir': '/'},
                                                        task_id=task_id, session_id=session_id)
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                raise RequestError(502, 'attachment_prepare_failed') from None
            output = result.get('output', '')
            if result.get('exit_code') != 0 or not isinstance(output, str) or output.split()[:1] != [meta['sha256']]:
                raise RequestError(502, 'attachment_prepare_failed')
            # Revocation or deletion while the backend was reading must not publish a ready receipt.
            self.store.bound_file(file_id, profile, self.authorized_sessions(profile, session_id))
            return {**meta, 'agentPath': visible, **lease}
        except Exception:
            # A concurrent/retried prepare may share an already-running lease. Failure must not
            # release that execution's protection; an unused reservation expires after ten minutes.
            raise
