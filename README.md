# deskrpg-hermes-plugin

DeskRPG 전용 라우트를 Hermes API Server 에 등록하는 Hermes 플러그인이다.
프로필 목록·생성·삭제, SOUL.md(인격) 읽기/쓰기, 프로필 설정 읽기/쓰기에 더해
**0.6.0 부터 칸반(보드·카드·동작·첨부·디스패치)·통합 사건 스트림·크론(프로필별 잡)** 을
제공한다. 0.8.0 부터 **아티팩트**(저장소·`artifact_save` 도구·`post_tool_call` 자동 승격)까지, 0.8.1 부터 **응답 속 큰 코드 블록 자동 승격**(`post_llm_call`, `HERMES_DESKRPG_CAPTURE_RESPONSES=0` 으로 끔)까지, 0.8.2 부터 **보존 규칙**(git 저장소 안 파일·설정 파일은 자동 승격에서 제외, 아티팩트당 최근 20 버전만 파일을 남김 — `HERMES_DESKRPG_ARTIFACT_MAX_VERSIONS`, 0 은 무제한)까지, 0.8.3 부터 **링크 아티팩트**(답변·도구 결과의 http(s) 링크를 `link` 로 자동 저장, `artifact_save url=` 명시 저장 — `HERMES_DESKRPG_CAPTURE_LINKS=0` 으로 자동 수집을 끔)까지, 0.8.4 부터 목록 `task_id=` 필터까지, 0.9.0 부터 직원 설정 피커·기본 프로필 복제·프로바이더 인증까지 더해
쉰아홉 개 라우트다. DeskRPG 가 Hermes 관리 화면(대시보드) 없이 자기 화면에서
칸반과 크론을 보고 조작하도록, DeskRPG 설계 문서 부록 A 의 HTTP 계약을 그대로 낸다.

- **직원 설정 피커(0.9.0)** — `GET /p/{profile}/deskrpg/toolsets`·`/skills` 가 Hermes 의 툴셋·스킬 목록을 프로필 기준으로 돌려주고,
  `PUT …/config` 가 `enabledToolsets`(대화·크론·칸반 워커에 같은 목록)와 `disabledSkills` 를 받는다.
  `POST /deskrpg/profiles {"cloneFrom": "default"}` 는 기본 프로필의 모델 설정과 모델 프로바이더 키만 물려준다 —
  봇 토큰·OAuth 로그인·인격·메모리는 복사하지 않는다.
- **프로바이더 인증(0.9.0)** — `PUT/DELETE /p/{profile}/deskrpg/provider-keys/{provider}` 로 모델 프로바이더 API 키를
  프로필 `.env` 에 쓰기 전용으로 넣고(키 이름은 서버가 정한다), `/p/{profile}/deskrpg/oauth/*` 로 Hermes 대시보드와 같은
  디바이스 코드 로그인을 한다(앱 안에서는 Codex 만 — Nous·xAI·MiniMax 는 `cliCommand` 로 CLI 로그인을 안내한다). 카탈로그 행은 `authType`·`envVars`·`cliCommand` 를 싣는다.
- **도구별 프로바이더(0.10.0)** — `GET /p/{profile}/deskrpg/toolsets/{toolset}/providers` 가 `hermes tools` 의 프로바이더
  행(키 이름·키 설정 여부·active·readiness)을, `PUT …/toolsets/{toolset}/provider {provider, env?}` 가 프로바이더 선택과 그
  행의 키만 쓰기 전용으로 저장한다. Hermes 대시보드 도구 설정 라우터와 같은 함수를 쓴다. 키 설정 여부는 프로필 `.env` 로
  판정하고(게이트웨이 프로세스 환경을 섞지 않는다), 설치·구독 로그인이 필요한 행은 `setup: "cli"` 로 표시하고 PUT 을 409 로
  거절한다. 툴셋 목록 행에 `hasProviders` 가 붙는다.
