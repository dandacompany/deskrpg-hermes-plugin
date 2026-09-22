# DeskRPG Hermes Plugin

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
plugin_sha=$(git ls-remote https://github.com/dandacompany/deskrpg-hermes-plugin.git refs/tags/v0.13.1 | cut -f1)
hermes plugins install https://github.com/dandacompany/deskrpg-hermes-plugin --ref "$plugin_sha"
hermes plugins enable deskrpg
hermes plugins doctor deskrpg
```

Restart your Hermes gateway after installation or updates so the HTTP routes are attached. Connect the gateway from DeskRPG using its API Server credentials.

Once admitted to the Hermes plugin catalog, installation by the catalog name `deskrpg` will use the reviewed commit. Catalog updates go through a reviewed SHA-bump PR and `hermes plugins update deskrpg`. The plugin does not download or replace its own code.

## Permissions and data access

This plugin runs inside the Hermes gateway with the gateway process's filesystem authority. It registers an `api_server` platform handler, two tools (`artifact_save`, `propose_kanban_card`), two hooks (`post_tool_call`, `post_llm_call`), system-prompt sections and an artifact skill. It requires no additional API key to load.

HTTP routes require Hermes API Server authentication. Owner-key routes can manage all profiles and shared kanban boards, dispatch or terminate workers, and read automation results across profiles. Profile routes manage identity, configuration, cron jobs, provider credentials and OAuth flows. Profile-key isolation requires Hermes multiplex profiles; single-profile gateways resolve their own profile prefix to the listener owner key. Keep owner credentials server-side and restrict access to the gateway.

Creating a profile returns its newly generated API key once to the authenticated caller. Provider credentials are stored through the profile's Hermes configuration. Artifacts and automation metadata are persisted in local storage; automatic capture can retain content produced during agent work.

Optional artifact controls include `HERMES_DESKRPG_CAPTURE_RESPONSES=0`, `HERMES_DESKRPG_CAPTURE_LINKS=0`, `HERMES_DESKRPG_ARTIFACT_MAX_VERSIONS` (default 20; 0 is unlimited), `HERMES_DESKRPG_ARTIFACT_MAX_BYTES`, `HERMES_DESKRPG_ARTIFACTS_ROOT`, and `HERMES_DESKRPG_ARTIFACT_SOURCE_ROOTS`.

## Task approval compatibility

New DeskRPG cards require the `kanban_review_policy_v1` capability. It is advertised only when the complete native Hermes policy API is available. Installing this plugin alone does not add that API to an unpatched Hermes installation. Legacy cards and reads remain available; unsupported creation fails before writing.

The tested Hermes source is the Dante Labs compatibility branch `deskrpg/mixed-approval-v1` at commit `622a2f793f` in [dandacompany/hermes-agent](https://github.com/dandacompany/hermes-agent/tree/deskrpg/mixed-approval-v1), based on upstream `e2f8a0731bf2`. This patch is not an upstream Hermes release. See [the native policy guide](https://github.com/dandacompany/hermes-agent/blob/deskrpg/mixed-approval-v1/website/docs/user-guide/features/kanban-review-policy.md) for the contract and operator commands.

Stop the gateway and back up its kanban databases before replacing core code. Install the pinned source in your existing Hermes environment using its normal editable-install procedure, then restart the gateway. Keep this policy-aware core when rolling back the UI: older cores cannot enforce policies for existing protected cards. A normal upstream update can remove this compatibility patch.

New cards default to human approval. An explicitly delegated task can be approved by a different AI profile. Approvals bind to the submitted result; generic status changes cannot substitute for approval. AI reviewers must inspect the actual submitted text or artifacts; this does not guarantee the semantic quality of an AI review. Existing cards are not converted automatically.

## Release

0.13.1 adds native per-task human and independent-agent approvals, submission-bound receipts, authenticated human display names, and capability gating. The runtime distribution excludes development instructions and test scaffolding. The source master retains CI tests.

0.13.1 corrects installation instructions for Hermes releases that require a full commit SHA in `--ref`. Runtime approval behavior is unchanged from 0.13.0.
