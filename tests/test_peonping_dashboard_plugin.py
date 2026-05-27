"""Tests for the hermes-peonping dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PLUGIN_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "dashboard"
    / "plugin_api.py"
)


@pytest.fixture
def plugin_api(tmp_path, monkeypatch):
    """Load plugin_api fresh per test so module-level caches don't leak."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("HERMES_PEONPING_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_PEONPING_PACK_ROOTS", raising=False)
    monkeypatch.delenv("PEON_PING_SCRIPT", raising=False)
    monkeypatch.delenv("CLAUDE_PEON_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

    spec = importlib.util.spec_from_file_location(
        f"peonping_plugin_api_test_{id(tmp_path)}", PLUGIN_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module


def _stub_run_peon(plugin_api, scripted: Dict[tuple, Dict[str, Any]]):
    """Replace plugin_api._run_peon with a scripted stub.

    scripted maps args-tuple -> {returncode, stdout, stderr}.
    """
    def fake_run_peon(*args, timeout=30):
        key = tuple(args)
        if key not in scripted:
            raise plugin_api.CommandError(
                f"unscripted peon call: {args!r}", returncode=127
            )
        out = scripted[key]
        return {
            "returncode": out.get("returncode", 0),
            "stdout": out.get("stdout", ""),
            "stderr": out.get("stderr", ""),
            "command": "/fake/peon",
            "args": list(args),
        }
    plugin_api._run_peon = fake_run_peon


# ---------------------------------------------------------------------------
# Task 2: status + command runner
# ---------------------------------------------------------------------------


def test_status_when_peon_is_missing(plugin_api):
    plugin_api._run_peon = lambda *args, timeout=30: (_ for _ in ()).throw(
        plugin_api.CommandError("PeonPing executable not found", returncode=127, status_code=503)
    )
    plugin_api.resolve_peon_command = lambda _cfg: ""
    payload = plugin_api._status_payload()
    assert payload["ok"] is True
    assert payload["peon_found"] is False
    assert payload["peon_command"] == ""
    assert payload["adapter"]["peon_command"] == ""
    assert payload["active_pack"] == ""
    assert payload["adapter"]["enabled"] is True


def test_status_reports_rotation_as_active_when_adapter_uses_rotation(plugin_api):
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="abe", use_rotation=True))
    plugin_api.resolve_peon_command = lambda _cfg: ""

    payload = plugin_api._status_payload()

    assert payload["active_pack"] == "Rotation"
    assert payload["adapter"]["voicepack"] == "abe"
    assert payload["adapter"]["use_rotation"] is True


def test_status_parses_audio_controls(plugin_api):
    plugin_api._run_peon_json = lambda *_args, **_kwargs: None

    def fake_run_peon(*args, timeout=30):
        if tuple(args) == ("status", "--verbose"):
            return {
                "returncode": 0,
                "stdout": "\n".join(
                    [
                        "peon-ping: paused",
                        "volume: 75%",
                        "active pack (here): abe (Abe)",
                        "rotation mode: session_override",
                        "desktop notifications off (sounds still play)",
                    ]
                ),
                "stderr": "",
                "command": "/fake/peon",
                "args": list(args),
            }
        raise plugin_api.CommandError(f"unexpected call: {args!r}", returncode=1)

    plugin_api._run_peon = fake_run_peon

    status = plugin_api._peon_status_text()

    assert status["muted"] is True
    assert status["volume"] == 0.75
    assert status["desktop_notifications"] is False
    assert status["active_pack"] == "abe (Abe)"
    assert status["rotation_mode"] == "session_override"


def test_config_peon_command_saves_custom_path(plugin_api, tmp_path):
    peon = tmp_path / "peon"
    peon.write_text("#!/bin/sh\n", encoding="utf-8")
    peon.chmod(0o755)

    response = plugin_api.post_config_peon_command({"peon_command": str(peon)})

    assert response["ok"] is True
    assert response["peon_command"] == str(peon)
    assert response["status"]["peon_command"] == str(peon)
    assert response["status"]["adapter"]["peon_command"] == str(peon)
    saved = json.loads(Path(response["config_path"]).read_text(encoding="utf-8"))
    assert saved["peon_command"] == str(peon)


