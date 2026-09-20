# tests — 가짜 Hermes 스위트와 실제 Hermes 통합 스위트

## 범위

두 벌의 테스트. (1) `tests/test_*.py`: `_hermes_api.load()` 대신 `conftest.fake_api`(평면 `SimpleNamespace`) 와 인메모리 가짜(`fakes_kanban.py`, `fakes_kanban_actions.py`, `fakes_events.py`, `fakes_cron.py`)로 Hermes 없이 돈다. (2) `tests/integration/`: `hermes_cli` 가 import 될 때만 수집되고 실제 Hermes 의 sqlite·크론 저장소·세션 DB 위에서 같은 계약을 검증한다. `pytest.ini` 가 `pythonpath = .`, `asyncio_mode = auto`, `integration` 마커를 정한다.

**범위가 아닌 것**: 실제 워커 스폰(디스패치는 `dry_run=True`) · 실제 보조 LLM 호출(specify/decompose 는 가짜) · 게이트웨이 프로세스 기동(스모크는 수동) · DeskRPG 쪽 E2E · Hermes 자체의 동작 검증.

## 경계

- 플러그인 코드를 수정하지 않는다. 가짜는 플러그인이 부르는 Hermes 시그니처를 따라간다 — 반대가 아니다.
- 가짜는 Hermes 와 **같은 실패 방식**을 재현한다: 전이 거절은 False/RuntimeError/ValueError, `trigger_job` 은 일시정지를 풀어 버린다, `update_job` 은 없는 잡에 None, 스케줄 문법 오류는 ValueError, 프로필 이름 정규식은 실제와 같은 앵커 정규식, `create_profile` 은 wrapper 스크립트도 만든다. 느슨하게 만들면 테스트에서만 통과하는 회귀가 생긴다.
- 크론 가짜는 `use_cron_store(home)` 스코프 밖 호출에 AssertionError 를 낸다 — 스코프 규칙 위반을 잡는 장치이므로 풀지 않는다.
- `fakes_events.FakeEventsKanban` 은 `events.SQL_*` 문자열을 그대로 대조한다. 플러그인의 SQL 을 바꾸면 여기도 바꾼다. 그 밖의 문장은 `fakes_kanban.FakeConn.execute` 로 넘어가고, 모르는 문장은 `NotImplementedError` 로 시끄럽게 실패한다(조용히 빈 결과를 주지 않는다).
- 실제 홈을 절대 보지 않는다: `conftest._isolated_user_home`(autouse)이 `safedelete.user_home` 을 임시 폴더로, 통합 `hermes_env` 가 `HERMES_HOME`·`HERMES_KANBAN_HOME` 을 `tmp_path` 로 바꾸고 실제 `~/.hermes` 가 아님을 단정한다.

## 불변식

- `tests/test_routes_table.py` 의 `EXPECTED_ROUTES` 는 **손으로 적은** 51행이며 `routes.ROUTES` 와 정확히 같아야 한다(소유자 34, 프로필 17). 코드에서 도출하지 않는다.
- `tests/test_auth_enforced.py` 는 `ROUTES` 전 행에 대해 무인증 401 과 어댑터 호출(`checked == [path]`)을 단정한다. 경로 변수 치환표 `_PATH_VALUES` 에 없는 변수가 생기면 `{…}` 가 URL 에 남아 실패한다 — 새 변수는 표에 넣는다.
- `tests/test_info.py` 는 `_hermes_api.REQUIRED` 의 모든 이름이 `fake_api` 에 있음을 단정한다.
- `tests/test_hermes_api_guard.py` 는 심볼 하나 부재·모듈 부재·Hermes 부재가 전부 `MissingHermesApi` 임을 단정한다. "Hermes 부재" 는 `sys.modules` 의 `hermes_cli*` 를 먼저 걷어낸다(실제 Hermes venv 에서 캐시 때문에 통과하지 않던 문제).
- 통합 `conftest.py` 는 **모듈 최상단**에서 `HERMES_HOME`·`HERMES_KANBAN_HOME` 을 임시 폴더로 설정한 뒤 `pytest.importorskip("hermes_cli")` 한다. import 시점에 경로를 굳히는 Hermes 모듈이 있어 순서를 바꾸면 실제 홈에 파일이 생긴다.
- `HERMES_INTEGRATION_REQUIRED=1` 이면 수집 단계·실행 단계 skip 이 모두 실패로 바뀐다(`tests/conftest.py` 의 두 훅). CI integration 잡이 이 변수를 켠다.
- 통합 `hermes_env` 는 프로세스 안의 `*_API_KEY` 환경변수를 지운다. Hermes 가 설정 로드 시 자격증명 풀에 ingest 하기 때문이며, 로컬과 CI 의 결과를 같게 한다.

