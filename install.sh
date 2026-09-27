#!/usr/bin/env bash
# cproj installer — links the CLI and the skills, and wires the status line, the SessionEnd hook
# and two keybindings into Claude Code. Idempotent; backs up every file it changes.
#   ./install.sh              install (or update the links)
#   ./install.sh --uninstall  remove links and settings entries again
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CPROJ_BIN_DIR:-$HOME/.local/bin}"
CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MODE="${1:-install}"

command -v python3 >/dev/null || { echo "cproj needs python3" >&2; exit 1; }

if [ "$MODE" = "--uninstall" ]; then
  rm -f "$BIN/cproj"
  rm -f "$CLAUDE/skills/project" "$CLAUDE/skills/project-off"
else
  mkdir -p "$BIN" "$CLAUDE/skills"
  ln -sfn "$REPO/cproj.py" "$BIN/cproj"
  chmod +x "$REPO/cproj.py"
  for s in project project-off; do
    if [ -e "$CLAUDE/skills/$s" ] && [ ! -L "$CLAUDE/skills/$s" ]; then
      mv "$CLAUDE/skills/$s" "$CLAUDE/skills/$s.bak-$(date +%Y%m%d-%H%M%S)"
    fi
    ln -sfn "$REPO/skills/$s" "$CLAUDE/skills/$s"
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
    ours = lambda h: any(x.get("command") == CMD_END for x in h.get("hooks", []))
    if mode == "--uninstall":
        hooks["SessionEnd"] = [h for h in ends if not ours(h)]
        if not hooks["SessionEnd"]:
            del hooks["SessionEnd"]
        if not hooks:
            del d["hooks"]
        if (d.get("statusLine") or {}).get("command") == CMD_STATUS:
            del d["statusLine"]
        return d
    if not any(ours(h) for h in ends):
        ends.append({"hooks": [{"type": "command", "command": CMD_END, "timeout": 10}]})
    if "statusLine" not in d:
        d["statusLine"] = {"type": "command", "command": CMD_STATUS, "padding": 0}
    elif d["statusLine"].get("command") != CMD_STATUS:
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
        elif key not in chat["bindings"]:
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
  echo "cproj installed. Restart Claude Code, then type /project (ctrl+x p) — /project-off (ctrl+x o) to leave."
fi
