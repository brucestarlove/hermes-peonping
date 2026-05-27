"""Tests for the hermes-peonping lifecycle plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


class DummyContext:
    def __init__(self):
        self.hooks = []
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


def test_manifest_declares_standalone_lifecycle_plugin():
    pkg_root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((pkg_root / "plugin.yaml").read_text())

    assert manifest["name"] == "peonping"
    assert manifest["kind"] == "standalone"
    assert "pre_tool_call" in manifest["hooks"]
    assert "subagent_stop" in manifest["hooks"]
    assert "on_session_finalize" in manifest["hooks"]
    assert "on_session_reset" in manifest["hooks"]
    assert "on_session_end" not in manifest["hooks"]
    assert "peonping" in manifest["commands"]


def test_default_config_path_uses_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PEONPING_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    from hermes_peonping.config import default_config_path

    assert default_config_path() == tmp_path / "hermes-home" / "peonping" / "config.json"


def test_mapping_adds_voicepack_hint_and_session_prefix():
    from hermes_peonping.config import AdapterConfig
    from hermes_peonping.mapper import map_hermes_payload

    event = map_hermes_payload(
        {
            "hook_event_name": "pre_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {"is_first_turn": True},
        },
        AdapterConfig(voicepack="test-pack"),
    )

    assert event["hook_event_name"] == "SessionStart"
    assert event["session_id"] == "hermes-sess-1"
    assert event["cwd"] == "/repo"
    assert event["source"] == "hermes"
    assert event["voicepack"] == "test-pack"


def test_mapping_omits_voicepack_when_rotation_is_enabled():
    from hermes_peonping.config import AdapterConfig
    from hermes_peonping.mapper import map_hermes_payload

    event = map_hermes_payload(
        {
            "hook_event_name": "pre_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {"is_first_turn": True},
        },
        AdapterConfig(voicepack="test-pack", use_rotation=True),
    )

    assert "voicepack" not in event


def test_selected_pre_tool_call_maps_to_progress_notification():
    from hermes_peonping.config import AdapterConfig
    from hermes_peonping.mapper import map_hermes_payload

    event = map_hermes_payload(
        {
            "hook_event_name": "pre_tool_call",
            "tool_name": "terminal",
            "tool_input": {"command": "python -m pytest tests/test_peonping_plugin.py"},
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        },
        AdapterConfig(tool_progress_events=["terminal"]),
    )

    assert event["hook_event_name"] == "Notification"
    assert event["notification_type"] == "progress"
    assert event["cesp_category"] == "task.progress"
    assert event["tool_name"] == "terminal"
    assert "pytest" in event["message"]


def test_payload_from_kwargs_defaults_cwd_to_current_directory(monkeypatch, tmp_path):
    from hermes_peonping.mapper import payload_from_kwargs

    monkeypatch.chdir(tmp_path)

    payload = payload_from_kwargs("pre_llm_call", {"session_id": "sess-1"})

    assert payload["cwd"] == str(tmp_path)


def test_payload_from_kwargs_fails_open_when_cwd_unavailable(monkeypatch):
    from hermes_peonping import mapper

    monkeypatch.setattr(mapper.os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError("gone")))

    payload = mapper.payload_from_kwargs("pre_llm_call", {"session_id": "sess-1"})

    assert payload["cwd"] == ""


def test_string_boolean_config_values_are_parsed(tmp_path):
    from hermes_peonping.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "enabled": "false",
                "enabled_events": {
                    "post_llm_call": "false",
                    "pre_llm_call": "yes",
                    "pre_tool_call": "0",
                    "post_tool_call": "1",
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)

    assert cfg.enabled is False
    assert cfg.enabled_events["post_llm_call"] is False
    assert cfg.enabled_events["pre_llm_call"] is True
    assert cfg.enabled_events["pre_tool_call"] is False
    assert cfg.enabled_events["post_tool_call"] is True
    assert cfg.event_enabled("pre_llm_call") is False


def test_wrong_type_schema_version_falls_back_to_current_version(tmp_path):
    from hermes_peonping.config import SCHEMA_VERSION, load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"schema_version": {"x": 1}}), encoding="utf-8")

    cfg = load_config(cfg_path)

    assert cfg.schema_version == SCHEMA_VERSION


def test_non_finite_timeout_falls_back_to_default(tmp_path):
    from hermes_peonping.config import DEFAULT_TIMEOUT_SECONDS, load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"timeout_seconds": "Infinity"}), encoding="utf-8")

    cfg = load_config(cfg_path)

    assert cfg.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_terminal_failure_maps_to_post_tool_use_failure():
    from hermes_peonping.config import AdapterConfig
    from hermes_peonping.mapper import map_hermes_payload

    event = map_hermes_payload(
        {
            "hook_event_name": "post_tool_call",
            "tool_name": "terminal",
            "tool_input": {"command": "false"},
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {"result": json.dumps({"output": "", "exit_code": 1})},
        },
        AdapterConfig(tool_error_events=["terminal"]),
    )

    assert event["hook_event_name"] == "PostToolUseFailure"
    assert event["tool_name"] == "Bash"
    assert event["hermes_tool_name"] == "terminal"
    assert "exit code 1" in event["error"].lower()


def test_subagent_stop_maps_completion_status_and_summary():
    from hermes_peonping.config import AdapterConfig
    from hermes_peonping.mapper import map_hermes_payload

    event = map_hermes_payload(
        {
            "hook_event_name": "subagent_stop",
            "session_id": "parent-sess",
            "cwd": "/repo",
            "extra": {
                "child_status": "completed",
                "child_summary": "Implemented the feature and updated tests.",
            },
        },
        AdapterConfig(),
    )

    assert event["hook_event_name"] == "SubagentStop"
    assert event["cesp_category"] == "subagent.completed"
    assert event["status"] == "completed"
    assert "Implemented the feature" in event["summary"]


def test_plugin_registers_hooks_and_status_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PEONPING_CONFIG", str(tmp_path / "config.json"))

    import hermes_peonping
    from hermes_peonping.config import AdapterConfig, save_config

    save_config(AdapterConfig(voicepack="test-pack"), tmp_path / "config.json")
    ctx = DummyContext()

    hermes_peonping.register(ctx)

    hook_names = [name for name, _ in ctx.hooks]
    assert "pre_tool_call" in hook_names
    assert "subagent_stop" in hook_names
    assert "on_session_finalize" in hook_names
    assert "on_session_reset" in hook_names
    assert "on_session_end" not in hook_names
    assert "peonping" in ctx.commands
    status = ctx.commands["peonping"]["handler"]("")
    assert "PeonPing adapter" in status
    assert "test-pack" in status


def test_invalid_config_fails_open_for_hook_and_reports_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PEONPING_CONFIG", str(tmp_path / "config.json"))
    (tmp_path / "config.json").write_text("{not-json", encoding="utf-8")

    import hermes_peonping

    ctx = DummyContext()
    hermes_peonping.register(ctx)

    hook = dict(ctx.hooks)["post_llm_call"]
    assert hook(session_id="sess-1") is None

    status = ctx.commands["peonping"]["handler"]("")
    assert "status: config error" in status
    assert str(tmp_path / "config.json") in status
    assert "Invalid PeonPing adapter config JSON" in status


def test_config_directory_fails_open_for_hook_and_reports_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PEONPING_CONFIG", str(tmp_path))

    import hermes_peonping

    ctx = DummyContext()
    hermes_peonping.register(ctx)

    hook = dict(ctx.hooks)["post_llm_call"]
    assert hook(session_id="sess-1") is None

    status = ctx.commands["peonping"]["handler"]("")
    assert "status: config error" in status
    assert str(tmp_path) in status
    assert "Could not read PeonPing adapter config" in status


def test_emit_payload_fails_open_when_config_path_is_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PEONPING_CONFIG", str(tmp_path))

    from hermes_peonping.adapter import emit_payload

    result = emit_payload(
        {
            "hook_event_name": "post_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        }
    )

    assert result.skipped is True
    assert result.returncode == 0
    assert "Could not read PeonPing adapter config" in result.stderr


def test_argv_preserves_existing_command_path_with_spaces(tmp_path):
    from hermes_peonping.adapter import _argv

    command_path = tmp_path / "dir with spaces" / "peon"
    command_path.parent.mkdir()
    command_path.write_text("#!/bin/sh\n", encoding="utf-8")

    assert _argv(str(command_path)) == [str(command_path)]


def test_emit_payload_returns_error_when_peon_command_cannot_start():
    from hermes_peonping.adapter import emit_payload
    from hermes_peonping.config import AdapterConfig

    result = emit_payload(
        {
            "hook_event_name": "post_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        },
        AdapterConfig(peon_command="/definitely/missing/peon"),
    )

    assert result.returncode != 0
    assert result.skipped is False
    assert "failed to run" in result.stderr.lower()


def test_emit_payload_dry_run_prints_mapped_event(capsys):
    from hermes_peonping.adapter import emit_payload
    from hermes_peonping.config import AdapterConfig

    result = emit_payload(
        {
            "hook_event_name": "post_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        },
        AdapterConfig(),
        dry_run=True,
    )

    assert result.returncode == 0
    assert result.skipped is False
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["hook_event_name"] == "Stop"


def test_emit_payload_runs_fake_executable_successfully(tmp_path):
    from hermes_peonping.adapter import emit_payload
    from hermes_peonping.config import AdapterConfig

    output_path = tmp_path / "payload.json"
    script = tmp_path / "fake_peon.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.read(), encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = emit_payload(
        {
            "hook_event_name": "post_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        },
        AdapterConfig(peon_command=f"{os.sys.executable} {script} {output_path}"),
    )

    assert result.returncode == 0
    assert result.skipped is False
    assert json.loads(output_path.read_text(encoding="utf-8"))["hook_event_name"] == "Stop"


def test_emit_payload_uses_configured_timeout(tmp_path):
    from hermes_peonping.adapter import emit_payload
    from hermes_peonping.config import AdapterConfig

    script = tmp_path / "slow_peon.py"
    script.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")

    result = emit_payload(
        {
            "hook_event_name": "post_llm_call",
            "session_id": "sess-1",
            "cwd": "/repo",
            "extra": {},
        },
        AdapterConfig(peon_command=f"{os.sys.executable} {script}", timeout_seconds=0.01),
    )

    assert result.returncode == 1
    assert "timed out" in result.stderr.lower()
