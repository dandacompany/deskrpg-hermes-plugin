# deskrpg_plugin — 플러그인 본체

## 범위

Hermes 게이트웨이의 aiohttp 앱에 붙는 DeskRPG 전용 라우트 51개와 그 핸들러. Hermes 내부 API 로더(`_hermes_api.py`), 라우트 테이블(`routes.py`), 인증 래퍼(`auth.py`), 공용 도우미(`common.py`), 계약 키 집합(`contract_fields.py`), 기능 모듈(프로필·인격·설정·카탈로그·칸반 4개·삭제 장부·크론 2개·사건·아티팩트 8개(`artifacts_store/policy/detect/context/tool/hook/routes/prompt.py`)).

**이 모듈의 범위가 아닌 것**: Hermes 의 칸반 전이 규칙·크론 스케줄러·세션 저장(전부 Hermes 함수를 부를 뿐 재구현하지 않는다) · 게이트웨이 인증 키 선택(`_check_auth` 가 한다) · 워커 프로세스 스폰(디스패처가 한다) · 견적 LLM(없음, 501) · DeskRPG 화면 로직 · 대시보드(9119).

## 경계

- Hermes 패키지 import 는 `_hermes_api.py` 에서만. 다른 파일에서 `hermes_cli`·`cron`·`hermes_state`·`hermes_constants`·`hermes_time`·`gateway` 를 import 하지 않는다(`catalog.py` 의 핸들러 내부 지연 import 가 유일한 기존 예외).
- `app.router.add_route` 는 `routes.attach` 에서만.
- `os.environ["HERMES_HOME"]` 을 바꾸지 않는다. 프로필 홈은 `cron.cron_scope`(컨텍스트 변수 오버라이드)로만.
- Hermes 의 `delete_profile` 을 부르지 않는다(`safedelete` 가 대신한다). `_cleanup_gateway_service` 경로는 게이트웨이를 죽인다.
- 대시보드 플러그인 모듈(`plugins/kanban/dashboard/*`, `hermes_cli/web_server_cron.py`, `web_routers/cron.py`)을 import 하지 않는다 — 로직만 옮기고 원본 위치를 주석에.
- `tests/` 의 가짜를 알지 못한다. 가짜와의 접점은 `_hermes_api.load()` 가 돌려주는 평면 네임스페이스뿐.

## 불변식

- `routes.ROUTES` 의 모든 행이 `require_auth` 로 감싸여 등록된다. `Scope.PROFILE` ⇔ 경로에 `{profile}`. import 시점 검사 두 개(`_assert_scope_matches_path`, `_assert_every_route_has_a_handler`)가 실패하면 모듈 import 이 실패한다.
- `_hermes_api.SPEC` 의 이름은 유일하다. `REQUIRED` = `SPEC` 의 모든 이름. 하나라도 없으면 `load()` 가 `MissingHermesApi`.
- `routes.PLUGIN_VERSION` 은 `plugin.yaml` 의 `version:` 줄에서 읽힌다. 코드에 버전 문자열 리터럴이 없다.
- sqlite·파일·LLM 호출은 `common.run_blocking` 안에서만 일어나고, 칸반 연결은 `common.board_conn` 블록 안에서 열리고 닫힌다. 보드 존재 검사가 연결보다 먼저다.
- 세션 DB 는 `cron_results.open_session_db` 로 읽기 전용으로만 열린다. 실행 장부 존재는 `cron_results.executions_db_exists`(경로)로만 판단한다.
- 새 라우트의 오류는 `common.RequestError` → `json_error` 의 `{"error","detail"?}`. 응답은 `contract_fields` 키 집합으로 투영된다.
- 로그는 `common.log_event` 로만. 문자열 값은 길이로 바뀐다.
- 삭제 장부는 `deleted_log.append_deleted` 로만 쓰고 `deleted_log.read_deleted_since` 로만 읽는다. append 전용.
- `events.SQL_TAIL`·`SQL_EARLIER_FOR_TASK`·`SQL_MAX_ID` 문자열은 가짜 연결이 그대로 대조한다.
- **`blobs/` 에 쓰는 코드는 `artifacts_store.store_artifact_version` 뿐이다.** 라우트·도구·훅 어디서도 `write_bytes`/`open(..., "wb")` 를 직접 부르지 않는다. 정체성 판정과 dedup 비교는 그 함수가 쓰기 잠금(`BEGIN IMMEDIATE`) 안에서 한다 — 잠금 밖에서 하면 훅과 도구가 동시에 같은 정체성을 저장할 때 둘 다 새 아티팩트로 보고 경합한다.

