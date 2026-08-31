"""루트 __init__.py 가 Hermes 로더 방식대로 로드되는지 고정한다.

Hermes 는 `spec_from_file_location(module_name, <plugin_dir>/__init__.py,
submodule_search_locations=[plugin_dir])` 로 플러그인을 읽는다 — 이게
플러그인 디렉토리를 그 모듈의 __path__ 로 만들어, 루트 __init__.py 안의
`from .deskrpg_plugin import register` 상대 임포트가 풀리게 한다. 이 테스트는
그 로더 동작을 그대로 흉내 내서, 루트 쉼이 실제 설치 환경에서도 계속
`register` 를 노출하는지 고정한다.
"""

import importlib.util
import pathlib
import sys


def test_루트_패키지를_로더_방식대로_임포트하면_register_가_나온다():
    plugin_dir = pathlib.Path(__file__).resolve().parent.parent
    init_file = plugin_dir / "__init__.py"
    module_name = "deskrpg_root_shim_under_test"

    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(plugin_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    assert callable(module.register)
    assert module.register is not None
