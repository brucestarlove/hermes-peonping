(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const C = SDK.components || {};
  const cn = (SDK.utils && SDK.utils.cn) || function () {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
  };

  const API_BASE = "/api/plugins/peonping";
  const ROTATION_MODES = ["random", "round-robin", "shuffle", "session_override"];

  async function api(path, options) {
    const opts = options || {};
    const url = API_BASE + path;
    const token = window.__HERMES_SESSION_TOKEN__ || "";
    const headers = Object.assign({}, opts.headers || {});
    if (token) headers["X-Hermes-Session-Token"] = token;
    if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = headers["Content-Type"] || "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, Object.assign({}, opts, { headers }));
    const text = await res.text();
    let parsed = null;
    let parseFailed = false;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch (_) {
      parsed = null;
      parseFailed = true;
    }
    if (!res.ok) {
      const err = new Error(
        (parsed && parsed.detail && (parsed.detail.error || JSON.stringify(parsed.detail))) ||
        (parsed && parsed.error) ||
        (text || res.statusText)
      );
      err.status = res.status;
      err.payload = parsed;
      throw err;
    }
    if (parsed === null) {
      // 200 OK with non-JSON body — usually means the SPA catch-all served
      // index.html because the plugin's FastAPI router did not mount. Surface
      // the situation instead of crashing on a downstream property read.
      const looksLikeHtml = parseFailed && /^\s*<!doctype html/i.test(text);
      const detail = looksLikeHtml
        ? "PeonPing backend routes are not mounted (the dashboard SPA answered " + path + " instead). Check the dashboard log for: `Failed to load plugin peonping API routes`."
        : "PeonPing API at " + path + " returned an empty or non-JSON response.";
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return parsed;
  }

  function Btn(props, ...children) {
    if (C.Button) return React.createElement(C.Button, props, ...children);
    return React.createElement("button", props, ...children);
  }

  function Card(props, ...children) {
    const className = cn("pp-card", props && props.className);
    const rest = Object.assign({}, props, { className });
    if (C.Card) return React.createElement(C.Card, rest, ...children);
    return React.createElement("div", rest, ...children);
  }

  function CardBody(props, ...children) {
    const className = cn("pp-card-body", props && props.className);
    const rest = Object.assign({}, props, { className });
    if (C.CardContent) return React.createElement(C.CardContent, rest, ...children);
    return React.createElement("div", rest, ...children);
  }

  function StatusChip(props) {
    return React.createElement(
      "span",
      { className: cn("pp-chip", props.tone && "pp-chip-" + props.tone) },
      props.label,
      props.value !== undefined && props.value !== null && props.value !== ""
        ? React.createElement("strong", { className: "pp-chip-value" }, String(props.value))
        : null
    );
  }

  function statusVolumePercent(status, fallback) {
    const raw = status && typeof status.volume === "number" ? status.volume : null;
    if (typeof fallback === "number") return fallback;
    if (raw === null || !isFinite(raw)) return 50;
    return Math.max(0, Math.min(100, Math.round(raw * 100)));
  }

  function StatusHeader(props) {
    const status = props.status;
    if (!status) return null;
    const peonReady = status.peon_found === true;
    const muted = status.muted === true;
    const notificationsOn = status.desktop_notifications !== false;
    const volumePct = statusVolumePercent(status, props.volumeDraft);
    return React.createElement(
      "section",
      { className: "pp-hero" },
      React.createElement(
        "div",
        { className: "pp-hero-text" },
        React.createElement("div", { className: "pp-kicker" }, "Hermes plugin"),
        React.createElement("h1", null, "Sound Alerts - PeonPing"),
        React.createElement(
          "p",
          null,
          "Lifecycle sounds and voicepack control for Hermes."
        )
      ),
      React.createElement(
        "div",
        { className: "pp-hero-side" },
        React.createElement(
          "div",
          { className: "pp-chips" },
          React.createElement(StatusChip, {
            label: "PeonPing",
            value: status.peon_found ? "found" : "missing",
            tone: status.peon_found ? "ok" : "warn",
          }),
          React.createElement(StatusChip, {
            label: "Adapter",
            value: status.adapter && status.adapter.enabled ? "enabled" : "disabled",
            tone: status.adapter && status.adapter.enabled ? "ok" : "warn",
          }),
          React.createElement(StatusChip, {
            label: "Active pack",
            value: status.active_pack || "—",
          }),
          React.createElement(StatusChip, {
            label: "Rotation",
            value: status.rotation_mode || "—",
          })
        ),
        React.createElement(
          "div",
          { className: "pp-quick-controls" },
          Btn({
            onClick: function () { props.onMuteToggle(!muted); },
            disabled: props.busy || !peonReady,
            className: cn("pp-btn-secondary", muted && "pp-toggle-active"),
            title: muted ? "Unmute PeonPing sounds" : "Mute PeonPing sounds",
          }, muted ? "Unmute" : "Mute"),
          React.createElement(
            "label",
            { className: "pp-volume-control" },
            React.createElement("span", null, "Volume ", React.createElement("strong", null, volumePct + "%")),
            React.createElement("input", {
              type: "range",
              min: "0",
              max: "100",
              step: "5",
              value: volumePct,
              disabled: props.busy || !peonReady,
              onChange: function (e) { props.onVolumeChange(Number(e.target.value)); },
              "aria-label": "PeonPing volume",
            })
          ),
          Btn({
            onClick: function () { props.onNotificationsToggle(!notificationsOn); },
            disabled: props.busy || !peonReady,
            className: cn("pp-btn-secondary", notificationsOn && "pp-toggle-active"),
            title: notificationsOn ? "Disable PeonPing desktop notifications" : "Enable PeonPing desktop notifications",
          }, notificationsOn ? "Turn notifications off" : "Turn notifications on")
        )
      )
    );
  }

  function OperationLog(props) {
    const op = props.operation;
    if (!op && !props.error) return null;
    return React.createElement(
      "section",
      { className: cn("pp-op-log", props.error && "pp-op-log-error") },
      props.error
        ? React.createElement("div", { className: "pp-op-log-line" },
            React.createElement("strong", null, "Error: "),
            String(props.error))
        : null,
      op
        ? React.createElement("div", null,
            React.createElement("div", { className: "pp-op-log-line" },
              React.createElement("strong", null, "$ peon "),
              (op.args || []).join(" "),
              op.returncode !== undefined
                ? React.createElement("span", { className: "pp-op-rc" }, " (exit " + op.returncode + ")")
                : null),
            op.stdout ? React.createElement("pre", { className: "pp-op-stream" }, op.stdout) : null,
            op.stderr ? React.createElement("pre", { className: "pp-op-stream pp-op-stderr" }, op.stderr) : null
          )
        : null
    );
  }

  function isMissingPeonError(message) {
    return /PeonPing executable not found/i.test(String(message || ""));
  }

  function SetupCard(props) {
    const status = props.status || {};
    const copyStatus = props.copyStatus || "";
    return React.createElement("section", { className: "pp-panel pp-setup", "data-peonping-setup": "missing-peon" },
      React.createElement("div", { className: "pp-card-body" },
        React.createElement("div", { className: "pp-setup-head" },
          React.createElement("strong", null, "PeonPing setup"),
          Btn({ onClick: props.onCheck, disabled: props.busy, className: "pp-btn-secondary" }, "Check again")
        ),
        React.createElement("p", { className: "pp-setup-lead" },
          "Registry browsing works now. Installing packs, switching packs, rotation, and sound playback need the ",
          React.createElement("code", null, "peon"),
          " CLI."),
        React.createElement("div", { className: "pp-setup-cta" },
          React.createElement("div", { className: "pp-setup-cta-head" }, "Install peon"),
          React.createElement("div", { className: "pp-setup-cmd-row" },
            React.createElement("span", { className: "pp-setup-cmd-label" }, "macOS (Homebrew):"),
            React.createElement("code", { className: "pp-setup-cmd" }, "brew install PeonPing/tap/peon-ping"),
            Btn({
              onClick: function () { props.onCopyCommand("brew install PeonPing/tap/peon-ping"); },
              className: "pp-btn-secondary pp-setup-copy",
            }, "Copy")
          ),
          React.createElement("div", { className: "pp-setup-cmd-row" },
            React.createElement("span", { className: "pp-setup-cmd-label" }, "Linux / WSL:"),
            React.createElement("code", { className: "pp-setup-cmd" }, "curl -fsSL peonping.com/install | bash"),
            Btn({
              onClick: function () { props.onCopyCommand("curl -fsSL peonping.com/install | bash"); },
              className: "pp-btn-secondary pp-setup-copy",
            }, "Copy")
          ),
          React.createElement("div", { className: "pp-setup-cta-foot" },
            "More options at ",
            React.createElement("a", {
              href: "https://peonping.com",
              target: "_blank",
              rel: "noreferrer noopener",
              className: "pp-setup-link",
            }, "peonping.com"),
            ". After installing, refresh this page."
          ),
          copyStatus ? React.createElement("div", { className: "pp-setup-copy-status" }, copyStatus) : null),
        React.createElement("div", { className: "pp-setup-path" },
          React.createElement("label", { className: "pp-label" }, "Custom peon path"),
          React.createElement("div", { className: "pp-setup-path-row" },
            React.createElement("input", {
              type: "text",
              className: "pp-input",
              placeholder: "/opt/homebrew/bin/peon",
              value: props.commandValue,
              onChange: function (e) { props.onCommandChange(e.target.value); },
              "aria-label": "Custom peon command path",
            }),
            Btn({
              onClick: props.onSaveCommand,
              disabled: props.busy,
            }, "Save path")
          )
        ),
        React.createElement("p", { className: "pp-setup-fine" },
          "Already installed but not detected? Make sure ", React.createElement("code", null, "peon"),
          " is on your ", React.createElement("code", null, "PATH"), ", or save ",
          React.createElement("code", null, "peon_command"), " to ",
          React.createElement("code", null, status.config_path || "~/.hermes/peonping/config.json"), "."))
    );
  }

  function PackExpansion(props) {
    const pack = props.pack;
    // Only installed packs ever reach the expansion (registry cards now show
    // an inline "Install …" footer button instead of an expand affordance).
    if (props.loading) {
      return React.createElement("div", { className: "pp-pack-expansion" },
        React.createElement("div", { className: "pp-muted" }, "Loading sounds…")
      );
    }
    const sb = props.soundboard;
    if (!sb || !sb.categories || sb.categories.length === 0) {
      return React.createElement("div", { className: "pp-pack-expansion" },
        React.createElement("div", { className: "pp-muted" }, "This pack has no playable sounds in its openpeon.json.")
      );
    }
    return React.createElement("div", { className: "pp-pack-expansion", onClick: function (e) { e.stopPropagation(); } },
      pack.description
        ? React.createElement("p", { className: "pp-pack-desc pp-pack-desc-expanded" }, pack.description)
        : null,
      sb.categories.map(function (cat) {
        return React.createElement(CategorySection, {
          key: cat.name,
          category: cat,
          nowPlayingId: props.nowPlayingId,
          onPlay: props.onPlay,
        });
      })
    );
  }

  function PackCard(props) {
    const pack = props.pack;
    const selected = props.selected;
    const installed = pack.installed;
    const allowExpand = !!props.allowExpand;
    const expanded = props.expanded && installed && allowExpand;
    const active = pack.active;
    const displayName = pack.display_name || pack.name;
    const playing = expanded && props.nowPlayingId;
    const meta = [];
    if (pack.language) meta.push(pack.language.toUpperCase());
    if (pack.sound_count) meta.push(pack.sound_count + " sounds");
    if (pack.category_count) meta.push(pack.category_count + " categories");
    return React.createElement(
      "div",
      {
        className: cn(
          "pp-pack-card",
          selected && "pp-pack-card-selected",
          active && "pp-pack-card-active",
          expanded && "pp-pack-card-expanded",
          !installed && "pp-pack-card-uninstalled"
        ),
        role: "button",
        tabIndex: 0,
        onClick: function () { if (installed) props.onExpand(pack.name, true); },
        onKeyDown: function (e) {
          if (e.key === " " || e.key === "Enter") {
            e.preventDefault();
            if (installed) props.onExpand(pack.name, true);
          }
        },
        "aria-expanded": installed ? !!expanded : undefined,
        "aria-label": installed ? ((expanded ? "Collapse " : "Expand ") + displayName) : displayName,
      },
      React.createElement("div", { className: "pp-pack-card-head" },
        React.createElement("input", {
          type: "checkbox",
          className: "pp-pack-checkbox",
          checked: !!selected,
          onChange: function () { props.onToggle(pack.name); },
          onClick: function (e) { e.stopPropagation(); },
          "aria-label": (selected ? "Deselect " : "Select ") + displayName,
          tabIndex: -1,
        }),
        React.createElement("span", { className: "pp-pack-name", title: displayName }, displayName),
        active ? React.createElement("span", { className: "pp-pack-badge pp-pack-badge-active" }, "active") : null
      ),
      meta.length
        ? React.createElement("div", { className: "pp-pack-meta" }, meta.join(" · "))
        : null,
      !expanded && pack.description
        ? React.createElement("p", { className: "pp-pack-desc" }, pack.description)
        : null,
      installed && !allowExpand
        ? null
        : React.createElement("div", { className: cn("pp-pack-card-foot", !installed && "pp-pack-card-foot-install") },
            installed && !active
              ? Btn({
                  onClick: function (e) { e.stopPropagation(); props.onUseOne(pack.name); },
                  disabled: !!props.busy,
                  className: "pp-btn-use",
                  title: "Use " + displayName,
                  "aria-label": "Use " + displayName,
                }, "Use")
              : null,
            installed
              ? React.createElement("button", {
                  type: "button",
                  className: cn("pp-pack-play", playing && "pp-pack-play-active"),
                  onClick: function (e) { e.stopPropagation(); props.onExpand(pack.name, true); },
                  "aria-label": (expanded ? "Collapse " : "Expand ") + displayName + " sounds",
                  "aria-expanded": !!expanded,
                  title: expanded ? "Collapse" : "Preview sounds",
                },
                  React.createElement("span", { "aria-hidden": "true" }, expanded ? "❚❚" : "▶")
                )
              : Btn({
                  onClick: function (e) { e.stopPropagation(); props.onInstallOne(pack.name); },
                  disabled: !!props.busy,
                  className: "pp-btn-install",
                  title: "Install " + displayName,
                  "aria-label": "Install " + displayName,
                }, "Install")
          ),
      expanded && installed
        ? React.createElement(PackExpansion, {
            pack: pack,
            loading: props.expandedLoading,
            soundboard: props.soundboard,
            nowPlayingId: props.nowPlayingId,
            onPlay: props.onPlay,
          })
        : null
    );
  }

  const REGISTRY_PAGE_SIZE = 60;

  function cardPropsFor(props, pack, allowExpand) {
    return {
      pack: pack,
      selected: props.selected.has(pack.name),
      expanded: props.expandedName === pack.name,
      expandedLoading: props.expandedName === pack.name && props.expandedLoading,
      soundboard: props.expandedName === pack.name ? props.expandedSoundboard : null,
      nowPlayingId: props.nowPlayingId,
      busy: props.busy,
      allowExpand: !!allowExpand,
      onToggle: props.onToggle,
      onExpand: props.onExpand,
      onPlay: props.onPlay,
      onInstallOne: props.onInstallOne,
      onUseOne: props.onUseOne,
    };
  }

  function SelectionBar(props) {
    const count = props.selected.size;
    const visibleCount = props.visibleNames.length;
    const allVisibleSelected = visibleCount > 0 && props.visibleNames.every(function (n) { return props.selected.has(n); });
    return React.createElement("div", { className: "pp-selbar" },
      React.createElement("span", { className: "pp-selbar-count" },
        count === 0
          ? "Nothing selected"
          : count + (count === 1 ? " pack selected" : " packs selected")
      ),
      React.createElement("div", { className: "pp-selbar-actions" },
        props.hideSelectAll
          ? null
          : Btn({
              onClick: function () {
                if (allVisibleSelected) {
                  props.onSetSelected(new Set());
                } else {
                  const next = new Set(props.selected);
                  props.visibleNames.forEach(function (n) { next.add(n); });
                  props.onSetSelected(next);
                }
              },
              className: "pp-btn-secondary",
              disabled: visibleCount === 0,
            }, allVisibleSelected ? "Deselect visible" : ("Select " + (props.searchActive ? "visible" : "all"))),
        Btn({
          onClick: function () { props.onSetSelected(new Set()); },
          className: "pp-btn-secondary",
          disabled: count === 0,
        }, "Clear")
      )
    );
  }

  function RegistryBrowser(props) {
    const filtered = React.useMemo(function () {
      const term = (props.search || "").trim().toLowerCase();
      if (!term) return props.packs || [];
      return (props.packs || []).filter(function (p) {
        const hay = (p.name + " " + (p.display_name || "") + " " + (p.description || "")).toLowerCase();
        return hay.indexOf(term) >= 0;
      });
    }, [props.packs, props.search]);

    const [visibleCount, setVisibleCount] = React.useState(REGISTRY_PAGE_SIZE);
    React.useEffect(function () {
      setVisibleCount(REGISTRY_PAGE_SIZE);
    }, [props.search, props.packs]);

    const total = (props.packs || []).length;
    const matched = filtered.length;
    const shown = Math.min(visibleCount, matched);
    const visiblePacks = React.useMemo(function () {
      return filtered.slice(0, shown);
    }, [filtered, shown]);
    const hasMore = matched > shown;

    const scrollRef = React.useRef(null);
    React.useEffect(function () {
      if (!hasMore) return undefined;
      const node = scrollRef.current;
      if (!node) return undefined;
      function onScroll() {
        if (node.scrollTop + node.clientHeight >= node.scrollHeight - 240) {
          setVisibleCount(function (n) { return n + REGISTRY_PAGE_SIZE; });
        }
      }
      node.addEventListener("scroll", onScroll, { passive: true });
      return function () { node.removeEventListener("scroll", onScroll); };
    }, [hasMore, shown]);

    return React.createElement(
      "section",
      { className: "pp-panel" },
      React.createElement("header", { className: "pp-panel-head" },
        React.createElement("h2", null,
          "Registry packs",
          React.createElement("span", { className: "pp-count" },
            matched === total ? total : (matched + " of " + total))
        ),
        React.createElement("div", { className: "pp-panel-actions" },
          React.createElement("input", {
            type: "search",
            className: "pp-search",
            placeholder: "Search packs…",
            value: props.search,
            onChange: function (e) { props.onSearch(e.target.value); },
            "aria-label": "Search registry packs",
          }),
          Btn({ onClick: props.onRefresh, disabled: props.busy, className: "pp-refresh", title: "Refresh registry" }, "Refresh")
        )
      ),
      matched === 0
        ? React.createElement("div", { className: "pp-empty" },
            total === 0 && props.peonAvailable === false
              ? "Registry unavailable until peon is installed or peon_command is configured."
              : (total === 0 ? "No registry packs available." : "No packs match this filter."))
        : React.createElement("div", { className: "pp-grid-scroll", ref: scrollRef },
            React.createElement("div", { className: "pp-grid" }, visiblePacks.map(function (p) {
              return React.createElement(PackCard, Object.assign({ key: p.name }, cardPropsFor(props, p, false)));
            })),
            hasMore
              ? React.createElement("div", { className: "pp-grid-more" },
                  React.createElement("span", { className: "pp-muted" },
                    "Showing " + shown + " of " + matched),
                  Btn({
                    onClick: function () { setVisibleCount(function (n) { return n + REGISTRY_PAGE_SIZE; }); },
                    className: "pp-btn-secondary",
                  }, "Load more")
                )
              : React.createElement("div", { className: "pp-grid-more pp-muted" },
                  "Showing all " + matched + (matched === 1 ? " pack" : " packs"))
          ),
      matched === 0
        ? null
        : React.createElement(SelectionBar, {
            selected: props.selected,
            visibleNames: visiblePacks.map(function (p) { return p.name; }),
            searchActive: !!(props.search && props.search.trim()),
            onSetSelected: props.onSetSelected,
            hideSelectAll: true,
          })
    );
  }

  function InstalledPanel(props) {
    const installed = props.packs || [];
    return React.createElement("section", { className: "pp-panel" },
      React.createElement("header", { className: "pp-panel-head" },
        React.createElement("h2", null,
          "Installed packs",
          React.createElement("span", { className: "pp-count" }, installed.length)
        )
      ),
      installed.length === 0
        ? React.createElement("div", { className: "pp-empty" }, "No packs installed. Install a pack from the registry above.")
        : React.createElement("div", { className: "pp-grid pp-grid-compact" }, installed.map(function (p) {
            return React.createElement(PackCard, Object.assign({ key: p.name }, cardPropsFor(props, p, true)));
          })),
      installed.length === 0
        ? null
        : React.createElement(SelectionBar, {
            selected: props.selected,
            visibleNames: installed.map(function (p) { return p.name; }),
            searchActive: false,
            onSetSelected: props.onSetSelected,
          })
    );
  }

  function PackActionsBar(props) {
    return React.createElement("div", { className: "pp-actions" },
      Btn({
        onClick: props.onInstall,
        disabled: props.busy || props.selectedRegistry.size === 0,
        title: "Install selected registry pack(s)",
      }, "Install selected (" + props.selectedRegistry.size + ")"),
      Btn({
        onClick: props.onUseInstall,
        disabled: props.busy || props.selectedRegistry.size !== 1,
        title: "Install and switch to a single selected registry pack",
      }, "Install + Use"),
      Btn({
        onClick: props.onUse,
        disabled: props.busy || props.useTargetName == null,
        title: "Use selected installed pack",
      }, "Use selected" + (props.useTargetName ? ": " + props.useTargetName : "")),
      Btn({
        onClick: props.onRemove,
        disabled: props.busy || props.selectedInstalled.size === 0,
        title: "Remove selected installed pack(s)",
        className: "pp-btn-danger",
      }, "Remove selected (" + props.selectedInstalled.size + ")")
    );
  }

  function LocalInstallCard(props) {
    return React.createElement(Card, { className: "pp-local" },
      React.createElement(CardBody, null,
        React.createElement("strong", null, "Install local pack"),
        React.createElement("p", { className: "pp-muted" }, "Point to a folder that contains an ", React.createElement("code", null, "openpeon.json"), "."),
        React.createElement("div", { className: "pp-local-row" },
          React.createElement("input", {
            type: "text",
            className: "pp-input",
            placeholder: "/path/to/voicepack",
            value: props.value,
            onChange: function (e) { props.onChange(e.target.value); },
            "aria-label": "Local pack path",
          }),
          Btn({ onClick: props.onInstall, disabled: props.busy || !props.value.trim() }, "Install local")
        )
      )
    );
  }

  function RotationPanel(props) {
    const rotation = props.rotation || { mode: "", packs: [] };
    const rotationActive = !!props.rotationActive;
    return React.createElement("section", { className: "pp-panel" },
      React.createElement("header", { className: "pp-panel-head" },
        React.createElement("h2", null, "Rotation"),
        React.createElement("div", { className: "pp-chips" },
          rotationActive
            ? React.createElement("span", { className: "pp-chip pp-chip-ok" },
                "Active",
                React.createElement("strong", { className: "pp-chip-value" }, "rotation"))
            : null,
          React.createElement("span", { className: "pp-muted" }, rotation.packs.length + " in rotation")
        )
      ),
      React.createElement("div", { className: "pp-rotation-row" },
        React.createElement("label", { className: "pp-label" }, "Mode"),
        React.createElement("select", {
          className: "pp-select",
          value: rotation.mode || "",
          onChange: function (e) { props.onMode(e.target.value); },
          disabled: props.busy,
          "aria-label": "Rotation mode",
        },
          React.createElement("option", { value: "" }, "— select —"),
          ROTATION_MODES.map(function (m) {
            return React.createElement("option", { key: m, value: m }, m);
          })
        )
      ),
      rotation.packs.length === 0
        ? React.createElement("div", { className: "pp-empty" }, "No packs in rotation.")
        : React.createElement("div", { className: "pp-chips pp-chips-removable" },
            rotation.packs.map(function (name) {
              return React.createElement("span", { key: name, className: "pp-chip" },
                name,
                React.createElement("button", {
                  type: "button",
                  className: "pp-chip-remove",
                  "aria-label": "Remove " + name + " from rotation",
                  onClick: function () { props.onRemoveOne(name); },
                  disabled: props.busy,
                }, "×")
              );
            })
          ),
      React.createElement("div", { className: "pp-actions" },
        Btn({
          onClick: function () { props.onUseRotation(!rotationActive); },
          disabled: props.busy || (!rotationActive && rotation.packs.length === 0),
          className: rotationActive ? "pp-btn-danger" : undefined,
        }, rotationActive ? "Stop using rotation" : "Use rotation"),
        Btn({
          onClick: props.onAdd,
          disabled: props.busy || props.selectedAddable.size === 0,
        }, "Add selected (" + props.selectedAddable.size + ")"),
        Btn({
          onClick: props.onAddInstall,
          disabled: props.busy || props.selectedAddable.size === 0,
        }, "Add + Install"),
        Btn({
          onClick: props.onClear,
          disabled: props.busy || rotation.packs.length === 0,
          className: "pp-btn-danger",
        }, "Clear rotation")
      )
    );
  }

  function SoundPad(props) {
    const sound = props.sound;
    return React.createElement("button", {
      type: "button",
      className: cn("pp-pad", props.playing && "pp-pad-playing"),
      onClick: function () { props.onPlay(sound); },
      title: sound.file,
      "aria-label": "Play " + sound.label,
    },
      React.createElement("span", { className: "pp-pad-icon", "aria-hidden": "true" }, props.playing ? "❚❚" : "▶"),
      React.createElement("span", { className: "pp-pad-label" }, sound.label),
      React.createElement("span", { className: "pp-pad-meta" }, sound.file.split("/").pop())
    );
  }

  function CategorySection(props) {
    const cat = props.category;
    return React.createElement("section", { className: "pp-cat" },
      React.createElement("header", { className: "pp-cat-head" },
        React.createElement("span", { className: "pp-cat-label" }, cat.label || cat.name),
        React.createElement("span", { className: "pp-muted" }, cat.sounds.length + " sounds")
      ),
      React.createElement("div", { className: "pp-pads" }, cat.sounds.map(function (sound) {
        return React.createElement(SoundPad, {
          key: sound.id,
          sound: sound,
          playing: props.nowPlayingId === sound.id,
          onPlay: props.onPlay,
        });
      }))
    );
  }

  function PeonPingPage() {
    const [loading, setLoading] = React.useState(true);
    const [busy, setBusy] = React.useState(false);
    const [error, setError] = React.useState(null);
    const [status, setStatus] = React.useState(null);
    const [installed, setInstalled] = React.useState([]);
    const [registry, setRegistry] = React.useState([]);
    const [registryMeta, setRegistryMeta] = React.useState({ peonAvailable: true });
    const [rotation, setRotation] = React.useState({ mode: "", packs: [] });
    const [selectedRegistry, setSelectedRegistry] = React.useState(new Set());
    const [selectedInstalled, setSelectedInstalled] = React.useState(new Set());
    const [search, setSearch] = React.useState("");
    const [expandedName, setExpandedName] = React.useState(null);
    const [expandedSoundboard, setExpandedSoundboard] = React.useState(null);
    const [expandedLoading, setExpandedLoading] = React.useState(false);
    const [nowPlayingId, setNowPlayingId] = React.useState(null);
    const [localInstallPath, setLocalInstallPath] = React.useState("");
    const [operation, setOperation] = React.useState(null);
    const [setupCommand, setSetupCommand] = React.useState("");
    const [copyStatus, setCopyStatus] = React.useState("");
    const [volumeDraft, setVolumeDraft] = React.useState(50);
    const audioRef = React.useRef(null);
    const expandedReqRef = React.useRef(null);
    const volumeTimerRef = React.useRef(null);

    const setSelected = function (which, name) {
      const setter = which === "registry" ? setSelectedRegistry : setSelectedInstalled;
      setter(function (prev) {
        const next = new Set(prev);
        if (next.has(name)) next.delete(name); else next.add(name);
        return next;
      });
    };

    const fetchStatus = React.useCallback(async function () {
      try {
        const data = await api("/status");
        setStatus(data);
        if (data && typeof data.volume === "number" && isFinite(data.volume)) {
          setVolumeDraft(Math.max(0, Math.min(100, Math.round(data.volume * 100))));
        }
        if (data && data.adapter && data.adapter.peon_command) {
          setSetupCommand(data.adapter.peon_command);
        }
        return data;
      } catch (e) {
        setError(e.message);
        return null;
      }
    }, []);

    const fetchInstalled = React.useCallback(async function () {
      try {
        const data = await api("/packs");
        setInstalled(data.packs || []);
        return data.packs || [];
      } catch (e) {
        if (!isMissingPeonError(e.message)) setError(e.message);
        return [];
      }
    }, []);

    const fetchRegistry = React.useCallback(async function () {
      try {
        const data = await api("/packs?registry=true");
        setRegistry(data.packs || []);
        setRegistryMeta({ peonAvailable: data.peon_available !== false });
        return data.packs || [];
      } catch (e) {
        if (!isMissingPeonError(e.message)) setError(e.message);
        setRegistryMeta({ peonAvailable: false });
        return [];
      }
    }, []);

    const fetchRotation = React.useCallback(async function () {
      try {
        const data = await api("/rotation");
        setRotation(data.rotation || { mode: "", packs: [] });
      } catch (e) {
        if (!isMissingPeonError(e.message)) setError(e.message);
      }
    }, []);

    const fetchSoundboard = React.useCallback(async function (name) {
      if (!name) return null;
      try {
        return await api("/packs/" + encodeURIComponent(name) + "/sounds");
      } catch (e) {
        setError(e.message);
        return null;
      }
    }, []);

    const loadAll = React.useCallback(async function () {
      setLoading(true);
      setError(null);
      await Promise.all([fetchStatus(), fetchInstalled(), fetchRegistry(), fetchRotation()]);
      setLoading(false);
    }, [fetchStatus, fetchInstalled, fetchRegistry, fetchRotation]);

    React.useEffect(function () {
      loadAll();
    }, [loadAll]);

    React.useEffect(function () {
      return function () {
        if (volumeTimerRef.current) window.clearTimeout(volumeTimerRef.current);
      };
    }, []);

    const runCommand = async function (label, sender) {
      setBusy(true);
      setError(null);
      try {
        const data = await sender();
        if (data && (data.stdout !== undefined || data.stderr !== undefined)) {
          setOperation({
            label: label,
            args: data.args || [],
            stdout: data.stdout || "",
            stderr: data.stderr || "",
            returncode: data.returncode,
          });
        }
        return data;
      } catch (e) {
        setError(e.message);
        if (e.payload && e.payload.detail) {
          const d = e.payload.detail;
          setOperation({
            label: label,
            args: [],
            stdout: d.stdout || "",
            stderr: d.stderr || String(d.error || e.message),
            returncode: d.returncode != null ? d.returncode : -1,
          });
        }
        return null;
      } finally {
        setBusy(false);
      }
    };

    const handleMuteToggle = async function (muted) {
      const data = await runCommand(muted ? "mute" : "unmute", function () {
        return api("/mute", { method: "POST", body: { muted: !!muted } });
      });
      if (data && data.status) setStatus(data.status);
      else await fetchStatus();
    };

    const commitVolume = async function (pct) {
      const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
      const volume = Math.round(clamped) / 100;
      const data = await runCommand("volume " + volume.toFixed(2), function () {
        return api("/volume", { method: "POST", body: { volume: volume } });
      });
      if (data && data.status) setStatus(data.status);
      else await fetchStatus();
    };

    const handleVolumeInput = function (pct) {
      const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
      setVolumeDraft(clamped);
      if (volumeTimerRef.current) window.clearTimeout(volumeTimerRef.current);
      volumeTimerRef.current = window.setTimeout(function () {
        volumeTimerRef.current = null;
        commitVolume(clamped);
      }, 350);
    };

    const handleNotificationsToggle = async function (enabled) {
      const data = await runCommand(enabled ? "notifications on" : "notifications off", function () {
        return api("/notifications", { method: "POST", body: { enabled: !!enabled } });
      });
      if (data && data.status) setStatus(data.status);
      else await fetchStatus();
    };

    const handleInstall = async function () {
      const names = Array.from(selectedRegistry);
      if (names.length === 0) return;
      const data = await runCommand("install", function () {
        return api("/packs/install", { method: "POST", body: { names: names } });
      });
      if (data && data.ok) {
        setSelectedRegistry(new Set());
        await fetchInstalled();
        await fetchStatus();
      }
    };

    const handleUse = async function () {
      const installedSelection = Array.from(selectedInstalled);
      if (installedSelection.length !== 1) return;
      const name = installedSelection[0];
      const data = await runCommand("use " + name, function () {
        return api("/packs/use", { method: "POST", body: { name: name } });
      });
      if (data && data.ok) {
        await fetchStatus();
        await fetchInstalled();
      }
    };

    const handleUseOne = async function (name) {
      if (!name) return;
      const data = await runCommand("use " + name, function () {
        return api("/packs/use", { method: "POST", body: { name: name } });
      });
      if (data && data.ok) {
        await fetchStatus();
        await fetchInstalled();
      }
    };

    const handleUseInstall = async function () {
      const names = Array.from(selectedRegistry);
      if (names.length !== 1) return;
      const name = names[0];
      const data = await runCommand("use --install " + name, function () {
        return api("/packs/use", { method: "POST", body: { name: name, install: true } });
      });
      if (data && data.ok) {
        setSelectedRegistry(new Set());
        await fetchInstalled();
        await fetchStatus();
      }
    };

    const handleInstallOne = async function (name) {
      if (!name) return;
      const data = await runCommand("install " + name, function () {
        return api("/packs/install", { method: "POST", body: { names: [name] } });
      });
      if (data && data.ok) {
        await fetchInstalled();
        await fetchStatus();
        // Pop the pack open in the Installed panel so the user can preview
        // the sounds they just downloaded without hunting for the card.
        setExpandedName(name);
        expandedReqRef.current = name;
        setExpandedLoading(true);
        const sb = await fetchSoundboard(name);
        if (expandedReqRef.current === name) {
          setExpandedSoundboard(sb);
          setExpandedLoading(false);
        }
      }
    };

    const handleRemove = async function () {
      const names = Array.from(selectedInstalled);
      if (names.length === 0) return;
      const activePack = (status && status.active_pack) || "";
      const activeInSelection = activePack && names.indexOf(activePack) !== -1;
      if (activeInSelection) {
        setError(
          "Cannot remove '" + activePack + "' — it is the currently active pack. " +
          "Switch to another installed pack first (click 'Use' on a different pack), then retry."
        );
        return;
      }
      if (typeof window.confirm === "function") {
        if (!window.confirm("Remove " + names.length + " pack(s)?\n" + names.join(", "))) return;
      }
      const data = await runCommand("remove", function () {
        return api("/packs/remove", { method: "POST", body: { names: names } });
      });
      if (data && data.ok) {
        setSelectedInstalled(new Set());
        await fetchInstalled();
        await fetchStatus();
      }
    };

    const handleLocalInstall = async function () {
      const path = localInstallPath.trim();
      if (!path) return;
      const data = await runCommand("install-local " + path, function () {
        return api("/packs/install-local", { method: "POST", body: { path: path } });
      });
      if (data && data.ok) {
        setLocalInstallPath("");
        await fetchInstalled();
        await fetchStatus();
      }
    };

    const handleCopyCommand = async function (command) {
      setCopyStatus("");
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          throw new Error("Clipboard API is not available.");
        }
        await navigator.clipboard.writeText(command);
        setCopyStatus("Copied.");
      } catch (e) {
        setCopyStatus("Select the command text and copy it manually.");
      }
    };

    const handleSavePeonCommand = async function () {
      const command = setupCommand.trim();
      if (!command) {
        setError("Enter the full path to the peon executable first.");
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const data = await api("/config/peon-command", { method: "POST", body: { peon_command: command } });
        if (data && data.status) setStatus(data.status);
        await fetchInstalled();
        await fetchRotation();
      } catch (e) {
        setError(e.message);
      } finally {
        setBusy(false);
      }
    };

    const handleCheckPeon = async function () {
      setBusy(true);
      setError(null);
      try {
        const nextStatus = await fetchStatus();
        await fetchInstalled();
        await fetchRotation();
        if (nextStatus && nextStatus.peon_found) setError(null);
      } finally {
        setBusy(false);
      }
    };

    const handleSetMode = async function (mode) {
      if (!mode) return;
      await runCommand("rotation " + mode, function () {
        return api("/rotation/mode", { method: "POST", body: { mode: mode } });
      });
      await fetchRotation();
      await fetchStatus();
    };

    const handleAddRotation = async function (install) {
      const names = Array.from(selectedRegistry).concat(Array.from(selectedInstalled));
      const unique = Array.from(new Set(names));
      if (unique.length === 0) return;
      await runCommand("rotation add", function () {
        return api("/rotation/add", { method: "POST", body: { names: unique, install: !!install } });
      });
      setSelectedRegistry(new Set());
      setSelectedInstalled(new Set());
      await fetchRotation();
      await fetchInstalled();
      await fetchStatus();
    };

    const handleRemoveRotation = async function (name) {
      await runCommand("rotation remove " + name, function () {
        return api("/rotation/remove", { method: "POST", body: { names: [name] } });
      });
      await fetchRotation();
      await fetchStatus();
    };

    const handleClearRotation = async function () {
      if (typeof window.confirm === "function") {
        if (!window.confirm("Clear all packs from rotation?")) return;
      }
      await runCommand("rotation clear", function () {
        return api("/rotation/clear", { method: "POST", body: {} });
      });
      await fetchRotation();
      await fetchStatus();
    };

    const handleUseRotation = async function (enabled) {
      const data = await runCommand(enabled ? "rotation use" : "rotation stop", function () {
        return api("/rotation/use", { method: "POST", body: { enabled: !!enabled } });
      });
      if (data && data.ok) {
        if (data.status) setStatus(data.status);
        if (data.packs) setInstalled(data.packs);
        await fetchRotation();
        await fetchStatus();
        await fetchInstalled();
      }
    };

    const revokeCurrentBlob = function () {
      const audio = audioRef.current;
      if (audio && audio.src && audio.src.indexOf("blob:") === 0) {
        URL.revokeObjectURL(audio.src);
      }
    };

    const handlePlay = async function (sound) {
      const audio = audioRef.current;
      if (!audio) return;
      if (nowPlayingId === sound.id) {
        audio.pause();
        setNowPlayingId(null);
        return;
      }
      // The dashboard auth middleware requires the session-token header on every
      // /api/* request, but <audio src="…"> can't attach custom headers. Fetch
      // the file via the authenticated client and play it from a blob URL.
      try {
        const token = window.__HERMES_SESSION_TOKEN__ || "";
        const headers = token ? { "X-Hermes-Session-Token": token } : {};
        const res = await fetch(sound.audio_url, { headers: headers, credentials: "same-origin" });
        if (!res.ok) {
          throw new Error("HTTP " + res.status + " " + res.statusText);
        }
        const blob = await res.blob();
        revokeCurrentBlob();
        audio.src = URL.createObjectURL(blob);
        audio.currentTime = 0;
        setNowPlayingId(sound.id);
        await audio.play();
      } catch (err) {
        setError("Audio playback failed: " + (err && err.message ? err.message : err));
        setNowPlayingId(null);
      }
    };

    const handleStop = function () {
      const audio = audioRef.current;
      if (audio) audio.pause();
      setNowPlayingId(null);
    };

    const handleExpand = async function (name, isInstalled) {
      // Toggle collapse if the same card was clicked.
      if (expandedName === name) {
        handleStop();
        setExpandedName(null);
        setExpandedSoundboard(null);
        setExpandedLoading(false);
        expandedReqRef.current = null;
        return;
      }
      handleStop();
      setExpandedName(name);
      setExpandedSoundboard(null);
      expandedReqRef.current = name;
      if (!isInstalled) {
        setExpandedLoading(false);
        return;
      }
      setExpandedLoading(true);
      const sb = await fetchSoundboard(name);
      // Drop the result if the user expanded a different card in the meantime.
      if (expandedReqRef.current !== name) return;
      setExpandedSoundboard(sb);
      setExpandedLoading(false);
    };

    React.useEffect(function () {
      const audio = audioRef.current;
      if (!audio) return;
      const onEnded = function () { setNowPlayingId(null); };
      audio.addEventListener("ended", onEnded);
      return function () {
        audio.removeEventListener("ended", onEnded);
        revokeCurrentBlob();
      };
    }, []);

    const useTargetName = selectedInstalled.size === 1
      ? Array.from(selectedInstalled)[0]
      : null;

    const selectedAddable = React.useMemo(function () {
      const merged = new Set();
      selectedRegistry.forEach(function (n) { merged.add(n); });
      selectedInstalled.forEach(function (n) { merged.add(n); });
      return merged;
    }, [selectedRegistry, selectedInstalled]);

    if (loading) {
      return React.createElement("div", { className: "pp-page" },
        React.createElement("section", { className: "pp-hero" },
          React.createElement("div", { className: "pp-hero-text" },
            React.createElement("div", { className: "pp-kicker" }, "Hermes plugin"),
            React.createElement("h1", null, "Sound Alerts - PeonPing"),
            React.createElement("p", null, "Loading…")
          )
        )
      );
    }

    const setupNeeded = (status && status.peon_found !== true) || registryMeta.peonAvailable === false;
    const showError = error && !(setupNeeded && isMissingPeonError(error));

    return React.createElement("div", { className: "pp-page" },
      React.createElement(StatusHeader, {
        status: status,
        busy: busy,
        volumeDraft: volumeDraft,
        onMuteToggle: handleMuteToggle,
        onVolumeChange: handleVolumeInput,
        onNotificationsToggle: handleNotificationsToggle,
      }),
      setupNeeded
        ? React.createElement(SetupCard, {
            status: status,
            busy: busy,
            commandValue: setupCommand,
            copyStatus: copyStatus,
            onCommandChange: setSetupCommand,
            onCopyCommand: handleCopyCommand,
            onSaveCommand: handleSavePeonCommand,
            onCheck: handleCheckPeon,
          })
        : null,
      showError
        ? React.createElement("div", { className: "pp-error" },
            React.createElement("strong", null, "Error: "), String(error),
            Btn({ onClick: function () { setError(null); }, className: "pp-btn-secondary pp-error-dismiss" }, "Dismiss"))
        : null,
      React.createElement(RegistryBrowser, {
        packs: registry,
        peonAvailable: registryMeta.peonAvailable,
        search: search,
        selected: selectedRegistry,
        busy: busy,
        expandedName: expandedName,
        expandedSoundboard: expandedSoundboard,
        expandedLoading: expandedLoading,
        nowPlayingId: nowPlayingId,
        onSearch: setSearch,
        onToggle: function (name) { setSelected("registry", name); },
        onSetSelected: setSelectedRegistry,
        onExpand: handleExpand,
        onPlay: handlePlay,
        onInstallOne: handleInstallOne,
        onUseOne: handleUseOne,
        onRefresh: async function () {
          setBusy(true);
          try { await fetchRegistry(); } finally { setBusy(false); }
        },
      }),
      React.createElement(PackActionsBar, {
        busy: busy,
        selectedRegistry: selectedRegistry,
        selectedInstalled: selectedInstalled,
        useTargetName: useTargetName,
        onInstall: handleInstall,
        onUse: handleUse,
        onUseInstall: handleUseInstall,
        onRemove: handleRemove,
      }),
      React.createElement(InstalledPanel, {
        packs: installed,
        selected: selectedInstalled,
        busy: busy,
        expandedName: expandedName,
        expandedSoundboard: expandedSoundboard,
        expandedLoading: expandedLoading,
        nowPlayingId: nowPlayingId,
        onToggle: function (name) { setSelected("installed", name); },
        onSetSelected: setSelectedInstalled,
        onExpand: handleExpand,
        onPlay: handlePlay,
        onInstallOne: handleInstallOne,
        onUseOne: handleUseOne,
      }),
      React.createElement(LocalInstallCard, {
        value: localInstallPath,
        onChange: setLocalInstallPath,
        onInstall: handleLocalInstall,
        busy: busy,
      }),
      React.createElement(RotationPanel, {
        rotation: rotation,
        rotationActive: !!(status && status.adapter && status.adapter.use_rotation),
        busy: busy,
        selectedAddable: selectedAddable,
        onMode: handleSetMode,
        onAdd: function () { handleAddRotation(false); },
        onAddInstall: function () { handleAddRotation(true); },
        onRemoveOne: handleRemoveRotation,
        onUseRotation: handleUseRotation,
        onClear: handleClearRotation,
      }),
      React.createElement(OperationLog, { operation: operation }),
      React.createElement("audio", { ref: audioRef, className: "pp-audio", preload: "none" })
    );
  }

  window.__HERMES_PLUGINS__.register("peonping", PeonPingPage);
})();
