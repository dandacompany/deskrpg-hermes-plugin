# deskrpg-hermes-plugin

DeskRPG 전용 라우트를 Hermes API Server 에 등록하는 Hermes 플러그인이다.
프로필 목록·생성·삭제, SOUL.md(인격) 읽기/쓰기, 프로필 설정(model/provider/toolsets)
읽기/쓰기 — 여덟 개 라우트를 제공한다.

## 요구사항

- Hermes `>=0.20.6`
- 런타임 의존은 `aiohttp` 와 `PyYAML` 뿐이다.

## 설치

```bash
hermes plugins install https://github.com/dandacompany/deskrpg-hermes-plugin
hermes plugins enable deskrpg
systemctl --user restart hermes-gateway   # 또는 게이트웨이를 다시 시작하는 방법
```

**`hermes plugins enable` 을 빠뜨리지 말 것.** `hermes plugins doctor deskrpg`
가 통과해도 런타임에 라우트가 붙는다는 뜻이 아니다 — doctor 는 플러그인
디렉토리를 직접 로드해 import/register 만 검사하고, 실제 게이트웨이는
`~/.hermes/config.yaml` 의 `plugins.enabled` 화이트리스트를 한 번 더 거른다.
설치만 하고 enable 을 건너뛰면 모든 라우트가 404 를 반환한다.

설치 확인:

```bash
hermes plugins doctor deskrpg
```

`OK: runtime discovery, manifest parsing, import, and registration passed` 가
나오면 된다. `registrations: 0 tool(s), 0 hook(s)` 는 정상이다 — 이 플러그인은
tool/hook 이 아니라 `api_server` 플랫폼 핸들러 하나만 등록한다.

## ⚠️ 경고 — 프로필 목록·생성·삭제는 게이트웨이 전체 권한이다

`GET/POST /deskrpg/profiles` 와 `DELETE /deskrpg/profiles/{name}` 은
**프리픽스 없는 경로**다. Hermes 의 API Server 는 프리픽스 없는 경로를
**default(리스너 소유자) 키**로 인증한다 — 특정 프로필의 키가 아니다.

즉 이 세 라우트를 호출할 수 있는 클라이언트는 게이트웨이에 등록된 **모든**
프로필을 만들고 지울 수 있다. `API_SERVER_KEY`(default 키)를 스코프가 좁은
클라이언트나 외부로 노출하지 말 것. 프로필 하나의 인격/설정만 다루는 나머지
다섯 라우트는 `/p/{profile}/...` 프리픽스가 있어 그 프로필의 키로만
인증된다 — **단, 이는 `gateway.multiplex_profiles` 가 켜진 게이트웨이에서만
참이다.** 이 옵션이 꺼진 게이트웨이(단일 프로필 구성이 많다)에서는 자기
프로필을 가리키는 `/p/{profile}/...` 프리픽스가 `None` 으로 해소되어, 그
프로필의 키가 아니라 **default 키**로 인증된다. 이런 게이트웨이에 프로필
키만 들고 접근하면 인증에 실패한다.

## 라우트

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/info` | default | 플러그인 버전과 라우트 목록 (**default 키 전용** — 프로필 키로는 발견에 쓸 수 없다) |
| GET | `/deskrpg/profiles` | default | 프로필 목록 (`hasCustomPersona` 포함) |
| POST | `/deskrpg/profiles` | default | 프로필 생성 (**응답이 새 키를 한 번만 싣는다**) |
| DELETE | `/deskrpg/profiles/{name}` | default | 프로필 삭제 (`?confirm={name}` 필수) |
| GET | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 읽기 (읽을 수 없으면 200 + `unreadable: true`) |
| PUT | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 쓰기 (`ifRevision` 필수 · 읽을 수 없으면 409 `identity_unreadable`) |
| GET | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 읽기 (읽을 수 없으면 200 + `unreadable: true`) |
| GET | `/p/{profile}/deskrpg/catalog` | profile | 모델·프로바이더·추론 강도 목록 |
| PUT | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 쓰기 (읽을 수 없으면 409 `config_unreadable`) |

### DELETE — `?confirm={name}` 필수

`DELETE /deskrpg/profiles/{name}` 은 쿼리스트링 `confirm` 이 경로의 이름과
정확히 일치해야 지운다. 없거나 다르면 400. `default` 프로필은 이 파라미터가
맞아도 삭제할 수 없다(400) — 리스너 소유자 프로필이 사라지면 게이트웨이 전체가
인증 기준을 잃는다.

성공하면 `{"name": ..., "removed": {"profileDir": true, "wrapperScript": <bool>}}`
를 돌려준다. `wrapperScript` 는 삭제 시점에 wrapper 스크립트가 실제로 있었고
지워졌으면 `true`, 애초에 없었으면 `false` 다.

**이 라우트는 Hermes 의 `delete_profile` 을 부르지 않는다 — 그 함수는 멀티플렉스
게이트웨이를 죽인다.** `_cleanup_gateway_service()` 가 지울 프로필의 서비스를
정리하려고 `os.environ["HERMES_HOME"]` 을 바꾼 뒤 `get_service_name()` 을 부르는데,
그 안의 `get_hermes_home()` 은 **컨텍스트 로컬 override → 환경변수 → 기본값** 순으로
해소한다(`hermes_constants.py:114`). 게이트웨이 안에서는 override 가 살아 있어
환경변수가 무시되고, 서비스 이름이 `hermes-gateway` — 지금 돌고 있는 게이트웨이
자신 — 으로 접힌다. 실측(v0.21.0):

```
⚠ Service cleanup: Command '['systemctl','--user','stop','hermes-gateway']'
    timed out after 10 seconds
