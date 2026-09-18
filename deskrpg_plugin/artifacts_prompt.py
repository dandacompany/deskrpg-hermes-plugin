"""모델에게 아티팩트 저장 규칙을 알리는 두 겹 — 상시 시스템 프롬프트 섹션(짧게)과 스킬(상세)."""

from pathlib import Path

SECTION_ID = "deskrpg.artifacts"
SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "artifact" / "SKILL.md"

SECTION_TEXT = (
    "## 아티팩트\n"
    "사용자가 결과물을 \"아티팩트로 저장/보관/등록해\", \"결과물로 남겨\" 라고 하면 `artifact_save` 도구를 부른다. "
    "저장 전에 산출물을 종류에 맞는 완결된 형태로 만든다 — 웹 페이지는 하나의 완전한 HTML 파일, 데이터는 CSV/JSON, "
    "보고서는 Markdown, 이미지·미디어는 파일 경로. 이미 저장한 것을 고쳤으면 `supersedes` 에 이전 artifact_id 를 넣고 "
    "`note` 에 무엇을 바꿨는지 적는다. 사용자가 말하지 않은 중간 산출물은 저장하지 않는다. "
    "저장 뒤에는 제목과 artifact_id 를 한 줄로 알린다."
)
