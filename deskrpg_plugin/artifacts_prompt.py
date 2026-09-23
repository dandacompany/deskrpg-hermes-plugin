"""모델에게 아티팩트 저장 규칙을 알리는 두 겹 — 상시 시스템 프롬프트 섹션(짧게)과 스킬(상세)."""

from pathlib import Path

SECTION_ID = "deskrpg.artifacts"
SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "artifact" / "SKILL.md"

SECTION_TEXT = (
    "## Artifacts\n"
    "When the user asks to save, keep or register a result as an artifact (in any language), call the `artifact_save` tool. "
    "Before saving, put the output in the complete form for its kind — a web page is one complete HTML file, data is CSV/JSON, "
    "a report is Markdown, images and media are file paths. If you revised something already saved, put the previous "
    "artifact_id in `supersedes` and say what changed in `note`. Do not save intermediate outputs the user did not ask for. "
    "When asked to keep a link, pass only `kind=link`, `url` and `summary` to `artifact_save`. "
    "After saving, report the title and artifact_id in one line."
)
