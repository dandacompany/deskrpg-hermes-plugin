"""프로필 `.env` 를 줄 단위로 다룬다 — 남의 파일을 고치는 규칙.

기존 줄은 한 줄도 잃지 않는다(주석·빈 줄·모르는 키). 우리가 쓰는 키만 갈아 끼우고, 같은 키가 여러 번 있으면
**첫 자리에 한 줄만** 남긴다 — dotenv 는 나중 정의가 이기므로 뒷줄을 남기면 우리가 쓴 값이 조용히 무시된다.
권한은 0600, 교체는 원자적이다(임시 파일 → `os.replace`).

**값을 해석하지 않는다.** 복제는 원문 줄을 그대로 옮긴다 — 따옴표·이스케이프를 다시 조립하다 값을 바꾸지 않으려고.
이 모듈은 어떤 값도 로그에 남기지 않는다. 여기서 나가는 예외는 `OSError`/`UnicodeDecodeError` 뿐이고,
호출자는 사유에 **예외 타입 이름만** 싣는다.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

# `#` 로 시작하는 주석 줄은 정의가 아니다 — 정규식이 첫 비공백 문자로 식별자를 요구해 자연히 걸러진다.
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _key_of(line: str) -> str | None:
    match = _ASSIGN_RE.match(line)
    return match.group(1) if match else None


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def read_assignments(path: Path) -> dict[str, str]:
    """`{키: 원문 줄}` — 같은 키는 마지막 줄이 이긴다(dotenv 와 같다)."""
    out: dict[str, str] = {}
    for line in _read_lines(path):
        key = _key_of(line)
        if key:
            out[key] = line.strip()
    return out


def write_text_atomic(path: Path, text: str) -> None:
    """`text` 를 0600 임시 파일에 쓰고 `os.replace` 로 갈아 끼운다.

    `.env` 뿐 아니라 키가 인라인으로 들어갈 수 있는 파일(복제한 config.yaml 의 `providers`)도 쓴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        # mkstemp 는 0600 으로 만든다 — 값이 담긴 채로 넓은 권한에 놓이는 순간이 없다.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write(path: Path, lines: list[str]) -> None:
    write_text_atomic(path, "".join(f"{line}\n" for line in lines))


def upsert_lines(path: Path, lines_by_key: dict[str, str]) -> None:
    """키마다 원문 줄을 넣는다. 있던 자리(첫 정의)를 갈아 끼우고, 없으면 끝에 붙인다."""
    pending = dict(lines_by_key)
    out: list[str] = []
    for line in _read_lines(path):
        key = _key_of(line)
        if key in lines_by_key:
            if key in pending:
                out.append(pending.pop(key))
            continue  # 같은 키의 둘째 줄부터는 버린다
        out.append(line)
    out.extend(pending[key] for key in lines_by_key if key in pending)
    _write(path, out)


def remove_keys(path: Path, keys) -> list[str]:
    """키의 모든 정의 줄을 지우고, 실제로 지운 이름을 처음 만난 순서로 돌려준다."""
    wanted = set(keys)
    removed: list[str] = []
    out: list[str] = []
    for line in _read_lines(path):
        key = _key_of(line)
        if key in wanted:
            if key not in removed:
                removed.append(key)
            continue
        out.append(line)
    if removed:
        _write(path, out)
    return removed
