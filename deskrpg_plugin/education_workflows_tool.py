"""Narrow training-workflow tools backed by DeskRPG's canonical workflow API."""

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("deskrpg_plugin")

TOOLSET = "education_workflow"
API_PATH = "/api/internal/education-workflows"
MAX_RESPONSE_BYTES = 48_000


def _error(code: str, **extra) -> str:
    return json.dumps({"error": code, **extra}, ensure_ascii=False)


def _api_settings():
    base = (os.environ.get("DESKRPG_EDUCATION_WORKFLOW_API_URL") or os.environ.get("DESKRPG_EDUCATION_WORKFLOW_URL") or "").strip().rstrip("/")
    token = (os.environ.get("DESKRPG_EDUCATION_WORKFLOW_TOKEN") or "").strip()
    if not base or not token:
        return None, None
    if len(token) < 32 or not (base.startswith("https://") or base.startswith("http://127.0.0.1") or base.startswith("http://localhost") or base.startswith("http://deskrpg:")):
        return None, None
    return base + API_PATH, token


def _public_url(workflow_id: str) -> Optional[str]:
    """Build a user-facing link; never expose Docker-only hostnames in Slack."""
    base = (os.environ.get("DESKRPG_EDUCATION_WORKFLOW_URL") or "").strip().rstrip("/")
    if not base:
        return None
    from urllib.parse import quote, urlparse
    parsed = urlparse(base)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        return None
    if not parsed.netloc:
        return None
    return f"{base}/workflows/education?taskId={quote(workflow_id)}"


def _request(operation: str, body: dict, timeout: int = 15) -> str:
    url, token = _api_settings()
    if not url or not token:
        return _error("education_workflow_not_configured", detail="DeskRPG workflow connection is not configured")
    request = urllib.request.Request(
        url,
        data=json.dumps({"operation": operation, **body}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return _error("response_too_large")
            return json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — never forward transport details or headers
            data = {"errorCode": "desk_rpg_request_failed"}
        logger.info("[deskrpg] education workflow API returned HTTP %s", exc.code)
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — no request body, URL or credentials in logs
        logger.warning("[deskrpg] education workflow API unavailable: %s", type(exc).__name__)
        return _error("desk_rpg_unavailable", detail="DeskRPG API is unavailable")


def _clean(args, key, limit, required=True):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None if not required else ""
    return value.strip()[:limit]


def _task_link(workflow_id: str) -> Optional[str]:
    return _public_url(workflow_id)


def _unwrap(raw: str, *, workflow_id: Optional[str] = None) -> str:
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw
    if isinstance(data, dict):
        workflow = data.get("workflow")
        if isinstance(workflow, dict):
            data["deskRpgUrl"] = _task_link(str(workflow.get("id") or workflow_id or ""))
        elif workflow_id:
            data["deskRpgUrl"] = _task_link(workflow_id)
    return json.dumps(data, ensure_ascii=False)


LIST_SCHEMA = {
    "name": "education_workflow_list",
    "description": "List recent shared education and presentation workflows from DeskRPG. Use before creating a task if the current Slack thread may already have one.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
CREATE_SCHEMA = {
    "name": "education_workflow_create",
    "description": "Create or idempotently reopen a shared education workflow in DeskRPG. Call at the start of a new education/presentation request. The request is saved to the shared DeskRPG database and visible in both DeskRPG and Slack.",
    "parameters": {"type": "object", "required": ["title", "requestText"], "properties": {
        "title": {"type": "string", "maxLength": 200},
        "requestText": {"type": "string", "maxLength": 8000},
    }, "additionalProperties": False},
}
GET_SCHEMA = {
    "name": "education_workflow_get",
    "description": "Read the latest shared DeskRPG state, selected topic, stage, version and decision history for an existing education workflow before replying or changing it.",
    "parameters": {"type": "object", "required": ["workflowId"], "properties": {
        "workflowId": {"type": "string", "maxLength": 64},
    }, "additionalProperties": False},
}
UPDATE_SCHEMA = {
    "name": "education_workflow_update",
    "description": "Persist one user choice or workflow result to the canonical DeskRPG record so it appears in both Slack and DeskRPG. Always call get first, use its current version, preserve the returned task ID, and use a fresh UUID eventId. On stale_version, fetch again; never silently overwrite.",
    "parameters": {"type": "object", "required": ["workflowId", "expectedVersion", "eventId", "action"], "properties": {
        "workflowId": {"type": "string", "maxLength": 64},
        "expectedVersion": {"type": "integer", "minimum": 0},
        "eventId": {"type": "string", "maxLength": 64, "description": "A fresh UUID for this exact update; reuse only if retrying the same update."},
        "action": {"type": "string", "enum": ["save_candidates", "choose_topic", "save_brief", "select_team", "start_research", "save_research", "approve_research", "save_outline", "approve_outline", "save_cards", "save_document", "approve_final", "pause", "resume", "request_revision"]},
        "payload": {"type": "object", "description": "JSON data for this action, e.g. {candidateId}, {candidates:[{id,title,rationale}]}, {team:[profile names]}, {sources:[{title,url,note}]}, or {note}."},
    }, "additionalProperties": False},
}


def make_handler(api, operation: str):
    def handler(args, **kwargs) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            if operation == "list":
                return _request("list", {})
            if operation == "get":
                workflow_id = _clean(args, "workflowId", 64)
                if not workflow_id:
                    return _error("invalid_workflow_id")
                return _unwrap(_request("get", {"workflowId": workflow_id}), workflow_id=workflow_id)
            if operation == "create":
                title, request_text = _clean(args, "title", 200), _clean(args, "requestText", 8000)
                if not title or not request_text:
                    return _error("title_and_request_text_required")
                # Tool retries in one Hermes turn share task_id; new user turns get a new key.
                source = "|".join((str(kwargs.get("profile") or "training"), str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""), request_text))
                key = hashlib.sha256(source.encode("utf-8")).hexdigest()
                raw = _request("create", {"title": title, "requestText": request_text, "idempotencyKey": key,
                                           "sessionId": str(kwargs.get("session_id") or "")[:255]})
                return _unwrap(raw)
            workflow_id = _clean(args, "workflowId", 64)
            event_id = _clean(args, "eventId", 64)
            action = _clean(args, "action", 40)
            expected = args.get("expectedVersion")
            if not workflow_id or not event_id or not action or not isinstance(expected, int) or isinstance(expected, bool):
                return _error("workflow_id_version_event_id_and_action_required")
            raw = _request("update", {"workflowId": workflow_id, "expectedVersion": expected,
                                      "eventId": event_id, "action": action,
                                      "payload": args.get("payload") if isinstance(args.get("payload"), dict) else {}})
            return _unwrap(raw, workflow_id=workflow_id)
        except Exception as exc:  # noqa: BLE001 — avoid failing the Slack turn for an API client issue
            logger.warning("[deskrpg] education workflow tool failed: %s", type(exc).__name__)
            return _error("education_workflow_failed")
    return handler