## 구현 패턴

- **핸들러 팩토리**: `def xxx_handler(api): async def handler(request): …; return handler`. `routes._HANDLERS` 에 `"이름": lambda api: 모듈.xxx_handler(api)` 로 매핑. 동작은 `kanban_actions.action_handler(api, name)` 하나가 이름별로.
- **워커 스레드 작업**: 핸들러 안에서 `def work(): with board_conn(api, slug) as conn: …; return dict` 를 만들고 `await run_blocking(work)`. `RequestError` 는 `work` 안에서 던져도 되고 핸들러가 `except RequestError as exc: return exc.response()` 로 받는다. 칸반 보드 모듈은 `_guarded` 데코레이터가 이 처리와 500 변환을 한다.
- **보드 열기**: 존재 검사가 필요한 곳은 `kanban_actions.open_board(api, slug)`(404 포함) 또는 `kanban_board._require_board` + `board_conn`.
- **카드 응답**: `kanban_board._task_full(api, conn, task_id)` 가 롤업·진단을 붙여 `KANBAN_TASK_FULL_KEYS` 로 투영한다. 상세·생성·수정·동작 응답이 전부 이것을 쓴다 — 갈라지면 안 된다.
- **크론 스코프**: `with cron_scope(api, profile) as home:` 안에서 `api.list_jobs()` 등. 잡 응답은 `with_state(api, job)` 로 `state` 를 덮는다. 생성은 `create_job_in_scope`(424 운반체 `_RegistrationFailed` 포함).
- **입력 검사**: `read_json_object` → `_reject_unknown_keys`/`require_str|int|bool|str_list`. 정수 검사는 bool 을 먼저 거른다.
- **Hermes 거절 변환**: False → `_check(ok, detail)`; RuntimeError/ValueError → `transition_error(exc)`; 둘 다 409 `invalid_transition`.
- **행위자**: `kanban_actions.actor_from_request(request)`(`X-DeskRPG-Actor` → `deskrpg:<값>`).
- **결과 본문·상태 판정**: 크론 runs 와 사건 스트림이 `cron_results` 의 같은 함수(`result_text_for`, `run_status_for`, `find_session_for_execution`, `to_epoch`)를 쓴다. 한쪽만 고치면 두 화면이 어긋난다.
- **커서 전진**: `events.merge` 가 돌려주는 실린 사건 id 집합으로 `advance_kanban`/`advance_deleted`/`advance_cron` 이 출처별 위치를 옮긴다. 실리지 않은 사건 너머로 전진시키는 코드는 계약 위반.

## 테스트 지침

- 라우트를 더하면: `tests/test_routes_table.py` 의 목록·행 수·스코프 수, `tests/test_auth_enforced.py` 의 경로 변수 치환표(새 변수 이름이 생기면), `tests/test_wiring.py` 의 군별 스모크.
- 심볼을 더하면: `tests/conftest.py` `_add_automation_fakes` 에 기본 가짜(`test_info` 가 `REQUIRED` 전부를 단정).
- 응답 키를 바꾸면: `contract_fields.py` 상수와 그 상수를 쓰는 단정(`REQUIRED <= set(body) <= KEYS`).
- 반드시 고정해야 할 엣지: 무인증 401(모든 행) · 소유자/프로필 스코프 표식 · 보드 없음 404 · 슬러그 형식 400 · 전이 거절 409 · `status:"running"` 409 · 부모 미완료 ready 409 · request-changes 세 갈래 + 댓글 선기록 · terminate 409 `no_active_run` · estimate 501 · 첨부 413 과 상한 도달 시 읽기 중단 · 첨부 경로가 루트 밖이면 404 · 커서 `unknown_cursor` · limit 잘림 시 재발행·has_more · run.finished+status 쌍 보존 · 크론 장부 없는 프로필의 폴백 · run 의 `job_paused`/`job_terminal` · 템플릿 422/404 · 설정 PUT 허용 키·타입·`config_unreadable` · 인격 `revision_mismatch`·`identity_unreadable` · 프로필 삭제 `confirm`·default·`profile_has_service` · 키 발급 실패 시 201 + `keyIssued:false`.
- 실제 Hermes 위에서만 잡히는 것(전이 규칙·`delete_task` 가 사건까지 지움·`dispatcher_missing`·실제 장부/세션 → result_text)은 `tests/integration/` 에 둔다.
