"""Original conversation attachment limits and browser-safe metadata."""
from datetime import datetime, timezone

MAX_BYTES = 5 * 1024 * 1024
MAX_COUNT = 3
GRANT_TTL = 300
LEASE_TTL = 600
UNLINKED_TTL = 86400
FILE_FIELDS = ('id', 'name', 'mimeType', 'bytes', 'sha256')


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


def file_meta(row):
    return {key: row[key] for key in FILE_FIELDS}