- **승인 대기 카드·묶음 조회·카드 제안(0.11.0)** — 카드 생성 본문이 `initial_status`(`running`·`blocked`)를 받는다.
  `blocked` 로 만든 카드는 디스패치되지 않고 `unblock` 으로만 풀리므로 "사람이 승인한 뒤 실행" 을 만들 수 있다(Hermes 의
  `create_task` 가 그 인자를 받을 때만 capability `initial_status` 가 붙는다). `GET /deskrpg/kanban/links`·`GET /deskrpg/kanban/runs`
  가 보드 전체의 부모·자식 쌍과 실행 기록을 한 번에 준다(capability `kanban_views` — 목록 트리·실적 타임라인용). 프로필은
  `propose_kanban_card` 도구로 대화 중에 "업무 카드로 남길 만한 요청" 을 **제안**만 하고, 카드로 만들지는 사람이 고른다
  (capability `card_proposals`, 사건 `card_proposal.created` 는 `include=card_proposals` 옵트인).

## 요구사항

- Hermes `>=0.21.1` (`plugin.yaml` 의 최상위 `requires_hermes`). 스테이징은 0.21.2(커밋 `3f86ed75`)로 검증했다.
  0.21.0 이하에는 칸반 보드(`kanban_db_connect`)·크론 실행 장부(`cron.executions`) 심볼이 없어 **플러그인 전체가
  로드를 포기한다** — 프로필만 되고 칸반만 500 을 내는 반쯤 뜬 상태는 만들지 않는다(`deskrpg_plugin/_hermes_api.py`).
- 런타임 의존은 Hermes 가 이미 가진 `aiohttp` 와 `PyYAML` 뿐이다. 새 의존을 두지 않는다.

## 개발 — 푸시 전에 돌린다

```bash
scripts/ci-local.sh          # 단위 스위트만 (몇 초)
scripts/ci-local.sh --full   # CI 와 동일 (Hermes 를 받아 editable 설치, 약 1분)
```

`--full` 은 CI 의 두 잡을 그대로 재현한다: 핀된 Hermes 를 받아 editable 로 깔고,
통합 스위트를 돌리고, **실제 Hermes 가 설치된 환경에서 단위 스위트를 한 번 더** 돌린다.
마지막 단계가 중요하다 — 가짜(`fake_api`)만으로는 통과하는데 진짜 Hermes 앞에서는
깨지는 테스트가 실제로 있었다(`sys.modules` 에 남은 실제 모듈이 "모듈 없음" 흉내를
무력화했다).

클론마다 한 번, 푸시 시점에 이 검사를 강제하도록 훅을 켠다:

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-push` 가 `scripts/ci-local.sh --full` 을 돌리고, 실패하면 푸시를 막는다.
의도적으로 건너뛸 때만 `git push --no-verify`. PR 게이트를 두지 않기로 했으므로
이것이 마지막 방어선이다.

핀된 Hermes 커밋의 단일 출처는 **`.hermes-ref`** 다. 워크플로와 이 스크립트가 같은
파일을 읽으므로 로컬 검사와 CI 가 갈라질 수 없다. 올릴 때는 그 파일만 고친다.

> 2026-09-15 0.6.0 부터 master CI 가 6연속 빨강이었다. 전부 master 직접 푸시였고,
> 로컬에서 CI 와 같은 것을 돌릴 방법이 없어 푸시 전에 알 수가 없었다. 이 스크립트가
> 그 구멍이다.

## 설치 · 업데이트 · 확인

```bash
# 처음 설치
hermes plugins install https://github.com/dandacompany/deskrpg-hermes-plugin
hermes plugins enable deskrpg
systemctl --user restart hermes-gateway   # 또는 게이트웨이를 다시 시작하는 방법