def test_config_peon_command_rejects_missing_path(plugin_api, tmp_path):
    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_config_peon_command({"peon_command": str(tmp_path / "missing-peon")})
    assert excinfo.value.status_code == 400
    assert "not found" in str(excinfo.value.detail)


def test_mute_volume_and_notifications_commands(plugin_api):
    calls = []
    plugin_api._safe_status = lambda: {"ok": True}

    def fake_run_peon(*args, timeout=30):
        calls.append(tuple(args))
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "command": "/fake/peon",
            "args": list(args),
        }

    plugin_api._run_peon = fake_run_peon

    assert plugin_api.post_mute({"muted": True})["ok"] is True
    assert plugin_api.post_mute({"muted": False})["ok"] is True
    assert plugin_api.post_volume({"volume": 0.35})["ok"] is True
    assert plugin_api.post_notifications({"enabled": False})["ok"] is True

    assert calls == [
        ("mute",),
        ("unmute",),
        ("volume", "0.35"),
        ("notifications", "off"),
    ]


def test_volume_rejects_out_of_range(plugin_api):
    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_volume({"volume": 1.5})
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# Filesystem-backed pack management (remove + install-local)
# ---------------------------------------------------------------------------


def _make_pack(root: Path, name: str, manifest_extra: Optional[Dict[str, Any]] = None) -> Path:
    pack_dir = root / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"name": name, "display_name": name.title()}
    if manifest_extra:
        manifest.update(manifest_extra)
    (pack_dir / "openpeon.json").write_text(json.dumps(manifest), encoding="utf-8")
    (pack_dir / "dummy.txt").write_text("payload", encoding="utf-8")
    return pack_dir


def _install_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "packs"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_PEONPING_PACK_ROOTS", str(root))
    return root


