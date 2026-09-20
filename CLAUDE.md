# deskrpg-hermes-plugin

Hermes Agent 게이트웨이(API 서버, aiohttp, 8642)에 DeskRPG 전용 HTTP 라우트 51개를 붙이는 standalone Hermes 플러그인(Python 3.11+, 런타임 의존은 Hermes 가 이미 가진 aiohttp·PyYAML 뿐). 프로필 CRUD·인격(SOUL.md)·설정·카탈로그, 0.6.0 부터 칸반(보드·카드·동작·첨부·디스패치)·통합 사건 스트림·프로필별 크론, 0.8.0 부터 아티팩트 저장소(`artifact_save` 도구·`post_tool_call` 자동 승격·조회/편집/삭제 라우트)를 DeskRPG 가 정해 둔 HTTP 계약 그대로 낸다. 사용자는 DeskRPG 를 운영하는 단테랩스 한 사람이고, 배포 대상은 MiniPC 스테이징 게이트웨이(Hermes 0.21.2, 프로필 7개) 하나다 — 코드 20개 모듈·테스트 500건 규모를 넘기는 설계는 과하다.

## 프로젝트 구조

```
deskrpg-hermes-plugin/
├── CLAUDE.md                      ← 이 파일 (AGENTS.md 와 바이트 동일)
├── AGENTS.md                      ← Codex 진입점 (CLAUDE.md 와 바이트 동일)
├── plugin.yaml                    ← 매니페스트 — version 과 requires_hermes 의 정본
├── __init__.py                    ← Hermes 로더용 루트 쉼 (register 재수출만)
├── deskrpg_plugin/
│   ├── AGENTS.md                  ← 플러그인 모듈의 범위·경계·불변식·패턴
│   ├── _hermes_api.py             ← Hermes 내부 심볼을 import 하는 유일한 지점 (전부 아니면 전무)
│   ├── routes.py                  ← 라우트 선언 테이블 + 등록 (모든 행이 require_auth 를 거친다)
│   ├── auth.py · common.py · contract_fields.py
│   ├── profiles.py · safedelete.py · keyissue.py · identity.py · config.py · catalog.py
│   ├── kanban_board.py · kanban_actions.py · kanban_files.py · kanban_ops.py · deleted_log.py
│   ├── cron.py · cron_results.py · events.py
│   └── artifacts_store.py · artifacts_policy.py · artifacts_detect.py · artifacts_context.py ·
│       artifacts_tool.py · artifacts_hook.py · artifacts_routes.py · artifacts_prompt.py
├── tests/
│   ├── AGENTS.md                  ← 가짜 Hermes 와 실제 Hermes 두 벌 테스트의 규칙
│   ├── conftest.py · fakes_*.py · test_*.py
│   └── integration/               ← 실제 Hermes 가 설치된 venv 에서만 도는 통합 테스트
├── docs/
│   ├── architecture.md            ← 게이트웨이 안에서 플러그인이 차지하는 자리와 요청 흐름
│   ├── business-rules.md          ← 카드 전이·동작·사건 커서·크론·프로필 삭제의 규칙
│   ├── security.md                ← 소유자 키/프로필 키 스코프, 키 발급, 로그·비밀 정책
│   ├── standards.md               ← 어기면 깨지는 규칙 (의존·심볼·라우트·스레드·로깅)
│   ├── engineering-notes.md       ← 실측 사고와 함정 (증상 → 원인 → 대응)
│   ├── operations.md              ← 로컬 검증·통합 테스트 venv·스테이징 설치/업데이트 절차
│   ├── contracts.md               ← DeskRPG 가 붙드는 HTTP 계약 전부 (입력·출력·오류)
│   ├── BACKLOG.md                 ← 실증된 결함·부족의 등록부
│   └── tracking/
│       ├── status.md              ← 만든 것·검증한 것·남은 것
│       ├── decisions/             ← 실제 트레이드오프가 있던 결정 기록
│       └── findings.md            ← 지금 풀 수 없는 문제와 그 이유
└── .github/workflows/ci.yml       ← 가짜 스위트 잡 + 실제 Hermes 통합 잡
```

## 하드 게이트