## 구현 패턴

- 핸들러 단위 테스트는 `web.Application()` 에 해당 핸들러만 `require_auth(FakeAdapter, Scope, handler)` 로 붙여 `aiohttp_client` 로 때린다(`tests/kanban_app.py` 가 보드·카드 군의 예). 배선 테스트(`test_wiring.py`)만 `routes.attach` 로 전체 테이블을 붙인다.
- 가짜 설치: `install_fake_kanban(fake_api, root)` / `install_fake_kanban_actions` / `install_fake_cron(fake_api, tmp_path)` — `fake_api` 의 해당 심볼을 인스턴스 메서드로 바꿔치기하고 인스턴스를 돌려준다. 테스트는 인스턴스의 도우미(`start_run`, `add_attachment`, `emit`, `remove_task`, `append_deleted`, `store.add_job(home, …)`, `write_worker_log`)로 상태를 심고 `calls`/`notified`/`signals` 로 호출을 확인한다.
- 특정 Hermes 동작을 바꾸려면 `fake_api.<name> = …` 로 그 테스트 안에서만 덮는다.
- 통합 테스트는 `client`(실제 `_hermes_api.load()` + `routes.attach`), `profile`(임시 홈 아래 실제 `create_profile("sophie")`), `hermes_env` fixture 를 쓴다. 실행 장부 행은 Hermes 공개 함수(`cron.executions.create_execution`·`mark_execution_running`·`finish_execution`)로, 세션은 `api.SessionDB(home/"state.db")` 로 직접 쓴다. 끝난 일회성 잡은 미래 시각으로 만든 뒤 `update_job` 으로 상태를 만든다(과거 시각은 Hermes 가 생성을 거절).
- 응답 키 단정은 `contract_fields` 상수로: `cf.X_REQUIRED <= set(body) <= cf.X_KEYS`.
- 테스트 이름은 한국어 서술형(`test_보드_생성은_201_이고_다시_만들면_200_에_기존_보드다`).

## 실행

```bash
.venv/bin/python -m pytest -q                                                   # 가짜, 666건 + skip 1
env -i HOME="$HOME" PATH="$PATH" HERMES_INTEGRATION_REQUIRED=1 \
  .venv-hermes/bin/python -m pytest -q -m integration tests/integration          # 실제 Hermes, 35건
env -i HOME="$HOME" PATH="$PATH" .venv-hermes/bin/python -m pytest -q            # 가짜 스위트를 실제 Hermes 옆에서
```

`env -i` 없이 통합을 돌리면 셸의 플랫폼 토큰·API 키가 Hermes 에 들어간다. 통합 뒤 `~/.hermes` 와 `~/.local/bin` 에 새 파일이 없는지 본다.

## 반드시 고정해야 할 엣지

무인증 401(전 행) · 스코프 표식 일치 · 경로 변수명 ⇔ 스코프 · 고정 세그먼트가 `{action}` 앞 · 버전 == plugin.yaml · `requires_hermes` 최상위 · 심볼 부재 시 로드 포기 · 보드 멱등 생성(name 불변) · 슬러그 400 · 보드 없음 404 · 열 순서 · PATCH 갈래별 Hermes 함수 호출(`calls` 로 확인) · 전이 거절 409 · running 직접 지정 409 · 부모 게이트 · 삭제 장부 한 줄 + `task.deleted` 합성 · 댓글 author 절단 · 링크 순환 400 · 동작 10개 각 규칙(특히 request-changes 세 갈래와 댓글 선기록, terminate 409, estimate 501, specify/decompose 409/504) · 첨부 413·상한 도달 시 읽기 중단·경로 루트 밖 404 · 로그 tail 상한 · 디스패치 경고 · 운영 설정 `restart_required`·프로필 존재 400 · 커서 왕복·`unknown_cursor`·limit 잘림 재발행·has_more·병합 순서·쌍 보존 · 크론 kind 매핑 표 전부와 무시 목록 · 크론 장부/폴백 두 경로 · result_text 세 단계 폴백과 절단 · 크론 생성 prompt/script·424·400 · updates 허용 키 · pause/resume/run 의 409 · runs status 규칙 · 배달처 local 우선 · 템플릿 deliver options 교체·422·404 · 프로필 생성 키 발급 실패 201 · 삭제 `confirm`·default·서비스 유닛 409·wrapper 여부 · 인격 ifRevision·unreadable · 설정 허용 키·타입·`config_unreadable`·백업 찌꺼기 0개.