✓ Removed /home/dante/.hermes/profiles/probe-tmp
Main process exited, code=exited, status=1/FAILURE
```

서빙 중이던 모든 프로필이 함께 멈췄다. `Restart=always` 가 걸려 있는데도 되살아나지
않는다 — 명시적 `systemctl stop` 에는 재시작 정책이 적용되지 않는다. `disable` 은
성공하므로 재부팅 생존성까지 잃는다. 유닛 파일 삭제는 그 앞의 타임아웃 예외가
우연히 막았다.

그래서 이 라우트는 **대상 프로필이 자기 유닛(`hermes-gateway-{name}.service`,
macOS 는 `ai.hermes.gateway-{name}.plist`)을 가졌는지 먼저 확인**하고,

- **가졌으면 아무것도 지우지 않고 409** `{"error": "profile_has_service", "unit": ...}`
  — 지우면 고아 유닛이 남는다. 셸에서 `hermes profile delete {name}` 로 정리하도록 안내한다.
- **없으면 디렉토리만 지운다.** 서비스 관리자를 아예 건드리지 않는다. DeskRPG 가 만드는
  NPC 프로필은 거의 항상 여기 해당한다.

디렉토리만 지워도 그 프로필은 다음 요청부터 사라진다 — 서빙 집합은 요청마다
`profiles_to_serve()` 로 다시 계산되기 때문이다(실측 확인).

### PUT identity — `ifRevision` 필수

기존 `SOUL.md` 를 읽을 수 없을 때(권한·인코딩 오류)는 config 와 같은 규약을
따른다: `GET` 은 500 대신 200 + `{"body": null, "isDefaultTemplate": null,
"revision": null, "unreadable": true}` 를 돌려주고, `PUT` 은 `409 {"error":
"identity_unreadable"}` 로 거절한다. `isDefaultTemplate` 이 `false` 가 아니라
`null` 인 것이 중요하다 — 읽기 실패가 "기본 템플릿이니 덮어써도 안전"으로
둔갑하면 사람이 쓴 인격을 모르고 지우게 된다. `revision: null` 을 받은
클라이언트는 그것을 `ifRevision` 에 실어 보내지 말고 사용자에게 파일 문제를
알려야 한다.

`PUT /p/{profile}/deskrpg/identity` 는 본문에 `ifRevision` 을 요구한다. 현재
SOUL.md 의 revision(내용의 SHA-256 앞 16자)과 일치하지 않으면 덮어쓰지 않고
409 + 현재 본문을 돌려준다 — 클라이언트가 먼저 GET 으로 최신 상태를 읽지
않고서는 쓸 수 없게 강제한다. 파일이 아직 없는 최초 생성만 예외로 허용된다
(지울 인격이 없으므로 유실 위험이 없다).

### PUT config — 값 타입까지 검사한다

`PUT /p/{profile}/deskrpg/config` 는 `{model, provider, toolsets}` 세 키만
받는다. 이 허용목록 밖의 키는 거절한다(400) — 임의 YAML 키 하나가 프로필을
못 뜨게 만들 수 있어서다. 키가 허용목록에 있어도 값의 타입이 틀리면 거절한다:
`model`/`provider` 는 비어 있지 않은 문자열, `toolsets` 는 문자열 리스트여야
한다.

기존 `config.yaml` 을 읽거나 해석할 수 없을 때(권한·인코딩·YAML 문법 오류·
최상위가 매핑이 아님 등)의 동작도 클라이언트가 다뤄야 한다: `GET` 은 500 대신
200 + `{"model": null, "provider": null, "toolsets": null, "unreadable": true}`
를 돌려주고, `PUT` 은 백업-후-덮어쓰기 대신 `409 {"error": "config_unreadable",
"reason": "..."}` 로 거절한다(망가진 파일 위에 쓰면 "성공"을 보고하면서 원본을
영영 잃을 수 있어서다).

### 생성 응답의 `apiKey` — 한 번만 지나간다

Hermes 의 `create_profile` 은 `.env` 를 **빈 파일로** 씨딩한다. 그런데 named
프로필의 API 인증은 fail-closed 라 자기 `.env` 의 `API_SERVER_KEY` 를 요구한다.
그래서 갓 만든 프로필은 **아무도 말을 걸 수 없는 상태**로 태어나고, 키를 넣으려면
셸로 들어가야 한다 — DeskRPG 가 사용자를 셸에서 빼내려고 이 플러그인을 쓴다는
점에서 그건 목적을 배반한다.

그래서 `POST /deskrpg/profiles` 는 강한 키(`secrets.token_urlsafe(32)`)를 만들어
프로필 `.env` 에 기록하고(기존 줄은 보존, 권한 0600), **201 응답 본문에 딱 한 번**
돌려준다:

```json
{"name": "olivia", "apiKey": "...", "keyIssued": true}
```

이것이 키가 평문으로 오가는 유일한 순간이다. 클라이언트는 즉시 저장해야 하며,
다시 조회할 방법은 없다. 서버는 이 값을 어떤 로그에도 남기지 않는다.

키 발급이 실패해도 응답은 **201 이다** — 프로필은 실제로 만들어졌기 때문이다.
그때는 `{"keyIssued": false, "keyError": "..."}` 가 오고 `apiKey` 는 없다.
500 으로 덮으면 사용자는 만들어진 프로필을 모른 채 같은 이름으로 다시 시도해
409 를 만나게 된다.

### GET catalog — 목록을 우리가 만들지 않는다

드롭다운에 채울 **프로바이더·모델·추론 강도**를 준다. 목록을 이 플러그인이 복제하지
않고 Hermes 자신의 것을 그대로 옮긴다:

- 프로바이더 — `hermes_cli.auth.PROVIDER_REGISTRY` (실측 79개)
- 인증 여부 — `get_auth_status(pid)` 의 `configured` **또는** `logged_in`
  (API 키형은 앞을, OAuth 형은 뒤를 채운다 — 한쪽만 보면 절반을 놓친다)
- 모델 — `model_setup_flows_common._models_dev_merged(pid, curated)`
  = models.dev 의 agentic 모델 + `model_catalog.get_catalog()` 의 큐레이션
- 추론 강도 — `hermes_cli/models.py:73` 과 `config_defaults.py:1244` 를 합친 값.
  Hermes 가 상수로 export 하지 않아 이 플러그인이 적어 두었다(늘어나면 갱신 필요)

그래서 Hermes 가 모델을 추가하거나 models.dev 가 갱신되면(20분 TTL) **자동으로
따라간다.** 우리가 목록을 들고 있으면 반드시 낡는다.

```json
{"providers": [{"id": "copilot", "name": "Copilot", "authenticated": true}, ...],
 "models": {"copilot": ["claude-opus-5", ...]},
 "reasoningEfforts": ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]}
```

**인증되지 않은 프로바이더도 목록에 남는다**(모델만 비운다). 지우면 사용자가 "왜 내가
쓰는 모델이 없지" 를 알 수 없다 — Hermes 의 CLI 피커도 못 쓰는 것을 회색으로 보여주지
지우지 않는다. 인증된 것이 목록 앞으로 온다(실측: 79개 중 4개).

**프로필 스코프인 이유**: `_auth_file_path()` 가 `HERMES_HOME` 을 따르므로 프로필별
인증이 가능한 구조다. 실측(2026-09-07)에서는 프로필끼리 같은 값이 나오는데, 프로필
`auth.json` 의 `providers` 가 비어 있고 `auth.py:467` 의 루트 폴백이 채우기 때문이다.
지금은 사실상 전역이지만, 프로필이 자기 자격증명을 갖는 순간 갈린다 — 그때 스코프를
좁히면 이미 쓰던 화면이 깨지므로 처음부터 좁게 둔다.

## 테스트

```bash
python -m pytest
```

`hermes plugins doctor` 는 Hermes 설치가 있어야 돌아가므로 CI 에 넣지 않았다.
설치 후 수동으로 위 명령을 돌려 확인할 것.
