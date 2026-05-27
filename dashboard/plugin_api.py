"""Hermes PeonPing dashboard plugin backend.

Mounted at /api/plugins/peonping/ by the Hermes dashboard.

Wraps the external `peon` CLI for pack management and reads installed
``openpeon.json`` files to render a soundboard. Prefers `--json` CLI output
when available and falls back to tolerant text parsing.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import FileResponse
except Exception:  # pragma: no cover - allow unit tests without FastAPI
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: Any = None) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:  # type: ignore[no-redef]
        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

    def Query(default=None, **_kwargs):  # type: ignore[no-redef]
        return default

    def FileResponse(*_args, **_kwargs):  # type: ignore[no-redef]
        raise RuntimeError("FileResponse requires fastapi to be installed")


def _load_sibling_modules():
    """Import this plugin's adapter/config siblings when loaded standalone.

    The dashboard loads ``plugin_api.py`` via ``spec_from_file_location``,
    which means the module runs outside the normal package context. When the
    plugin is installed as a user plugin at ``~/.hermes/plugins/peonping/``,
    the ``hermes_peonping`` package is not on ``sys.path`` — load the sibling
    files from disk using a private package name so relative imports inside
    ``adapter.py`` (``from .config import ...``) still resolve correctly.
    """
    import importlib
    import importlib.util as _ilu
    import sys as _sys

    pkg_dir = Path(__file__).resolve().parents[1]
    pkg_name = "_hermes_peonping_dashboard_pkg"

    if pkg_name not in _sys.modules:
        spec = _ilu.spec_from_file_location(
            pkg_name,
            pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load peonping package at {pkg_dir}")
        pkg = _ilu.module_from_spec(spec)
        _sys.modules[pkg_name] = pkg
        spec.loader.exec_module(pkg)

    adapter = importlib.import_module(f"{pkg_name}.adapter")
    config = importlib.import_module(f"{pkg_name}.config")
    return adapter, config


try:
    from hermes_peonping.adapter import resolve_peon_command
    from hermes_peonping.config import (
        AdapterConfig,
        ConfigLoadError,
        default_config_path,
        load_config,
        save_config,
    )
except ImportError:
    _adapter_mod, _config_mod = _load_sibling_modules()
    resolve_peon_command = _adapter_mod.resolve_peon_command
    AdapterConfig = _config_mod.AdapterConfig
    ConfigLoadError = _config_mod.ConfigLoadError
    default_config_path = _config_mod.default_config_path
    load_config = _config_mod.load_config
    save_config = _config_mod.save_config


router = APIRouter()


READ_TIMEOUT_SECONDS = 30
WRITE_TIMEOUT_SECONDS = 120
REGISTRY_CACHE_TTL_SECONDS = 60
REGISTRY_INDEX_URL = "https://peonping.github.io/registry/index.json"
REGISTRY_FETCH_TIMEOUT_SECONDS = 15
REGISTRY_FETCH_MAX_BYTES = 20 * 1024 * 1024

PACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROTATION_MODES = ("random", "round-robin", "shuffle", "session_override")
AUTOMATIC_ROTATION_MODES = ("random", "round-robin", "shuffle")
INACTIVE_ROTATION_MODE = "session_override"

_registry_cache_lock = threading.Lock()
_registry_cache: Dict[str, Any] = {"at": 0.0, "value": None}


class CommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int = 1,
        stdout: str = "",
        stderr: str = "",
        status_code: int = 500,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


# `peon` always colourises output (no --no-color flag at time of writing), so
# its stdout/stderr come back littered with ANSI SGR sequences like
# ``\x1b[90m``. They confuse our text parser (the display name ends up being
# the escape sequence itself) and render as garbage in the dashboard's
# operation log. Strip them at the subprocess boundary so every downstream
# helper sees plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    if not text:
        return text
    return _ANSI_RE.sub("", text)


def _command_argv(command: str) -> List[str]:
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


def _run_peon(
    *args: str,
    timeout: int = READ_TIMEOUT_SECONDS,
    stdin_input: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        cfg = load_config()
    except ConfigLoadError as exc:
        raise CommandError(f"PeonPing config error: {exc}", returncode=2, status_code=500) from exc
    command = resolve_peon_command(cfg)
    if not command:
        raise CommandError(
            "PeonPing executable not found. Install `peon` or set peon_command in the PeonPing adapter config.",
            returncode=127,
            status_code=503,
        )
    argv = _command_argv(command) + list(args)
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            input=stdin_input,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"PeonPing command timed out after {timeout}s: {' '.join(args)}",
            returncode=124,
            status_code=504,
            stderr=str(exc),
        ) from exc
    except (OSError, ValueError) as exc:
        raise CommandError(
            f"Failed to run PeonPing executable: {exc}",
            returncode=1,
            status_code=500,
        ) from exc
    return {
        "returncode": proc.returncode,
        "stdout": _strip_ansi(proc.stdout or ""),
        "stderr": _strip_ansi(proc.stderr or ""),
        "command": command,
        "args": list(args),
    }


def _run_peon_json(*args: str, timeout: int = READ_TIMEOUT_SECONDS) -> Optional[Any]:
    """Try `<args> --json`; return parsed JSON on success, else None.

    Returns None when --json isn't supported, the CLI errored, or the output
    wasn't parseable. The caller should then fall back to text parsing.
    """
    try:
        result = _run_peon(*args, "--json", timeout=timeout)
    except CommandError:
        return None
    if result["returncode"] != 0:
        return None
    text = (result["stdout"] or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _command_response(result: Dict[str, Any], *, ok: Optional[bool] = None) -> Dict[str, Any]:
    if ok is None:
        ok = result["returncode"] == 0
    return {
        "ok": bool(ok),
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "command": result.get("command", ""),
        "args": result.get("args", []),
    }


# ---------------------------------------------------------------------------
# Adapter config helpers
# ---------------------------------------------------------------------------


def _adapter_payload() -> Dict[str, Any]:
    try:
        cfg = load_config()
        cfg_dict = asdict(cfg)
        cfg_dict["_config_load_error"] = None
    except ConfigLoadError as exc:
        cfg = AdapterConfig()
        cfg_dict = asdict(cfg)
        cfg_dict["_config_load_error"] = str(exc)
    cfg_dict["_config_path"] = str(default_config_path())
    return cfg_dict


def _automatic_rotation_mode(mode: Any) -> str:
    if isinstance(mode, str):
        normalized = mode.strip()
        if normalized in AUTOMATIC_ROTATION_MODES:
            return normalized
    return ""


def _configured_rotation_mode() -> str:
    try:
        cfg = load_config()
    except ConfigLoadError:
        return "random"
    return _automatic_rotation_mode(getattr(cfg, "last_rotation_mode", "")) or "random"


def _preferred_rotation_mode(current_mode: Any = "") -> str:
    return _automatic_rotation_mode(current_mode) or _configured_rotation_mode()


def _set_adapter_voicepack(name: str, *, last_rotation_mode: str = "") -> None:
    try:
        cfg = load_config()
    except ConfigLoadError:
        cfg = AdapterConfig()
    cfg.voicepack = name
    cfg.use_rotation = False
    mode = _automatic_rotation_mode(last_rotation_mode)
    if mode:
        cfg.last_rotation_mode = mode
    try:
        save_config(cfg)
    except OSError:
        pass


def _set_adapter_use_rotation(enabled: bool, *, last_rotation_mode: str = "") -> None:
    try:
        cfg = load_config()
    except ConfigLoadError:
        cfg = AdapterConfig()
    cfg.use_rotation = bool(enabled)
    mode = _automatic_rotation_mode(last_rotation_mode)
    if mode:
        cfg.last_rotation_mode = mode
    try:
        save_config(cfg)
    except OSError:
        pass


def _disable_peon_rotation_if_needed(rotation: Dict[str, Any]) -> str:
    mode = _automatic_rotation_mode((rotation or {}).get("mode"))
    if not mode:
        return ""
    result = _run_peon("rotation", INACTIVE_ROTATION_MODE, timeout=WRITE_TIMEOUT_SECONDS)
    if result["returncode"] != 0:
        raise CommandError(
            "Failed to stop PeonPing rotation",
            returncode=result["returncode"],
            stdout=result["stdout"],
            stderr=result["stderr"],
            status_code=500,
        )
    return mode


def _enable_peon_rotation(rotation: Dict[str, Any]) -> str:
    current_mode = (rotation or {}).get("mode")
    mode = _preferred_rotation_mode(current_mode)
    if current_mode == mode:
        return mode
    result = _run_peon("rotation", mode, timeout=WRITE_TIMEOUT_SECONDS)
    if result["returncode"] != 0:
        raise CommandError(
            "Failed to use PeonPing rotation",
            returncode=result["returncode"],
            stdout=result["stdout"],
            stderr=result["stderr"],
            status_code=500,
        )
    return mode


# ---------------------------------------------------------------------------
# Pack list parsers (JSON-first with tolerant text fallback)
# ---------------------------------------------------------------------------


# Pack-row pattern handles common `peon packs list` shapes:
#   `name  display name  (lang, N sounds, M categories)`
#   `* name  display name  description`
# The leading marker (`*`, `>`, `-`, or whitespace) flags the active pack.
_PACK_ROW_RE = re.compile(
    r"^(?P<marker>[\*\->\s]*)?(?P<name>[A-Za-z0-9][A-Za-z0-9._-]+)\s+(?P<rest>.+)$"
)
_PACK_META_RE = re.compile(
    r"\(([^)]*?)\)\s*$"  # last parenthesized group, e.g. "(en, 17 sounds, 7 categories)"
)
_SOUND_COUNT_RE = re.compile(r"(\d+)\s*sounds?")
_CATEGORY_COUNT_RE = re.compile(r"(\d+)\s*(?:categor(?:y|ies)|cats?)")
_LANG_RE = re.compile(r"\b([a-z]{2}(?:-[A-Za-z]{2})?)\b")


def _normalize_pack(record: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    name = _strip_ansi(str(record.get("name") or record.get("id") or "")).strip()
    if not name:
        return {}
    display_name = _strip_ansi(str(
        record.get("display_name")
        or record.get("displayName")
        or record.get("label")
        or record.get("title")
        or name
    )).strip() or name
    description = _strip_ansi(str(record.get("description") or record.get("summary") or "")).strip()
    # Some `peon packs list` rows squeeze "<-- active" into the description as
    # a status marker rather than a true description. Surface it as the active
    # flag instead of letting it pollute the visible text.
    active_from_desc = False
    if description:
        if re.search(r"<--\s*active", description, re.IGNORECASE):
            active_from_desc = True
            description = re.sub(r"\s*<--\s*active\s*", "", description, flags=re.IGNORECASE).strip()
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "language": _strip_ansi(str(record.get("language") or record.get("lang") or "")).strip().lower(),
        "sound_count": _as_int(record.get("sound_count") or record.get("sounds")),
        "category_count": _as_int(record.get("category_count") or record.get("categories")),
        "installed": bool(record.get("installed", source != "registry")),
        "active": bool(record.get("active", False)) or active_from_desc,
        "source": source,
        "path": str(record.get("path") or "") or None,
    }


def _as_int(value: Any) -> int:
    try:
        if isinstance(value, list):
            return len(value)
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


_STANDALONE_SOUND_CELL_RE = re.compile(r"^\d+\s*sounds?$", re.IGNORECASE)
_ACTIVE_MARKER_CELL_RE = re.compile(r"^<--\s*active$", re.IGNORECASE)
_ACTIVE_INLINE_RE = re.compile(r"<--\s*active", re.IGNORECASE)
_HEADER_WITH_COUNT_RE = re.compile(r"^(installed|available|registry)\s+packs\s*\(\d+\)", re.IGNORECASE)
# Status / progress chatter `peon` prints before the pack table (e.g.
# "Fetching pack registry...", "Loading manifests...", "Registry: 329 packs
# available"). These would otherwise be parsed as a pack named "Fetching" /
# "Loading" / etc.
_STATUS_PREFIX_RE = re.compile(
    r"^(fetching|loading|downloading|syncing|scanning|updating|refreshing|registry:)",
    re.IGNORECASE,
)
# Treat parenthesised tail as metadata only when it actually looks like meta
# (`(en, 17 sounds, 7 categories)`), not when it's part of the display name
# like `The Abbot (Stronghold Crusader)`.
_PAREN_META_LOOKS_LIKE_META_RE = re.compile(r"\b(sound|categor)", re.IGNORECASE)
# `peon packs list --registry` postfixes installed entries with a check mark.
_INSTALLED_GLYPH_RE = re.compile(r"[✓✔✅]\s*$")


def _parse_pack_list_text(stdout: str, *, source: str) -> List[Dict[str, Any]]:
    """Parse `peon packs list` output into normalized pack dicts.

    Supports both layouts we've seen in the wild:

    * Legacy / test format: ``<marker> <slug>  <Display Name>  (en, N sounds, M categories)``
    * Real ``peon`` (>= 0.x) columnar: ``<slug>   <N sounds>   <Display Name>   [<-- active]``

    ANSI escapes are stripped at ``_run_peon`` already; we strip here too as
    defence-in-depth for callers that hand us raw text.
    """
    packs: List[Dict[str, Any]] = []
    for raw in (stdout or "").splitlines():
        line = _strip_ansi(raw).rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lowered = stripped.lower()
        if lowered.startswith(("name ", "installed packs", "available packs", "registry", "no packs")):
            if "no packs" in lowered:
                return []
            continue
        if _HEADER_WITH_COUNT_RE.match(lowered):
            continue
        if _STATUS_PREFIX_RE.match(lowered) or stripped.endswith("..."):
            # Status chatter ("Fetching pack registry...") — skip.
            continue
        match = _PACK_ROW_RE.match(line)
        if not match:
            continue
        marker = (match.group("marker") or "").strip()
        name = match.group("name").strip()
        if not PACK_NAME_RE.match(name):
            continue
        rest = match.group("rest").strip()
        # `peon packs list --registry` marks installed packs with a trailing
        # check-mark glyph. Promote it to the installed flag and strip from
        # the display text so it doesn't render as a literal "✓".
        installed_glyph = bool(_INSTALLED_GLYPH_RE.search(rest))
        if installed_glyph:
            rest = _INSTALLED_GLYPH_RE.sub("", rest).rstrip()
        active = marker in {"*", ">"} or bool(_ACTIVE_INLINE_RE.search(rest))
        installed = (
            source != "registry"
            or installed_glyph
            or "[installed]" in rest.lower()
            or active
        )

        cells = [c.strip() for c in re.split(r"\s{2,}", rest) if c.strip()]
        sound_cell_idx = next(
            (i for i, cell in enumerate(cells) if _STANDALONE_SOUND_CELL_RE.match(cell)),
            -1,
        )

        if sound_cell_idx >= 0:
            # Columnar layout (real peon).
            sound_count = int(re.match(r"^(\d+)", cells[sound_cell_idx]).group(1))
            display_cells = [
                cell
                for i, cell in enumerate(cells)
                if i != sound_cell_idx and not _ACTIVE_MARKER_CELL_RE.match(cell)
            ]
            display_name = " ".join(display_cells).strip() or name
            description = ""
            language = ""
            category_count = 0
        else:
            # Legacy paren-meta layout (test fixtures + older `peon`).
            meta_match = _PACK_META_RE.search(rest)
            # Only honour the parens as metadata when they actually look like
            # metadata, otherwise `The Abbot (Stronghold Crusader)` would have
            # "Stronghold Crusader" parsed as metadata.
            if meta_match and not _PAREN_META_LOOKS_LIKE_META_RE.search(meta_match.group(1)):
                meta_match = None
            meta = meta_match.group(1) if meta_match else ""
            display_and_desc = rest[: meta_match.start()].strip() if meta_match else rest
            # When there's paren metadata we know the line is structured
            # `<display> [-/—] <description> (meta)` so the em-dash separator
            # is meaningful. Without that anchor, names like
            # "Abe's Oddysee - Abe" are single display names — only split on
            # 2+ spaces, never on a lone hyphen.
            split_pattern = r"\s{2,}|\s+[-–—]\s+" if meta_match else r"\s{2,}"
            parts = re.split(split_pattern, display_and_desc, maxsplit=1)
            display_name = parts[0].strip() or name
            description = parts[1].strip() if len(parts) > 1 else ""
            lang_match = _LANG_RE.search(meta) if meta else None
            sound_match = _SOUND_COUNT_RE.search(meta) if meta else None
            cat_match = _CATEGORY_COUNT_RE.search(meta) if meta else None
            language = lang_match.group(1) if lang_match else ""
            sound_count = int(sound_match.group(1)) if sound_match else 0
            category_count = int(cat_match.group(1)) if cat_match else 0

        packs.append(
            _normalize_pack(
                {
                    "name": name,
                    "display_name": display_name,
                    "description": description,
                    "language": language,
                    "sound_count": sound_count,
                    "category_count": category_count,
                    "installed": installed,
                    "active": active,
                },
                source=source,
            )
        )
    return [p for p in packs if p]


def _list_packs(*, registry: bool) -> List[Dict[str, Any]]:
    if not registry:
        # Installed list comes straight off disk so it stays in lockstep with
        # our own rmtree/copytree operations. Going through `peon packs list`
        # added a refresh-lag where a just-removed pack would reappear in the
        # dashboard until the next manual reload.
        return _list_installed_from_fs()
    args = ["packs", "list", "--registry"]
    source = "registry"
    json_data = _run_peon_json(*args)
    if json_data is not None:
        items = _coerce_pack_records(json_data)
        packs = [p for p in (_normalize_pack(r, source=source) for r in items) if p]
        if packs or isinstance(json_data, list):
            return packs

    cli_error: Optional[CommandError] = None
    result: Optional[Dict[str, Any]] = None
    try:
        result = _run_peon(*args)
    except CommandError as exc:
        cli_error = exc

    if result is not None:
        if result["returncode"] != 0:
            cli_error = CommandError(
                "`peon packs list --registry` failed",
                returncode=result["returncode"],
                stdout=result["stdout"],
                stderr=result["stderr"],
                status_code=502,
            )
        else:
            packs = _parse_pack_list_text(result["stdout"], source=source)
            if packs or "no packs" in (result["stdout"] or "").lower():
                return packs

    try:
        return _list_registry_from_http()
    except CommandError:
        if cli_error is not None:
            raise cli_error
        raise CommandError(
            "`peon packs list --registry` failed",
            returncode=1,
            stdout="",
            stderr="registry output was not parseable and HTTP fallback failed",
            status_code=502,
        )


def _list_registry_from_http() -> List[Dict[str, Any]]:
    """Fetch the public OpenPeon registry directly.

    Browsing the registry is read-only and does not require the local ``peon``
    executable. Installing and switching packs still go through the CLI.
    """
    url = (os.environ.get("HERMES_PEONPING_REGISTRY_URL") or REGISTRY_INDEX_URL).strip()
    if not url:
        url = REGISTRY_INDEX_URL
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "hermes-peonping/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REGISTRY_FETCH_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)) or 200)
            if status >= 400:
                raise OSError(f"HTTP {status}")
            raw = response.read(REGISTRY_FETCH_MAX_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        raise CommandError(
            f"could not fetch PeonPing registry index from {url}: {exc}",
            returncode=1,
            status_code=502,
        ) from exc

    if len(raw) > REGISTRY_FETCH_MAX_BYTES:
        raise CommandError(
            f"PeonPing registry index from {url} exceeded {REGISTRY_FETCH_MAX_BYTES} bytes",
            returncode=1,
            status_code=502,
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommandError(
            f"could not parse PeonPing registry index from {url}: {exc}",
            returncode=1,
            status_code=502,
        ) from exc

    items = _coerce_pack_records(data)
    return [p for p in (_normalize_pack(r, source="registry") for r in items) if p]


def _list_installed_from_fs() -> List[Dict[str, Any]]:
    """Enumerate installed packs by walking pack root candidates on disk."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for root in _pack_root_candidates():
        if not root.exists() or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            manifest_path = entry / "openpeon.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            # Pack identity for every other endpoint (remove, sounds, audio,
            # _find_pack_dir) is the directory name. The manifest's `name`
            # field is often a different slug (e.g. dir=aod, manifest=aod-pack)
            # and using it here would make the dashboard issue requests for a
            # path that doesn't exist on disk. Use entry.name as the id, fall
            # back to manifest name only for display.
            name = entry.name.strip()
            if not name or not PACK_NAME_RE.match(name) or name in seen:
                continue
            seen.add(name)
            sound_count, category_count = _count_pack_contents(data)
            normalized = _normalize_pack(
                {
                    "name": name,
                    "display_name": data.get("display_name") or data.get("displayName") or data.get("name") or name,
                    "description": data.get("description") or "",
                    "language": data.get("language") or data.get("lang") or "",
                    "sound_count": sound_count,
                    "category_count": category_count,
                    "installed": True,
                    "active": False,
                    "path": str(entry),
                },
                source="installed",
            )
            if normalized:
                out.append(normalized)
    return out


