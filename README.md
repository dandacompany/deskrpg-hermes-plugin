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
인증된다.

## 라우트

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/info` | default | 플러그인 버전과 라우트 목록 (**default 키 전용** — 프로필 키로는 발견에 쓸 수 없다) |
| GET | `/deskrpg/profiles` | default | 프로필 목록 (`hasCustomPersona` 포함) |
| POST | `/deskrpg/profiles` | default | 프로필 생성 |
| DELETE | `/deskrpg/profiles/{name}` | default | 프로필 삭제 (`?confirm={name}` 필수) |
| GET | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 읽기 |
| PUT | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 쓰기 (`ifRevision` 필수) |
| GET | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 읽기 |
| PUT | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 쓰기 |

### DELETE — `?confirm={name}` 필수

`DELETE /deskrpg/profiles/{name}` 은 쿼리스트링 `confirm` 이 경로의 이름과
정확히 일치해야 지운다. 없거나 다르면 400. `default` 프로필은 이 파라미터가
맞아도 삭제할 수 없다(400) — 리스너 소유자 프로필이 사라지면 게이트웨이 전체가
인증 기준을 잃는다.

성공하면 `{"name": ..., "removed": {"profileDir": true, "wrapperScript": <bool>}}`
를 돌려준다. `wrapperScript` 는 삭제 시점에 wrapper 스크립트가 실제로
있었고 지워졌으면 `true`, 애초에 없었으면 `false` 다 — 이 값을 진짜로 만들려면
플러그인이 Hermes 의 `delete_profile` 보다 **먼저** wrapper 를 지워 결과를
관찰해야 한다(`delete_profile` 자신도 뒤에 wrapper 정리를 시도하므로, 순서를
반대로 하면 이 필드는 항상 `false` 로 거짓말한다).

### PUT identity — `ifRevision` 필수

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

## 테스트

```bash
python -m pytest
```

`hermes plugins doctor` 는 Hermes 설치가 있어야 돌아가므로 CI 에 넣지 않았다.
설치 후 수동으로 위 명령을 돌려 확인할 것.
