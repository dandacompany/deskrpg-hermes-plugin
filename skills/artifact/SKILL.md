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
| react | `content`+`filename`(.tsx) | `export default` 컴포넌트 하나. import 는 react 만 |
| data | `content`+`filename`(.csv/.json) | 헤더 행이 있는 CSV 또는 배열 JSON |
| image / media | `path` | 워크스페이스에 이미 있는 파일 |

## 예시

```json
{"kind":"document","title":"9월 3주 AI 동향","summary":"주간 조사 요약. 편집장 검토용","content":"# 9월 3주 AI 동향\n…","filename":"ai-weekly-w38.md"}
```

개선본은 `supersedes` 로 잇는다:

```json
{"kind":"document","title":"9월 3주 AI 동향","summary":"…","content":"…","filename":"ai-weekly-w38.md","supersedes":"<이전 artifact_id>","note":"출처 링크 3건 보강"}
```

## 실패 응답

`{"error":"artifact_incomplete","detail":"…"}` 가 오면 detail 대로 고쳐 다시 부른다. `artifact_path_outside_root` 면 파일을 워크스페이스로 옮기거나 `content` 로 넘긴다. `artifact_too_large` 면 압축·분할·요약 중 하나를 고른다.