def test_remove_pack_filesystem_path(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe")
    _make_pack(root, "bee")

    # Defensive: any peon call would be a regression. Force a failure if hit.
    def fail_peon(*args, **kwargs):
        raise AssertionError(f"unexpected peon call: {args!r}")
    plugin_api._run_peon = fail_peon
    plugin_api._safe_list_packs = lambda **_: []
    plugin_api._resolve_active_pack = lambda: ""

    response = plugin_api.post_packs_remove({"names": ["abe"]})
    assert response["ok"] is True
    assert "abe" in response["stdout"]
    assert (root / "abe").exists() is False
    assert (root / "bee").exists() is True


def test_remove_pack_active_returns_409(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe")
    plugin_api._resolve_active_pack = lambda: "abe"
    plugin_api._safe_list_packs = lambda **_: []

    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_packs_remove({"names": ["abe"]})
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail.get("error_code") == "active_pack_conflict"
    assert detail.get("active_pack") == "abe"
    # Pack should still be on disk because we never reached the rmtree.
    assert (root / "abe").exists()


def test_remove_missing_pack_reports_error(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe")
    plugin_api._resolve_active_pack = lambda: ""
    plugin_api._safe_list_packs = lambda **_: []

    response = plugin_api.post_packs_remove({"names": ["ghost"]})
    assert response["ok"] is False
    assert "ghost" in response["stderr"]
    # Existing pack untouched.
    assert (root / "abe").exists()


def test_remove_all_enumerates_installed_packs(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe")
    _make_pack(root, "bee")
    plugin_api._resolve_active_pack = lambda: ""
    plugin_api._safe_list_packs = lambda **_: [{"name": "abe"}, {"name": "bee"}]

    response = plugin_api.post_packs_remove({"all": True})
    assert response["ok"] is True
    assert not (root / "abe").exists()
    assert not (root / "bee").exists()


def test_install_local_copies_pack(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    src = tmp_path / "src" / "fresh"
    _make_pack(src.parent, "fresh")  # creates tmp_path/src/fresh/openpeon.json
    plugin_api._resolve_active_pack = lambda: ""
    plugin_api._safe_list_packs = lambda **_: []

    response = plugin_api.post_packs_install_local({"path": str(src)})
    assert response["ok"] is True
    assert (root / "fresh" / "openpeon.json").exists()
    assert (root / "fresh" / "dummy.txt").read_text(encoding="utf-8") == "payload"


def test_install_local_already_installed_409(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "fresh")
    src = tmp_path / "src" / "fresh"
    _make_pack(src.parent, "fresh")
    plugin_api._resolve_active_pack = lambda: ""
    plugin_api._safe_list_packs = lambda **_: []

    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_packs_install_local({"path": str(src)})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error_code"] == "pack_already_installed"


def test_install_local_force_overwrites(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "fresh", {"display_name": "Old"})
    src = tmp_path / "src" / "fresh"
    _make_pack(src.parent, "fresh", {"display_name": "New"})
    plugin_api._resolve_active_pack = lambda: ""
    plugin_api._safe_list_packs = lambda **_: []

    response = plugin_api.post_packs_install_local({"path": str(src), "force": True})
    assert response["ok"] is True
    manifest = json.loads((root / "fresh" / "openpeon.json").read_text(encoding="utf-8"))
    assert manifest["display_name"] == "New"


def test_rotation_returns_empty_when_peon_missing(plugin_api):
    """Regression: /rotation must not 500 when `peon` isn't installed.

    `_get_rotation` originally called `_run_peon` directly after the JSON
    helper returned None. With peon missing, that raised a 503 CommandError
    that the @router.get("/rotation") handler didn't catch, so FastAPI
    surfaced it as a 500 Internal Server Error — and the dashboard ate
    a red error banner on every initial load.
    """
    def fail_peon(*_args, **_kwargs):
        raise plugin_api.CommandError(
            "PeonPing executable not found", returncode=127, status_code=503
        )
    plugin_api._run_peon = fail_peon

    rotation = plugin_api._get_rotation()
    assert rotation == {"mode": "", "packs": []}


def test_registry_packs_returns_empty_when_peon_missing(plugin_api):
    """Regression: GET /packs?registry=true degrades to empty list, not 503.

    The 503 surfaced as an error banner that visually overlapped the very
    install card meant to guide users out of the missing-peon state.
    """
    def fail_peon(*_args, **_kwargs):
        raise plugin_api.CommandError(
            "PeonPing executable not found", returncode=127, status_code=503
        )
    plugin_api._run_peon = fail_peon
    plugin_api._list_registry_from_http = lambda: (_ for _ in ()).throw(
        plugin_api.CommandError("registry unavailable", returncode=1, status_code=502)
    )
    plugin_api.resolve_peon_command = lambda _cfg: ""

    response = plugin_api.get_packs(registry=True)
    assert response["ok"] is True
    assert response["packs"] == []
    assert response["peon_available"] is False


def test_registry_packs_use_http_fallback_when_peon_missing(plugin_api):
    """Registry browsing should work before the local peon CLI is installed."""
    def fail_peon(*_args, **_kwargs):
        raise plugin_api.CommandError(
            "PeonPing executable not found", returncode=127, status_code=503
        )
    plugin_api._run_peon = fail_peon
    plugin_api._run_peon_json = lambda *_args, **_kwargs: None
    plugin_api.resolve_peon_command = lambda _cfg: ""
    plugin_api._list_registry_from_http = lambda: [
        plugin_api._normalize_pack(
            {
                "name": "glados",
                "display_name": "GLaDOS",
                "categories": ["session.start", "task.complete"],
                "sound_count": 28,
            },
            source="registry",
        )
    ]

    response = plugin_api.get_packs(registry=True)
    assert response["ok"] is True
    assert response["peon_available"] is False
    assert response["packs"][0]["name"] == "glados"
    assert response["packs"][0]["installed"] is False
    assert response["packs"][0]["category_count"] == 2


def test_registry_packs_propagates_non_peon_missing_errors(plugin_api):
    """Real CLI failures (not 'peon not found') should still bubble up."""
    def real_failure(*_args, **_kwargs):
        raise plugin_api.CommandError(
            "registry HTTP 502 from upstream",
            returncode=1,
            stdout="",
            stderr="bad gateway",
            status_code=502,
        )
    plugin_api._run_peon = real_failure
    plugin_api._run_peon_json = lambda *a, **kw: None  # force fall-through
    plugin_api._list_registry_from_http = lambda: (_ for _ in ()).throw(
        plugin_api.CommandError("registry unavailable", returncode=1, status_code=502)
    )
    # Bust the registry cache so we actually hit the call path.
    plugin_api._registry_cache["at"] = 0.0
    plugin_api._registry_cache["value"] = None

    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.get_packs(registry=True)
    assert excinfo.value.status_code == 502


def test_pack_listing_uses_dir_name_not_manifest_name(plugin_api, tmp_path, monkeypatch):
    """Regression: dir name and manifest name can diverge in real packs.

    User had ~/.openpeon/packs/aod/ with `openpeon.json {"name": "aod-pack"}`.
    Listing the pack as 'aod-pack' makes the dashboard hit /packs/aod-pack/...
    which 404s (the dir is 'aod'). Listing must use the directory name so all
    follow-up endpoints (remove, sounds, audio) line up with what's on disk.
    """
    root = _install_root(tmp_path, monkeypatch)
    # Dir 'aod' but manifest name 'aod-pack' (the real-world case).
    _make_pack(root, "aod", {"name": "aod-pack", "display_name": "Army of Darkness"})
    plugin_api._resolve_active_pack = lambda: ""

    listed = plugin_api._list_packs(registry=False)
    by_name = {p["name"]: p for p in listed}
    assert "aod" in by_name, f"pack should be listed under dir name, got: {list(by_name)}"
    assert "aod-pack" not in by_name
    assert by_name["aod"]["display_name"] == "Army of Darkness"

    # And the lookup path used by remove/sounds must agree with the listing.
    found = plugin_api._find_pack_dir("aod")
    assert found.name == "aod"

    # Removing via the listed (dir) name actually drops it.
    response = plugin_api.post_packs_remove({"names": ["aod"]})
    assert response["ok"] is True
    assert not (root / "aod").exists()


def test_installed_pack_active_uses_adapter_voicepack_fallback(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe", {"display_name": "Abe's Oddysee - Abe"})
    plugin_api._peon_status_text = lambda: {"active_pack": "", "rotation_mode": "", "raw": None}
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="abe"))

    response = plugin_api.get_packs(registry=False)
    by_name = {p["name"]: p for p in response["packs"]}
    assert by_name["abe"]["active"] is True


def test_installed_pack_active_uses_config_when_peon_reports_display_name(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe", {"display_name": "Abe's Oddysee - Abe"})
    plugin_api._peon_status_text = lambda: {
        "active_pack": "Abe's Oddysee - Abe",
        "rotation_mode": "",
        "raw": None,
    }
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="abe"))

    response = plugin_api.get_packs(registry=False)
    by_name = {p["name"]: p for p in response["packs"]}
    assert by_name["abe"]["active"] is True


def test_installed_pack_not_marked_active_when_rotation_is_active(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe", {"display_name": "Abe's Oddysee - Abe"})
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="abe", use_rotation=True))
    plugin_api._peon_status_text = lambda: {
        "active_pack": "Abe's Oddysee - Abe",
        "rotation_mode": "random",
        "raw": None,
    }

    response = plugin_api.get_packs(registry=False)
    by_name = {p["name"]: p for p in response["packs"]}
    assert by_name["abe"]["active"] is False


def test_installed_list_reflects_filesystem_after_remove(plugin_api, tmp_path, monkeypatch):
    """Regression: removing a pack must drop it from GET /packs immediately.

    Before the FS-backed list, /packs delegated to `peon packs list` and could
    keep showing a pack that we'd just rmtree-d, leaving the dashboard stuck
    on a stale row until manual refresh.
    """
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "abe")
    _make_pack(root, "bee")

    # Any peon subprocess call here would defeat the purpose of the test.
    plugin_api._run_peon = lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError(f"unexpected peon call: {a!r}")
    )
    plugin_api._resolve_active_pack = lambda: ""

    before = {p["name"] for p in plugin_api._list_packs(registry=False)}
    assert before == {"abe", "bee"}

    plugin_api.post_packs_remove({"names": ["abe"]})

    after = {p["name"] for p in plugin_api._list_packs(registry=False)}
    assert after == {"bee"}, f"abe still listed after rmtree: {after}"


