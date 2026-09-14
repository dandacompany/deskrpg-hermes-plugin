"""플러그인이 지운 카드의 장부 — `board_dir(slug)/deskrpg_deleted.jsonl`.

Hermes 의 `delete_task` 는 `task_events` 까지 같이 지우므로 삭제 자체는 사건 스트림에
흔적이 없다. 그래서 플러그인이 지운 경우에만 여기 한 줄을 남기고, 사건 tail(`events.py`)이
`task.deleted` 를 이 파일에서 합성한다(spec §5.3).

- **append 전용**이다. 회전·절단하지 않는다 — 사건 커서가 줄 번호(`d`)를 쓰므로 줄이
  사라지거나 밀리면 커서가 엉뚱한 곳을 가리킨다.
- `n` 은 물리적 줄 번호(1부터)다. 깨진 줄(부분 기록)도 자리는 차지한다 — 읽을 때 건너뛰되
  번호는 보존한다.
- 같은 프로세스 안의 동시 삭제는 락으로 직렬화한다(줄 번호를 세고 쓰는 사이에 끼어들면
  두 줄이 같은 n 을 갖는다). 다른 프로세스와의 경합은 이 플러그인의 범위 밖이다 — 삭제는
  게이트웨이 한 프로세스만 한다.
"""

import json
import threading
import time

FILENAME = "deskrpg_deleted.jsonl"

_lock = threading.Lock()


def log_path(api, slug: str):
    """`board_dir(slug)/deskrpg_deleted.jsonl` — default 보드도 `board_dir("default")` 아래다."""
    return api.board_dir(slug) / FILENAME


_path = log_path


def _read_lines(path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def deleted_count(api, slug: str) -> int:
    """장부의 물리적 줄 수(= 마지막 줄 번호). 파일이 없으면 0."""
    return len(_read_lines(_path(api, slug)))


def append_deleted(api, slug: str, task_id: str, title: str) -> int:
    """한 줄 append 하고 그 줄 번호를 돌려준다. 보드 폴더가 없으면 만든다."""
    path = _path(api, slug)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        n = len(_read_lines(path)) + 1
        record = {"n": n, "task_id": task_id, "title": title, "ts": int(time.time())}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return n


def read_deleted_since(api, slug: str, after_line_no: int) -> list[dict]:
    """`after_line_no` 보다 큰 줄 번호의 기록만 `[{n, task_id, title, ts}]` 로. 깨진 줄은 건너뛴다."""
    out = []
    for idx, line in enumerate(_read_lines(_path(api, slug)), start=1):
        if idx <= after_line_no:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        out.append({"n": idx, "task_id": rec.get("task_id"), "title": rec.get("title"), "ts": rec.get("ts")})
    return out
