#!/usr/bin/env python3
"""cproj — project index and session companion for Claude Code's /project command.

Finds your projects on this machine, on an SSH server and on GitHub, briefs Claude on where a
project stands, shows the active project as a badge in the status line, and remembers handoffs
and sessions per project. Standard library only.

  cproj pick [QUERY | new NAME | status | cold [NAME] | + | -]
                                   what /project runs: choose, badge, brief (one marker line first)
  cproj new NAME [--where=local|server] [--github=private|public|none] [--desc=TEXT] [--force]
  cproj off                        deselect the project of this session
  cproj handoff TEXT [--key=NAME]  save a handoff note (shows up in the next briefing)
  cproj status [--fetch]           every project with loose ends (dirty, unpushed, PRs, local vs server)
  cproj list [--cold|--all|--json] the index
  cproj sessions NAME              Claude Code sessions that worked on a project
  cproj ignore RULE…               hide projects (name or path glob; server:/github:/local: = one source)
  cproj brief NAME | set NAME | clear
  cproj launch                     desktop launcher: picker → new terminal running Claude in the project
  cproj current [--json|--tsv]     the active project (for prompts/status bars of any harness)
  cproj statusline                 status line command (reads Claude Code's JSON on stdin)
  cproj session-end                SessionEnd hook (reads the hook JSON on stdin)
  cproj refresh                    reload the server/GitHub cache now

Configuration: ~/.config/cproj/config.json (see config.example.json). Everything is optional.
Badge slot: Claude Code's session id; elsewhere $CPROJ_SESSION, or one shared slot.
"""
import calendar
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "cproj"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "cproj"
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "cproj"
SESSIONS = CACHE / "sessions"
TRANSCRIPTS = HOME / ".claude/projects"
IGNORE_FILE = CONFIG_DIR / "ignore"
TTL = 30 * 60

DEFAULTS = {
    "projects_dirs": ["~/Projects"],
    "skip_dirs": [],
    "server": None,
    "github": True,
    "cold_days": 120,
    "index_md": None,
    "registry_json": None,
    "template_dir": None,
    "on_create": None,
    "git_identity": None,
    "picker": ["fuzzel", "--dmenu", "--index", "--no-sort", "-p", "  ", "-w", "40", "-l", "10"],
    "terminal": None,
}
SERVER_DEFAULTS = {"host": None, "roots": ["~"], "new_root": "~",
                   "exclude": ["backups", "*_backup*", "backup_*", "bin", "snap", "node_modules", ".*"]}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((Path(os.environ.get("CPROJ_CONFIG", CONFIG_DIR / "config.json"))).read_text()))
    except FileNotFoundError:
        pass
    if cfg.get("server"):
        cfg["server"] = {**SERVER_DEFAULTS, **cfg["server"]}
        if not cfg["server"]["host"]:
            cfg["server"] = None
    return cfg


CFG = load_config()
PROJECT_DIRS = [Path(os.path.expanduser(d)) for d in CFG["projects_dirs"]]
SERVER = CFG["server"]
SSH = ["ssh", "-o", "ConnectTimeout=4", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", SERVER["host"]] if SERVER else []
COLD_DAYS = CFG["cold_days"]


def sh(cmd, timeout=10, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=cwd)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def key_of(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def gh_slug(url):
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url or "")
    return m.group(1).lower() if m else None


def tilde(path):
    return str(path).replace(str(HOME), "~", 1)


# ---------------------------------------------------------------- optional metadata

def read_index_md():
    """Optional markdown table: | **name** | description | status | … | `entry file` |"""
    out = {}
    if not CFG["index_md"]:
        return out
    try:
        for line in Path(os.path.expanduser(CFG["index_md"])).read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].startswith("**"):
                continue
            out[key_of(cells[0].strip("*"))] = {"desc": cells[1], "status": cells[2],
                                                "entry": cells[-1].strip("`") if len(cells) >= 5 else None}
    except OSError:
        pass
    return out


# registry_json may use these field names or their aliases
REG_FIELDS = {"path": ("path", "pfad"), "note": ("note", "notiz"), "status": ("status",),
              "progress": ("progress", "prozent"), "open_items": ("open_items", "offene_punkte"),
              "docs": ("docs", "docs_pfad")}


