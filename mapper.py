from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from .config import AdapterConfig

Payload = Dict[str, Any]


def payload_from_kwargs(hook_event_name: str, kwargs: Dict[str, Any]) -> Payload:
    """Normalize Hermes plugin hook kwargs into a PeonPing adapter payload."""
    tool_input = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else kwargs.get("tool_input")
    return {
        "hook_event_name": hook_event_name,
        "tool_name": kwargs.get("tool_name"),
        "tool_input": tool_input if isinstance(tool_input, dict) else None,
        "session_id": kwargs.get("session_id") or kwargs.get("parent_session_id") or kwargs.get("session_key") or "",
        "cwd": kwargs.get("cwd") or _safe_cwd(),
        "extra": {
            k: v
            for k, v in kwargs.items()
            if k not in {"tool_name", "args", "tool_input", "session_id", "parent_session_id", "session_key", "cwd"}
        },
    }


def map_hermes_payload(payload: Payload, config: AdapterConfig) -> Optional[Payload]:
    """Map one Hermes lifecycle hook payload to a PeonPing event payload.

    Returns None when the event is disabled or intentionally silent.
    """
    if not config.enabled:
        return None

    event_name = str(payload.get("hook_event_name") or "")
    if not config.event_enabled(event_name):
        return None

    raw_extra = payload.get("extra")
    extra: Payload = raw_extra if isinstance(raw_extra, dict) else {}

    if event_name == "pre_llm_call":
        peon_event = "SessionStart" if bool(extra.get("is_first_turn")) else "UserPromptSubmit"
        return _base_event(peon_event, payload, config)

    if event_name == "post_llm_call":
        event = _base_event("Stop", payload, config)
        _copy_text(event, extra, "assistant_response", "last_assistant_message")
        return event

    if event_name == "pre_approval_request":
        event = _base_event("PermissionRequest", payload, config)
        command = str(extra.get("command") or extra.get("description") or "")
        event.update(
            {
                "notification_type": "permission_prompt",
                "tool_name": _approval_tool_name(extra),
                "message": command[:240],
            }
        )
        return event

    if event_name == "pre_tool_call":
        tool_name = str(payload.get("tool_name") or "")
        if tool_name not in set(config.tool_progress_events):
            return None
        event = _base_event("Notification", payload, config)
        message = _progress_message(tool_name, payload.get("tool_input"), extra)
        event.update(
            {
                "notification_type": "progress",
                "cesp_category": "task.progress",
                "tool_name": tool_name,
                "message": message[:240],
            }
        )
        return event

    if event_name == "post_tool_call":
        tool_name = str(payload.get("tool_name") or "")
        if tool_name not in set(config.tool_error_events):
            return None
        error = _tool_error_message(tool_name, extra.get("result"))
        if not error:
            return None
        event = _base_event("PostToolUseFailure", payload, config)
        event.update(
            {
                "tool_name": "Bash" if tool_name == "terminal" else tool_name,
                "hermes_tool_name": tool_name,
                "error": error[:500],
                "message": error[:240],
            }
        )
        return event

    if event_name == "subagent_stop":
        event = _base_event("SubagentStop", payload, config)
        _copy_text(event, extra, "child_summary", "summary")
        child_status = str(extra.get("child_status") or "").strip().lower()
        if child_status:
            event["status"] = child_status
        if child_status == "completed":
            event["cesp_category"] = "subagent.completed"
        elif child_status in {"failed", "timeout", "interrupted", "error"}:
            event["cesp_category"] = "subagent.blocked"
        return event

    if event_name in {"on_session_finalize", "on_session_reset"}:
        event = _base_event("SessionEnd", payload, config)
        event["cesp_category"] = "session.end"
        return event

    return None


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _base_event(peon_event: str, payload: Payload, config: AdapterConfig) -> Payload:
    session_id = str(payload.get("session_id") or "")
    if session_id and config.session_prefix and not session_id.startswith(config.session_prefix):
        session_id = config.session_prefix + session_id
    event: Payload = {
        "hook_event_name": peon_event,
        "session_id": session_id,
        "cwd": str(payload.get("cwd") or ""),
        "permission_mode": "",
        "source": config.source,
    }
    if config.voicepack and not config.use_rotation:
        event["voicepack"] = config.voicepack
    return event


def _copy_text(target: Payload, source: Payload, source_key: str, target_key: str) -> None:
    value = source.get(source_key)
    if isinstance(value, str) and value.strip():
        target[target_key] = re.sub(r"\s+", " ", value).strip()[:500]


def _progress_message(tool_name: str, tool_input: Any, extra: Payload) -> str:
    args = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "terminal" and isinstance(args.get("command"), str) and args.get("command", "").strip():
        return args["command"].strip()
    for key in ("goal", "path", "url", "query", "name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {value.strip()}"
    description = extra.get("description") if isinstance(extra, dict) else None
    if isinstance(description, str) and description.strip():
        return description.strip()
    return f"{tool_name} in progress"


def _approval_tool_name(extra: Payload) -> str:
    command = str(extra.get("command") or "")
    pattern_key = str(extra.get("pattern_key") or "")
    if pattern_key:
        return pattern_key
    if command:
        return "terminal"
    return "approval"


def _tool_error_message(tool_name: str, raw_result: Any) -> str:
    result = _coerce_result(raw_result)
    if isinstance(result, dict):
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            detail = _first_text(result, "stderr", "error", "output")
            return f"{tool_name} failed with exit code {exit_code}" + (f": {detail}" if detail else "")
        error = _first_text(result, "error", "stderr")
        if error:
            return f"{tool_name} failed: {error}"
        return ""
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return ""
        lowered = text.lower()
        if "error" in lowered or "traceback" in lowered or "failed" in lowered:
            return text
    return ""


def _coerce_result(raw_result: Any) -> Any:
    if isinstance(raw_result, (dict, list)):
        return raw_result
    if isinstance(raw_result, str):
        try:
            return json.loads(raw_result)
        except json.JSONDecodeError:
            return raw_result
    return raw_result


def _first_text(data: Payload, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()[:300]
    return ""