def test_install_local_force_blocked_when_active(plugin_api, tmp_path, monkeypatch):
    root = _install_root(tmp_path, monkeypatch)
    _make_pack(root, "fresh")
    src = tmp_path / "src" / "fresh"
    _make_pack(src.parent, "fresh")
    plugin_api._resolve_active_pack = lambda: "fresh"
    plugin_api._safe_list_packs = lambda **_: []

    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_packs_install_local({"path": str(src), "force": True})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error_code"] == "active_pack_conflict"


def test_rotation_use_sets_adapter_rotation_flag(plugin_api):
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="abe", use_rotation=False))
    plugin_api._get_rotation = lambda: {"mode": "random", "packs": ["abe", "bee"]}
    plugin_api._safe_list_packs = lambda **_: []
    plugin_api.resolve_peon_command = lambda _cfg: ""
    calls = []

    def fake_run_peon(*args, timeout=30):
        calls.append(tuple(args))
        return {
            "returncode": 0,
            "stdout": "peon-ping: rotation mode set to session_override",
            "stderr": "",
            "command": "/fake/peon",
            "args": list(args),
        }

    plugin_api._run_peon = fake_run_peon

    response = plugin_api.post_rotation_use({"enabled": True})

    assert response["ok"] is True
    assert response["status"]["active_pack"] == "Rotation"
    assert response["status"]["adapter"]["use_rotation"] is True
    assert plugin_api.load_config().use_rotation is True

    response = plugin_api.post_rotation_use({"enabled": False})
    assert response["ok"] is True
    assert response["status"]["adapter"]["use_rotation"] is False
    assert response["status"]["adapter"]["voicepack"] == "abe"
    assert ("rotation", "session_override") in calls


