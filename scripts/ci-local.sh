#!/usr/bin/env bash
# CI 와 같은 것을 로컬에서 돌린다. **푸시 전에 이걸 돌린다.**
#
# 왜 있나: 2026-09-15 0.6.0 부터 master CI 가 6연속 빨강이었다. 전부 master 직접
# 푸시였고, 로컬에서 CI 와 같은 것을 돌릴 방법이 없어 아무도 푸시 전에 알 수 없었다.
# 특히 integration 잡은 실제 Hermes 를 깔고 도는데, 그 환경에서만 드러나는 결함이
# 실제로 있었다(가짜만으로는 통과하는 테스트).
#
#   scripts/ci-local.sh              # 단위 스위트만 (빠름, 몇 초)
#   scripts/ci-local.sh --full       # CI 와 동일 (Hermes 를 받아 editable 설치)
#
# 작업 산출물은 전부 gitignore 된 .ci-venv/ · .ci-hermes/ 에 들어가고 재사용된다.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="$ROOT/.ci-venv"
HERMES_SRC="$ROOT/.ci-hermes"
REF="$(tr -d '[:space:]' < "$ROOT/.hermes-ref")"
FULL=0
[ "${1:-}" = "--full" ] && FULL=1

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "venv 준비"
[ -d "$VENV" ] || python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements-dev.txt

say "단위 스위트 (CI 의 test 잡)"
"$PY" -m pytest -q

if [ "$FULL" -eq 0 ]; then
  printf '\n단위 스위트 통과. 통합까지 보려면 --full 로 다시 돌린다.\n'
  exit 0
fi

say "Hermes 소스 ($REF)"
if [ ! -d "$HERMES_SRC/.git" ]; then
  git clone --filter=blob:none --no-checkout \
    https://github.com/NousResearch/hermes-agent.git "$HERMES_SRC"
fi
git -C "$HERMES_SRC" fetch --depth 1 origin "$REF"
git -C "$HERMES_SRC" checkout --quiet "$REF"

say "Hermes editable 설치"
# 업스트림이 wheel·sdist 빌드를 막았다(setup.py 빌드 가드). editable 은 build_editable
# 을 쓰므로 가드에 걸리지 않는다 — 여기를 `pip install <url>` 로 되돌리지 말 것.
"$PY" -m pip install -q -e "$HERMES_SRC"
"$PY" -c "import hermes_cli, cron, hermes_state; print('hermes ok')"

say "가드 — 우리 tests 패키지가 가려지지 않았는가"
"$PY" - <<'GUARD'
import pathlib, tests
here = pathlib.Path.cwd().resolve()
paths = [pathlib.Path(p).resolve() for p in list(getattr(tests, "__path__", []))]
assert paths, f"tests 패키지를 찾지 못했다: {tests!r}"
for path in paths:
    assert path == here / "tests", f"tests 가 가려졌다 → {path}"
print("tests ok:", paths)
GUARD

say "통합 스위트 (CI 의 integration 잡)"
HERMES_INTEGRATION_REQUIRED=1 "$PY" -m pytest -q -m integration tests/integration

say "실제 Hermes 환경에서 단위 스위트 재실행"
# 가짜만으로는 통과하는 테스트를 여기서 잡는다.
HERMES_INTEGRATION_REQUIRED=1 "$PY" -m pytest -q

printf '\n\033[1mCI 와 동일한 검사를 전부 통과했다.\033[0m\n'
