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
plugin_sha=$(git ls-remote https://github.com/dandacompany/deskrpg-hermes-plugin.git refs/tags/v0.26.0 | cut -f1)
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
- **Scan override.** Hub preview returns Hermes' security-scan verdict as `allow`, `ask` or `block`. A caller holding the profile's key can install an `ask` skill by sending `force: true`, which passes `--force` to `hermes skills install` — the same override Hermes' own CLI offers. `block` is refused whatever `force` says, and `force` is ignored for `allow`.
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

### Moving an install off the patched core

The plugin can enforce approvals on upstream Hermes with its own hooks and approval store. To move an install that
ran the patched core, carry its policies over first — the command reads the patch's table and never writes it:

```bash
# on the gateway host, in the Hermes environment, once per DeskRPG board
python -m deskrpg_plugin.review_migrate --board <board-slug> --dry-run   # report only
python -m deskrpg_plugin.review_migrate --board <board-slug>
```

It copies each card's policy and recorded approvals into the approval store and leaves a card that was waiting for
a person in `review` with nobody assigned. Cards it cannot move are listed under `skipped`. Then stop the gateway,
back up the kanban databases, reinstall upstream Hermes in the same environment, and restart the gateway.

## Upstream Hermes main

CI also runs the integration suite on upstream Hermes `main` (job `integration-upstream`, non-blocking; locally
`scripts/ci-local.sh --upstream`). Tests that need the approval-policy core patch are excluded there. Known
upstream breaks, excluded from that job with the `upstream_known_break` marker:

- **NPC skill management** (`profile_skill_admin`): Hermes main removed
  `hermes_cli.web_server_gateway._dashboard_spawn_executable`, which skill jobs use to start `hermes` commands.
  On such a build the capability is not announced and the skill routes are not registered.

## Release

0.26.0 runs on upstream Hermes main without Dante Labs patches for everything except review policies: the required Hermes API list no longer names underscore internals, status and field edits go through the public kanban verbs the upstream dashboard uses (`reclaim_task`, `promote_task`, `edit_task`), reopening a finished card whose descendants are running no longer fails, and PyYAML is declared in `python_dependencies` because upstream Hermes no longer ships it. `hermes plugins enable deskrpg` prepares the dependency. CI also runs the integration suite against upstream main.

0.25.0 creates swarms on boards with review policies (capability `swarm_review_policy`): workers get the requested policy and verifiers and synthesizers get human review, attached in the same write transaction that creates the swarm so no card becomes ready without its policy; the root stays a policy-free structure card. The capability is announced only when the Hermes internals it relies on are present with the expected signatures; otherwise a policy swarm answers `428 swarm_review_policy_unsupported`.

0.24.4 keeps the artifact id clock in one process-wide place too, so an upload through the route and a save from a tool in the same millisecond still get ordered ids.

0.24.3 keeps `ask_user` sessions and questions in one process-wide store, because Hermes imports a directory plugin once per profile home under a different module name and the route and the tool could otherwise see different copies.

0.24.2 finds an `ask_user` session registration by its session id (the profile comes from the registration route) because a multiplexed gateway's tool thread can read the root profile, and logs why a question fell back to assuming.

0.24.1 tells the model when and how to call `deskrpg_ask_user` in a system prompt section (Hermes Tool Search can hide plugin tools behind `tool_search`), and `task.run.started` events now carry the card's `assignee` so DeskRPG shows who is working as soon as a run starts.

0.24.0 lets an NPC ask its user a question with choices in DeskRPG 1:1 chat (capability `ask_user`): the `deskrpg_ask_user` tool (question, 2-4 choices, optional free answer) waits until the user answers or stops the run, but only in chat sessions DeskRPG registered (`POST /p/{profile}/deskrpg/ask-user/sessions`); kanban, cron and other sessions fall back to assuming at once. Pending questions are listed with `GET /p/{profile}/deskrpg/questions?session_id=` and answered with `POST /p/{profile}/deskrpg/questions/{id}/answer`; they live in memory only. Session sources now read kanban workspaces and card attachments from Hermes' boards root.

0.23.0 lists what a session read (capability `session_sources`): `GET /p/{profile}/deskrpg/sessions/{session_id}/sources` with the profile key reads the session's stored tool calls and returns web pages (`web_extract`, `browser_navigate`: URL and title, with credentials, fragments and token-like query values removed) and files (`read_file`, `delegate` results: paths relative to the working directory, or `<card>/<path>` inside a kanban card workspace; absolute paths are never returned). Nothing is stored; sessions follow Hermes' own retention.

0.22.0 issues a key for a profile that already exists (capability `profile_key_issue`): `POST /deskrpg/profiles/{name}/key` with the owner key writes a new `API_SERVER_KEY` to that profile's `.env` and returns it once. A profile that already has a key answers `409 key_exists` unless the body says `{"rotate": true}`; an existing key is never returned. The default profile (`400 default_profile`) and profiles whose key comes from an external secret provider (`409 external_secret_provider`) are refused. Hermes reads the new key without a restart.

0.21.0 adds run history for cards (capability `kanban_run_events`): card detail events carry the `run_id` of the run they belong to (null for card-level events), the event stream now closes runs that end as `spawn_failed` or `rate_limited` instead of dropping them, and board cards include `consecutive_failures`.

0.20.0 adds board status transitions for rework metrics (capability `kanban_task_events`): `GET /deskrpg/kanban/events?board=&from=&to=&kind=status&limit=` returns the status changes that happened inside the window (epoch seconds, inclusive, default 7 days), each with the status the card left even when that was set before the window; up to 1000 by default (5000 max), keeping the most recent with `truncated: true`.

0.19.0 adds board archiving (capability `board_archive`): `PATCH /deskrpg/kanban/boards/{slug}` accepts `archived: true|false`, archived boards leave `GET /deskrpg/kanban/boards` unless `?include_archived=true`, board metadata reports `archived`, the default board cannot be archived (`400 invalid_board`), and a board with running cards is refused with `409 board_has_running_cards` and its `running` count. A card-proposal resolve now records `task_id` only for the card choice, and time-based tests no longer depend on wall-clock budgets.

0.18.1 adds the cron job name (`jobName`, from the profile's `cron/jobs.json`) to `approval.blocked` events.

0.18.0 adds the unattended run approval policy (capability `profile_approval_policy`, routes under `/p/{profile}/deskrpg/approval-policy`): read and set `approvals.cron_mode` and `approvals.single_query_mode` (`deny`/`approve` only) and edit `command_allowlist` (dangerous-pattern keys). Worker processes also record `approval.blocked` events (opt-in `include=approvals`) when a cron job or kanban run is stopped by that policy or by an untrusted MCP write tool, with the pattern key when there is one and a redacted command.

0.17.1 prevents overlapping MCP OAuth attempts for the same server. Hermes lets a new attempt cancel an older one, but if the older worker finishes after the newer attempt has been approved, its rollback restores the pre-attempt token snapshot and erases the fresh authorization. `POST …/mcp/servers/{name}/oauth` now returns `409 oauth_in_progress` (with the open `sessionId`) while an attempt is still running; with `{"restart": true}` it cancels the open attempt, waits up to 10 seconds for its worker to exit, and only then starts a new one (`409 oauth_busy` if the worker has not exited). Cancelling also waits for the worker; if it has not exited the response adds `workerDone: false` and new attempts stay blocked until it does.

0.17.0 adds NPC MCP connector management (capability `profile_mcp_admin`, routes under `/p/{profile}/deskrpg/mcp`): list, add, edit and remove a profile's MCP servers (http or stdio; new stdio servers must repeat their name as `confirmName`, and custom stdio servers default to `trust: untrusted`); Hermes' own security check runs before every save and a rejection returns `422 mcp_security_rejected` with its reasons; enable/disable, `trust`, and tool selection (`tools.include`/`exclude`); write-only secrets that live only in the profile `.env` (responses report `hasValue`, never values); connection tests as background jobs that read each tool's read-only/destructive annotations; OAuth completed by pasting the loopback redirect URL; catalog install without prompts; reload scoped to the profile; and a secret-free export for copying a server to another profile. The last connection result and a value-free change log are kept in `plugin-data/deskrpg/`.

0.16.0 makes worker propagation opt-in and switches default text to English. Propagation into profile homes now runs only when the operator enables `plugins.entries.deskrpg.worker_propagation` (or `DESKRPG_WORKER_PROPAGATION=1`); otherwise `POST /deskrpg/worker-plugin` returns `409 worker_propagation_disabled`, profile creation skips it, and `/deskrpg/info` reports `worker_plugin.propagation`. Existing links are kept. The manifest description, the always-on system-prompt sections, tool descriptions, the artifact skill, and tool, route and log messages are now English. See [Permissions](#permissions).

0.15.0 adds NPC skill management (capability `profile_skill_admin`, routes under `/p/{profile}/deskrpg/skills`, `/curator` and `/learning`): skill list with provenance, usage and pin state; detail and file tree; editing `SKILL.md`, `references/` and `templates/` through Hermes' own write path with optimistic concurrency (`baseHash`); creating skills; per-skill and bulk enable/disable; pin, archive, restore and single-skill purge from the archive (ledger-recorded; pinned skills cannot be archived); Skills Hub search, preview with scan verdict, and install/uninstall/update as background jobs (one per profile); curator status, pause/resume and run; and the learning graph, whose memory nodes are only returned on request (`includeMemory=1`) and are edited or deleted only against a content hash. Deleted memory chunks are kept in `plugin-data/deskrpg/memory_deleted.jsonl` (0600) because Hermes has no undo for them.

0.14.0 adds `POST /deskrpg/events/handoff` and the `event_cursor_handoff` capability. When DeskRPG archives the project whose board carries a channel's event stream, the plugin merges the old carrier's global position into the new board's cursor so no card, cron or artifact event is lost across the handoff. DeskRPG 2026.922.3 and later requires this capability before archiving a carrier board; older DeskRPG versions ignore the route.

0.13.1 adds native per-task human and independent-agent approvals, submission-bound receipts, authenticated human display names, and capability gating. The runtime distribution excludes development instructions and test scaffolding. The source master retains CI tests.

0.13.1 corrects installation instructions for Hermes releases that require a full commit SHA in `--ref`. Runtime approval behavior is unchanged from 0.13.0.