def test_packs_use_disables_peon_rotation(plugin_api):
    plugin_api.save_config(plugin_api.AdapterConfig(voicepack="old", use_rotation=True))
    plugin_api._get_rotation = lambda: {"mode": "round-robin", "packs": ["old", "abe"]}
    plugin_api._safe_list_packs = lambda **_: []
    plugin_api.resolve_peon_command = lambda _cfg: ""
    calls = []

    def fake_run_peon(*args, timeout=30):
        calls.append(tuple(args))
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "command": "/fake/peon",
            "args": list(args),
        }

    plugin_api._run_peon = fake_run_peon

    response = plugin_api.post_packs_use({"name": "abe"})

    cfg = plugin_api.load_config()
    assert response["ok"] is True
    assert cfg.voicepack == "abe"
    assert cfg.use_rotation is False
    assert cfg.last_rotation_mode == "round-robin"
    assert calls[:2] == [("packs", "use", "abe"), ("rotation", "session_override")]


def test_rotation_use_reenables_last_automatic_mode(plugin_api):
    plugin_api.save_config(
        plugin_api.AdapterConfig(
            voicepack="abe",
            use_rotation=False,
            last_rotation_mode="shuffle",
        )
    )
    plugin_api._get_rotation = lambda: {"mode": "session_override", "packs": ["abe", "bee"]}
    plugin_api._safe_list_packs = lambda **_: []
    plugin_api.resolve_peon_command = lambda _cfg: ""
    calls = []

    def fake_run_peon(*args, timeout=30):
        calls.append(tuple(args))
        return {
            "returncode": 0,
            "stdout": "peon-ping: rotation mode set to shuffle",
            "stderr": "",
            "command": "/fake/peon",
            "args": list(args),
        }

    plugin_api._run_peon = fake_run_peon

    response = plugin_api.post_rotation_use({"enabled": True})

    cfg = plugin_api.load_config()
    assert response["ok"] is True
    assert cfg.use_rotation is True
    assert cfg.last_rotation_mode == "shuffle"
    assert ("rotation", "shuffle") in calls


def test_rotation_use_rejects_empty_rotation(plugin_api):
    plugin_api._get_rotation = lambda: {"mode": "random", "packs": []}

    with pytest.raises(plugin_api.HTTPException) as excinfo:
        plugin_api.post_rotation_use({"enabled": True})
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error_code"] == "empty_rotation"
