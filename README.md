# DeskRPG Hermes Plugin

**DeskRPG backend — install through the DeskRPG setup wizard.** It is not a general-purpose Hermes plugin; on its own it only adds HTTP routes that DeskRPG calls.

Connect [DeskRPG](https://github.com/dandacompany/deskrpg), a self-hosted virtual office, to Hermes Agent. This standalone Python plugin extends the Hermes API Server; it is not a Hermes Desktop UI extension.

## Features

- Manage Hermes profiles, identity, model configuration, toolsets and skills from DeskRPG.
- Expose Hermes kanban boards, task actions, attachments and execution records.
- Manage profile-scoped cron jobs and retrieve automation events.
- Save versioned artifacts with `artifact_save`, and capture supported tool outputs, response code blocks and links.
- Propose kanban cards with `propose_kanban_card`.

## Requirements and installation

Python 3.11+ and Hermes Agent >=0.21.1 with the API Server enabled. Runtime dependencies are aiohttp and PyYAML, provided by Hermes. Internal Hermes API availability is checked during registration.

```sh
plugin_sha=$(git ls-remote https://github.com/dandacompany/deskrpg-hermes-plugin.git refs/tags/v0.17.1 | cut -f1)
hermes plugins install https://github.com/dandacompany/deskrpg-hermes-plugin --ref "$plugin_sha"
hermes plugins enable deskrpg
hermes plugins doctor deskrpg
```

Restart your Hermes gateway after installation or updates so the HTTP routes are attached. Connect the gateway from DeskRPG using its API Server credentials.

Once admitted to the Hermes plugin catalog, installation by the catalog name `deskrpg` will use the reviewed commit. Catalog updates go through a reviewed SHA-bump PR and `hermes plugins update deskrpg`. The plugin does not download or replace its own code.

## Permissions

This plugin runs inside the Hermes gateway with the gateway process's filesystem authority. It registers an `api_server` platform handler, two tools (`artifact_save`, `propose_kanban_card`), two hooks (`post_tool_call`, `post_llm_call`), system-prompt sections and an artifact skill. It requires no additional API key to load.

HTTP routes require Hermes API Server authentication. Owner-key routes can manage all profiles and shared kanban boards, dispatch or terminate workers, and read automation results across profiles. Profile routes manage identity, configuration, cron jobs, provider credentials and OAuth flows. Profile-key isolation requires Hermes multiplex profiles; single-profile gateways resolve their own profile prefix to the listener owner key. Keep owner credentials server-side and restrict access to the gateway.

Creating a profile returns its newly generated API key once to the authenticated caller. Provider credentials are stored through the profile's Hermes configuration. Artifacts and automation metadata are persisted in local storage; automatic capture can retain content produced during agent work.

### Worker propagation (off by default)

Kanban workers run as `hermes -p <profile>` and cron runs with `HERMES_HOME=<profile home>`. Hermes loads user plugins only from the running home's `plugins/` directory and its `plugins.enabled` list, so a plugin installed only in the gateway's root home is not loaded in those processes, and artifacts produced by workers are not captured.

When worker propagation is **enabled**, this plugin, on profile creation and on an owner-key `POST /deskrpg/worker-plugin`:

- creates a symlink `<profile home>/plugins/deskrpg` pointing to the root installation, and
- appends `deskrpg` to that profile's `config.yaml` `plugins.enabled` (a timestamped `0600` backup of the previous file is written next to it).

It never overwrites an existing non-link directory, and never enables the plugin in a profile that lists `deskrpg` under `plugins.disabled`. A symlink is used instead of a copy so every profile runs the same version as the root install. **Hermes' install-time security scan does not re-scan the linked directory**; each profile loads the code that was scanned when the root copy was installed or updated.

Propagation is **off by default**. The plugin only reads the setting; it has no code path that turns it on. Enable it on the gateway host with either:

```sh
hermes config set plugins.entries.deskrpg.worker_propagation true
# or, in the gateway's environment
DESKRPG_WORKER_PROPAGATION=1
```

The DeskRPG setup wizard asks before writing this setting. The value is read on every call, so no restart is needed. While it is off, `POST /deskrpg/worker-plugin` returns `409 worker_propagation_disabled` and writes nothing, profile creation returns `workerPlugin: {"skipped": "propagation_disabled"}`, and `/deskrpg/info` reports `worker_plugin.propagation: "disabled"`. Turning it off does not remove links or `plugins.enabled` entries that already exist; `/deskrpg/info` keeps reporting them.

### Skill management (0.15.0)

Routes under `/p/{profile}/deskrpg/skills`, `/curator` and `/learning` act on that profile's Hermes home:

- **Subprocesses.** Skills Hub install, uninstall and update, and curator runs, start `hermes -p <profile> skills install|uninstall|update …` or `hermes -p <profile> curator run` as a background job (one per profile) using Hermes' own executable and its profile-action environment, so the gateway profile's credentials are not passed to the child. Only the last 4 KB of output is kept, with secret-like values masked.
- **Network.** Hub search and preview go to the skill sources configured in Hermes, through Hermes' own source router. The plugin makes no network requests of its own.
- **File writes.** Editing and creating skills writes `SKILL.md`, `references/` and `templates/` through Hermes' skill write functions (other paths, symlinks and non-local skills are refused). Enable/disable writes `skills.disabled` in the profile's `config.yaml` with a backup. Pin, archive, restore and purge use Hermes' lifecycle functions; purge is recorded in the Hermes skill ledger.
- **Memory.** Learning-graph memory nodes are returned only on request (`includeMemory=1`) and are edited or deleted only when a content hash matches. Hermes has no undo for memory deletion, so each deleted chunk is appended to `<profile home>/plugin-data/deskrpg/memory_deleted.jsonl` (`0600`) first.

### Other settings

Optional artifact controls include `HERMES_DESKRPG_CAPTURE_RESPONSES=0`, `HERMES_DESKRPG_CAPTURE_LINKS=0`, `HERMES_DESKRPG_ARTIFACT_MAX_VERSIONS` (default 20; 0 is unlimited), `HERMES_DESKRPG_ARTIFACT_MAX_BYTES`, `HERMES_DESKRPG_ARTIFACTS_ROOT`, and `HERMES_DESKRPG_ARTIFACT_SOURCE_ROOTS`.

## Task approval compatibility

New DeskRPG cards require the `kanban_review_policy_v1` capability. It is advertised only when the complete native Hermes policy API is available. Installing this plugin alone does not add that API to an unpatched Hermes installation. Legacy cards and reads remain available; unsupported creation fails before writing.

The tested Hermes source is the Dante Labs compatibility branch `deskrpg/mixed-approval-v1` at commit `622a2f793f` in [dandacompany/hermes-agent](https://github.com/dandacompany/hermes-agent/tree/deskrpg/mixed-approval-v1), based on upstream `e2f8a0731bf2`. This patch is not an upstream Hermes release. See [the native policy guide](https://github.com/dandacompany/hermes-agent/blob/deskrpg/mixed-approval-v1/website/docs/user-guide/features/kanban-review-policy.md) for the contract and operator commands.

Stop the gateway and back up its kanban databases before replacing core code. Install the pinned source in your existing Hermes environment using its normal editable-install procedure, then restart the gateway. Keep this policy-aware core when rolling back the UI: older cores cannot enforce policies for existing protected cards. A normal upstream update can remove this compatibility patch.

New cards default to human approval. An explicitly delegated task can be approved by a different AI profile. Approvals bind to the submitted result; generic status changes cannot substitute for approval. AI reviewers must inspect the actual submitted text or artifacts; this does not guarantee the semantic quality of an AI review. Existing cards are not converted automatically.

## Release

0.17.1 prevents overlapping MCP OAuth attempts for the same server. Hermes lets a new attempt cancel an older one, but if the older worker finishes after the newer attempt has been approved, its rollback restores the pre-attempt token snapshot and erases the fresh authorization. `POST …/mcp/servers/{name}/oauth` now returns `409 oauth_in_progress` (with the open `sessionId`) while an attempt is still running; with `{"restart": true}` it cancels the open attempt, waits up to 10 seconds for its worker to exit, and only then starts a new one (`409 oauth_busy` if the worker has not exited). Cancelling also waits for the worker; if it has not exited the response adds `workerDone: false` and new attempts stay blocked until it does.

0.17.0 adds NPC MCP connector management (capability `profile_mcp_admin`, routes under `/p/{profile}/deskrpg/mcp`): list, add, edit and remove a profile's MCP servers (http or stdio; new stdio servers must repeat their name as `confirmName`, and custom stdio servers default to `trust: untrusted`); Hermes' own security check runs before every save and a rejection returns `422 mcp_security_rejected` with its reasons; enable/disable, `trust`, and tool selection (`tools.include`/`exclude`); write-only secrets that live only in the profile `.env` (responses report `hasValue`, never values); connection tests as background jobs that read each tool's read-only/destructive annotations; OAuth completed by pasting the loopback redirect URL; catalog install without prompts; reload scoped to the profile; and a secret-free export for copying a server to another profile. The last connection result and a value-free change log are kept in `plugin-data/deskrpg/`.

0.16.0 makes worker propagation opt-in and switches default text to English. Propagation into profile homes now runs only when the operator enables `plugins.entries.deskrpg.worker_propagation` (or `DESKRPG_WORKER_PROPAGATION=1`); otherwise `POST /deskrpg/worker-plugin` returns `409 worker_propagation_disabled`, profile creation skips it, and `/deskrpg/info` reports `worker_plugin.propagation`. Existing links are kept. The manifest description, the always-on system-prompt sections, tool descriptions, the artifact skill, and tool, route and log messages are now English. See [Permissions](#permissions).

0.15.0 adds NPC skill management (capability `profile_skill_admin`, routes under `/p/{profile}/deskrpg/skills`, `/curator` and `/learning`): skill list with provenance, usage and pin state; detail and file tree; editing `SKILL.md`, `references/` and `templates/` through Hermes' own write path with optimistic concurrency (`baseHash`); creating skills; per-skill and bulk enable/disable; pin, archive, restore and single-skill purge from the archive (ledger-recorded; pinned skills cannot be archived); Skills Hub search, preview with scan verdict, and install/uninstall/update as background jobs (one per profile); curator status, pause/resume and run; and the learning graph, whose memory nodes are only returned on request (`includeMemory=1`) and are edited or deleted only against a content hash. Deleted memory chunks are kept in `plugin-data/deskrpg/memory_deleted.jsonl` (0600) because Hermes has no undo for them.

0.14.0 adds `POST /deskrpg/events/handoff` and the `event_cursor_handoff` capability. When DeskRPG archives the project whose board carries a channel's event stream, the plugin merges the old carrier's global position into the new board's cursor so no card, cron or artifact event is lost across the handoff. DeskRPG 2026.922.3 and later requires this capability before archiving a carrier board; older DeskRPG versions ignore the route.

0.13.1 adds native per-task human and independent-agent approvals, submission-bound receipts, authenticated human display names, and capability gating. The runtime distribution excludes development instructions and test scaffolding. The source master retains CI tests.

0.13.1 corrects installation instructions for Hermes releases that require a full commit SHA in `--ref`. Runtime approval behavior is unchanged from 0.13.0.
