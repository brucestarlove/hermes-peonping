from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_constants import get_hermes_home

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 2.0
AUTOMATIC_ROTATION_MODES = {"random", "round-robin", "shuffle"}


class ConfigLoadError(ValueError):
    """Raised when the PeonPing config file exists but cannot be loaded safely."""


DEFAULT_ENABLED_EVENTS: Dict[str, bool] = {
    "pre_tool_call": True,
    "pre_llm_call": True,
    "post_llm_call": True,
    "pre_approval_request": True,
    "post_tool_call": True,
    "subagent_stop": True,
    "on_session_finalize": True,
    "on_session_reset": True,
}


@dataclass
class AdapterConfig:
    """User controls for Hermes lifecycle events emitted to PeonPing."""

    schema_version: int = SCHEMA_VERSION
    enabled: bool = True
    source: str = "hermes"
    session_prefix: str = "hermes-"
    peon_command: str = ""
    peon_dir: str = ""
    voicepack: str = ""
    use_rotation: bool = False
    last_rotation_mode: str = "random"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    enabled_events: Dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ENABLED_EVENTS))
    tool_error_events: List[str] = field(default_factory=lambda: ["terminal"])
    tool_progress_events: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdapterConfig":
        merged_events = dict(DEFAULT_ENABLED_EVENTS)
        raw_events = data.get("enabled_events", {})
        if isinstance(raw_events, dict):
            for key, value in raw_events.items():
                parsed = _optional_bool(value)
                if parsed is not None:
                    merged_events[str(key)] = parsed

        return cls(
            schema_version=_schema_version(data.get("schema_version", SCHEMA_VERSION)),
            enabled=_bool_value(data.get("enabled", True), default=True),
            source=str(data.get("source", "hermes") or "hermes"),
            session_prefix=str(data.get("session_prefix", "hermes-") or ""),
            peon_command=str(data.get("peon_command", "") or ""),
            peon_dir=str(data.get("peon_dir", "") or ""),
            voicepack=str(data.get("voicepack", "") or ""),
            use_rotation=_bool_value(data.get("use_rotation", False), default=False),
            last_rotation_mode=_rotation_mode(data.get("last_rotation_mode"), default="random"),
            timeout_seconds=_timeout_seconds(data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            enabled_events=merged_events,
            tool_error_events=_string_list(data.get("tool_error_events"), ["terminal"]),
            tool_progress_events=_string_list(data.get("tool_progress_events"), []),
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        return data

    def event_enabled(self, event_name: str) -> bool:
        return self.enabled and bool(self.enabled_events.get(event_name, False))


def _string_list(value: Any, default: Iterable[str]) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return [p for p in parts if p]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default)


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _bool_value(value: Any, *, default: bool) -> bool:
    parsed = _optional_bool(value)
    return default if parsed is None else parsed


def _rotation_mode(value: Any, *, default: str) -> str:
    if isinstance(value, str):
        mode = value.strip()
        if mode in AUTOMATIC_ROTATION_MODES:
            return mode
    return default


def _schema_version(value: Any) -> int:
    try:
        version = int(value or SCHEMA_VERSION)
    except (TypeError, ValueError):
        return SCHEMA_VERSION
    if version <= 0:
        return SCHEMA_VERSION
    return version


def _timeout_seconds(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(timeout, 30.0)


def default_config_path() -> Path:
    env_path = os.environ.get("HERMES_PEONPING_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    return get_hermes_home() / "peonping" / "config.json"


def load_config(path: Optional[Path | str] = None) -> AdapterConfig:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AdapterConfig()
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"Invalid PeonPing adapter config JSON at {cfg_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigLoadError(f"Could not read PeonPing adapter config at {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"PeonPing adapter config at {cfg_path} must be a JSON object")
    try:
        return AdapterConfig.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigLoadError(f"Invalid PeonPing adapter config at {cfg_path}: {exc}") from exc


def save_config(config: AdapterConfig, path: Optional[Path | str] = None) -> Path:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return cfg_path