1. **모든 라우트는 `routes.py` 테이블 한 곳에 있고 예외 없이 `require_auth` 를 거친다.** Hermes 는 인증을 미들웨어로 걸지 않아 핸들러 하나만 빠져도 무인증 프로필 CRUD 가 열린다. 프로필 키 라우트는 경로에 반드시 `{profile}` 변수를, 소유자 키 라우트는 절대 그 변수를 담지 않는다 — import 시점에 검사한다.
2. **Hermes 내부 심볼은 `_hermes_api.py` 에서만 import 하고, 하나라도 없으면 플러그인 전체가 로드를 포기한다.** 프로필은 되는데 칸반만 500 을 내는 반쯤 뜬 상태를 만들지 않는다.
3. **블로킹(sqlite·파일·LLM)은 워커 스레드에서, 칸반 연결은 요청마다 그 스레드 안에서 열고 닫는다.** 이벤트 루프에서 sqlite 를 만지면 게이트웨이의 모든 프로필 응답이 함께 멎는다.
4. **카드 본문·댓글·프롬프트·실행 결과·키 값은 로그와 오류 메시지에 싣지 않는다.** 길이·개수·id 만.
5. **Hermes 의 `delete_profile` 을 부르지 않는다.** 멀티플렉스 게이트웨이 안에서 자기 자신에게 `systemctl stop` 을 걸어 게이트웨이를 죽인 사고가 있다 — 전용 서비스 유닛이 있으면 409 로 거절하고, 없을 때만 디렉터리를 지운다.

## 작업 전 확인

- 기본: `docs/standards.md`, `docs/engineering-notes.md`, 손댈 모듈의 `AGENTS.md`.
- **라우트를 더하거나 경로를 바꿀 때**: `docs/standards.md` 의 라우트 규칙(고정 세그먼트가 `{action}` 와일드카드보다 앞, `{profile}` 변수 규칙)과 `tests/test_routes_table.py` 의 손으로 적은 51행 목록 — 행 수와 스코프 수 단정을 같이 고친다.
- **응답 필드를 더하거나 뺄 때**: `docs/contracts.md` 의 해당 라우트와 `deskrpg_plugin/contract_fields.py` 의 키 집합. DeskRPG 가 이미 이 계약으로 구현을 끝냈다 — 이름·상태 코드를 바꾸려면 사용자에게 먼저 묻는다.
- **Hermes 심볼을 새로 쓸 때**: `_hermes_api.SPEC` 에 추가하고 `tests/conftest.py` 의 가짜에 기본값을 넣는다(`test_info` 가 모든 심볼의 가짜 존재를 단정한다). 대시보드 플러그인 함수는 FastAPI 를 import 하므로 가져오지 않고 로직만 이식한다.
- **프로필 삭제·서비스·키 발급을 건드릴 때**: `docs/security.md` 의 키 수명 절과 `docs/engineering-notes.md` 의 게이트웨이 사망 사고.
- **사건 스트림·커서를 건드릴 때**: `docs/business-rules.md` 의 커서 전진 규칙 — 출처별 위치는 실제로 응답에 실은 사건까지만 전진한다. `events.SQL_*` 문자열을 바꾸면 `tests/fakes_events.py` 도 같이 바꾼다.
- **통합 테스트를 돌릴 때**: `docs/operations.md` 의 격리 절차. Hermes 는 설정 로드 시 셸의 `*_API_KEY` 와 플랫폼 토큰을 그대로 ingest 한다 — 사용자 셸 환경으로 게이트웨이 스모크를 돌렸다가 실제 Telegram 봇에 접속한 적이 있다. 항상 `env -i` 또는 명시적 제거.
- **버전을 올릴 때**: `plugin.yaml` 만 고친다. 코드에 버전 문자열을 두지 않는다(두 번 갈라진 사고).
- **아티팩트 저장소·도구·훅을 건드릴 때**: `deskrpg_plugin/AGENTS.md` 의 불변식 — `blobs/` 에 쓰는 코드는 `artifacts_store.store_artifact_version` 뿐이다. 정체성 판정·dedup 비교는 쓰기 잠금(`BEGIN IMMEDIATE`) 안에서 하고, `docs/contracts.md` 의 아티팩트 절과 `docs/architecture.md` 의 "아티팩트" 절을 같이 고친다.

## 문제가 생기면

**즉시 사용자에게 보고**: 인증 없이 응답하는 라우트 발견 · 프로필 키로 소유자 라우트가 열리거나 그 반대 · 프로필 삭제가 서비스 관리자를 건드림 · 로그·응답에 키 값 또는 본문 노출 · `_hermes_api` 가 없는 심볼을 조용히 None 으로 통과 · 사건 스트림이 사건을 건너뜀(중복은 허용, 구멍은 아님) · 통합 테스트가 사용자의 실제 `~/.hermes` 나 외부 플랫폼에 닿음 · 스테이징 게이트웨이 사망.

**그 외**: `docs/tracking/findings.md` 에 "조건 → 증상 → 영향 → 지금 못 푸는 이유" 로 기록한다. 이 플러그인 자체의 실증된 결함은 `docs/BACKLOG.md` 형식(증상·근거·근인·영향·방향·완료 조건)으로 등록한다.

## 푸시 전 필수

`scripts/ci-local.sh --full` 을 돌려 통과시킨 뒤에만 master 에 푸시한다.
CI 의 두 잡을 그대로 재현한다(약 1분). 단위 스위트만 돌리고 푸시하지 않는다 —
실제 Hermes 환경에서만 드러나는 결함이 실제로 있었다.
핀 커밋은 `.hermes-ref` 하나가 정본이다.
