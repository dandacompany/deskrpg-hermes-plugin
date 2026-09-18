---
name: artifact
description: 결과물을 DeskRPG 아티팩트로 저장하는 규칙과 artifact_save 호출 예시. 사용자가 아티팩트 저장·보관·등록을 말할 때 읽는다.
---

# 아티팩트 저장

`artifact_save` 는 사용자가 결과물을 남기라고 할 때만 부른다. 훅이 산출 도구의 파일을 자동으로 잡으므로, 말하지 않은 중간 산출물은 저장하지 않는다.

## 종류별 완결 조건

| kind | 넘기는 것 | 조건 |
| --- | --- | --- |
| document | `content`+`filename`(.md/.txt) 또는 `path`(.pdf/.docx) | 보고서는 제목·요약·본문이 있는 Markdown |
| web | `content`+`filename`(.html) | `<!doctype html>` 로 시작하는 **하나의 완전한** HTML. 외부 스크립트 없이 |
| react | `content`+`filename`(.tsx) | `export default` 컴포넌트 하나(.tsx/.jsx). DeskRPG 뷰어에서 바로 실행되려면 import 는 react 만 쓴다 — 도구가 거부하지는 않는다 |
| data | `content`+`filename`(.csv/.json/.jsonl) | 헤더 행이 있는 CSV, 배열 JSON, 또는 한 줄에 객체 하나인 JSONL |
| image / media | `path`(절대 경로) | 워크스페이스에 이미 있는 파일 |
| file | `path`(절대 경로) 또는 `content`+`filename` | 위 어디에도 맞지 않는 결과물(.zip 등)을 형식 검사 없이 그대로 남기는 탈출구. 맞는 kind 가 있으면 그걸 쓴다 |

`path` 는 항상 **절대 경로**다(`/…` 또는 `~/…`). 상대 경로는 거부된다.

## 예시

```json
{"kind":"document","title":"9월 3주 AI 동향","summary":"주간 조사 요약. 편집장 검토용","content":"# 9월 3주 AI 동향\n…","filename":"ai-weekly-w38.md"}
```

개선본은 `supersedes` 로 잇는다:

```json
{"kind":"document","title":"9월 3주 AI 동향","summary":"…","content":"…","filename":"ai-weekly-w38.md","supersedes":"<이전 artifact_id>","note":"출처 링크 3건 보강"}
```

## 실패 응답

`{"error":"artifact_incomplete","detail":"…"}` 가 오면 detail 대로 고쳐 다시 부른다. `artifact_path_outside_root` 면 경로가 절대 경로인지 먼저 확인하고(상대 경로는 거부된다), 그래도 루트 밖이면 파일을 워크스페이스로 옮기거나 `content` 로 넘긴다. `artifact_too_large` 면 압축·분할·요약 중 하나를 고른다. `artifact_path_sensitive` 면 자격증명·설정 파일(.env, auth.json, config.yaml 등)은 아티팩트로 저장할 수 없다 — 그 파일을 저장하려 하지 말고, 필요한 내용만 새 문서로 정리해 `content` 로 저장한다(비밀값은 옮기지 않는다). 데이터베이스 파일(.db·.sqlite·.sqlite3 와 -wal/-shm/-journal)과 이미 아티팩트 저장소 안에 있는 파일도 같은 오류로 거부된다 — DB 내용이 필요하면 조회 결과를 CSV/JSON 으로 뽑아 `data` 로 저장하고, 이미 저장된 아티팩트를 고친 것이면 `supersedes` 로 새 버전을 만든다.
