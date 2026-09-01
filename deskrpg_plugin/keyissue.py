"""새 프로필에 `API_SERVER_KEY` 를 발급한다.

**왜 필요한가.** Hermes 의 `create_profile` 은 `.env` 를 **빈 파일로** 씨딩한다
(`hermes_cli/profiles.py`: "Seed an empty .env so the profile has its own
credentials file from day one"). 그런데 named 프로필의 API 인증은 fail-closed 라
자기 `.env` 의 `API_SERVER_KEY` 를 요구한다(`api_server.py:_expected_api_key`).
그래서 갓 만든 프로필은 **아무도 말을 걸 수 없는 상태**로 태어난다. 키를 손으로
써 넣으려면 셸에 들어가야 하는데, DeskRPG 가 사용자를 셸에서 빼내려고 이
플러그인을 만들었다는 점에서 그건 목적을 정면으로 배반한다.

**왜 우리가 파일을 쓰는가.** Hermes 에는 프로필 `.env` 에 값을 넣는 공개
헬퍼가 없다 — 대시보드가 자체 라우트로 직접 쓴다(실측: `hermes_cli` 전체에
`set_env`/`write_env` 류 공개 함수 없음). 그래서 여기서도 직접 쓰되, 남의 파일을
다루는 규칙을 지킨다: **기존 줄은 한 줄도 잃지 않고**, 우리 키만 더하거나 바꾸며,
파일 권한은 0600 으로 조인다(Hermes 자신이 clone 후 `.env` 를 owner-only 로
조이는 것과 같은 이유).

**절대 로그에 남기지 않는다.** 이 모듈이 다루는 값은 그 자체로 게이트웨이의
한 프로필을 여는 자격증명이다. 예외 메시지에도 값이 새지 않게 한다.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILENAME = ".env"
KEY_NAME = "API_SERVER_KEY"

# Hermes 는 `has_usable_secret(key, min_length=16)` 를 요구한다. token_urlsafe(32) 는
# 43자 안팎이라 여유롭게 넘는다 — 길이를 줄이면 그 문턱에 걸릴 수 있으니 줄이지 말 것.
_TOKEN_BYTES = 32


class KeyIssueFailed(RuntimeError):
    """키를 발급하지 못했다. 프로필 자체는 이미 만들어졌을 수 있다.

    `reason` 은 사람이 읽을 설명이며 **키 값을 담지 않는다**.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def generate_key() -> str:
    """추측 불가능한 새 키."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _render(lines: list[str], key: str) -> list[str]:
    """`API_SERVER_KEY` 줄만 갈아끼운 새 줄 목록.

    같은 키가 여러 번 있으면 **첫 줄만** 바꾸고 나머지는 지운다 — dotenv 는
    나중 정의가 이기므로, 첫 줄만 고치고 뒷줄을 남기면 우리가 쓴 값이
    조용히 무시된다. 여기서 정확히 한 줄만 남기는 이유다.
    """
    out: list[str] = []
    written = False
    for line in lines:
        stripped = line.lstrip()
        # 주석 처리된 정의는 정의가 아니다 — 그대로 둔다.
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        name = stripped.split("=", 1)[0].strip()
        if name != KEY_NAME:
            out.append(line)
            continue
        if not written:
            out.append(f"{KEY_NAME}={key}")
            written = True
        # 두 번째 이후의 같은 키는 버린다.
    if not written:
        out.append(f"{KEY_NAME}={key}")
    return out


def write_key(profile_dir: Path, key: str) -> None:
    """프로필 `.env` 에 키를 기록한다. 기존 내용은 보존한다.

    실패는 `KeyIssueFailed` 로 올린다 — 호출자가 "프로필은 생겼는데 키는 없다"를
    사용자에게 정직하게 말할 수 있어야 하기 때문이다.
    """
    path = profile_dir / ENV_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (OSError, UnicodeDecodeError) as exc:
        raise KeyIssueFailed(f"{ENV_FILENAME} 을 읽을 수 없다: {type(exc).__name__}") from None

    body = "\n".join(_render(existing, key)) + "\n"
    try:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise KeyIssueFailed(f"{ENV_FILENAME} 을 쓸 수 없다: {type(exc).__name__}") from None

    # 값은 절대 찍지 않는다.
    logger.info("[deskrpg] %s 발급 완료: %s", KEY_NAME, profile_dir.name)


def issue(profile_dir: Path) -> str:
    """새 키를 만들어 기록하고 그 값을 돌려준다."""
    key = generate_key()
    write_key(profile_dir, key)
    return key
