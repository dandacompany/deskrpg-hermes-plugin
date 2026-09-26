"""모델에게 `deskrpg_ask_user` 를 언제 부르는지 알리는 상시 시스템 프롬프트 절.

Hermes 의 Tool Search 는 플러그인 도구를 tool_search 뒤로 미룬다 — 이 절이 이름을 알려 주지 않으면
모델은 도구가 있는 줄 모르고 평문으로 묻는다(2026-09-26 스테이징 실측)."""

SECTION_ID = "deskrpg_ask_user"

SECTION_TEXT = """## Asking the user with choices

When you need information only the user has, and the answer can be narrowed to 2-4 choices, call
`deskrpg_ask_user` with the question and the choices, then wait for the answer it returns and continue.
If it is not among your listed tools, find it with tool_search and call it through the tool-call bridge.

Ask open questions (ones without a short list of answers) in plain text instead. One question per call.
If the tool answers that no user is available, make the most reasonable assumption and say what you assumed.
"""
