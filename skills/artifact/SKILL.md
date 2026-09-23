---
name: artifact
description: Rules and artifact_save call examples for saving results as DeskRPG artifacts. Read it when the user asks to save, keep or register an artifact.
---

# Saving artifacts

Call `artifact_save` only when the user asks to keep a result. A hook already captures files from output tools, so do not save intermediate outputs the user did not ask for.

## What "complete" means per kind

| kind | What to pass | Condition |
| --- | --- | --- |
| document | `content`+`filename` (.md/.txt) or `path` (.pdf/.docx) | A report is Markdown with a title, a summary and a body |
| web | `content`+`filename` (.html) | **One complete** HTML file starting with `<!doctype html>`, without external scripts |
| react | `content`+`filename` (.tsx) | One `export default` component (.tsx/.jsx). To run directly in the DeskRPG viewer, import only react — the tool does not reject other imports |
| data | `content`+`filename` (.csv/.json/.jsonl) | CSV with a header row, a JSON array, or JSONL with one object per line |
| image / media | `path` (absolute path) | A file that already exists in the workspace |
| file | `path` (absolute path) or `content`+`filename` | An escape hatch that keeps a result that fits none of the above (.zip etc.) without format checks. Use the matching kind when there is one |
| link | `url` (http/https) | The result lives on the web (a deployed URL, a shared document, an upload). Do not pass `path`, `content` or `filename`. Without `title`, the last segment of the URL becomes the title |

`path` is always an **absolute path** (`/…` or `~/…`). Relative paths are rejected.

## Examples

```json
{"kind":"document","title":"AI trends, week 3 of September","summary":"Weekly research summary for the editor's review","content":"# AI trends, week 3 of September\n…","filename":"ai-weekly-w38.md"}
```

Link a revised version with `supersedes`:

```json
{"kind":"document","title":"AI trends, week 3 of September","summary":"…","content":"…","filename":"ai-weekly-w38.md","supersedes":"<previous artifact_id>","note":"Added three source links"}
```

A link:

```json
{"kind":"link","title":"September report (shared copy)","summary":"Shared link for the editor's review","url":"https://docs.example.com/d/abc"}
```

## Error responses

If you get `{"error":"artifact_incomplete","detail":"…"}`, fix what the detail says and call again. For `artifact_path_outside_root`, first check that the path is absolute (relative paths are rejected); if it is still outside the root, move the file into the workspace or pass it as `content`. For `artifact_too_large`, compress, split or summarize. For `artifact_path_sensitive`, credential and settings files (.env, auth.json, config.yaml etc.) cannot be saved as artifacts — do not try to save that file; write only the needed content into a new document and save it as `content` (never copy secret values). Database files (.db, .sqlite, .sqlite3 and -wal/-shm/-journal) and files already inside the artifact store are rejected with the same error — if you need database contents, export the query result as CSV/JSON and save it as `data`; if you revised an artifact that is already saved, create a new version with `supersedes`.
