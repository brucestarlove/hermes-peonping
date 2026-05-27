"""PeonPing plugin for Hermes lifecycle sounds.

The plugin maps selected Hermes lifecycle hooks into PeonPing-compatible event
payloads. PeonPing remains an optional external dependency; if the executable is
not installed, events are skipped without interrupting the agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    __package__ = "hermes_peonping"
    __path__ = [str(Path(__file__).resolve().parent)]
    sys.modules.setdefault(__package__, sys.modules[__name__])
    if __spec__ is not None:
        __spec__.name = __package__
        __spec__.submodule_search_locations = __path__

from .adapter import emit_payload, resolve_peon_command
from .config import ConfigLoadError, default_config_path, load_config
from .mapper import payload_from_kwargs

_HOOKS = (
    "pre_tool_call",
    "pre_llm_call",
    "post_llm_call",
    "pre_approval_request",
    "post_tool_call",
    "subagent_stop",
    "on_session_finalize",
    "on_session_reset",
)


def _make_hook(event_name: str):
    def _hook(**kwargs: Any) -> None:
        payload = payload_from_kwargs(event_name, kwargs)
        emit_payload(payload, quiet=True)
        return None

    _hook.__name__ = f"peonping_{event_name}"
    return _hook


def _slash(raw_args: str = "") -> str:
    try:
        cfg = load_config()
    except ConfigLoadError as exc:
        return "\n".join(
            [
                "PeonPing adapter",
                "status: config error",
                f"config: {default_config_path()}",
                f"error: {exc}",
            ]
        )
    args = (raw_args or "").strip().split()
    if args and args[0] == "json":
        return json.dumps(cfg.to_dict(), indent=2, sort_keys=True)
    lines = [
        "PeonPing adapter",
        f"enabled: {cfg.enabled}",
        f"config: {default_config_path()}",
        f"peon: {resolve_peon_command(cfg) or '<not found>'}",
        f"voicepack hint: {cfg.voicepack or '<PeonPing default>'}",
        f"timeout seconds: {cfg.timeout_seconds:g}",
        f"tool error sounds: {', '.join(cfg.tool_error_events) or '<none>'}",
        f"tool progress sounds: {', '.join(cfg.tool_progress_events) or '<none>'}",
        "events:",
    ]
    for name, enabled in sorted(cfg.enabled_events.items()):
        lines.append(f"  {'on ' if enabled else 'off'} {name}")
    return "\n".join(lines)


def register(ctx) -> None:
    for event_name in _HOOKS:
        ctx.register_hook(event_name, _make_hook(event_name))
    ctx.register_command(
        "peonping",
        handler=_slash,
        description="Show Hermes → PeonPing adapter status.",
        args_hint="[json]",
    )