def read_registry():
    """Optional JSON registry: a list (or {"projects": [...]}) of {name, path, status, note, open_items, …}."""
    if not CFG["registry_json"]:
        return {}
    try:
        data = json.loads(Path(os.path.expanduser(CFG["registry_json"])).read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    out = {}
    for p in data:
        if isinstance(p, dict) and p.get("name"):
            out[key_of(p["name"])] = {f: next((p[a] for a in aliases if a in p), None) for f, aliases in REG_FIELDS.items()}
    return out


def git_origin(path):
    try:
        cfg = (Path(path) / ".git/config").read_text()
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', cfg)
    return m.group(1) if m else None


# ---------------------------------------------------------------- sources

def local_projects():
    idx, reg = read_index_md(), read_registry()
    skip = set(CFG["skip_dirs"])
    dirs = {p.resolve() for base in PROJECT_DIRS if base.is_dir() for p in base.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in skip}
    dirs |= {Path(r["path"]).resolve() for r in reg.values() if r.get("path") and Path(r["path"]).is_dir()}

    def one(d):
        k = key_of(d.name)
        ts = sh(["git", "-C", str(d), "log", "-1", "--format=%ct"], 3).strip()
        r, i = reg.get(k, {}), idx.get(k, {})
        return {"key": k, "name": d.name, "local": str(d),
                "desc": i.get("desc") or r.get("note") or "", "status": i.get("status") or r.get("status") or "",
                "entry": i.get("entry"), "updated": int(ts) if ts.isdigit() else int(d.stat().st_mtime),
                "github": gh_slug(git_origin(d))}
    with ThreadPoolExecutor(16) as ex:
        return list(ex.map(one, sorted(dirs)))


def server_scan_script():
    roots = " ".join(f"{r.rstrip('/')}/*/" for r in SERVER["roots"])
    skip = "|".join(f"*/{p}" for p in SERVER["exclude"]) or "''"
    return (f'for d in {roots}; do d=${{d%/}}; case "$d" in {skip}) continue;; esac\n'
            'if [ -d "$d/.git" ] || [ -f "$d/CLAUDE.md" ] || [ -f "$d/README.md" ] || [ -f "$d/package.json" ] '
            '|| [ -f "$d/pyproject.toml" ]; then\n'
            ' ts=$(git -C "$d" log -1 --format=%ct 2>/dev/null || stat -c %Y "$d"); '
            'url=$(git -C "$d" config --get remote.origin.url 2>/dev/null)\n'
            " desc=$(grep -m1 -E '^\\s*(\\*\\*)?[[:alpha:]]' \"$d/README.md\" 2>/dev/null "
            "| grep -vE '^\\s*(cd|npm|pip|git|sudo) ' | cut -c1-140)\n"
            ' printf \'%s\\t%s\\t%s\\t%s\\n\' "${ts:-0}" "$d" "$url" "$desc"; fi; done')


def server_projects(timeout=12):
    if not SERVER:
        return []
    out = sh(SSH + [server_scan_script()], timeout=timeout)
    res = []
    for line in out.splitlines():
        ts, path, url, desc = (line.split("\t") + ["", "", "", ""])[:4]
        name = path.rstrip("/").rsplit("/", 1)[-1]
        res.append({"key": key_of(name), "name": name, "server": path, "desc": desc,
                    "updated": int(ts) if ts.isdigit() else 0, "github": gh_slug(url)})
    return res if out else None


def github_projects(timeout=12):
    if not CFG["github"]:
        return []
    out = sh(["gh", "api", "user/repos?per_page=100&affiliation=owner,collaborator,organization_member&sort=pushed",
              "--paginate", "--jq", ".[] | [.full_name, .pushed_at, .description // \"\", .archived] | @tsv"], timeout)
    res = []
    for line in out.splitlines():
        full, pushed, desc, archived = (line.split("\t") + ["", "", "", ""])[:4]
        if archived == "true":
            continue
        try:
            ts = calendar.timegm(time.strptime(pushed, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            ts = 0
        res.append({"key": key_of(full.split("/")[1]), "name": full.split("/")[1], "github": full.lower(),
                    "github_name": full, "desc": desc, "updated": ts})
    return res if out else None


REMOTE = {"server": server_projects, "github": github_projects}


def cached(src):
    """Remote sources are cached; a stale cache is served at once and refreshed in the background."""
    f = CACHE / f"{src}.json"
    try:
        c = json.loads(f.read_text())
    except (OSError, ValueError):
        c = None
    if c and time.time() - c["ts"] < TTL:
        return c["items"]
    if c:
        refresh_bg()
        return c["items"]
    items = REMOTE[src]()
    if items is None:  # offline: keep what we had
        return []
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"ts": time.time(), "items": items}))
    return items