def _count_pack_contents(manifest: Dict[str, Any]) -> Tuple[int, int]:
    sounds = 0
    categories = 0
    for _cat_name, sound_records in _iter_manifest_sounds(manifest):
        categories += 1
        sounds += len(sound_records)
    return sounds, categories


def _coerce_pack_records(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("packs", "items", "results", "data"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
        mapped: List[Dict[str, Any]] = []
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault("name", str(key))
            mapped.append(item)
        if mapped:
            return mapped
    return []


def _mark_active(packs: List[Dict[str, Any]], active_name: Optional[str] | List[str]) -> List[Dict[str, Any]]:
    raw_names = active_name if isinstance(active_name, list) else [active_name]
    active_names = {str(name).strip() for name in raw_names if str(name or "").strip()}
    if not active_names:
        return packs
    active_names.update(Path(name).name for name in list(active_names))
    active_lookup = {name.lower() for name in active_names}
    for pack in packs:
        pack_names = {
            str(pack.get("name") or "").strip().lower(),
            str(pack.get("display_name") or "").strip().lower(),
        }
        if pack_names & active_lookup:
            pack["active"] = True
    return packs


# ---------------------------------------------------------------------------
# Rotation parsers
# ---------------------------------------------------------------------------


_ROTATION_MODE_RE = re.compile(r"\bmode\b\s*[:=]\s*([A-Za-z_-]+)", re.IGNORECASE)
_ROTATION_PACK_RE = re.compile(r"^[\s\-\*•]*([A-Za-z0-9][A-Za-z0-9._-]+)\s*$")


def _parse_rotation_text(stdout: str) -> Dict[str, Any]:
    mode = ""
    packs: List[str] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        mode_match = _ROTATION_MODE_RE.search(line)
        if mode_match:
            mode = mode_match.group(1).strip()
            continue
        pack_match = _ROTATION_PACK_RE.match(line)
        if pack_match:
            name = pack_match.group(1)
            if PACK_NAME_RE.match(name) and name.lower() not in {"rotation", "mode", "packs", "none"}:
                packs.append(name)
    return {"mode": mode, "packs": packs}


def _get_rotation() -> Dict[str, Any]:
    json_data = _run_peon_json("packs", "rotation", "list")
    if isinstance(json_data, dict):
        return {
            "mode": str(json_data.get("mode") or ""),
            "packs": [str(p) for p in (json_data.get("packs") or []) if isinstance(p, str)],
        }
    if isinstance(json_data, list):
        return {"mode": "", "packs": [str(p) for p in json_data if isinstance(p, str)]}
    # Without peon installed, `_run_peon` would raise a 503 CommandError. The
    # dashboard already signals "PeonPing missing" via /status, so silently
    # degrading to an empty rotation here keeps the page useful (the setup
    # card still renders) instead of bubbling up as a 500 / error banner.
    try:
        result = _run_peon("packs", "rotation", "list")
    except CommandError:
        return {"mode": "", "packs": []}
    if result["returncode"] != 0:
        return {"mode": "", "packs": []}
    return _parse_rotation_text(result["stdout"])


# ---------------------------------------------------------------------------
# Status payload
# ---------------------------------------------------------------------------


def _peon_status_text() -> Dict[str, Any]:
    json_data = _run_peon_json("status")
    if isinstance(json_data, dict):
        volume = _parse_volume_value(json_data.get("volume"))
        notifications = _optional_status_bool(
            json_data.get("desktop_notifications", json_data.get("notifications"))
        )
        return {
            "active_pack": str(
                json_data.get("active_pack")
                or json_data.get("voicepack")
                or json_data.get("pack")
                or ""
            ),
            "rotation_mode": str(json_data.get("rotation_mode") or json_data.get("mode") or ""),
            "muted": _optional_muted_bool(json_data.get("muted", json_data.get("paused"))),
            "volume": volume,
            "desktop_notifications": notifications,
            "raw": json_data,
        }
    try:
        result = _run_peon("status", "--verbose")
    except CommandError:
        return _empty_peon_status()
    if result["returncode"] != 0:
        return _empty_peon_status()
    return _parse_status_text(result["stdout"])


def _empty_peon_status() -> Dict[str, Any]:
    return {
        "active_pack": "",
        "rotation_mode": "",
        "muted": None,
        "volume": None,
        "desktop_notifications": None,
        "raw": None,
    }


def _parse_status_text(stdout: str) -> Dict[str, Any]:
    active = ""
    mode = ""
    muted: Optional[bool] = None
    volume: Optional[float] = None
    desktop_notifications: Optional[bool] = None
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line_l = line.lower()
        if line_l.startswith("peon-ping: paused"):
            muted = True
        elif line_l.startswith("peon-ping: active"):
            muted = False
        elif line_l.startswith("peon-ping: volume"):
            volume = _parse_volume_value(line.split("volume", 1)[-1].strip())
        elif line_l.startswith("desktop notifications on"):
            desktop_notifications = True
        elif line_l.startswith("desktop notifications off"):
            desktop_notifications = False
        if ":" in line:
            key, _, value = line.partition(":")
            key_l = key.strip().lower()
            value_s = value.strip()
            if key_l in {"active", "active pack", "active pack (here)", "voicepack", "pack"}:
                active = value_s
            elif key_l in {"mode", "rotation", "rotation mode"}:
                mode = value_s
            elif key_l == "volume":
                volume = _parse_volume_value(value_s)
    return {
        "active_pack": active,
        "rotation_mode": mode,
        "muted": muted,
        "volume": volume,
        "desktop_notifications": desktop_notifications,
        "raw": stdout,
    }


def _parse_volume_value(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        raw = float(value)
        if math.isfinite(raw):
            return max(0.0, min(1.0, raw if raw <= 1.0 else raw / 100.0))
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    try:
        raw = float(text)
    except ValueError:
        return None
    if not math.isfinite(raw):
        return None
    if is_percent or raw > 1.0:
        raw = raw / 100.0
    return max(0.0, min(1.0, raw))


def _optional_status_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled", "active"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _optional_muted_bool(value: Any) -> Optional[bool]:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"paused", "muted"}:
            return True
        if lowered in {"active", "resumed", "unmuted"}:
            return False
    return _optional_status_bool(value)


def _status_payload() -> Dict[str, Any]:
    try:
        cfg = load_config()
        cfg_error = None
    except ConfigLoadError as exc:
        cfg = AdapterConfig()
        cfg_error = str(exc)
    peon_command = resolve_peon_command(cfg)
    peon_found = bool(peon_command)

    peon_status: Dict[str, Any] = _empty_peon_status()
    rotation: Dict[str, Any] = {"mode": "", "packs": []}
    last_error: Optional[str] = cfg_error

    if peon_found:
        try:
            peon_status = _peon_status_text()
        except CommandError as exc:
            last_error = str(exc)
        try:
            rotation = _get_rotation()
        except CommandError as exc:
            if last_error is None:
                last_error = str(exc)

    active_pack = "Rotation" if cfg.use_rotation else (peon_status.get("active_pack") or cfg.voicepack or "")

    return {
        "ok": True,
        "peon_found": peon_found,
        "peon_command": peon_command,
        "config_path": str(default_config_path()),
        "adapter": {
            "enabled": cfg.enabled,
            "peon_command": cfg.peon_command,
            "voicepack": cfg.voicepack,
            "use_rotation": cfg.use_rotation,
            "enabled_events": dict(cfg.enabled_events),
            "timeout_seconds": cfg.timeout_seconds,
            "source": cfg.source,
        },
        "active_pack": active_pack,
        "rotation_mode": peon_status.get("rotation_mode") or rotation.get("mode") or "",
        "rotation_packs": rotation.get("packs") or [],
        "muted": peon_status.get("muted"),
        "volume": peon_status.get("volume"),
        "desktop_notifications": peon_status.get("desktop_notifications"),
        "last_error": last_error,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_pack_names(names: Any) -> List[str]:
    if not isinstance(names, list):
        raise HTTPException(status_code=400, detail="`names` must be a list of pack names")
    cleaned: List[str] = []
    for item in names:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail="pack names must be strings")
        name = item.strip()
        if not PACK_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail=f"invalid pack name: {item!r}")
        cleaned.append(name)
    if not cleaned:
        raise HTTPException(status_code=400, detail="provide at least one pack name")
    return cleaned


def _validate_pack_name(name: Any) -> str:
    if not isinstance(name, str) or not PACK_NAME_RE.match(name.strip()):
        raise HTTPException(status_code=400, detail=f"invalid pack name: {name!r}")
    return name.strip()


def _validate_bool(value: Any, field: str) -> bool:
    parsed = _optional_status_bool(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"`{field}` must be a boolean")
    return parsed


def _validate_volume(value: Any) -> float:
    parsed = _parse_volume_value(value)
    if parsed is None:
        raise HTTPException(status_code=400, detail="`volume` must be a number between 0.0 and 1.0")
    raw = float(value) if isinstance(value, (int, float)) else float(str(value).strip().rstrip("%"))
    if not math.isfinite(raw):
        raise HTTPException(status_code=400, detail="`volume` must be a finite number")
    is_percent = isinstance(value, str) and value.strip().endswith("%")
    if raw < 0 or (is_percent and raw > 100) or (not is_percent and raw > 1.0):
        raise HTTPException(status_code=400, detail="`volume` must be between 0.0 and 1.0")
    return round(parsed, 2)


def _validate_peon_command(raw: Any) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="`peon_command` must be a string")
    command = raw.strip()
    if not command:
        return ""
    try:
        argv = _command_argv(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid peon_command: {exc}") from exc
    if not argv:
        raise HTTPException(status_code=400, detail="`peon_command` did not resolve to an executable")

    executable = argv[1] if len(argv) >= 2 and argv[0] == "bash" else argv[0]
    expanded = os.path.expanduser(executable)
    candidate = Path(expanded)
    if candidate.exists():
        if candidate.is_dir():
            raise HTTPException(status_code=400, detail=f"peon_command points to a directory: {candidate}")
        if candidate.suffix != ".sh" and not os.access(candidate, os.X_OK):
            raise HTTPException(status_code=400, detail=f"peon_command is not executable: {candidate}")
        return command
    if os.sep not in executable and shutil.which(executable):
        return command
    raise HTTPException(status_code=400, detail=f"peon executable not found: {executable}")


def _validate_local_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=400, detail="`path` is required")
    candidate = Path(os.path.expanduser(raw.strip())).resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise HTTPException(status_code=400, detail=f"local pack directory not found: {candidate}")
    if not (candidate / "openpeon.json").exists():
        raise HTTPException(status_code=400, detail="local pack directory does not contain openpeon.json")
    return candidate


def _validate_rotation_mode(mode: Any) -> str:
    if not isinstance(mode, str) or mode.strip() not in ROTATION_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid rotation mode; expected one of {list(ROTATION_MODES)}",
        )
    return mode.strip()