# 업데이트 (이미 설치된 경우)
hermes plugins update deskrpg
systemctl --user restart hermes-gateway   # 라우트는 게이트웨이 기동 시에만 붙는다 — 재시작 없이는 옛 버전이 돈다
```

설치·업데이트 뒤 **버전과 capability 를 게이트웨이에서 직접 확인**한다(default 키):

```bash
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/deskrpg/info | jq '{version, capabilities, kanban}'
# {"version":"0.6.0","capabilities":["kanban","cron","events"],
#  "kanban":{"dispatcher_present":true,"attachments":true,"attachment_max_bytes":10000000}}
```

`version` 이 낮으면 재시작을 빠뜨린 것이고, `capabilities` 가 셋이 아니면 이 버전이 아니다(부분 로드는 없다 —
심볼이 하나라도 없으면 `hermes plugins doctor deskrpg` 가 `MissingHermesApi` 로 실패한다).

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

## ⚠️ 경고 — 프로필 CRUD·칸반·사건 스트림은 게이트웨이 전체 권한이다

`GET/POST /deskrpg/profiles`·`DELETE /deskrpg/profiles/{name}`, 그리고 0.6.0 의
**`/deskrpg/kanban/*` 전부와 `/deskrpg/events`** 는 **프리픽스 없는 경로**다. Hermes 의
API Server 는 프리픽스 없는 경로를 **default(리스너 소유자) 키**로 인증한다 — 특정
프로필의 키가 아니다.

즉 이 라우트들을 호출할 수 있는 클라이언트는 게이트웨이에 등록된 **모든** 프로필을 만들고
지울 수 있고, **호스트의 모든 칸반 보드**(칸반은 프로필과 무관한 공유 저장소 `HERMES_KANBAN_HOME` 이다)의
카드를 만들고·지우고·워커를 종료(`terminate` 는 실제 SIGTERM→SIGKILL)하며, 사건 스트림으로
**모든 프로필의 크론 실행 결과 본문**(`cron.run.finished.payload.result_text`)을 읽을 수 있다.
`API_SERVER_KEY`(default 키)를 스코프가 좁은 클라이언트나 외부로 노출하지 말 것.
프로필 하나의 인격/설정/크론만 다루는 `/p/{profile}/...` 라우트는 그 프로필의 키로만
인증된다 — **단, 이는 `gateway.multiplex_profiles` 가 켜진 게이트웨이에서만
참이다.** 이 옵션이 꺼진 게이트웨이(단일 프로필 구성이 많다)에서는 자기
프로필을 가리키는 `/p/{profile}/...` 프리픽스가 `None` 으로 해소되어, 그
프로필의 키가 아니라 **default 키**로 인증된다. 이런 게이트웨이에 프로필
키만 들고 접근하면 인증에 실패한다.

## 라우트

스코프 `default` = 프리픽스 없음, 리스너 소유자 키. `profile` = `/p/{profile}/...`, 그 프로필의 키
(위 경고의 멀티플렉스 단서 포함). 모든 행은 `deskrpg_plugin/routes.py` 의 테이블 한 곳에 있고 예외 없이
`require_auth` 로 감싸인다 — `tests/test_routes_table.py` 가 이 표와 같은 53 행을 손으로 적어 대조한다.

### 플러그인·프로필 (0.5.0 까지의 아홉 개 + 0.9.0 피커 두 개)

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/info` | default | 버전·라우트 목록·`capabilities`(`kanban, cron, events, artifacts, kanban_views, card_proposals` + Hermes 빌드에 따라 `swarm`·`profile_*`·`initial_status`)·`timezone`·`kanban{dispatcher_present, attachments, attachment_max_bytes}`·`dashboard_url`(0.7.1)·`artifact_max_bytes`(0.8.0) (**default 키 전용**) |
| GET | `/deskrpg/profiles` | default | 프로필 목록 (`hasCustomPersona` 포함) |
| POST | `/deskrpg/profiles` | default | 프로필 생성 (**응답이 새 키를 한 번만 싣는다**) |
| DELETE | `/deskrpg/profiles/{name}` | default | 프로필 삭제 (`?confirm={name}` 필수) |
| GET | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 읽기 (읽을 수 없으면 200 + `unreadable: true`) |
| PUT | `/p/{profile}/deskrpg/identity` | profile | SOUL.md 쓰기 (`ifRevision` 필수 · 읽을 수 없으면 409 `identity_unreadable`) |
| GET | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 읽기 (읽을 수 없으면 200 + `unreadable: true`) |
| GET | `/p/{profile}/deskrpg/catalog` | profile | 모델·프로바이더·추론 강도 목록 |
| PUT | `/p/{profile}/deskrpg/config` | profile | 프로필 설정 쓰기 (읽을 수 없으면 409 `config_unreadable`) |
| GET | `/p/{profile}/deskrpg/toolsets` | profile | `api_server` 플랫폼 툴셋 목록 `{name, label, description, enabled, configured}` (0.9.0) |
| GET | `/p/{profile}/deskrpg/skills` | profile | 프로필 스킬 목록 `{name, category, description, disabled, essential}` (0.9.0) |
| GET | `/p/{profile}/deskrpg/toolsets/{toolset}/providers` | profile | 도구 프로바이더 행 `{name, badge, tag, envVars[{key,prompt,url,isSet}], active, status, setup}` · `activeProvider` · `cliCommand` (0.10.0) |
| PUT | `/p/{profile}/deskrpg/toolsets/{toolset}/provider` | profile | 프로바이더 선택 + 그 행의 키 저장(쓰기 전용). `missing_keys`·`unknown_env_key` 400, `provider_needs_cli` 409 (0.10.0) |

### 칸반 (0.6.0 · 전부 default · `?board=<slug>` 필수인 곳은 표시)

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/kanban/boards` | default | `{boards:[BoardMeta+{total, counts}], current}` |
| POST | `/deskrpg/kanban/boards` | default | `{slug, name, default_workdir?}` → 새로 만들면 201, 이미 있으면 200 + 기존(멱등, name 불변). slug 는 Hermes 규칙 `^[a-z0-9][a-z0-9\-_]{0,63}$` |
| PATCH | `/deskrpg/kanban/boards/{slug}` | default | `{name?, description?, default_workdir?}` (`""` 는 비움) |
| GET | `/deskrpg/kanban/board?board=&include_archived=` | default | 열 순서 `triage, todo, scheduled, ready, running, blocked, review, done`(+`archived`) · `tenants`·`assignees`·`latest_event_id`·`now` |
| POST | `/deskrpg/kanban/tasks?board=` | default | 카드 생성 → 201 `{task, warning?}`. `X-DeskRPG-Actor` 헤더가 `created_by = deskrpg:<값>` 이 된다. 게이트웨이 디스패처가 없으면 `warning:"dispatcher_missing"` |
| GET | `/deskrpg/kanban/tasks/{id}?board=` | default | `{task, comments, events, attachments, links:{parents,children}, runs}` |
| PATCH | `/deskrpg/kanban/tasks/{id}?board=` | default | `title/body/priority/assignee/status/model_override/provider_override/reasoning_effort` 부분 갱신. 전이 거절 409 `invalid_transition` |
| DELETE | `/deskrpg/kanban/tasks/{id}?board=` | default | `{ok:true}` — 삭제 기록(`deskrpg_deleted.jsonl`)에 한 줄 남겨 사건 스트림이 `task.deleted` 를 합성한다 |
| POST | `/deskrpg/kanban/tasks/{id}/comments?board=` | default | `{author?, body}` → 201 `{comment}` |
| POST | `/deskrpg/kanban/tasks/{id}/{action}?board=` | default | `reassign · reclaim · specify · decompose · estimate(501) · approve · request-changes · unblock · terminate · archive` — 응답 `{task, …}`. 모르는 이름 404 `unknown_action` |
| GET | `/deskrpg/kanban/tasks/{id}/attachments?board=` | default | `{attachments}` |
| POST | `/deskrpg/kanban/tasks/{id}/attachments?board=` | default | multipart `file` 파트 → 201 `{attachment}`. 초과 시 413 `attachment_too_large` (아래 상한) |
| GET | `/deskrpg/kanban/attachments/{id}?board=` | default | 파일 바이트 (`Content-Type`·`Content-Disposition`) |
| DELETE | `/deskrpg/kanban/attachments/{id}?board=` | default | `{ok}` — Hermes 가 blob 도 지운다 |
| POST | `/deskrpg/kanban/links?board=` | default | `{parent_id, child_id}` → `{ok}`. 순환(자기 자신 포함) 400 `link_cycle`, 없는 카드 404 |
| DELETE | `/deskrpg/kanban/links?board=` | default | 링크 해제 |
| GET | `/deskrpg/kanban/links?board=` | default | 보드 전체의 `{links:[{parent_id, child_id}]}` (0.11.0) |
| GET | `/deskrpg/kanban/runs?board=&from=&to=&limit=` | default | 창 안의 실행 기록 `{runs, window:{from,to}, truncated}` — 시각은 epoch 초, 창 기본 최근 7일, `limit` 기본 1000·최대 5000, 넘으면 최근 것부터 남기고 `truncated:true`. `from > to` 는 400 `invalid_query` (0.11.0) |
| POST | `/deskrpg/card-proposals/{proposal_id}/resolve` | default | `{choice:"card"\|"inline", task_id?}` → `{resolved:true}`. 한 번만 해소된다 — 404 `card_proposal_not_found`, 409 `card_proposal_already_resolved` (0.11.0) |
| POST | `/deskrpg/card-proposals/{proposal_id}/unresolve` | default | 카드가 기록되지 않은 해소를 되돌린다 → `{resolved:false}`. 409 `card_proposal_not_unresolvable` (0.11.0) |
| POST | `/deskrpg/card-proposals/{proposal_id}/task` | default | `{task_id}` → `{recorded:true}`. `card` 로 해소된 제안에 만든 카드 id 를 적는다. 409 `card_proposal_task_not_recordable` (0.11.0) |
| POST | `/deskrpg/kanban/dispatch?board=&max=8` | default | **수동** 디스패치 한 틱 → `{spawned:[{task_id, profile}], skipped_locked, warning?}`. `kanban.*` 운영 설정(`max_in_progress*` 등)을 적용하지 않고 `dispatch_once` 를 바로 부른다 — 대시보드의 수동 디스패치와 같다 (`kanban.dispatch_in_gateway` 가 꺼져 있으면 `warning:"embedded_dispatcher_disabled"`) |
| GET | `/deskrpg/kanban/tasks/{id}/log?board=&tail=` | default | 워커 로그 `{exists, size_bytes, content, truncated}` (tail 기본 16384, 최대 1 MiB) |
| GET | `/deskrpg/kanban/orchestration` | default | `kanban.*` 운영 설정 + 해소된 프로필 |
| PUT | `/deskrpg/kanban/orchestration` | default | 같은 모양 + `restart_required` (`max_in_progress*` 는 디스패처가 기동 시에만 읽는다) |
| GET | `/deskrpg/kanban/profiles` | default | `{profiles:[{name, is_default, description}]}` |

### 사건 스트림 (0.6.0)

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/events?board=&cursor=&limit=200` | default | 칸반·삭제·크론 사건을 시간순으로 합친 `{events, cursor, has_more}` (아래) |

### 아티팩트 (0.8.0 · 전부 default — 호스트 공유 저장소)

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/deskrpg/artifacts?profiles=&board=&kind=&source=&q=&cursor=&limit=` | default | `{artifacts:[ArtifactSummary], cursor, has_more}` |
| GET | `/deskrpg/artifacts/{artifact_id}` | default | `{artifact:ArtifactSummary, versions:[ArtifactVersion]}` |
| GET | `/deskrpg/artifacts/{artifact_id}/versions/{v}/content` | default | 바이트 스트림(Range 지원, 단일 범위만). `?download=1` 이면 첨부, 아니면 인라인. 항상 `Content-Security-Policy: sandbox`·`X-Content-Type-Options: nosniff` |
| POST | `/deskrpg/artifacts/{artifact_id}/versions` | default | 사람이 편집(JSON `{content, filename, note?}` 또는 multipart `file`+`note`) → 201 `{version}`. 초과 시 413 `{error, detail, max_bytes}` |
| POST | `/deskrpg/artifacts/{artifact_id}/rework` | default | **501 `not_implemented`** — 수정 루프는 자리만 잡아 뒀다 |
| DELETE | `/deskrpg/artifacts/{artifact_id}` | default | 소프트 삭제 → `{ok:true}` |

모델은 `artifact_save` 도구로, 산출 도구(생성·다운로드·저장·렌더 계열)가 만든 파일은 `post_tool_call`
훅이 사용자가 말하지 않아도 자동으로 승격한다(호출당 최대 8개). 저장소는 게이트웨이당 `registry.db` 하나와
`deskrpg/artifacts/blobs/` 뿐이며, `HERMES_DESKRPG_ARTIFACTS_ROOT` 로 옮길 수 있다. 사건 스트림
(`/deskrpg/events`)에 `artifact.created`·`artifact.versioned`·`artifact.deleted`·`artifact.delete_partial`·
`artifact.capture_failed` 다섯 kind 로 합류하며, 게이트웨이 전역이라 채널/보드로 거르는 것은 DeskRPG 쪽 몫이다.
카드 제안(0.11.0)의 `card_proposal.created` 도 같은 출처·같은 커서로 흐른다 — 두 옵트인 토큰은 `include=artifacts,card_proposals`
처럼 **함께** 켠다. 한쪽만 켜면 그쪽을 읽으며 커서가 전진해 다른 쪽 사건을 지나치고, 그 사건은 다시 오지 않는다.
모르는 `include` 토큰은 400 이 아니라 무시된다(구버전 플러그인도 같다).

### 크론 (0.6.0 · 전부 profile — 잡은 그 프로필의 `cron/jobs.json` 에만 산다)

| Method | Path | Scope | 설명 |
|---|---|---|---|
| GET | `/p/{profile}/deskrpg/cron/jobs?include_disabled=` | profile | `{jobs:[CronJob]}` (`state` 는 `effective_job_state`) |
| POST | `/p/{profile}/deskrpg/cron/jobs` | profile | `{schedule 필수, prompt?, name?, deliver?, model?, provider?, skills?, paused?, repeat?, script?}` → 201 `{job}`. `origin={"source":"deskrpg"}` |
| GET | `/p/{profile}/deskrpg/cron/jobs/{id}` | profile | `{job}` |
| PUT | `/p/{profile}/deskrpg/cron/jobs/{id}` | profile | `{updates:{schedule, prompt, name, deliver, model, provider, enabled, skills, script}}` (그 외 키 400) |
| DELETE | `/p/{profile}/deskrpg/cron/jobs/{id}` | profile | `{ok:true}` |
| GET | `/p/{profile}/deskrpg/cron/jobs/{id}/runs?limit=20` | profile | `{runs:[{id, started_at, ended_at, status, summary, result_text}], limit}` (세션 DB 기준, limit 최대 50) |
| POST | `/p/{profile}/deskrpg/cron/jobs/{id}/pause` | profile | `{job}` |
| POST | `/p/{profile}/deskrpg/cron/jobs/{id}/resume` | profile | `{job}` · 끝난 일회성 409 `job_terminal` |
| POST | `/p/{profile}/deskrpg/cron/jobs/{id}/run` | profile | 202 `{accepted:true, job}` — 다음 스케줄러 틱이 돌린다 · 일시정지 중 409 `job_paused` · 끝난 일회성 409 `job_terminal` |
| GET | `/p/{profile}/deskrpg/cron/delivery-targets` | profile | `{targets:[{id, name, home_target_set, home_env_var}, …]}` — `local` 이 항상 첫 항목. `home_env_var` 는 **null 일 수 있다**(local 은 항상 null; 플랫폼 배달처도 환경변수로 홈을 정하지 않으면 null) |
| GET | `/p/{profile}/deskrpg/cron/blueprints` | profile | Hermes 템플릿 카탈로그(`deliver` 옵션은 위 배달처로 교체) |
| POST | `/p/{profile}/deskrpg/cron/blueprints/instantiate` | profile | `{blueprint, values}` → 201 `{job}` · 모르는 키 404 · 값 오류 422 `invalid_blueprint_values` |

오류 본문은 전부 `{"error": <code>, "detail"?: <str>}` 다. 상태 코드: 400 잘못된 입력 · 404 없음 ·
409 상태 충돌(Hermes 가 전이를 거절) · 413 본문 초과 · 422 템플릿 값 오류 · 424 크론 스케줄러 등록 실패 ·
501 미구현 · 504 LLM 타임아웃(`specify`/`decompose`) · 500 예상 못 한 예외(본문·비밀은 싣지 않는다).

### 첨부 상한 — 실효 10 MB

`info.kanban.attachment_max_bytes = min(게이트웨이 MAX_REQUEST_BYTES, KANBAN_ATTACHMENT_MAX_BYTES)`.
실측(Hermes 0.21.2): 게이트웨이 본문 상한 10,000,000 바이트 < 칸반 자체 상한 25 MiB 이므로 **10 MB** 가
실효 상한이다. 첨부는 요청 본문에 실려 오므로 게이트웨이가 먼저 자른다 — 이 플러그인이 올릴 수 없는 값이다.
클라이언트는 업로드 전에 이 값으로 거른다. 분할 업로드는 없다(`docs/BACKLOG.md`).

### 사건 스트림 — kind 와 커서

`GET /deskrpg/events?board=<slug>&cursor=<token>&limit=200` 은 세 출처를 `ts` 오름차순으로 합친다.

- **kind**: `task.created` · `task.status`(payload `{from, to, parent_count, title, assignee}`) · `task.comment` ·
  `task.link`(payload 에 `action: linked|unlinked`) · `task.updated`(payload `{fields}`) · `task.run.started` ·
  `task.run.finished`(+ 상태가 바뀌었으면 `task.status` 한 건 더) · `task.deleted`(플러그인이 지운 경우만) ·
  `cron.run.started`(`{job_id, job_name, profile, session_id, started_at}`) ·
  `cron.run.finished`(+ `{status: ok|error, ended_at, result_text}`).
- **사건 id**: 칸반 `k:<task_events.id>`, 삭제 `d:<줄 번호>`, 크론 `c:<profile>:<exec_id>:<started|finished>`.
- **커서는 불투명 토큰**이다(`v1.` + base64url JSON). DeskRPG 는 해석하지 않고 되돌려 준다.
  - **첫 호출은 `cursor` 없이** 부른다 → `{events: [], cursor: <지금>, has_more: false}`. "지금" 이전 사건은 주지 않는다.
  - 파싱 실패·버전 불일치 → **400 `unknown_cursor`** → 커서 없이 다시 시작한다.
  - 각 출처의 위치는 **실제로 응답에 실은 사건까지만** 전진한다. `limit` 에 잘린 사건은 다음 호출에 다시 나온다 —
    구멍보다 중복이 낫고, 응답 기준으로는 중복도 없다. `has_more` 는 어느 출처든 잘린 것이 있을 때 참.
  - 보드 slug 가 없으면 404 `board_not_found` (크론 부분은 시도하지 않는다).
- **크론 사건은 호스트의 모든 프로필**을 본다 — 프로필별 `cron/executions.db` 가 **파일로 있을 때만** 읽고
  (열면 Hermes 가 파일을 만들어 버린다), 없는 프로필은 잡 레코드(`fire_claim`·`last_run_at`)로 폴백한다.
- `result_text` 의 출처: 그 실행에 대응하는 세션(`state.db` 의 `source='cron'`, id `cron_<job_id>_…`, 시작 시각
  ±120 초)의 **마지막 assistant 메시지** → 없으면 잡 출력 폴더의 해당 실행 파일 → 없으면 `""`. 20,000 자 초과분은
  잘라내고 끝에 `…`.

### 크론 — 알아둘 규칙

- **`prompt` 는 `script` 가 없을 때 필수**(400). `script` 는 프로필 홈의 `scripts/` 아래로 샌드박스된다.
- `run` 은 **동기 실행이 아니다** — `trigger_job` 으로 다음 틱에 돌도록 표시만 하고 202 를 준다. 실행은 게이트웨이의
  스케줄러 틱이 한다. 일시정지 중이면 409 `job_paused`(Hermes 의 `trigger_job` 은 재개까지 해 버리므로 막는다).
  끝난 일회성(`state: completed|error`)은 `run`·`resume` 모두 409 `job_terminal`.
- 과거 시각의 일회성 잡은 Hermes 가 생성을 거절한다(실측: "more than 120s in the past") → 400 `invalid_schedule`.
- 스케줄러 등록 실패는 424. 외부 프로바이더 + 프로필 2개 이상이면 프로바이더 재조정을 건너뛴다(대시보드와 같다).
- 실행 결과(`runs[].result_text`, `cron.run.finished.result_text`)의 출처는 위 사건 스트림 절과 같다.

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
정리하려고 `HERMES_HOME` 환경변수를 바꾼 뒤 `get_service_name()` 을 부르는데,
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

`PUT /p/{profile}/deskrpg/config` 는 `{model, provider, toolsets, reasoning_effort}` 네 키와
0.9.0 의 피커 키 `enabledToolsets`(`platform_toolsets.api_server/cron/cli` 에 같은 목록)·`disabledSkills`(`skills.disabled`)만
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
  (API 키형은 앞을, OAuth 형은 뒤를 채운다 — 한쪽만 보면 절반을 놓친다).
  **요청한 프로필의 홈에서** 판정한다(0.7.2). Hermes 는 NPC(프로필)마다 로그인하므로
  (업스트림 #111724 부터 프로필은 default 의 `auth.json` 을 물려받지 않는다), default 로
  로그인했다고 noah 가 쓸 수 있는 것이 아니다. 프로필 로그인은 Hermes 대시보드에서
  상단 프로필을 그 이름으로 바꾼 뒤 Keys 화면에서 한다(`/env?profile=<이름>`).
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

두 벌이다. 둘 다 초록이어야 한다(spec §9 T4).

```bash
# 1. 가짜 단위 테스트 — Hermes 없이. `_hermes_api` 를 SimpleNamespace 로 바꿔 라우트별 인증 스코프·상태 코드·
#    응답 필드 집합·커서 왕복·전이 규칙을 고정한다. CI 의 `test` 잡.
python -m pytest -q

# 2. 통합 테스트 — 실제 Hermes 가 설치된 venv 에서만. `hermes_cli` 를 import 할 수 없으면 skip 되고,
#    `HERMES_INTEGRATION_REQUIRED=1` 이면 그 skip 이 실패가 된다(조용히 안 도는 초록 금지). CI 의 `integration` 잡.
python -m venv .venv-hermes && .venv-hermes/bin/pip install -r requirements-dev.txt \
  "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@3f86ed75dad1933036c52018e991dbd839837126"
HERMES_INTEGRATION_REQUIRED=1 .venv-hermes/bin/python -m pytest -q -m integration tests/integration
.venv-hermes/bin/python -m pytest -q      # 가짜 스위트도 실제 Hermes 옆에서 한 번 더
```

통합 테스트는 `HERMES_HOME`·`HERMES_KANBAN_HOME` 을 **테스트마다 `tmp_path` 아래 새 폴더**로 돌려놓고, 실제
`kanban_db`·`cron.jobs`·`cron.executions`·`SessionDB` 를 만들어 보드 생성→카드→링크→댓글→전이→삭제→사건 tail,
크론 생성→pause/resume/run→실행 장부→사건 합성·`result_text` 를 검증한다. 사용자의 `~/.hermes` 는 건드리지 않고
(fixture 가 단정한다), 디스패치 실제 스폰도 하지 않는다(`dry_run=True`). 셸의 `*_API_KEY` 는 테스트 프로세스
안에서 지운다 — Hermes 가 자격증명 풀에 ingest 하는 것을 막는다.

`hermes plugins doctor deskrpg` 는 설치된 게이트웨이에서 수동으로 돌려 확인한다.
