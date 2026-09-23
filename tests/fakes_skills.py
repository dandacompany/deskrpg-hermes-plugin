"""Hermes 스킬 관리 함수의 파일 기반 가짜 — 요청 프로필 홈(`get_hermes_home()`)의 `skills/` 를 쓴다.

실제 Hermes 와 같은 규칙만 흉내 낸다: 폴더 이름으로 찾는 `_find_skill`, 프론트매터 이름으로 찾는
`_find_skill_dir`, hub/bundled 는 쓰기·보관 거절, 보관은 `skills/.archive/<name>` 으로 옮김.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path


def _front_name(skill_md: Path) -> str:
    for line in skill_md.read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return skill_md.parent.name


LEGACY_ROWS = (
    {"name": "hermes-agent", "description": "필수", "category": "core"},
    {"name": "pdf", "description": "PDF 다루기", "category": "docs"},
    {"name": "xlsx", "description": "엑셀", "category": "docs"},
)


class FakeSkills:
    def __init__(self, api):
        self.api = api
        self.hub: dict[str, set[str]] = {}
        self.bundled: dict[str, set[str]] = {}
        self.usage: dict[str, dict[str, dict]] = {}
        self.ledger: list[dict] = []
        self.external: dict[str, Path] = {}
        self.cache_clears = 0
        self.paused: dict[str, bool] = {}
        self.memory: dict[str, list[str]] = {}

    # --- 경로 ---
    def home(self) -> Path:
        return Path(self.api.get_hermes_home())

    def key(self) -> str:
        return str(self.home())

    def skills_dir(self) -> Path:
        return self.home() / "skills"

    # --- 씨앗 ---
    def seed(self, profile, name, *, source="local", body=None, files=None, folder=None, category=None):
        home = Path(self.api.get_profile_dir(profile))
        base = home / "skills" / (category or "") / (folder or name)
        base.mkdir(parents=True, exist_ok=True)
        (base / "SKILL.md").write_text(
            body or f"---\nname: {name}\ndescription: {name} 설명\n---\n# {name}\n", encoding="utf-8")
        for rel, text in (files or {}).items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        if source == "hub":
            self.hub.setdefault(str(home), set()).add(name)
        if source == "bundled":
            self.bundled.setdefault(str(home), set()).add(name)
        return base

    # --- Hermes 가짜 ---
    def find_all_skills(self, *, skip_disabled=False):
        rows = []
        for md in sorted(self.skills_dir().rglob("SKILL.md")) if self.skills_dir().exists() else []:
            if ".archive" in md.parts:
                continue
            rows.append({"name": _front_name(md), "description": f"{_front_name(md)} 설명", "category": ""})
        for name, path in self.external.items():
            rows.append({"name": name, "description": "외부", "category": ""})
        if not rows:
            # 씨앗을 심지 않은 테스트(0.9.0 부터의 config·picker 테스트)가 기대하는 옛 고정 목록.
            return [dict(r) for r in LEGACY_ROWS]
        return rows

    def find_skill(self, dirname):
        for md in self.skills_dir().rglob("SKILL.md") if self.skills_dir().exists() else []:
            if ".archive" not in md.parts and md.parent.name == dirname:
                return {"path": md.parent}
        return None

    def find_skill_dir(self, name):
        for md in self.skills_dir().rglob("SKILL.md") if self.skills_dir().exists() else []:
            if ".archive" not in md.parts and _front_name(md) == name:
                return md.parent
        return None

    def find_external_skill_dir(self, name):
        return self.external.get(name)

    def is_external_skill_path(self, path):
        return any(Path(path) == p for p in self.external.values())

    def is_hub_installed(self, name):
        return name in self.hub.get(self.key(), set())

    def is_bundled(self, name):
        return name in self.bundled.get(self.key(), set())

    def load_usage(self):
        return self.usage.setdefault(self.key(), {})

    def is_curator_managed(self, name):
        return self.load_usage().get(name, {}).get("created_by") == "agent"

    def set_pinned(self, name, pinned):
        if self.is_hub_installed(name) or self.is_bundled(name) or name in self.external:
            return False
        self.load_usage().setdefault(name, {})["pinned"] = bool(pinned)
        return True

    def archive_dir(self):
        return self.skills_dir() / ".archive"

    def archive_skill(self, name):
        if self.is_hub_installed(name):
            return False, f"skill '{name}' is hub-installed; never archive"
        if self.is_bundled(name):
            return False, f"skill '{name}' is a bundled built-in"
        src = self.find_skill_dir(name)
        if src is None:
            return False, f"skill '{name}' not found"
        dest = self.archive_dir() / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
        self.load_usage().setdefault(name, {})["state"] = "archived"
        return True, f"archived to {dest}"

    def restore_skill(self, name):
        src = self.archive_dir() / name
        if not src.is_dir():
            return False, f"skill '{name}' not found in archive"
        dest = self.skills_dir() / name
        if dest.exists():
            return False, f"destination already exists: {dest}"
        src.rename(dest)
        self.load_usage().setdefault(name, {})["state"] = "active"
        return True, f"restored to {dest}"

    def list_archived(self):
        root = self.archive_dir()
        return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []

    def create_skill(self, name, content, category=None):
        if self.find_skill(name) or self.find_skill_dir(name):
            return {"success": False, "error": f"A skill named '{name}' already exists"}
        if not content.startswith("---"):
            return {"success": False, "error": "SKILL.md must start with YAML frontmatter"}
        base = self.skills_dir() / (category or "") / name
        base.mkdir(parents=True)
        (base / "SKILL.md").write_text(content, encoding="utf-8")
        return {"success": True, "message": f"Skill '{name}' created."}

    def edit_skill(self, dirname, content):
        found = self.find_skill(dirname)
        if not found:
            return {"success": False, "error": f"Skill '{dirname}' not found."}
        if not content.startswith("---"):
            return {"success": False, "error": "SKILL.md must start with YAML frontmatter"}
        (found["path"] / "SKILL.md").write_text(content, encoding="utf-8")
        self.ledger.append({"action": "edit", "skill": dirname})
        return {"success": True}

    def write_file(self, dirname, file_path, content):
        found = self.find_skill(dirname)
        if not found:
            return {"success": False, "error": f"Skill '{dirname}' not found."}
        if file_path.split("/")[0] not in ("references", "templates", "scripts", "assets"):
            return {"success": False, "error": "file_path must be under an allowed subdir"}
        target = found["path"] / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.ledger.append({"action": "write_file", "skill": dirname, "path": file_path})
        return {"success": True}

    def capture_before(self, root, *, complete_package=False, skill=None):
        return [{"path": str(root), "sha256": "x"}]

    def append_entry(self, action, skill, before=None, after=None, actor=None, evidence=None):
        self.ledger.append({"action": action, "skill": skill, "actor": actor, "evidence": evidence or {}})
        return "e1"

    # --- curator / 관계도 ---
    def curator_state(self):
        return {"last_run_at": "2026-09-20T00:00:00+00:00"}

    def memory_cards(self):
        return [{"source": "memory", "title": c.splitlines()[0], "text": c}
                for c in self.memory.get(self.key(), [])]

    def build_graph(self):
        skills = [r["name"] for r in self.find_all_skills()]
        cards = self.memory_cards()
        nodes = [{"id": n, "label": n, "kind": "skill", "timestamp": 1} for n in skills]
        nodes += [{"id": f"memory:memory:{i}", "label": c["title"], "kind": "memory", "timestamp": 2}
                  for i, c in enumerate(cards)]
        edges = []
        if skills and cards:
            edges.append({"source": "memory:memory:0", "target": skills[0]})
        if len(skills) > 1:
            edges.append({"source": skills[0], "target": skills[1]})
        return {"nodes": nodes, "edges": edges, "memory": cards, "stats": {"memory_nodes": len(cards)}}

    def node_detail(self, node_id):
        if node_id.startswith("memory:"):
            idx = int(node_id.split(":")[2])
            chunks = self.memory.get(self.key(), [])
            if idx >= len(chunks):
                return {"ok": False, "message": "memory index out of range"}
            return {"ok": True, "kind": "memory", "id": node_id, "content": chunks[idx]}
        d = self.find_skill_dir(node_id)
        if d is None:
            return {"ok": False, "message": "not found"}
        return {"ok": True, "kind": "skill", "id": node_id, "content": (d / "SKILL.md").read_text(encoding="utf-8")}

    def edit_node(self, node_id, content):
        idx = int(node_id.split(":")[2])
        self.memory[self.key()][idx] = content
        return {"ok": True, "message": "updated"}

    def delete_node(self, node_id):
        if node_id.startswith("memory:"):
            idx = int(node_id.split(":")[2])
            del self.memory[self.key()][idx]
            return {"ok": True, "message": "deleted"}
        ok, msg = self.archive_skill(node_id)
        return {"ok": ok, "message": msg}


def install_fake_skills(api) -> FakeSkills:
    fs = FakeSkills(api)
    api.__dict__.update(
        _find_all_skills=fs.find_all_skills,
        load_usage=fs.load_usage,
        activity_count=lambda rec: sum(int(rec.get(k) or 0) for k in ("use_count", "view_count", "patch_count")),
        latest_activity_at=lambda rec: rec.get("last_used_at") or rec.get("last_viewed_at"),
        is_curator_managed=fs.is_curator_managed,
        is_hub_installed=fs.is_hub_installed,
        is_bundled=fs.is_bundled,
        set_pinned=fs.set_pinned,
        archive_skill=fs.archive_skill,
        restore_skill=fs.restore_skill,
        list_archived_skill_names=fs.list_archived,
        _archive_dir=fs.archive_dir,
        _find_skill_dir=fs.find_skill_dir,
        _find_external_skill_dir=fs.find_external_skill_dir,
        is_external_skill_path=fs.is_external_skill_path,
        capture_before=fs.capture_before,
        append_entry=fs.append_entry,
        set_ledger_actor=lambda actor: actor,
        reset_ledger_actor=lambda token: None,
        _create_skill=fs.create_skill,
        _edit_skill=fs.edit_skill,
        _write_file=fs.write_file,
        _find_skill=fs.find_skill,
        clear_skills_system_prompt_cache=lambda clear_snapshot=False: setattr(fs, "cache_clears", fs.cache_clears + 1),
        load_state=fs.curator_state,
        is_enabled=lambda: True,
        is_paused=lambda: fs.paused.get(fs.key(), False),
        set_paused=lambda paused: fs.paused.__setitem__(fs.key(), bool(paused)),
        get_interval_hours=lambda: 168,
        get_min_idle_hours=lambda: 2.0,
        get_stale_after_days=lambda: 14,
        get_archive_after_days=lambda: 30,
        build_learning_graph=fs.build_graph,
        node_detail=fs.node_detail,
        edit_node=fs.edit_node,
        delete_node=fs.delete_node,
        parse_node_kind=lambda node_id: "memory" if node_id.startswith("memory:") else "skill",
        create_source_router=lambda: ["src"],
        parallel_search_sources=lambda sources, **kw: ([], {}, []),
        _resolve_source_meta_and_bundle=lambda ident, sources: (None, None, None),
        quarantine_bundle=lambda bundle: Path("/nonexistent"),
        scan_skill=lambda path, source=None: types.SimpleNamespace(
            skill_name="x", source="community", trust_level="community", verdict="safe", summary="", findings=[]),
        should_allow_install=lambda result, force=False: (True, ""),
        _profile_action_environment=lambda subcommand, env_overrides=None: {"PATH": "/usr/bin", "HERMES_HOME": "x"},
        _dashboard_spawn_executable=lambda: "/usr/bin/python3",
    )
    api.skills = fs
    return fs
