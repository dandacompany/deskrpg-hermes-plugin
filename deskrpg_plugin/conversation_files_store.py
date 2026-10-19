"""Hermes-owned originals, grants and execution leases. No core database writes."""
import contextlib
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from .common import RequestError
from .conversation_files_types import GRANT_TTL, LEASE_TTL, MAX_BYTES, MAX_COUNT, UNLINKED_TTL, file_meta, timestamp

_BOOT_TIME = time.time()


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise RequestError(400, 'attachment_invalid_selection')
    return value


def safe_directory(path):
    path = Path(path)
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise RequestError(400, 'attachment_invalid_file')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def store_root(api):
    return Path(api.get_profile_dir('default')) / 'deskrpg' / 'conversation-files'


class FileStore:
    def __init__(self, root, *, quota=None):
        self.root = safe_directory(root)
        self.blobs = safe_directory(self.root / 'blobs')
        self.quota = quota if quota is not None else int(os.environ.get('DESKRPG_CONVERSATION_FILE_QUOTA_BYTES', 1073741824))
        self.db_path = self.root / 'manifest.sqlite'
        if self.db_path.is_symlink():
            raise RequestError(400, 'attachment_invalid_file')
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, request TEXT NOT NULL,
                    name TEXT NOT NULL, mimeType TEXT NOT NULL, bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, created REAL NOT NULL, linked INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(conversation, request));
                CREATE TABLE IF NOT EXISTS links (
                    file TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(file,message));
                CREATE TABLE IF NOT EXISTS grants (
                    digest TEXT PRIMARY KEY, file TEXT NOT NULL, conversation TEXT NOT NULL,
                    profile TEXT NOT NULL, session_key TEXT NOT NULL, session_id TEXT NOT NULL,
                    expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS bindings (
                    file TEXT NOT NULL, conversation TEXT NOT NULL, profile TEXT NOT NULL,
                    session_key TEXT NOT NULL, session_id TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(file,profile,session_id));
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY, file TEXT NOT NULL, profile TEXT NOT NULL,
                    session_id TEXT NOT NULL, request TEXT NOT NULL, expires REAL NOT NULL,
                    released INTEGER NOT NULL DEFAULT 0, UNIQUE(profile,session_id,request,file));
                CREATE TABLE IF NOT EXISTS prepare_requests (
                    profile TEXT NOT NULL, request TEXT NOT NULL, file TEXT NOT NULL, session_id TEXT NOT NULL,
                    PRIMARY KEY(profile,request));
                CREATE TABLE IF NOT EXISTS copies (
                    file TEXT NOT NULL, profile TEXT NOT NULL, path TEXT NOT NULL,
                    bytes INTEGER NOT NULL, PRIMARY KEY(file,profile));
            ''')

    @contextlib.contextmanager
    def connection(self):
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def blob_path(self, file_id):
        try:
            if str(uuid.UUID(file_id)) != file_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise RequestError(404, 'attachment_not_found') from None
        path = self.blobs / file_id
        if path.is_symlink():
            raise RequestError(404, 'attachment_not_found')
        return path

    def _row(self, db, file_id, *, deleted=False):
        row = db.execute('SELECT * FROM files WHERE id=?', (identifier(file_id),)).fetchone()
        if row is None or (row['deleted'] and not deleted):
            raise RequestError(404, 'attachment_not_found')
        return row

    def store_file(self, conversation, request, name, mime_type, chunks, *, now=None):
        identifier(conversation); identifier(request)
        if not isinstance(name, str) or not name or len(name) > 255 or name in {'.', '..'} or any(c in name for c in '/\\') or any(ord(c) < 32 for c in name):
            raise RequestError(400, 'attachment_invalid_file')
        identifier(mime_type)
        body = bytearray()
        for chunk in chunks:
            if len(body) + len(chunk) > MAX_BYTES:
                raise RequestError(413, 'attachment_too_large')
            body.extend(chunk)
        digest = hashlib.sha256(body).hexdigest()
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM files WHERE conversation=? AND request=?', (conversation, request)).fetchone()
            if old:
                if old['deleted'] or (old['sha256'], old['name'], old['mimeType']) != (digest, name, mime_type):
                    raise RequestError(409, 'attachment_conflict')
                if not self.blob_path(old['id']).is_file():
                    raise RequestError(502, 'attachment_prepare_failed')
                return file_meta(old), False
            used = db.execute('SELECT COALESCE(SUM(bytes),0) FROM files').fetchone()[0]
            if used + len(body) > self.quota:
                raise RequestError(413, 'attachment_quota_exceeded')
            fid = str(uuid.uuid4()); target = self.blob_path(fid)
            temp = self.blobs / (fid + '.part')
            try:
                with temp.open('xb') as stream:
                    stream.write(body); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, target)
                db.execute('INSERT INTO files(id,conversation,request,name,mimeType,bytes,sha256,created) VALUES(?,?,?,?,?,?,?,?)',
                           (fid, conversation, request, name, mime_type, len(body), digest, now))
            except Exception:
                temp.unlink(missing_ok=True); target.unlink(missing_ok=True)
                raise
            return file_meta(self._row(db, fid)), True

    def metadata(self, file_id):
        with self.connection() as db:
            return file_meta(self._row(db, file_id))

    def read_file(self, file_id):
        with self.connection() as db:
            row = self._row(db, file_id)
            path = self.blob_path(file_id)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    data = stream.read(MAX_BYTES + 1)
            except OSError:
                raise RequestError(404, 'attachment_not_found') from None
            if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise RequestError(422, 'attachment_checksum_mismatch')
            return data

    def link_file(self, file_id, conversation, message):
        identifier(message)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, file_id)
            if row['conversation'] != conversation:
                raise RequestError(404, 'attachment_not_found')
            existing = db.execute('SELECT 1 FROM links WHERE file=? AND message=?', (file_id, message)).fetchone()
            count = db.execute('SELECT COUNT(*) FROM links l JOIN files f ON f.id=l.file WHERE l.message=? AND f.conversation=? AND f.deleted=0', (message, conversation)).fetchone()[0]
            if not existing and count >= MAX_COUNT:
                raise RequestError(400, 'attachment_count_exceeded')
            db.execute('INSERT OR IGNORE INTO links VALUES(?,?)', (file_id, message))
            db.execute('UPDATE files SET linked=1 WHERE id=?', (file_id,))
        return {'state': 'linked'}

    def issue_grant(self, file_id, conversation, profile, session_key, session_id, *, now=None):
        for value in (profile, session_key, session_id):
            identifier(value)
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, file_id)
            if row['conversation'] != conversation:
                raise RequestError(404, 'attachment_not_found')
            existing = db.execute('SELECT conversation FROM bindings WHERE profile=? AND session_id=? AND revoked=0 LIMIT 1', (profile, session_id)).fetchone()
            if existing and existing['conversation'] != conversation:
                raise RequestError(409, 'attachment_conflict')
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,0)', (hashlib.sha256(token.encode()).hexdigest(), file_id, conversation, profile, session_key, session_id, now + GRANT_TTL))
            db.execute('INSERT INTO bindings VALUES(?,?,?,?,?,0) ON CONFLICT(file,profile,session_id) DO UPDATE SET revoked=0,session_key=excluded.session_key', (file_id, conversation, profile, session_key, session_id))
        return {'grant': token, 'expiresAt': timestamp(now + GRANT_TTL)}

    def authorize_grant(self, token, profile, *, now=None):
        identifier(token)
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute('SELECT * FROM grants WHERE digest=? AND profile=? AND revoked=0 AND expires>?', (hashlib.sha256(token.encode()).hexdigest(), profile, now)).fetchone()
            if row is None:
                raise RequestError(404, 'attachment_not_found')
            self._row(db, row['file'])
            return dict(row)

    def list_bound(self, profile, session_ids):
        if not session_ids:
            return []
        with self.connection() as db:
            rows = db.execute('SELECT DISTINCT f.* FROM files f JOIN bindings b ON b.file=f.id WHERE b.profile=? AND b.session_id IN (' + ','.join('?' for _ in session_ids) + ') AND b.revoked=0 AND f.deleted=0 ORDER BY f.created,f.id', (profile, *session_ids)).fetchall()
            return [file_meta(row) for row in rows]

    def bound_file(self, file_id, profile, session_ids):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM bindings WHERE file=? AND profile=? AND revoked=0', (file_id, profile)).fetchall()
            row = next((r for r in rows if r['session_id'] in session_ids), None)
            if row is None:
                raise RequestError(404, 'attachment_not_found')
            self._row(db, file_id)
            return dict(row)

    def revoke_binding(self, conversation, profile, session_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('bindings', 'grants'):
                db.execute(f'UPDATE {table} SET revoked=1 WHERE conversation=? AND profile=? AND session_id=?', (conversation, profile, session_id))
        return {'state': 'revoked'}

    def acquire_lease(self, file_id, profile, session_id, request, *, now=None):
        identifier(request)
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE'); self._row(db, file_id)
            previous = db.execute('SELECT file,session_id FROM prepare_requests WHERE profile=? AND request=?', (profile, request)).fetchone()
            if previous and (previous['file'], previous['session_id']) != (file_id, session_id):
                raise RequestError(409, 'attachment_conflict')
            db.execute('INSERT OR IGNORE INTO prepare_requests VALUES(?,?,?,?)', (profile, request, file_id, session_id))
            old = db.execute('SELECT * FROM leases WHERE profile=? AND session_id=? AND request=? AND file=?', (profile, session_id, request, file_id)).fetchone()
            lid = old['id'] if old else str(uuid.uuid4())
            db.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires,released=0', (lid, file_id, profile, session_id, request, now + LEASE_TTL))
        return {'leaseId': lid, 'expiresAt': timestamp(now + LEASE_TTL)}

    def renew_lease(self, lease_id, profile, session_id, request, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM leases WHERE id=? AND profile=? AND session_id=? AND request=? AND released=0 AND expires>?', (lease_id, profile, session_id, request, now)).fetchone()
            if row is None:
                raise RequestError(404, 'attachment_not_found')
            db.execute('UPDATE leases SET expires=? WHERE id=?', (now + LEASE_TTL, lease_id))
        return {'leaseId': lease_id, 'expiresAt': timestamp(now + LEASE_TTL)}

    def release_lease(self, lease_id, profile, session_id, request, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM leases WHERE id=? AND profile=? AND session_id=? AND request=?', (lease_id, profile, session_id, request)).fetchone()
            if row is None:
                raise RequestError(404, 'attachment_not_found')
            db.execute('UPDATE leases SET released=1 WHERE id=?', (lease_id,))
            pending = db.execute('SELECT deleted FROM files WHERE id=?', (row['file'],)).fetchone()
            if pending and pending['deleted']:
                return self._delete_ready(db, row['file'], now)
        return {'state': 'released'}

    def _delete_ready(self, db, file_id, now):
        if db.execute('SELECT 1 FROM leases WHERE file=? AND released=0 AND expires>?', (file_id, now)).fetchone():
            return {'state': 'pending'}
        for row in db.execute('SELECT path FROM copies WHERE file=?', (file_id,)).fetchall():
            path = Path(row['path'])
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise RequestError(400, 'attachment_invalid_file')
            path.unlink(missing_ok=True)
        self.blob_path(file_id).unlink(missing_ok=True)
        for table in ('copies', 'grants', 'bindings', 'links'):
            db.execute(f'DELETE FROM {table} WHERE file=?', (file_id,))
        # Keep the request tombstone to prevent a deleted upload being resurrected by retry.
        db.execute('UPDATE files SET bytes=0 WHERE id=?', (file_id,))
        return {'state': 'deleted'}

    def delete_file(self, file_id, *, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE'); self._row(db, file_id, deleted=True)
            db.execute('UPDATE files SET deleted=1 WHERE id=?', (file_id,))
            db.execute('UPDATE grants SET revoked=1 WHERE file=?', (file_id,))
            db.execute('UPDATE bindings SET revoked=1 WHERE file=?', (file_id,))
            return self._delete_ready(db, file_id, now)

    def sweep_files(self, *, now=None, grace=True):
        now = time.time() if now is None else now
        if grace and now < _BOOT_TIME + LEASE_TTL:
            return {'deleted': 0}
        with self.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM files f WHERE bytes>0 AND (deleted=1 OR (linked=0 AND created<? AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.file=f.id AND l.released=0 AND l.expires>?)))', (now - UNLINKED_TTL, now))]
        count = 0
        for fid in ids:
            count += self.delete_file(fid, now=now)['state'] == 'deleted'
        # Reconcile files left by a crash between rename and transaction commit.
        with self.connection() as db:
            known = {r[0] for r in db.execute('SELECT id FROM files')}
            for path in self.blobs.iterdir():
                if path.name not in known and not path.is_symlink() and path.stat().st_mtime < now - UNLINKED_TTL:
                    path.unlink()
        return {'deleted': count}
