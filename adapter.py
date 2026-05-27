from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AdapterConfig, ConfigLoadError, load_config
from .mapper import map_hermes_payload

LOGGER = logging.getLogger(__name__)
_CONFIG_ERROR_LOGGED = False


@dataclass
class EmitResult:
    skipped: bool
    event: Optional[Dict[str, Any]] = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    command: str = ""


def emit_payload(
    payload: Dict[str, Any],
    config: Optional[AdapterConfig] = None,
    *,
    dry_run: bool = False,
    quiet: bool = True,
) -> EmitResult:
    try:
        cfg = config or load_config()
    except ConfigLoadError as exc:
        _log_config_error_once(exc)
        return EmitResult(skipped=True, stderr=str(exc))
    peon_event = map_hermes_payload(payload, cfg)
    if peon_event is None:
        if dry_run:
            print("{}")
        return EmitResult(skipped=True)

    if dry_run:
        print(json.dumps(peon_event, ensure_ascii=False, sort_keys=True))
        return EmitResult(skipped=False, event=peon_event)

    command = resolve_peon_command(cfg)
    if not command:
        msg = (
            "PeonPing executable not found. "
            "Set peon_command in the PeonPing adapter config."
        )
        if not quiet:
            print(msg, file=sys.stderr)
        return EmitResult(skipped=False, event=peon_event, returncode=127, stderr=msg)

    try:
        argv = _argv(command)
        proc = subprocess.run(
            argv,
            input=json.dumps(peon_event, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=cfg.timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        msg = f"Failed to run PeonPing executable: {exc}"
        if not quiet:
            print(msg, file=sys.stderr)
        return EmitResult(
            skipped=False,
            event=peon_event,
            returncode=1,
            stderr=msg,
            command=command,
        )
    if not quiet and proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    return EmitResult(
        skipped=False,
        event=peon_event,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        command=command,
    )


def resolve_peon_command(config: AdapterConfig) -> str:
    if config.peon_command:
        return config.peon_command

    env_script = os.environ.get("PEON_PING_SCRIPT")
    if env_script:
        return env_script

    if config.peon_dir:
        script = Path(config.peon_dir).expanduser() / "peon.sh"
        if script.exists():
            return str(script)

    candidates: List[Path] = []
    claude_peon_dir = os.environ.get("CLAUDE_PEON_DIR")
    if claude_peon_dir:
        candidates.append(Path(claude_peon_dir).expanduser() / "peon.sh")

    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_config:
        candidates.append(Path(claude_config).expanduser() / "hooks" / "peon-ping" / "peon.sh")

    home = Path.home()
    candidates.extend(
        [
            home / ".claude" / "hooks" / "peon-ping" / "peon.sh",
            home / ".openpeon" / "peon.sh",
            home / ".local" / "bin" / "peon",
            Path("/opt/homebrew/bin/peon"),
            Path("/usr/local/bin/peon"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    peon = shutil.which("peon")
    if peon:
        return peon
    return ""


def _argv(command: str) -> List[str]:
    expanded = os.path.expanduser(command)
    existing_path = Path(expanded)
    if existing_path.exists():
        if str(existing_path).endswith(".sh"):
            return ["bash", str(existing_path)]
        return [str(existing_path)]
    parts = shlex.split(expanded)
    if len(parts) == 1 and parts[0].endswith(".sh"):
        return ["bash", parts[0]]
    return parts


def _log_config_error_once(exc: Exception) -> None:
    global _CONFIG_ERROR_LOGGED
    if _CONFIG_ERROR_LOGGED:
        return
    _CONFIG_ERROR_LOGGED = True
    LOGGER.warning("PeonPing disabled for this hook because config could not be loaded: %s", exc)