def refresh_bg():
    lock = CACHE / "refresh.lock"
    try:
        if time.time() - lock.stat().st_mtime < 120:
            return
    except OSError:
        pass
    CACHE.mkdir(parents=True, exist_ok=True)
    lock.touch()
    subprocess.Popen([sys.executable, __file__, "refresh"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def refresh():
    for src, fn in REMOTE.items():
        items = fn(timeout=90)
        if items is not None:
            CACHE.mkdir(parents=True, exist_ok=True)
            (CACHE / f"{src}.json").write_text(json.dumps({"ts": time.time(), "items": items}))
    (CACHE / "refresh.lock").unlink(missing_ok=True)


# ---------------------------------------------------------------- index

def read_ignore():
    try:
        return [l.strip() for l in IGNORE_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def ignored(e, rules):
    """A rule matches the name or a path; `server:` / `github:` / `local:` limit it to one source."""
    sources = {"server": ["server"], "github": ["github"], "local": ["local"]}
    for r in rules:
        src, _, pat = r.partition(":") if r.split(":", 1)[0] in sources else ("", "", r)
        for field in sources.get(src) or ["local", "server", "github"]:
            val = e.get(field)
            if val and (fnmatch.fnmatch(e["name"].lower(), pat.lower()) or fnmatch.fnmatch(val.lower(), pat.lower())):
                e.pop(field)
    return not any(e.get(f) for f in ("local", "server", "github"))


def add_ignore(rules):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    have = read_ignore()
    new = [r for r in rules if r not in have]
    if not IGNORE_FILE.exists():
        IGNORE_FILE.write_text("# cproj: hidden projects — name or path glob; server:/github:/local: = one source only\n")
    with open(IGNORE_FILE, "a") as f:
        f.writelines(r + "\n" for r in new)
    print(f"CPROJ: HIDDEN ({tilde(IGNORE_FILE)}): {', '.join(new) or 'nothing new'}")


def read_recent():
    try:
        return json.loads((CACHE / "recent.json").read_text())
    except (OSError, ValueError):
        return {}


def mark_recent(key):
    r = read_recent()
    r[key] = time.time()
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "recent.json").write_text(json.dumps(r))


def is_cold(e, recent):
    """Cold = neither code nor a visit for COLD_DAYS. Picking it via `/project cold` warms it up again."""
    return time.time() - max(e["updated"], recent.get(e["key"], 0)) > COLD_DAYS * 86400


def load_all():
    with ThreadPoolExecutor(3) as ex:
        fl, fs, fg = ex.submit(local_projects), ex.submit(cached, "server"), ex.submit(cached, "github")
        local, server, github = fl.result(), fs.result(), fg.result()
    by_key, by_gh = {}, {}
    for item in local + server + github:
        e = (by_gh.get(item.get("github")) if item.get("github") else None) or by_key.get(item["key"])
        if e is None:
            e = by_key[item["key"]] = {"key": item["key"], "name": item["name"], "desc": "", "status": "", "updated": 0}
        for f in ("local", "server", "github", "github_name", "entry", "desc", "status"):
            if item.get(f) and not e.get(f):
                e[f] = item[f]
        e["updated"] = max(e["updated"], item.get("updated") or 0)
        if e.get("github"):
            by_gh[e["github"]] = e
    recent, rules = read_recent(), read_ignore()
    items = [e for e in by_key.values() if not (rules and ignored(e, rules))]
    items.sort(key=lambda e: (-recent.get(e["key"], 0), -e["updated"]))
    return items


def load_index(cold=False):
    """cold=False: active projects only; True: all, with a `cold` flag; "only": cold storage."""
    items, recent = load_all(), read_recent()
    for e in items:
        e["cold"] = is_cold(e, recent)
    if cold == "only":
        return [e for e in items if e["cold"]]
    return items if cold else [e for e in items if not e["cold"]]


# ---------------------------------------------------------------- matching and formatting

def where(e):
    return "".join(c if e.get(f) else "·" for c, f in (("L", "local"), ("S", "server"), ("G", "github")))


def ago(ts):
    if not ts:
        return "?"
    h = (time.time() - ts) / 3600
    return "now" if h < 1 else f"{int(h)}h" if h < 24 else f"{int(h / 24)}d" if h < 1440 else f"{int(h / 720)}mo"


def since(ts):
    a = ago(ts)
    return "just now" if a == "now" else f"{a} ago"


HEADER = f"{'PROJECT':<28} L S G   SEEN  STATUS      DESCRIPTION"


def row(e, width=70):
    return (f"{e['name']:<28} {where(e)}  {ago(e['updated']):>5}  {(e.get('status') or '')[:11]:<11} "
            f"{(e.get('desc') or '')[:width]}")


def score(e, q):
    n, k = e["name"].lower(), e["key"]
    ql, qk = q.lower(), key_of(q)
    if ql in (n, k) or qk == k:
        return 100
    if k.startswith(qk) or n.startswith(ql):
        return 80
    if qk in k:
        return 60
    if all(t in (n + " " + (e.get("desc") or "")).lower() for t in ql.split()):
        return 30
    it = iter(k)
    return 20 if all(c in it for c in qk.replace("-", "")) else 0


def resolve(items, query):
    ranked = [(s, e) for s, e in sorted(((score(e, query), e) for e in items), key=lambda t: -t[0]) if s > 0]
    if not ranked:
        return None, []
    best = [e for s, e in ranked if s == ranked[0][0]]
    if ranked[0][0] >= 60 and len(best) == 1:
        return best[0], []
    return None, [e for _, e in ranked[:8]]


def gui_pick(lines, prompt=None):
    """Index chosen in the configured dmenu-style picker; None if cancelled or unavailable."""
    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY") or not shutil.which(CFG["picker"][0]):
        return None
    cmd = list(CFG["picker"]) + (["-p", prompt] if prompt else [])
    try:
        out = subprocess.run(cmd, input="\n".join(lines), capture_output=True, text=True, timeout=300).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out) if out.isdigit() and int(out) < len(lines) else None


# ---------------------------------------------------------------- badge and events

def session_id():
    """Which badge slot this call belongs to: $CPROJ_SESSION, else the harness's own session id (Codex, Gemini CLI,
    Claude Code), else one slot per harness ("hermes") or a single "shared" one."""
    env = os.environ.get
    harness = next((f"{name}-{env(var)}" for name, var in (("codex", "CODEX_THREAD_ID"), ("gemini", "GEMINI_SESSION_ID"))
                    if env(var)), None)
    if not harness and (env("HERMES_INTERACTIVE") or env("_HERMES_GATEWAY")):
        harness = "hermes"  # Hermes shells carry no session id; don't borrow an inherited Claude one
    return env("CPROJ_SESSION") or harness or env("CLAUDE_CODE_SESSION_ID") or env("CLAUDE_SESSION_ID") or "shared"


def current(fmt="plain"):
    """The active project of this slot — for prompts and status bars of other tools."""
    b = current_badge()
    if fmt == "json":
        return print(json.dumps(b or {}, ensure_ascii=False))
    if b:
        print(f"◆ {b['name']}" if fmt == "plain" else f"{b['name']}\t{b.get('local') or b.get('server') or b.get('github')}")


def log_event(event, key, name, sid=None, **extra):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "events.jsonl", "a") as f:
        f.write(json.dumps({"ts": int(time.time()), "sid": sid or session_id(), "event": event,
                            "key": key, "name": name, **extra}, ensure_ascii=False) + "\n")


def read_events(key=None):
    try:
        lines = (STATE / "events.jsonl").read_text().splitlines()
    except OSError:
        return []
    evs = [json.loads(l) for l in lines if l.strip()]
    return [e for e in evs if key is None or e["key"] == key]


def current_badge(sid=None):
    try:
        return json.loads((SESSIONS / f"{sid or session_id()}.json").read_text())
    except (OSError, ValueError, TypeError):
        return None


def set_badge(e, sid=None):
    sid = sid or session_id()
    if not sid:
        return False
    head = sh(["git", "-C", e["local"], "rev-parse", "HEAD"], 3).strip() if e.get("local") else ""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{sid}.json").write_text(json.dumps({
        "key": e["key"], "name": e["name"], "where": where(e), "local": e.get("local"), "server": e.get("server"),
        "github": e.get("github_name") or e.get("github"), "since": int(time.time()), "head": head}))
    mark_recent(e["key"])
    log_event("pick", e["key"], e["name"], sid)
    return True


