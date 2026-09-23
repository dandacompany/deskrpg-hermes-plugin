"""프로필을 게이트웨이를 죽이지 않고 지운다.

**왜 Hermes 의 `delete_profile` 을 부르지 않는가.** 그 함수의
`_cleanup_gateway_service()` 는 지울 프로필의 서비스를 정리하려고
`os.environ["HERMES_HOME"]` 을 프로필 경로로 바꾼 뒤 `get_service_name()` 을 부른다.
그런데 그 안의 `get_hermes_home()` 은 **컨텍스트 로컬 override → 환경변수 → 기본값**
순으로 해소한다(`hermes_constants.py:114`). 멀티플렉스 게이트웨이 안에서는 override 가
살아 있으므로 **환경변수가 무시되고**, 접미사가 빈 문자열이 되어 서비스 이름이
`hermes-gateway` — 지금 돌고 있는 게이트웨이 자신 — 로 해소된다. 그 뒤:

    systemctl --user disable hermes-gateway   # 성공한다
    systemctl --user stop    hermes-gateway   # 자기가 자기를 멈추라 시켜 교착, 10초 타임아웃
    svc_file.unlink(missing_ok=True)          # 유닛 파일 삭제

실측(2026-09-01, v0.21.0): 프로필 하나를 지우자 게이트웨이가 죽었고, 서빙 중이던 모든
프로필이 함께 멈췄다. `Restart=always` 가 걸려 있는데도 되살아나지 않았다 — 명시적
`systemctl stop` 에는 재시작 정책이 적용되지 않기 때문이다. `disable` 은 성공해서
재부팅 생존성까지 잃었다.

**그래서 여기서는 두 가지를 한다.** (1) 대상 프로필이 자기 systemd/launchd 유닛을 가졌는지
먼저 확인하고, 가졌으면 **손대지 않고 거절**한다(그 정리는 사람이 셸에서 해야 한다).
(2) 없으면 디렉토리만 지운다 — 서비스 관리자를 아예 건드리지 않는다. DeskRPG 가 만드는
NPC 프로필은 거의 항상 (2)에 해당한다.

디렉토리만 지워도 그 프로필은 다음 요청부터 사라진다. 서빙 집합은 요청마다
`profiles_to_serve()` 로 다시 계산되기 때문이다(실측으로 확인).
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# hermes_cli/gateway.py 의 _SERVICE_BASE 와 같은 값. 프로필별 유닛은 여기에 프로필
# 이름을 붙인 형태다(`get_service_name`: 기본 홈은 접미사 없음, 프로필은 그 이름).
SERVICE_BASE = "hermes-gateway"

# launchd 는 이름 규칙이 다르다. `get_launchd_plist_path`: 기본 홈은
# `ai.hermes.gateway.plist`, 프로필은 `ai.hermes.gateway-<name>.plist`.
LAUNCHD_BASE = "ai.hermes.gateway"


class ProfileHasService(RuntimeError):
    """이 프로필은 자기 서비스를 갖고 있다 — 우리가 지우면 고아 유닛이 남는다."""

    def __init__(self, unit: str, path: Path) -> None:
        super().__init__(unit)
        self.unit = unit
        self.path = path


def user_home() -> Path:
    """유닛 파일을 찾을 홈 디렉토리.

    별도 함수인 이유는 이것이 **환경에 대한 가정이 들어가는 유일한 지점**이기
    때문이다 — 서비스 관리자의 유닛이 어디 사는지는 배포 형태에 따라 달라진다.
    한 곳에 모아두면 나중에 바꿀 자리가 분명하고, 테스트가 가짜 홈을 넣을 수 있다.
    """
    return Path.home()


def service_unit_name(profile: str) -> str:
    """프로필 전용 서비스 이름.

    Hermes 의 `get_service_name()` 을 **다시 구현하지 않고 결과만 맞춘다** — 그 함수는
    프로세스의 현재 홈에 의존하는데(위 주석의 결함), 우리는 지울 프로필의 이름을
    이미 알고 있으므로 그 의존을 통째로 피한다.
    """
    return f"{SERVICE_BASE}-{profile}"


def _candidate_unit_paths(profile: str, home: Path) -> list[Path]:
    unit = service_unit_name(profile)
    if sys.platform == "darwin":
        return [home / "Library" / "LaunchAgents" / f"{LAUNCHD_BASE}-{profile}.plist"]
    return [
        home / ".config" / "systemd" / "user" / f"{unit}.service",
        Path("/etc/systemd/system") / f"{unit}.service",
    ]


def find_own_service(profile: str, home: Path) -> Path | None:
    """이 프로필의 전용 유닛 파일. 없으면 None."""
    for path in _candidate_unit_paths(profile, home):
        try:
            if path.exists():
                return path
        except OSError:
            # 권한 때문에 확인조차 못 하면 "없다"로 단정하지 않는다 — 있다고 보고
            # 거절하는 쪽이 안전하다. 여기서 그 경로를 그대로 돌려준다.
            return path
    return None


def delete_profile_tree(profile: str, profile_dir: Path, home: Path | None = None) -> None:
    """서비스 관리자를 건드리지 않고 프로필 디렉토리를 지운다.

    전용 유닛이 있으면 `ProfileHasService` 를 올리고 **아무것도 지우지 않는다**.
    """
    if profile == "default":
        # 호출자도 막지만, 이 함수만 떼어 쓰는 미래를 위해 여기서도 막는다.
        raise ValueError("default profile cannot be deleted")

    unit = find_own_service(profile, home if home is not None else user_home())
    if unit is not None:
        raise ProfileHasService(service_unit_name(profile), unit)

    shutil.rmtree(profile_dir)
    logger.warning("[deskrpg] profile directory deleted: %s", profile)