# ---------------------------------------------------------------------------
# Pack directory discovery + soundboard manifest
# ---------------------------------------------------------------------------


def _pack_root_candidates() -> List[Path]:
    env = os.environ.get("HERMES_PEONPING_PACK_ROOTS", "").strip()
    roots: List[Path] = []
    if env:
        for entry in env.split(os.pathsep):
            entry = entry.strip()
            if entry:
                roots.append(Path(os.path.expanduser(entry)))
    try:
        cfg = load_config()
    except ConfigLoadError:
        cfg = AdapterConfig()
    if cfg.peon_dir:
        roots.append(Path(os.path.expanduser(cfg.peon_dir)) / "packs")
    home = Path.home()
    roots.extend(
        [
            home / ".openpeon" / "packs",
            home / ".peonping" / "packs",
            home / ".claude" / "hooks" / "peon-ping" / "packs",
        ]
    )
    seen: set = set()
    unique: List[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(root)
    return unique


def _resolve_active_pack() -> str:
    """Best-effort current active pack name. Prefers peon status, falls back to adapter config."""
    try:
        cfg = load_config()
    except ConfigLoadError:
        cfg = AdapterConfig()
    if cfg.use_rotation:
        return ""
    active = ""
    try:
        active = str(_peon_status_text().get("active_pack") or "").strip()
    except CommandError:
        active = ""
    return active or (cfg.voicepack or "").strip()


def _primary_pack_install_root() -> Path:
    """First existing pack root, or the first configured candidate if none exist yet."""
    candidates = _pack_root_candidates()
    if not candidates:
        raise HTTPException(
            status_code=500,
            detail="No pack root configured. Set peon_dir in the PeonPing adapter config or HERMES_PEONPING_PACK_ROOTS.",
        )
    for root in candidates:
        if root.exists() and root.is_dir():
            return root
    return candidates[0]


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _find_pack_dir(pack_name: str) -> Path:
    name = _validate_pack_name(pack_name)
    for root in _pack_root_candidates():
        if not root.exists() or not root.is_dir():
            continue
        candidate = root / name
        if not (candidate / "openpeon.json").exists():
            continue
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return resolved
    raise HTTPException(status_code=404, detail=f"installed pack not found: {pack_name}")


def _load_openpeon_manifest(pack_dir: Path) -> Dict[str, Any]:
    manifest_path = pack_dir / "openpeon.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"could not read openpeon.json: {exc}") from exc


def _iter_manifest_sounds(manifest: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Yield (category_name, [sound_record, ...]) tuples from an openpeon manifest.

    Tolerates the three shapes we've seen:

    * Real ``openpeon.json``:
      ``{"categories": {"session.start": {"sounds": [{"file": "...", "label": "..."}]}}}``
    * List-of-objects: ``{"categories": [{"name": "...", "sounds": [...]}, ...]}``
    * Legacy flat: ``{"sounds": {"category_name": [files...]}}``
    """
    raw_categories = manifest.get("categories")
    if isinstance(raw_categories, dict):
        out: List[Tuple[str, List[Dict[str, Any]]]] = []
        for cat_name, value in raw_categories.items():
            if isinstance(value, dict):
                sounds = _normalize_sound_records(value.get("sounds"))
            else:
                sounds = _normalize_sound_records(value)
            if sounds:
                out.append((str(cat_name), sounds))
        if out:
            return out
    if isinstance(raw_categories, list):
        out = []
        for entry in raw_categories:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("category") or "").strip()
            sounds = _normalize_sound_records(entry.get("sounds"))
            if name and sounds:
                out.append((name, sounds))
        if out:
            return out
    raw_sounds = manifest.get("sounds")
    if isinstance(raw_sounds, dict):
        out = []
        for name, value in raw_sounds.items():
            sounds = _normalize_sound_records(value)
            if sounds:
                out.append((str(name), sounds))
        return out
    return []


def _normalize_sound_records(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append({"file": item, "label": ""})
            elif isinstance(item, dict):
                file_val = item.get("file") or item.get("path") or item.get("src") or ""
                label = item.get("label") or item.get("title") or item.get("text") or ""
                if file_val:
                    out.append({"file": str(file_val), "label": str(label)})
    return out


def _soundboard_payload(pack_name: str) -> Dict[str, Any]:
    pack_dir = _find_pack_dir(pack_name)
    manifest = _load_openpeon_manifest(pack_dir)
    categories_out: List[Dict[str, Any]] = []
    total_sounds = 0
    for cat_name, sounds in _iter_manifest_sounds(manifest):
        cat_sounds: List[Dict[str, Any]] = []
        for idx, sound in enumerate(sounds):
            sound_id = f"{cat_name}:{idx}"
            cat_sounds.append(
                {
                    "id": sound_id,
                    "file": sound["file"],
                    "label": sound["label"] or _label_from_file(sound["file"]),
                    "audio_url": (
                        f"/api/plugins/peonping/packs/{urllib.parse.quote(pack_name, safe='')}"
                        f"/audio/{urllib.parse.quote(sound_id, safe='')}"
                    ),
                }
            )
        total_sounds += len(cat_sounds)
        categories_out.append(
            {
                "name": cat_name,
                "label": cat_name.upper(),
                "sounds": cat_sounds,
            }
        )
    return {
        "ok": True,
        "pack": {
            "name": pack_name,
            "display_name": str(manifest.get("display_name") or manifest.get("displayName") or pack_name),
            "description": str(manifest.get("description") or "").strip(),
            "language": str(manifest.get("language") or manifest.get("lang") or "").strip().lower(),
            "sound_count": total_sounds,
            "category_count": len(categories_out),
            "path": str(pack_dir),
        },
        "categories": categories_out,
    }


def _label_from_file(file_path: str) -> str:
    name = Path(file_path).stem.replace("_", " ").replace("-", " ").strip()
    return name or file_path


def _resolve_sound_path(pack_name: str, sound_id: str) -> Path:
    pack_dir = _find_pack_dir(pack_name)
    manifest = _load_openpeon_manifest(pack_dir)
    if ":" not in sound_id:
        raise HTTPException(status_code=400, detail="invalid sound id")
    category, _, idx_text = sound_id.rpartition(":")
    try:
        idx = int(idx_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid sound index") from exc
    for cat_name, sounds in _iter_manifest_sounds(manifest):
        if cat_name != category:
            continue
        if idx < 0 or idx >= len(sounds):
            raise HTTPException(status_code=404, detail="sound index out of range")
        file_val = sounds[idx]["file"]
        candidate = (pack_dir / file_val).resolve()
        try:
            candidate.relative_to(pack_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="sound path escapes pack directory") from exc
        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(status_code=404, detail="sound file not found on disk")
        return candidate
    raise HTTPException(status_code=404, detail="sound category not found")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
def get_status() -> Dict[str, Any]:
    return _status_payload()


@router.post("/config/peon-command")
def post_config_peon_command(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    command = _validate_peon_command(body.get("peon_command", ""))
    try:
        cfg = load_config()
    except ConfigLoadError:
        cfg = AdapterConfig()
    cfg.peon_command = command
    try:
        path = save_config(cfg)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not save PeonPing config: {exc}") from exc
    status = _status_payload()
    return {
        "ok": True,
        "config_path": str(path),
        "peon_command": command,
        "status": status,
    }


@router.post("/mute")
def post_mute(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    if "muted" in body:
        muted = _validate_bool(body.get("muted"), "muted")
        args = ["mute" if muted else "unmute"]
    else:
        args = ["toggle"]
    try:
        result = _run_peon(*args)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["status"] = _safe_status()
    return response


@router.post("/volume")
def post_volume(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    volume = _validate_volume(body.get("volume"))
    try:
        result = _run_peon("volume", f"{volume:.2f}")
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["status"] = _safe_status()
    return response


@router.post("/notifications")
def post_notifications(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    enabled = _validate_bool(body.get("enabled"), "enabled")
    try:
        result = _run_peon("notifications", "on" if enabled else "off")
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["status"] = _safe_status()
    return response


@router.get("/packs")
def get_packs(
    registry: bool = Query(False),
    lang: str = Query(""),
) -> Dict[str, Any]:
    is_registry = bool(registry)
    peon_available = True
    if is_registry:
        try:
            peon_available = bool(resolve_peon_command(load_config()))
        except ConfigLoadError:
            peon_available = bool(resolve_peon_command(AdapterConfig()))
    try:
        if is_registry:
            with _registry_cache_lock:
                now = time.time()
                cached = _registry_cache.get("value")
                if cached is not None and now - _registry_cache.get("at", 0.0) < REGISTRY_CACHE_TTL_SECONDS:
                    packs = list(cached)
                else:
                    packs = _list_packs(registry=True)
                    _registry_cache["at"] = now
                    _registry_cache["value"] = list(packs)
        else:
            packs = _list_packs(registry=False)
            active_names: List[str] = []
            cfg = AdapterConfig()
            use_rotation = False
            try:
                cfg = load_config()
                use_rotation = bool(cfg.use_rotation)
            except ConfigLoadError:
                pass
            try:
                if not use_rotation:
                    status = _peon_status_text()
                    if status.get("active_pack"):
                        active_names.append(str(status.get("active_pack")))
            except CommandError:
                pass
            if not use_rotation:
                if cfg.voicepack:
                    active_names.append(cfg.voicepack)
            packs = _mark_active(packs, active_names)
    except CommandError as exc:
        # 503 = `peon` binary is missing. The dashboard /status already shows
        # "PeonPing missing" and renders the install card; surfacing a 503
        # here would also pop an error banner over the install card, hiding
        # the actual remediation steps. Degrade to empty packs instead and
        # mark the response so the UI can distinguish "no results" from
        # "peon unavailable" if it wants to. Other CommandErrors (real CLI
        # failures) still bubble up.
        if getattr(exc, "status_code", 500) == 503:
            packs = []
            peon_available = False
        else:
            raise _http_from_command_error(exc) from exc

    lang_filter = (lang or "").strip().lower()
    if lang_filter and lang_filter != "all":
        packs = [p for p in packs if (p.get("language") or "").lower() == lang_filter]
    return {
        "ok": True,
        "registry": is_registry,
        "packs": packs,
        "peon_available": peon_available,
    }


@router.post("/packs/install")
def post_packs_install(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    install_all = bool(body.get("all", False))
    lang = body.get("lang")
    args: List[str] = ["packs", "install"]
    if install_all:
        args.append("--all")
    else:
        names = _validate_pack_names(body.get("names"))
        args.append(",".join(names))
    if isinstance(lang, str) and lang.strip():
        args.extend(["--lang", lang.strip()])
    try:
        result = _run_peon(*args, timeout=WRITE_TIMEOUT_SECONDS)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["packs"] = _safe_list_packs(registry=False)
    response["registry"] = _safe_list_packs(registry=True, use_cache=False)
    return response


@router.post("/packs/install-local")
def post_packs_install_local(body: Dict[str, Any]) -> Dict[str, Any]:
    # Same rationale as remove: this is `cp -r` into a known pack root, so we
    # do it directly instead of shelling out. Lets us return a structured 409
    # for the already-installed / active-pack cases.
    body = body or {}
    src = _validate_local_path(body.get("path"))
    force = bool(body.get("force", False))

    try:
        manifest = json.loads((src / "openpeon.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"could not read openpeon.json: {exc}") from exc
    name = str(manifest.get("name") or "").strip() if isinstance(manifest, dict) else ""
    if not name or not PACK_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"openpeon.json missing or invalid `name` field: {name!r}",
        )

    root = _primary_pack_install_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = (root / name).resolve()
    if dest.parent != root.resolve():
        raise HTTPException(status_code=400, detail="pack name resolves outside install root")

    if dest.exists():
        if not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "pack_already_installed",
                    "error": f"Pack '{name}' is already installed at {dest}. Pass force=true to overwrite.",
                    "name": name,
                    "path": str(dest),
                },
            )
        active = _resolve_active_pack()
        if active == name:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": "active_pack_conflict",
                    "error": (
                        f"Cannot overwrite '{name}' while it is the active pack. "
                        "Switch to another pack first, then retry."
                    ),
                    "active_pack": active,
                    "conflicts": [name],
                    "names": [name],
                },
            )
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not remove existing pack: {exc}") from exc

    try:
        shutil.copytree(src, dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"copy failed: {exc}") from exc

    return {
        "ok": True,
        "returncode": 0,
        "stdout": f"Installed local pack '{name}' to {dest}",
        "stderr": "",
        "command": "<filesystem>",
        "args": ["packs", "install-local", str(src)],
        "packs": _safe_list_packs(registry=False),
    }


@router.post("/packs/use")
def post_packs_use(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    name = _validate_pack_name(body.get("name"))
    install = bool(body.get("install", False))
    rotation = _get_rotation()
    args: List[str] = ["packs", "use"]
    if install:
        args.append("--install")
    args.append(name)
    try:
        result = _run_peon(*args, timeout=WRITE_TIMEOUT_SECONDS if install else READ_TIMEOUT_SECONDS)
        remembered_rotation_mode = ""
        if result["returncode"] == 0:
            remembered_rotation_mode = _disable_peon_rotation_if_needed(rotation)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    if result["returncode"] == 0:
        _set_adapter_voicepack(name, last_rotation_mode=remembered_rotation_mode)
    response = _command_response(result)
    response["status"] = _safe_status()
    response["packs"] = _safe_list_packs(registry=False)
    return response


@router.post("/rotation/use")
def post_rotation_use(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    enabled = bool(body.get("enabled", True))
    rotation = _get_rotation()
    if enabled and not rotation.get("packs"):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "empty_rotation",
                "error": "Add at least one pack to rotation before using rotation as the active pack.",
            },
        )
    try:
        if enabled:
            remembered_rotation_mode = _enable_peon_rotation(rotation)
        else:
            remembered_rotation_mode = _disable_peon_rotation_if_needed(rotation)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    _set_adapter_use_rotation(enabled, last_rotation_mode=remembered_rotation_mode)
    rotation = _get_rotation()
    return {
        "ok": True,
        "returncode": 0,
        "stdout": "Using rotation as active pack" if enabled else "Stopped using rotation as active pack",
        "stderr": "",
        "command": "<adapter-config+peon>",
        "args": ["rotation", "use" if enabled else "stop"],
        "rotation": rotation,
        "status": _safe_status(),
        "packs": _safe_list_packs(registry=False),
    }


@router.post("/packs/remove")
def post_packs_remove(body: Dict[str, Any]) -> Dict[str, Any]:
    # Removal is just `rm -rf` on a pack directory, so we bypass the `peon` CLI
    # entirely. That sidesteps `peon packs remove`'s interactive y/N prompt
    # (which hangs against the dashboard's non-TTY subprocess) and lets us
    # surface an explicit 409 when the user asks to delete the active pack,
    # instead of relying on whatever shell-exit code peon would have emitted.
    body = body or {}
    remove_all = bool(body.get("all", False))
    if remove_all:
        names = [str(p.get("name")) for p in _safe_list_packs(registry=False) if p.get("name")]
    else:
        names = _validate_pack_names(body.get("names"))

    if not names:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "no installed packs to remove",
            "stderr": "",
            "command": "<filesystem>",
            "args": ["packs", "remove"],
            "packs": _safe_list_packs(registry=False),
        }

    active = _resolve_active_pack()
    conflicts = [n for n in names if active and n == active]
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "active_pack_conflict",
                "error": (
                    f"Cannot remove '{active}' while it is the active pack. "
                    "Switch to another installed pack first, then retry."
                ),
                "active_pack": active,
                "conflicts": conflicts,
                "names": names,
            },
        )

    pack_roots = [r.resolve() for r in _pack_root_candidates() if r.exists() and r.is_dir()]
    removed: List[str] = []
    errors: List[str] = []
    for name in names:
        try:
            pack_dir = _find_pack_dir(name)
        except HTTPException as exc:
            errors.append(f"{name}: {exc.detail}")
            continue
        if not any(_path_is_within(pack_dir, root) for root in pack_roots):
            errors.append(f"{name}: pack directory {pack_dir} is outside known pack roots")
            continue
        try:
            shutil.rmtree(pack_dir)
            removed.append(name)
        except OSError as exc:
            errors.append(f"{name}: {exc}")

    ok = not errors
    return {
        "ok": ok,
        "returncode": 0 if ok else 1,
        "stdout": (
            f"Removed {len(removed)} pack(s): {', '.join(removed)}"
            if removed
            else ("nothing removed" if not errors else "")
        ),
        "stderr": "\n".join(errors),
        "command": "<filesystem>",
        "args": ["packs", "remove", ",".join(names)],
        "packs": _safe_list_packs(registry=False),
    }


@router.get("/rotation")
def get_rotation() -> Dict[str, Any]:
    return {"ok": True, "rotation": _get_rotation()}


@router.post("/rotation/mode")
def post_rotation_mode(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    mode = _validate_rotation_mode(body.get("mode"))
    try:
        result = _run_peon("rotation", mode)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["rotation"] = _get_rotation()
    if result["returncode"] == 0:
        if mode in AUTOMATIC_ROTATION_MODES and response["rotation"].get("packs"):
            _set_adapter_use_rotation(True, last_rotation_mode=mode)
        elif mode == INACTIVE_ROTATION_MODE:
            _set_adapter_use_rotation(False)
        elif mode in AUTOMATIC_ROTATION_MODES:
            _set_adapter_use_rotation(False, last_rotation_mode=mode)
    response["status"] = _safe_status()
    return response


@router.post("/rotation/add")
def post_rotation_add(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    names = _validate_pack_names(body.get("names"))
    install = bool(body.get("install", False))
    args: List[str] = ["packs", "rotation", "add"]
    if install:
        args.append("--install")
    args.append(",".join(names))
    try:
        result = _run_peon(*args, timeout=WRITE_TIMEOUT_SECONDS if install else READ_TIMEOUT_SECONDS)
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["rotation"] = _get_rotation()
    return response


@router.post("/rotation/remove")
def post_rotation_remove(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    names = _validate_pack_names(body.get("names"))
    try:
        result = _run_peon("packs", "rotation", "remove", ",".join(names))
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["rotation"] = _get_rotation()
    return response


@router.post("/rotation/clear")
def post_rotation_clear(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        result = _run_peon("packs", "rotation", "clear")
    except CommandError as exc:
        raise _http_from_command_error(exc) from exc
    response = _command_response(result)
    response["rotation"] = _get_rotation()
    return response


@router.get("/packs/{pack_name}/sounds")
def get_pack_sounds(pack_name: str) -> Dict[str, Any]:
    return _soundboard_payload(pack_name)


@router.get("/packs/{pack_name}/audio/{sound_id}")
def get_pack_audio(pack_name: str, sound_id: str):
    decoded_id = urllib.parse.unquote(sound_id)
    audio_path = _resolve_sound_path(pack_name, decoded_id)
    return FileResponse(str(audio_path), media_type=_audio_media_type(audio_path))


def _audio_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
    }.get(suffix, "application/octet-stream")


# ---------------------------------------------------------------------------
# Safe wrappers used inside command responses
# ---------------------------------------------------------------------------


def _safe_status() -> Dict[str, Any]:
    try:
        return _status_payload()
    except CommandError as exc:
        return {"ok": False, "error": str(exc)}


def _safe_list_packs(*, registry: bool, use_cache: bool = True) -> List[Dict[str, Any]]:
    try:
        if registry and not use_cache:
            with _registry_cache_lock:
                _registry_cache["at"] = 0.0
                _registry_cache["value"] = None
        return _list_packs(registry=registry)
    except CommandError:
        return []


def _http_from_command_error(exc: CommandError) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status_code", 500),
        detail={
            "error": str(exc),
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        },
    )