def clear_badge(sid=None):
    sid = sid or session_id()
    if sid:
        (SESSIONS / f"{sid}.json").unlink(missing_ok=True)


def work_summary(b):
    """What the session changed in the project — mechanical, no model involved."""
    if not b or not b.get("local"):
        return ""
    d = b["local"]
    dirty = len(sh(["git", "-C", d, "status", "--porcelain"], 5).splitlines())
    commits = sh(["git", "-C", d, "log", "--oneline", f"{b['head']}..HEAD"], 5).splitlines() if b.get("head") else []
    return f"{len(commits)} new commits, {dirty} uncommitted files"


def left_project(b, verb):
    t = time.strftime("%H:%M", time.localtime(b.get("since", 0)))
    return (f"CPROJ: {verb} {b['name']} (active since {t}; {work_summary(b) or 'no local checkout'})"
            + "".join(f"\n{k}: {b[k]}" for k in ("local", "server", "github") if b.get(k)))


def switch_to(sel):
    prev = current_badge()
    if prev and prev["key"] != sel["key"]:
        print(left_project(prev, "LEFT"))
        log_event("off", prev["key"], prev["name"])
    return set_badge(sel)


def off():
    b = current_badge()
    if not b:
        print("CPROJ: NO PROJECT active — nothing to deselect.")
        return
    print(left_project(b, "DESELECTED"))
    clear_badge()
    log_event("off", b["key"], b["name"])


def handoff(text, key=None):
    b = current_badge()
    if key is None and not b:
        print("CPROJ: NO PROJECT active — a handoff needs --key=<project>.")
        return
    key = key or b["key"]
    log_event("handoff", key, b["name"] if b and b["key"] == key else key, text=text.strip())
    print(f"CPROJ: HANDOFF saved for {key}.")


def session_end():
    """SessionEnd hook: close an open project with a mechanical handoff."""
    try:
        sid = json.load(sys.stdin).get("session_id")
    except ValueError:
        return
    b = current_badge(sid)
    if b:
        log_event("end", b["key"], b["name"], sid,
                  text=f"Session ended without a handoff ({work_summary(b) or 'state unknown'}).")
        clear_badge(sid)


def statusline():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    b = current_badge(data.get("session_id") or "-")
    if b:
        loc = "local" if b.get("local") else "server" if b.get("server") else "GitHub"
        parts = [f"\033[1;38;5;16;48;5;150m ◆ {b['name']} \033[0;38;5;150m {loc}\033[0m"]
    else:  # nothing picked: the working directory may still tell
        base = next((d for d in PROJECT_DIRS if cwd.startswith(f"{d}/")), None)
        name = cwd[len(str(base)) + 1:].split("/")[0] if base else ""
        parts = [f"\033[38;5;150m◇ {name}\033[0m" if name else "\033[2m◇ no project\033[0m"]
    model = (data.get("model") or {}).get("display_name")
    parts += [f"\033[2m{x}\033[0m" for x in (model, tilde(cwd) if cwd else None) if x]
    print("  ".join(parts))


# ---------------------------------------------------------------- sessions

def transcript_index():
    """Claude Code transcripts → {session: title, start dir, projects touched}; cached by size and mtime."""
    cf = CACHE / "transcripts.json"
    try:
        old = json.loads(cf.read_text())
    except (OSError, ValueError):
        old = {}
    new, changed = {}, False
    cwd_re = re.compile(r'"cwd":\s*"(?:%s)/([^/"]+)' % "|".join(re.escape(str(d)) for d in PROJECT_DIRS))
    for f in TRANSCRIPTS.glob("*/*.jsonl"):
        st, sid = f.stat(), f.stem
        o = old.get(sid)
        if o and o["mtime"] == st.st_mtime and o["size"] == st.st_size:
            new[sid] = o
            continue
        txt = f.read_text(errors="replace")
        titles = re.findall(r'"(?:customTitle|aiTitle)":\s*"((?:[^"\\]|\\.)*)"', txt)
        start = re.search(r'"cwd":\s*"([^"]+)"', txt)
        new[sid] = {"mtime": st.st_mtime, "size": st.st_size, "keys": sorted({key_of(m) for m in cwd_re.findall(txt)}),
                    "title": json.loads(f'"{titles[-1]}"') if titles else "",
                    "start": start.group(1) if start else str(HOME)}
        changed = True
    if changed or len(new) != len(old):
        CACHE.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(new))
    return new


def project_sessions(key, limit=3):
    idx = transcript_index()
    sids = {sid for sid, t in idx.items() if key in t["keys"]} | {e["sid"] for e in read_events(key) if e.get("sid")}
    return sorted(((idx[s]["mtime"], s, idx[s]) for s in sids if s in idx and s != session_id()), reverse=True)[:limit]


# ---------------------------------------------------------------- briefing

STATUS_DOCS = ["HANDOFF.md", "docs/PROGRESS.md", "PROGRESS.md", "docs/STATUS.md", "STATUS.md", "TODO.md"]


def clip(text, n):
    lines = text.rstrip().splitlines()
    return "\n".join(lines[:n]) + (f"\n… (+{len(lines) - n} lines)" if len(lines) > n else "")


def inline_imports(p):
    """Resolve CLAUDE.md import lines (`@AGENTS.md`), one level deep."""
    def sub(m):
        try:
            return f"[@{m.group(1)}:]\n" + (p.parent / m.group(1)).read_text(errors="replace")
        except OSError:
            return m.group(0)
    return re.sub(r"(?m)^@(\S+)\s*$", sub, p.read_text(errors="replace"))


