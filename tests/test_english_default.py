"""모델·사용자·운영자에게 나가는 문자열은 영어가 기본이다 — 매니페스트, 상시 프롬프트, 스킬, 도구·라우트 오류, 로그.

코드 주석과 docstring 은 대상이 아니다. 한글이 섞인 문자열 리터럴이 하나라도 생기면 여기서 걸린다.
"""

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")


def _docstring_ids(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def test_플러그인_코드의_문자열_리터럴에_한글이_없다():
    found = []
    for path in sorted((ROOT / "deskrpg_plugin").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and HANGUL.search(node.value)):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert found == []


def test_매니페스트_설명과_아티팩트_스킬은_영어다():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert not HANGUL.search(manifest["description"])
    assert not HANGUL.search((ROOT / "skills" / "artifact" / "SKILL.md").read_text(encoding="utf-8"))
