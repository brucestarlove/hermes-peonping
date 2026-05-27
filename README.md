# hermes-peonping

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that gives the Hermes CLI audible lifecycle cues through local [PeonPing](https://peonping.com) sound packs. It can react to session starts, turn completions, approval prompts, selected tool progress, terminal failures, subagent completion, and session end — including custom voice packs if you want Hermes to feel more present while you work.

**Credits**
- [PeonPing](https://peonping.com) (the underlying sound-alerts CLI) by **[@garysheng](https://github.com/garysheng)**
- This Hermes plugin by **[@brucestarlove](https://github.com/brucestarlove)**

PeonPing itself remains optional and external. If the plugin is enabled but the `peon` executable is not available, hooks fail open and do not interrupt the agent.

## What you get

- Sound cues for Hermes lifecycle events
- Optional **Sound Alerts** dashboard tab for browsing, installing, previewing, switching, and rotating PeonPing packs
- Works with stock PeonPing packs or custom/generated voice packs
- Safe failure mode: if PeonPing is missing or misconfigured, Hermes keeps running silently

## Installation

> **Status:** this plugin is not yet published to PyPI or the Hermes plugin registry. For now, install from a local clone using one of the methods below. The PyPI / `hermes plugins install ...` paths described at the bottom of this section are placeholders for after publication.

### Install PeonPing

This plugin is just the Hermes ↔ PeonPing adapter — the `peon` CLI that actually plays sounds is a separate dependency. Install it from [peonping.com](https://peonping.com):

```bash
# macOS (Homebrew):
brew install PeonPing/tap/peon-ping

# Linux / WSL:
curl -fsSL peonping.com/install | bash
```

The plugin runs fine without `peon` installed — hooks silently no-op and the dashboard shows a "PeonPing isn't installed yet" panel with these commands. Install `peon` whenever you're ready to hear sounds.

### Install the Hermes plugin from a local clone (recommended while unpublished)

Hermes scans `~/.hermes/plugins/` for plugin directories. Drop the cloned repo in there and Hermes picks it up on next start — no `pip install` step needed.

**Option A — sync the files (good for occasional updates):**

```bash
git clone https://github.com/brucestarlove/hermes-peonping ~/src/hermes-peonping
rsync -a --delete ~/src/hermes-peonping/ ~/.hermes/plugins/peonping/
```

Re-run the `rsync` whenever you pull updates. The trailing slashes matter (source-slash copies contents; dest-slash treats it as the directory to fill). `--delete` removes stale files from prior installs.

**Option B — symlink (good for active development):**

```bash
git clone https://github.com/brucestarlove/hermes-peonping ~/src/hermes-peonping
ln -s ~/src/hermes-peonping ~/.hermes/plugins/peonping
```

Edits in `~/src/hermes-peonping/` are reflected immediately — useful if you're hacking on the plugin itself.

**Option C — `hermes plugins install file://...`:**

```bash
hermes plugins install file:///absolute/path/to/cloned/hermes-peonping --enable
```

Goes through the same code path as a published-registry install and triggers a "local install" security warning at the prompt (expected).

After any of the above, restart your Hermes session. A **Sound Alerts** tab should appear in the dashboard sidebar. If it doesn't, check the Plugins page — visibility may be toggled off (`dashboard.hidden_plugins` in `~/.hermes/config.yaml`).

### After publication (not available yet)

Once this plugin ships to PyPI and the Hermes registry, these will work:

```bash
# Headless (hooks only, no dashboard tab):
pip install hermes-peonping
hermes plugins enable peonping
```

On modern macOS / Linux distributions, system Python is PEP 668 "externally managed" — `pip install` directly will be rejected. Install into a virtual environment or use `pipx`, or pass `--user` / `--break-system-packages` if you know what you're doing. The simplest path for most users is to skip this and use the directory install above.

```bash
# Full plugin with dashboard tab:
hermes plugins install brucestarlove/hermes-peonping --enable
```

The `pip install` path registers the hook adapter via entry-point; the dashboard tab only appears when the plugin directory is present in a scanned location like `~/.hermes/plugins/`.

## Hook → PeonPing event mapping

| Hook | PeonPing event |
|---|---|
| `pre_llm_call` | `SessionStart` on the first turn, `UserPromptSubmit` on later turns |
| `post_llm_call` | `Stop` |
| `pre_approval_request` | `PermissionRequest` |
| `pre_tool_call` | `Notification` for tools listed in `tool_progress_events` |
| `post_tool_call` | `PostToolUseFailure` for selected failing tools (default: `terminal`) |
| `subagent_stop` | `SubagentStop` |
| `on_session_finalize` / `on_session_reset` | `SessionEnd` |

## Configuration

Config path: `$HERMES_HOME/peonping/config.json` (override with `HERMES_PEONPING_CONFIG`).

```bash
mkdir -p "$HERMES_HOME/peonping"
cat > "$HERMES_HOME/peonping/config.json" <<'JSON'
{
  "schema_version": 1,
  "enabled": true,
  "peon_command": "peon",
  "timeout_seconds": 2,
  "voicepack": "",
  "tool_error_events": ["terminal"],
  "tool_progress_events": []
}
JSON
```

## Slash command

`/peonping` — shows the resolved config path, PeonPing executable, voicepack hint, timeout, and enabled events.  
`/peonping json` — returns the raw adapter config as JSON.

## Dashboard tab

When installed as a user plugin (via `hermes plugins install`) and the Hermes dashboard is running, a **Sound Alerts** tab appears in the sidebar. From there you can browse the pack registry, install/switch/remove voicepacks, configure rotation, and preview sounds directly in the browser.

## Custom voice packs

PeonPing supports many soundboard packs, but the fun part is that packs can be generated or customized. This plugin was built because I wanted Hermes to have a more personal voice during CLI work. You can use the dashboard to switch packs, preview sounds, and experiment with rotation.

## Executable resolution order

The adapter resolves the `peon` executable in this order:
1. `peon_command` from adapter config
2. `PEON_PING_SCRIPT` env var
3. `peon_dir/peon.sh` from config
4. `CLAUDE_PEON_DIR/peon.sh`
5. `CLAUDE_CONFIG_DIR/hooks/peon-ping/peon.sh`
6. `~/.claude/hooks/peon-ping/peon.sh`
7. `~/.openpeon/peon.sh`
8. `peon` on `PATH`

## Privacy / trust warning

Enabling this plugin may execute existing legacy PeonPing, OpenPeon, or Claude hook scripts from the locations above. Normal hook playback sends events to the local `peon` executable; the dashboard may fetch PeonPing's online pack registry when browsing available packs. Enabled events can pass session IDs, the current working directory, selected command/tool input text, approval prompt text, assistant response excerpts, subagent summaries, and tool error details to the resolved executable. Only enable the plugin when you trust the resolved executable and sound pack.

## Status

Early but usable. Tested on macOS; Linux/WSL should work through PeonPing's install path, but install reports, issues, and PRs are welcome.

## License

MIT
