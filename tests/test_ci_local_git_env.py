"""`scripts/ci-local.sh` 가 훅이 물려준 git 환경변수를 끊는지 본다.

git 은 pre-push 훅을 부를 때 `GIT_DIR`(과 `GIT_WORK_TREE` 등)을 환경에 넣는다. 그 값이 있으면
`git -C <다른 저장소>` 가 **무시되고** 명령이 훅을 띄운 저장소에서 돈다. 그래서 스크립트의
Hermes 소스 fetch 가 플러그인 레포의 원격에 Hermes 커밋을 물어 `not our ref` 로 죽었고,
0.11.0·0.11.1 은 `--no-verify` 로 올려야 했다(2026-09-21 실측).

여기서는 두 가지를 함께 고정한다 — 스크립트에 가드가 **있다**는 것, 그리고 그 가드가 실제로
`git -C` 를 되살린다는 것. 앞의 것만 보면 문구가 바뀌어도 통과하고, 뒤의 것만 보면 git 의
동작을 확인할 뿐 우리 스크립트와 무관해진다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci-local.sh"
GUARD = "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX"
# 가드가 이 줄들보다 **앞에** 있어야 한다 — 뒤에 있으면 fetch 가 이미 실패한 뒤다.
FIRST_FOREIGN_GIT = 'git -C "$HERMES_SRC" fetch'


# 이 테스트 자신이 훅 환경(또는 그것을 흉내 낸 실행)에서 돌 수 있다. 그때 `git init` 이
# 주변의 `GIT_DIR` 을 물려받으면 임시 저장소가 만들어지지 않고 바깥 저장소가 대신 답한다 —
# 실측으로 `git remote add` 가 "origin 리모트가 이미 있습니다" 로 죽었다. 픽스처는 자기 환경을
# 직접 통제한다.
CLEAN_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin"}


def _repo(path: Path, url: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=CLEAN_ENV)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True, env=CLEAN_ENV)
    return path


def test_스크립트가_훅이_물려준_git_환경변수를_먼저_끊는다() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert GUARD in body, "가드가 없다 — 훅에서 부르면 Hermes fetch 가 엉뚱한 원격에 묻는다"
    assert body.index(GUARD) < body.index(FIRST_FOREIGN_GIT), (
        "가드가 Hermes 저장소를 다루는 명령보다 뒤에 있다"
    )


@pytest.mark.parametrize("guarded", [False, True])
def test_가드가_있어야_다른_저장소를_가리키는_C_가_살아난다(tmp_path: Path, guarded: bool) -> None:
    hook_repo = _repo(tmp_path / "plugin", "https://example.invalid/plugin.git")
    other_repo = _repo(tmp_path / "hermes", "https://example.invalid/hermes.git")

    script = f'{GUARD}\n' if guarded else ""
    script += f'git -C "{other_repo}" remote get-url origin'
    result = subprocess.run(
        ["bash", "-c", script],
        env={**CLEAN_ENV, "GIT_DIR": str(hook_repo / ".git")},
        capture_output=True,
        text=True,
        check=True,
    )
    url = result.stdout.strip()
    if guarded:
        assert url.endswith("hermes.git"), "가드를 두고도 -C 가 무시된다"
    else:
        # 가드가 없으면 훅을 띄운 저장소가 답한다 — 이것이 실패의 정체다.
        assert url.endswith("plugin.git"), "이 테스트의 전제(git 의 GIT_DIR 우선)가 깨졌다"
