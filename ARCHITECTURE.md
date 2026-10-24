# DeskRPG Hermes Plugin Architecture

## Overview
The DeskRPG Hermes Plugin (`deskrpg-hermes-plugin`) integrates Hermes AI agent profiles with the DeskRPG multi-agent office environment. It provides worker hooks, background propagation across agent profiles, and office communication tools.

## Core Modules & Lifecycle
- `deskrpg_plugin/worker_plugin.py`: Profile propagation, skill linking, and Windows junction/symlink lifecycle (`_ensure_link`, `_ensure_enabled`, `_link_state`).
- `deskrpg_plugin/api.py`: REST and event bridge between Hermes runtime and DeskRPG.
- `skills/`: Packaged agent skills propagated to worker profile directories.

## Concurrency & Link Invariants
- `_ensure_link` must safely handle concurrent callers targeting the same destination link or profile directory without race conditions, dangling junction deletions, or `WinError 183` / `FileExistsError`.
- Creation of junctions/symlinks must either be serialized per target path or employ atomic/ownership-checked operations.

## Governance & Remediation
- Scoped defect fixes (such as PR #1 Windows junction concurrency remediation on task `t_e76de94e`) are managed under Kanban tracking task `t_267ab44b`.
- Session state is tracked in `agent_share.md`.
