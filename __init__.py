"""플러그인 루트 진입점.

Hermes 로더는 `<plugin_dir>/__init__.py` 를 플러그인 디렉토리의 __path__ 로
로드한다(hermes_cli/plugins.py 의 spec_from_file_location +
submodule_search_locations). 실제 코드는 `deskrpg_plugin/` 서브패키지에 있고
테스트도 그걸 `from deskrpg_plugin import ...` 로 임포트하므로, 여기서는 얇게
재수출만 하고 로직을 옮기지 않는다.
"""

try:
    from .deskrpg_plugin import register
except ImportError:
    # pytest 가 레포 루트를 Package 로 인식해 이 파일을 패키지 컨텍스트 없이
    # 단독 임포트할 때(테스트 하네스 한정)를 위한 대비. Hermes 로더는 항상
    # submodule_search_locations 를 주므로 위 상대 임포트가 정상 경로다.
    from deskrpg_plugin import register

__all__ = ["register"]