def brief_local(e):
    d = Path(e["local"])
    out = [f"## Local: {tilde(d)}"]
    if (d / ".git").exists():
        st = sh(["git", "-C", str(d), "status", "-sb"], 5).splitlines()
        out.append(f"Git: {st[0][3:] if st else '?'} · {max(len(st) - 1, 0)} changed/new files")
        if len(st) > 1:
            out.append(clip("\n".join(st[1:]), 12))
        out.append("Recent commits:\n" + sh(["git", "-C", str(d), "log", "-8", "--format=%cd %s",
                                             "--date=format:%m-%d %H:%M"], 5).rstrip())
    reg = read_registry().get(e["key"], {})
    if any(reg.values()):
        out.append(f"Registry: {reg.get('progress') if reg.get('progress') is not None else '?'} % · "
                   f"{reg.get('status') or ''} · {reg.get('note') or ''}")
        out += [f"  - {p}" for p in (reg.get("open_items") or [])[:6]]
    docs = []
    if e.get("entry") and CFG["index_md"]:
        docs.append((Path(os.path.expanduser(CFG["index_md"])).parent / e["entry"], 60))
    docs += [(d / "CLAUDE.md", 60)] + [(d / n, 50) for n in STATUS_DOCS]
    if reg.get("docs") and str(reg["docs"]).endswith(".md"):
        docs.append((Path(reg["docs"]), 50))
    docs.append((d / "README.md", 45))
    seen, shown = set(), 0
    for p, n in docs:
        if shown >= 3 or not p.is_file() or not str(p.resolve()).startswith(str(d)) or p.resolve() in seen:
            continue
        seen.add(p.resolve())
        shown += 1
        txt = inline_imports(p)
        body = clip(txt, n)
        if len(txt.splitlines()) > n * 2:  # long status doc: head + tail (the tail is usually newest)
            body = clip(txt, n // 2) + "\n[…]\n" + "\n".join(txt.rstrip().splitlines()[-(n // 2):])
        out.append(f"--- {p.relative_to(d)} ---\n{body}")
    if not shown:
        out.append("Files: " + " ".join(sorted(x.name for x in d.iterdir())[:40]))
    mem = TRANSCRIPTS / re.sub(r"[^A-Za-z0-9]", "-", str(d)) / "memory/MEMORY.md"
    if mem.is_file():
        out.append(f"--- Claude memory of this project ({tilde(mem)}) ---\n{clip(mem.read_text(), 25)}")
    return "\n".join(out)


def brief_server(e):
    p = e["server"]
    if e.get("local"):  # docs come from the local checkout — from the server only whether it differs
        cmd = f'cd "{p}" || exit; git status -sb | head -6; git log -5 --format="%cd %s" --date=format:"%m-%d %H:%M"'
    else:
        cmd = (f'cd "{p}" || exit; echo "Git: $(git status -sb 2>/dev/null | head -1)"; git log -6 --format="%cd %s" '
               f'--date=format:"%m-%d %H:%M" 2>/dev/null; for f in CLAUDE.md {" ".join(STATUS_DOCS)} README.md; do '
               f'[ -f "$f" ] && {{ echo "--- $f ---"; head -45 "$f"; }}; done | head -110; '
               f'echo "Files: $(ls | head -40 | tr "\\n" " ")"')
    return f"## Server: {SERVER['host']}:{p}\n" + (sh(SSH + [cmd], 15).rstrip() or "(server unreachable)")


def brief_github(e):
    slug = e.get("github_name") or e["github"]
    with ThreadPoolExecutor(3) as ex:
        v = ex.submit(sh, ["gh", "repo", "view", slug, "--json", "description,pushedAt,defaultBranchRef,url", "--jq",
                           '"\\(.url) · branch \\(.defaultBranchRef.name) · pushed \\(.pushedAt) · \\(.description // "")"'], 10)
        i = ex.submit(sh, ["gh", "issue", "list", "-R", slug, "--limit", "6", "--json", "number,title",
                           "--jq", '.[] | "  #\\(.number) \\(.title)"'], 10)
        pr = ex.submit(sh, ["gh", "pr", "list", "-R", slug, "--limit", "6", "--json", "number,title,headRefName",
                            "--jq", '.[] | "  PR #\\(.number) \\(.title) [\\(.headRefName)]"'], 10)
        out = [f"## GitHub: {slug}", v.result().strip(),
               ("Open issues:\n" + i.result().rstrip()) if i.result().strip() else "Open issues: none",
               ("Open PRs:\n" + pr.result().rstrip()) if pr.result().strip() else "Open PRs: none"]
    if not e.get("local") and not e.get("server"):
        rd = sh(["gh", "api", f"repos/{slug}/readme", "-H", "Accept: application/vnd.github.raw"], 10)
        if rd:
            out.append(f"--- README.md ---\n{clip(rd, 50)}")
    return "\n".join(out)


def history_block(e):
    out = []
    notes = [x for x in read_events(e["key"]) if x["event"] in ("handoff", "end")][-3:]
    if notes:
        out.append("## Recent handoffs")
        out += [f"- {since(x['ts'])} ({'session end' if x['event'] == 'end' else 'handoff'}): {x['text']}"
                for x in reversed(notes)]
    sess = project_sessions(e["key"])
    if sess:
        out.append("## Recent Claude sessions (resume in a new terminal)")
        out += [f"- {since(m)} · “{t['title'] or 'untitled'}” · `cd {tilde(t['start'])} && claude -r {sid}`"
                for m, sid, t in sess]
    return ("\n\n" + "\n".join(out)) if out else ""


def brief(e):
    head = (f"# Project: {e['name']}   [{where(e)} = local/server/GitHub]   last active: {since(e['updated'])}"
            + (f"\n{e['desc']}" if e.get("desc") else "") + (f"\nStatus: {e['status']}" if e.get("status") else ""))
    fns = [f for f, k in ((brief_local, "local"), (brief_server, "server"), (brief_github, "github")) if e.get(k)]
    with ThreadPoolExecutor(3) as ex:
        parts = list(ex.map(lambda f: f(e), fns))
    return head + history_block(e) + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------- creating projects

SKELETON = {
    "CLAUDE.md": "# {name}\n\n{desc}\n\n## Start here\n\n"
                 "1. `docs/PROGRESS.md` — where things stand, what happened last, what comes next.\n\n"
                 "## Rules\n\n- Whoever changes code adds an entry at the top of the log in `docs/PROGRESS.md`.\n"
                 "- Decisions with alternatives go into `docs/decisions/NNNN-title.md`.\n- No secrets in the repo.\n",
    "README.md": "# {name}\n\n{desc}\n",
    "docs/PROGRESS.md": "# Progress\n\nAdd an entry **at the top of the log** whenever something changes.\n\n"
                        "## Status\n\n{desc}\n\n## Log\n\n### {date} · created\n- Done: project created.\n"
                        "- Next: define the goal and the first milestone.\n",
    ".gitignore": ".env\n.env.*\n!.env.example\nnode_modules/\n__pycache__/\n.venv/\ndist/\n",
}


def skeleton(name, desc):
    fill = {"name": name, "desc": desc, "date": time.strftime("%Y-%m-%d")}
    tdir = Path(os.path.expanduser(CFG["template_dir"])) if CFG["template_dir"] else None
    if tdir and tdir.is_dir():
        files = {str(p.relative_to(tdir)): p.read_text() for p in tdir.rglob("*") if p.is_file()}
    else:
        files = SKELETON
    return {f: c.replace("{name}", name).replace("{desc}", desc).replace("{date}", fill["date"]) for f, c in files.items()}


def git_cmds():
    ident = CFG["git_identity"] or {}
    cfg = [["config", "user.name", ident["name"]], ["config", "user.email", ident["email"]]] if ident else []
    return [["init", "-q", "-b", "main"], *cfg, ["add", "-A"], ["commit", "-q", "-m", "Initial project skeleton"]]


def new_project(name, where_="local", github="none", desc="", force=False):
    items = load_index(cold=True)  # duplicates count in cold storage too
    k = key_of(name)
    similar = [e for e in items if score(e, name) >= 60 or (len(k) > 3 and k in e["key"])]
    if similar and not force:
        print(f"CPROJ: SIMILAR EXISTS — nothing created. Matches for “{name}”:")
        print("\n".join(row(e) for e in similar[:6]))
        return
    desc = desc or "Description to follow."
    files = skeleton(name, desc)
    entry = {"key": k, "name": name, "desc": desc, "updated": int(time.time())}
    if where_ == "server":
        if not SERVER:
            print("CPROJ: ERROR — no server configured (see config.example.json).")
            return
        path = f"{SERVER['new_root'].rstrip('/')}/{name}"
        script = f'set -e; [ ! -e {path} ] || {{ echo EXISTS; exit 1; }}; mkdir -p {path}/docs; cd {path}\n'
        for f, c in files.items():
            script += f"mkdir -p \"$(dirname '{f}')\"; cat > '{f}' <<'CPROJ_EOF'\n{c}CPROJ_EOF\n"
        script += "".join("git " + " ".join(f"'{a}'" for a in c) + "\n" for c in git_cmds()) + "pwd\n"
        r = subprocess.run(SSH + ["bash -s"], input=script, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"CPROJ: ERROR creating on the server: {(r.stdout + r.stderr).strip()[:300]}")
            return
        entry["server"] = r.stdout.strip().splitlines()[-1]
        (CACHE / "server.json").unlink(missing_ok=True)
    else:
        d = PROJECT_DIRS[0] / name
        if d.exists():
            print(f"CPROJ: ERROR — {tilde(d)} already exists.")
            return
        for f, c in files.items():
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text(c)
        for c in git_cmds():
            subprocess.run(["git", *c], cwd=d, capture_output=True)
        entry["local"] = str(d)
        if github in ("private", "public"):
            r = subprocess.run(["gh", "repo", "create", name, f"--{github}", "--source", str(d), "--push",
                                "--description", desc[:300]], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                entry["github"] = entry["github_name"] = gh_slug(git_origin(d))
            else:
                print(f"CPROJ: WARNING — GitHub repo not created: {r.stderr.strip()[:200]}")
            (CACHE / "github.json").unlink(missing_ok=True)
        if CFG["on_create"]:
            sh([a.replace("{name}", name).replace("{path}", str(d)).replace("{desc}", desc) for a in CFG["on_create"]], 15)
    set_badge(entry)
    print(f"CPROJ: CREATED {name}")
    print(brief(entry))


# ---------------------------------------------------------------- status overview

def local_git_state(e):
    d = e["local"]
    if not (Path(d) / ".git").exists():
        return None
    st = sh(["git", "-C", d, "status", "-sb", "--porcelain"], 5).splitlines()
    head = st[0] if st else ""
    branch = re.sub(r"^## (No commits yet on )?", "", head).split("...")[0]
    default = sh(["git", "-C", d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], 3).strip()
    off_main = 0
    if default and branch not in ("main", "master"):
        off_main = len(sh(["git", "-C", d, "log", "--oneline", f"{default}..HEAD"], 5).splitlines())
    num = lambda w: int(m.group(1)) if (m := re.search(rf"{w} (\d+)", head)) else 0
    return {"branch": branch, "dirty": max(len(st) - 1, 0), "ahead": num("ahead"), "behind": num("behind"),
            "off_main": off_main, "default": default, "upstream": "..." in head,
            "head": sh(["git", "-C", d, "rev-parse", "HEAD"], 3).strip()}


def server_git_states(paths):
    if not paths or not SERVER:
        return {}
    script = ("for d in " + " ".join(f'"{p}"' for p in paths) + '; do [ -d "$d/.git" ] || continue; '
              'printf "%s\\t%s\\t%s\\n" "$d" "$(git -C "$d" rev-parse HEAD 2>/dev/null)" '
              '"$(git -C "$d" status --porcelain 2>/dev/null | wc -l)"; done')
    out = {}
    for line in sh(SSH + [script], 20).splitlines():
        p, head, dirty = (line.split("\t") + ["", "", "0"])[:3]
        out[p] = {"head": head, "dirty": int(dirty or 0)}
    return out


def github_login():
    f = CACHE / "gh-login"
    try:
        return f.read_text().strip()
    except OSError:
        login = sh(["gh", "api", "user", "--jq", ".login"], 10).strip()
        if login:
            CACHE.mkdir(parents=True, exist_ok=True)
            f.write_text(login)
        return login


def open_prs():
    if not CFG["github"]:
        return [], ""
    me = github_login()
    q = ".[] | [.repository.nameWithOwner, (.number|tostring), .author.login, .title] | @tsv"
    searches = [["--review-requested", "@me"], ["--author", "@me"]] + ([["--owner", me]] if me else [])
    with ThreadPoolExecutor(3) as ex:
        outs = list(ex.map(lambda extra: sh(["gh", "search", "prs", "--state", "open", "--limit", "50", *extra,
                                             "--json", "repository,number,author,title", "--jq", q], 15), searches))
    res = {}
    for line in "".join(outs).splitlines():
        repo, num, author, title = (line.split("\t") + ["", "", "", ""])[:4]
        res[(repo.lower(), num)] = (repo, num, author, title)
    return list(res.values()), me


def status(fetch=False):
    items = load_index()
    local = [e for e in items if e.get("local")]
    if fetch:
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(lambda e: sh(["git", "-C", e["local"], "fetch", "-q", "--prune"], 20), local))
    with ThreadPoolExecutor(16) as ex:
        fs = ex.submit(server_git_states, [e["server"] for e in items if e.get("server") and e.get("local")])
        fp = ex.submit(open_prs)
        states = dict(zip([e["key"] for e in local], ex.map(local_git_state, local)))
        server, (prs, me) = fs.result(), fp.result()
    rows = []
    for e in items:
        notes, s = [], states.get(e["key"])
        if s:
            notes += [f"{s['dirty']} uncommitted"] if s["dirty"] else []
            notes += [f"{s['ahead']} unpushed"] if s["ahead"] else []
            notes += [f"{s['behind']} behind origin"] if s["behind"] else []
            if s["off_main"]:
                notes.append(f"branch {s['branch']}: {s['off_main']} commits not in {s['default']}")
            if not s["branch"]:
                notes.append("no valid branch (broken or no commits)")
            elif not s["upstream"] and e.get("github"):
                notes.append(f"branch {s['branch']} has no upstream")
            sv = server.get(e.get("server") or "")
            if sv and sv["head"] and sv["head"] != s["head"]:
                known = subprocess.run(["git", "-C", e["local"], "cat-file", "-e", sv["head"] + "^{commit}"],
                                       capture_output=True).returncode == 0
                notes.append("server behind local" if known else "server has commits local doesn't know")
            if sv and sv["dirty"]:
                notes.append(f"server: {sv['dirty']} uncommitted")
        mine = [p for p in prs if e.get("github") and p[0].lower() == e["github"]]
        if mine:
            others = [p for p in mine if p[2].lower() != me.lower()]
            notes.append(f"{len(mine)} open PRs" + (f" ({len(others)} by others → review)" if others else ""))
        if notes:
            rows.append((e, notes))
    print(f"CPROJ: STATUS — {len(rows)} projects with loose ends"
          + ("" if fetch else " (no fetch; `cproj status --fetch` updates remotes first)") + ":")
    for e, notes in rows:
        print(f"{e['name']:<26} {where(e)} {ago(e['updated']):>4}  " + " · ".join(notes))
    foreign = [p for p in prs if not any(e.get("github") == p[0].lower() for e in items)]
    if foreign:
        print("\nOther open PRs you are involved in:")
        print("\n".join(f"  {r}#{n} by {a}: {t}" for r, n, a, t in foreign))


# ---------------------------------------------------------------- desktop launcher

TERMINALS = [("ghostty", ["--working-directory={dir}", "--title={title}", "-e"]),
             ("kitty", ["--directory", "{dir}", "--title", "{title}"]),
             ("alacritty", ["--working-directory", "{dir}", "--title", "{title}", "-e"]),
             ("foot", ["-D", "{dir}", "-T", "{title}"]),
             ("wezterm", ["start", "--cwd", "{dir}", "--"])]


def terminal_cmd(d, title, cmd):
    if CFG["terminal"]:
        tmpl = CFG["terminal"]
    else:
        name, args = next(((n, a) for n, a in TERMINALS if shutil.which(n)), (None, None))
        if not name:
            return None
        tmpl = [name, *args]
    return [a.replace("{dir}", d).replace("{title}", title) for a in tmpl] + cmd


def launch():
    """Pick a project in a GUI picker → new terminal with Claude (new session or resume a recent one)."""
    items = load_index()
    i = gui_pick([f"{e['name']:<26} {where(e)} {ago(e['updated']):>5}" for e in items])
    if i is None:
        return
    sel = items[i]
    d, cmd = sel.get("local") or str(HOME), ["claude", f"/project {sel['name']}"]
    sess = [s for s in project_sessions(sel["key"], 3) if time.time() - s[0] < 7 * 86400]
    if sess:
        j = gui_pick(["New session"] + [f"Resume: {t['title'][:38] or sid[:8]} · {since(m)}" for m, sid, t in sess],
                     f"{sel['name']} ❯ ")
        if j is None:
            return
        if j > 0:
            m, sid, t = sess[j - 1]
            d, cmd = t["start"], ["claude", "-r", sid]
            set_badge(sel, sid)  # the resumed session gets its badge back right away
    full = terminal_cmd(d, f"◆ {sel['name']}", cmd)
    if full:
        subprocess.Popen(full, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- CLI

def pick(query, items):
    word = query.split(" ", 1)[0].lower()
    rest = query.split(" ", 1)[1].strip() if " " in query else ""
    if query in ("-", "off", "aus"):
        return off()
    if word in ("status", "overview"):
        return status("fetch" in query.split()[1:])
    if word in ("cold", "kalt"):
        return pick_cold(rest)
    if word in ("new", "neu"):  # only checks; creating happens after asking, via `cproj new`
        print(f"CPROJ: NEW {rest or '(name missing)'}")
        similar = [e for e in load_index(cold=True) if rest and (score(e, rest) >= 30 or (len(rest) > 3 and key_of(rest) in e["key"]))]
        print(("Similar existing projects:\n" + "\n".join(row(e) for e in similar[:6])) if similar
              else "No similar projects locally, on the server or on GitHub.")
        return
    if query == "+":
        i = gui_pick([f"{e['name']:<26} {where(e)} {ago(e['updated']):>5}" for e in items])
        if i is None:
            print("CPROJ: CANCELLED.")
            return
        sel, cands = items[i], []
    elif query:
        sel, cands = resolve(items, query)
    else:
        recent = read_recent()
        active = [e for e in items if e["key"] in recent or time.time() - e["updated"] < 30 * 86400]
        print(f"CPROJ: CHOOSE — {len(items)} projects known; recently used/active:\n{HEADER}")
        print("\n".join(row(e) for e in active[:10]))
        return
    if sel is None:
        frozen, _ = resolve(load_index(cold="only"), query)
        if not cands and frozen:
            print(f"CPROJ: IN COLD STORAGE — “{frozen['name']}” has been untouched for over {COLD_DAYS} days. "
                  f"Wake it with `/project cold {frozen['name']}`.")
            return
        if cands:
            print(f"CPROJ: AMBIGUOUS — several matches for “{query}”:")
        else:
            print(f"CPROJ: NO MATCH for “{query}” — create it or search differently. Recently active:")
            cands = items[:6]
        print(HEADER + "\n" + "\n".join(row(e) for e in cands))
        return
    ok = switch_to(sel)
    print(f"CPROJ: PICKED {sel['name']}" + ("" if ok else " (no session id → no badge)"))
    print(brief(sel))


def pick_cold(query):
    cold = load_index(cold="only")
    if not query:
        print(f"CPROJ: COLD — {len(cold)} projects untouched for over {COLD_DAYS} days "
              f"(`/project cold <name>` wakes one):\n{HEADER}")
        print("\n".join(row(e) for e in cold))
        return
    sel, cands = resolve(cold, query)
    if sel is None:
        print(f"CPROJ: AMBIGUOUS — several cold projects match “{query}”:" if cands
              else f"CPROJ: NO MATCH in cold storage for “{query}”.")
        print("\n".join(row(e) for e in cands))
        return
    switch_to(sel)  # the visit counts as activity → warm again
    print(f"CPROJ: WOKEN {sel['name']} (from cold storage, quiet for {ago(sel['updated'])})")
    print(f"CPROJ: PICKED {sel['name']}")
    print(brief(sel))


def main(argv):
    cmd, args = (argv[0] if argv else "pick"), argv[1:]
    query = " ".join(a for a in args if not a.startswith("--")).strip()
    opt = lambda f, d=None: next((a.split("=", 1)[1] for a in args if a.startswith(f"--{f}=")), d)
    if cmd == "current":
        return current("json" if "--json" in args else "tsv" if "--tsv" in args else "plain")
    simple = {"statusline": statusline, "refresh": refresh, "session-end": session_end, "launch": launch, "off": off}
    if cmd in simple:
        return simple[cmd]()
    if cmd == "clear":
        clear_badge()
        return print("CPROJ: badge removed.")
    if cmd == "ignore":
        return add_ignore(args)
    if cmd == "handoff":
        return handoff(query, opt("key") and key_of(opt("key")))
    if cmd == "sessions":
        for m, sid, t in project_sessions(key_of(query), 10):
            print(f"{ago(m):>5}  {sid}  {tilde(t['start'])}  {t['title']}")
        return
    if cmd == "status":
        return status("--fetch" in args)
    if cmd == "new":
        return new_project(re.sub(r"[^A-Za-z0-9._-]+", "-", query).strip("-"), opt("where", "local"),
                           opt("github", "none"), opt("desc", ""), "--force" in args)
    if cmd == "list":
        items = load_index(cold=True if "--json" in args or "--all" in args else "only" if "--cold" in args else False)
        return print(json.dumps(items, ensure_ascii=False, indent=1) if "--json" in args
                     else HEADER + "\n" + "\n".join(row(e) for e in items))
    if cmd == "pick":
        return pick(query, load_index())
    if cmd in ("brief", "set"):
        sel, cands = resolve(load_index(cold=True), query)
        if not sel:
            return print(f"CPROJ: NO UNIQUE MATCH for “{query}”.")
        if cmd == "set":
            return print(f"CPROJ: PICKED {sel['name']}" if switch_to(sel) else "CPROJ: no session id → no badge")
        return print(brief(sel))
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
