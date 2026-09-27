#!/usr/bin/env bash
# cproj installer — links the CLI and the two skills into every agent harness it finds, and wires the
# status line, the SessionEnd hook and two keybindings into Claude Code. Idempotent; backs up what it replaces.
#   ./install.sh              install (or update the links)
#   ./install.sh --uninstall  remove links and settings entries again
#
# Skill locations:
#   ~/.claude/skills                 Claude Code
#   ~/.agents/skills                 Codex, Cline, pi, oh-my-pi, OpenClaw, OpenCode, Gemini CLI (shared standard dir)
#   ~/.hermes/skills/productivity    Hermes Agent (only if ~/.hermes exists)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CPROJ_BIN_DIR:-$HOME/.local/bin}"
CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
AGENTS="${CPROJ_AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"
HERMES="${HERMES_HOME:-$HOME/.hermes}"
BACKUP="${XDG_DATA_HOME:-$HOME/.local/share}/cproj/backups"
MODE="${1:-install}"

command -v python3 >/dev/null || { echo "cproj needs python3" >&2; exit 1; }

TARGETS=("$CLAUDE/skills" "$AGENTS")
[ -d "$HERMES" ] && TARGETS+=("$HERMES/skills/productivity")

if [ "$MODE" = "--uninstall" ]; then
  rm -f "$BIN/cproj"
  for t in "${TARGETS[@]}"; do
    for s in project project-off; do
      [ -L "$t/$s" ] && rm -f "$t/$s"
    done
  done
else
  mkdir -p "$BIN"
  ln -sfn "$REPO/skills/project/scripts/cproj.py" "$BIN/cproj"
  chmod +x "$REPO/skills/project/scripts/cproj.py"
  for t in "${TARGETS[@]}"; do
    mkdir -p "$t"
    for s in project project-off; do
      if [ -e "$t/$s" ] && [ ! -L "$t/$s" ]; then  # a real folder of the same name: move it out of the way
        mkdir -p "$BACKUP"
        mv "$t/$s" "$BACKUP/$(echo "$t" | tr / _)_$s.$(date +%Y%m%d-%H%M%S)"
      fi
      ln -sfn "$REPO/skills/$s" "$t/$s"
    done
    echo "skills linked into $t"
  done
fi

python3 - "$MODE" "$CLAUDE" <<'PY'
import json, shutil, sys, time
from pathlib import Path

mode, claude = sys.argv[1], Path(sys.argv[2])
CMD_STATUS, CMD_END = "cproj statusline", "cproj session-end"
KEYS = {"ctrl+x p": "command:project", "ctrl+x o": "command:project-off"}


def edit(path, fn):
    data = json.loads(path.read_text()) if path.exists() else {}
    new = fn(json.loads(json.dumps(data)))
    if new != data:
        if path.exists():
            shutil.copy(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        print(f"updated {path}")


def settings(d):
    hooks = d.setdefault("hooks", {})
    ends = hooks.setdefault("SessionEnd", [])
    ours = lambda h: any(str(x.get("command", "")).endswith(CMD_END) for x in h.get("hooks", []))
    if mode == "--uninstall":
        hooks["SessionEnd"] = [h for h in ends if not ours(h)]
        if not hooks["SessionEnd"]:
            del hooks["SessionEnd"]
        if not hooks:
            del d["hooks"]
        if str((d.get("statusLine") or {}).get("command", "")).endswith(CMD_STATUS):
            del d["statusLine"]
        return d
    if not any(ours(h) for h in ends):
        ends.append({"hooks": [{"type": "command", "command": CMD_END, "timeout": 10}]})
    if "statusLine" not in d:
        d["statusLine"] = {"type": "command", "command": CMD_STATUS, "padding": 0}
    elif not str(d["statusLine"].get("command", "")).endswith(CMD_STATUS):
        print(f"note: you already have a statusLine ({d['statusLine'].get('command')!r}); left it alone.\n"
              f"      To show the project badge, use `{CMD_STATUS}` or call it from your own script.")
    return d


def keybindings(d):
    d.setdefault("$schema", "https://www.schemastore.org/claude-code-keybindings.json")
    blocks = d.setdefault("bindings", [])
    chat = next((b for b in blocks if b.get("context") == "Chat"), None)
    if chat is None:
        chat = {"context": "Chat", "bindings": {}}
        blocks.append(chat)
    for key, action in KEYS.items():
        if mode == "--uninstall":
            if chat["bindings"].get(key) == action:
                del chat["bindings"][key]
        elif key not in chat["bindings"] and action not in chat["bindings"].values():
            chat["bindings"][key] = action
    d["bindings"] = [b for b in blocks if b.get("bindings")]
    return d


claude.mkdir(parents=True, exist_ok=True)
edit(claude / "settings.json", settings)
edit(claude / "keybindings.json", keybindings)
PY

if [ "$MODE" = "--uninstall" ]; then
  echo "cproj removed. Your config and data stay in ~/.config/cproj, ~/.cache/cproj, ~/.local/state/cproj."
else
  case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH";; esac
  echo "cproj installed. Restart your agent, then use /project (Claude Code: ctrl+x p), /project-off to leave.
  Codex: \$project · pi / oh-my-pi: /skill:project · Hermes, Cline, OpenClaw, Gemini: /project"
fi
