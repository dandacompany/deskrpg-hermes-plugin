"""모델·프로바이더·추론 강도 목록을 화면에 넘긴다.

**목록을 우리가 만들지 않는다.** Hermes 자신의 카탈로그·프로바이더 레지스트리·인증
상태를 그대로 읽어 옮긴다. 그래야 Hermes 가 모델을 추가하거나 models.dev 가 갱신될 때
(20분 TTL) DeskRPG 가 자동으로 따라간다 — 우리가 목록을 복제해 두면 반드시 낡는다.

**프로필 스코프로 둔다.** `_auth_file_path()` 는 `HERMES_HOME` 을
따라 `~/.hermes/profiles/<name>/auth.json` 을 가리키므로 프로필별 인증이 **가능한** 구조다.
다만 실측(2026-09-07, 이 서버)에서는 sophie·mia·oliver 가 **같은 4개**(bedrock, copilot,
openai-codex, opencode-free)를 준다 — 프로필 `auth.json` 의 `providers` 가 전부 비어 있고
`auth.py:467` 의 루트 폴백("read-only fallback")과 credential_pool·환경변수가 채우기
때문이다. 즉 **현재는 사실상 전역이다.**

**그 관찰은 틀렸다(2026-09-17 정정).** 게이트웨이 프로세스 하나가 모든 `/p/{profile}` 요청을
받는데 이 핸들러가 홈을 갈아 끼우지 않아, 세 프로필 모두 **default 의 인증 상태**를 본 것이다.
Hostinger VPS(0.21.3 컨테이너)에서 default 로만 Codex 로그인한 뒤 noah 를 채용하자, 화면은
openai-codex 를 "인증됨" 으로 보여줬지만 실제 대화는 "No Codex credentials stored" 로 실패했다.
Hermes 는 NPC(프로필)마다 로그인한다 — 그래서 크론과 같은 방식으로 요청 프로필의 홈에서 읽는다.

**인증 여부를 숨기지 않고 함께 보낸다.** 인증되지 않은 프로바이더를 목록에서 지우면
사용자는 "왜 내가 쓰는 모델이 없지" 를 알 수 없다. Hermes 의 CLI 피커도 못 쓰는 것을
회색으로 보여주지 지우지 않는다 — 같은 태도를 따른다.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .common import guarded, run_blocking
from .contract_fields import in_app_device_login
from .cron import resolve_profile_home

logger = logging.getLogger(__name__)

# `hermes_cli/models.py:73` 의 COPILOT_REASONING_EFFORTS_GPT5 와
# `config_defaults.py:1244` 의 서브에이전트 effort 주석을 합친 값.
# Hermes 가 이 목록을 상수로 export 하지 않아 여기 적는다 — 그래서 **출처를 함께**
# 남긴다. 늘어나면 두 곳을 보고 갱신해야 한다.
REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def _auth_fields(api, pid: str, cfg, profile: str) -> dict:
    """프로바이더의 인증 방식 — 화면이 로그인 버튼·키 입력·CLI 안내 중 무엇을 보일지 정한다."""
    auth_type = getattr(cfg, "auth_type", None)
    env_vars = [str(n) for n in (getattr(cfg, "api_key_env_vars", None) or ()) if n]
    if api is not None and auth_type == "api_key" and env_vars:
        return {"authType": "api_key", "envVars": env_vars, "cliCommand": None}
    if in_app_device_login(api, pid):
        return {"authType": "oauth_device", "envVars": [], "cliCommand": None}
    command = None
    for entry in getattr(api, "_OAUTH_PROVIDER_CATALOG", None) or ():
        if isinstance(entry, dict) and entry.get("id") == pid and entry.get("cli_command"):
            command = str(entry["cli_command"])
            if command.startswith("hermes ") and profile != "default":
                command = f"hermes -p {profile} " + command[len("hermes "):]
            break
    return {"authType": "external", "envVars": [], "cliCommand": command}


def _provider_rows(api, profile: str) -> list[dict]:
    """프로바이더 목록 + 이 프로필의 인증 상태."""
    from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status

    rows = []
    for pid, cfg in PROVIDER_REGISTRY.items():
        authed = False
        try:
            st = get_auth_status(pid) or {}
            # `configured` 와 `logged_in` 은 프로바이더 종류마다 채워지는 쪽이 다르다
            # (API 키형은 configured, OAuth 형은 logged_in). 둘 중 하나면 쓸 수 있다.
            authed = bool(st.get("configured")) or bool(st.get("logged_in"))
        except Exception:
            # 한 프로바이더의 상태 조회 실패가 목록 전체를 죽이면 안 된다.
            # 모르면 "인증 안 됨" 으로 두되, 그 사실이 화면에 보인다.
            pass
        rows.append(
            {
                "id": pid,
                "name": getattr(cfg, "name", pid) or pid,
                "authenticated": authed,
                **_auth_fields(api, pid, cfg, profile),
            }
        )
    rows.sort(key=lambda r: (not r["authenticated"], r["id"]))
    return rows


def _models_for(provider_id: str) -> list[str]:
    """그 프로바이더의 모델 목록 — `hermes model` 피커가 쓰는 목록을 그대로 옮긴다.

    `cached_provider_model_ids` 는 프로바이더마다 전용 fetcher 로 분기한다
    (`hermes_cli/models.py:1421-1439`). 그래서 **인증 방식을 안다** — openai-codex 는
    `_codex_catalog` → `get_codex_model_ids(access_token)` 로 그 계정의 live 카탈로그를
    받아 backend `priority` 순으로 준다(`hermes_cli/codex_models.py:126-168`).

    옛 경로(큐레이션 + `_models_dev_merged`)는 인증 방식을 몰랐다. 그래서 ChatGPT 계정으로
    로그인한 프로필에도 models.dev 의 OpenAI 공개 API 목록이 나왔고, 거기서 고른
    `gpt-5.3-codex-spark` 는 대화할 때 400 "not supported when using Codex with a ChatGPT
    account" 로 처음 드러났다(2026-09-20 실측). 순서도 models.dev 삽입 순서
    그대로여서 세대·계열이 뒤섞였다.

    **우리가 다시 정렬하지 않는다.** 이 목록의 순서는 Hermes 가 의도한 순서다(codex 는
    priority 순, 일부 프로바이더는 큐레이션 우선). 여기서 다시 섞으면 CLI 피커와 순서가
    갈리고 그 의도를 잃는다.
    """
    try:
        from hermes_cli.models import cached_provider_model_ids

        models = [str(m) for m in (cached_provider_model_ids(provider_id) or []) if m]
        if models:
            return models
    except Exception:
        # 낡은 Hermes 빌드에 이 함수가 없거나 조회가 실패할 수 있다 — 아래 폴백으로 내려간다.
        pass

    curated: list[str] = []
    try:
        from hermes_cli.model_catalog import get_catalog

        entry = (get_catalog().get("providers") or {}).get(provider_id) or {}
        raw = entry.get("models") or []
        curated = [m.get("id") if isinstance(m, dict) else str(m) for m in raw]
        curated = [m for m in curated if m]
    except Exception:
        pass

    try:
        from hermes_cli.model_setup_flows_common import _models_dev_merged

        merged = _models_dev_merged(provider_id, curated)
        if merged:
            return merged
    except Exception:
        pass

    # models.dev 가 없거나 실패하면 큐레이션만이라도 준다. 빈 목록은 화면이
    # "직접 입력" 으로 떨어뜨릴 신호이지, 오류가 아니다.
    return curated


def _catalog_for_home(api, home, profile: str) -> dict:
    """요청 프로필의 홈으로 HERMES_HOME 을 갈아 끼운 채 목록을 만든다.

    오버라이드는 컨텍스트 변수라 이 워커 스레드에만 걸리고, 블록을 나가면 원상복구한다.
    """
    token = api.set_hermes_home_override(str(home))
    try:
        providers = _provider_rows(api, profile=profile)
        models: dict[str, list[str]] = {}
        for row in providers:
            # 인증된 프로바이더만 모델을 채운다. 79개 전부를 채우면 응답이 거대해지고
            # models.dev 왕복이 그만큼 늘어난다 — 화면이 실제로 고를 수 있는 것만 준다.
            if row["authenticated"]:
                models[row["id"]] = _models_for(row["id"])
    finally:
        api.reset_hermes_home_override(token)
    return {"providers": providers, "models": models, "reasoningEfforts": REASONING_EFFORTS}


def get_handler(api):
    """`GET /p/{profile}/deskrpg/catalog`"""

    @guarded
    async def handler(request):
        profile = api.normalize_profile_name(request.match_info["profile"])
        home = resolve_profile_home(api, profile)
        return web.json_response(await run_blocking(_catalog_for_home, api, home, profile))

    return handler
