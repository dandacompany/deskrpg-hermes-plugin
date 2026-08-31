"""DeskRPG 전용 라우트를 Hermes API Server 에 등록하는 플러그인."""

import logging

from ._hermes_api import MissingHermesApi, load

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    """플러그인 진입점.

    라우트 등록은 어댑터의 connect() 시점에 일어난다 — 여기서는 팩토리만 넘긴다.
    필요한 Hermes 내부 API 가 없으면 여기서 던진다. Hermes 는 각 플러그인의
    register 를 격리하므로 게이트웨이는 정상 기동하고 로그에만 남는다.
    """
    api = load()  # 없으면 MissingHermesApi 를 던진다

    def _wire(native, adapter) -> None:
        # native 는 라우터가 얼기 전의 aiohttp web.Application 이다.
        if native is None:
            logger.warning("[deskrpg] api_server 가 native app 을 주지 않았다 — 등록을 건너뛴다")
            return
        from .routes import attach

        attach(native, adapter, api)

    ctx.register_platform_handler("api_server", _wire)
