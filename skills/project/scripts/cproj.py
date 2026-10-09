#!/usr/bin/env python3
"""cproj — project index and session companion for Claude Code's /project command.

Finds your projects on this machine, on an SSH server and on GitHub, briefs Claude on where a
project stands, shows the active project as a badge in the status line, and remembers handoffs
and sessions per project. Standard library only.

  cproj pick [QUERY | new NAME | status | cold [NAME] | + | -]
                                   what /project runs: choose, badge, brief (one marker line first)
  cproj new NAME [--where=local|server] [--github=private|public|none] [--desc=TEXT] [--force]
  cproj sub [TOPIC | -] [--desc=TEXT]
                                   subproject of the active project: list, switch to (or create) one, or leave it
  cproj color NAME [COLOR | -]     badge colour of a project (256-colour number or #rrggbb; - = automatic)
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
  cproj temp NAME [--desc=TEXT]    create (or find) a temporary project, prints {"key","path"} as JSON
  cproj drop NAME                  delete a temporary project (refuses normal ones)
  cproj keep NAME                  turn a temporary project into a normal one (moves it to projects_dirs)

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
from zlib import crc32

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
    "temp_dir": "~/.local/share/cproj/temp",
    "colors": {},
    "big_after": 8,
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
TEMP_DIR = Path(os.path.expanduser(CFG["temp_dir"]))
TEMP_MARK = ".cproj-temp"
SUBS = STATE / "subs.json"
# 256-colour codes for project badges, all light enough for black text and as text on a dark terminal
PALETTE = [150, 117, 215, 183, 210, 114, 222, 147, 174, 80, 186, 140]


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
    if TEMP_DIR.is_dir():
        dirs |= {p.resolve() for p in TEMP_DIR.iterdir() if (p / TEMP_MARK).is_file()}

    def one(d):
        k = key_of(d.name)
        ts = sh(["git", "-C", str(d), "log", "-1", "--format=%ct"], 3).strip()
        r, i = reg.get(k, {}), idx.get(k, {})
        e = {"key": k, "name": d.name, "local": str(d),
             "desc": i.get("desc") or r.get("note") or "", "status": i.get("status") or r.get("status") or "",
             "entry": i.get("entry"), "updated": int(ts) if ts.isdigit() else int(d.stat().st_mtime),
             "github": gh_slug(git_origin(d)), "worktree_of": worktree_of(d)}
        mark = d / TEMP_MARK
        if mark.is_file():
            try:
                e["desc"] = e["desc"] or json.loads(mark.read_text()).get("desc", "")
            except ValueError:
                pass
            e.update(temp=True, status="temp")
        return e
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
            "| grep -vE '^\\s*(cd|npm|pip|git) ' | cut -c1-140)\n"
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
        for f in ("local", "server", "github", "github_name", "entry", "desc", "status", "temp", "worktree_of"):
            if item.get(f) and not e.get(f):
                e[f] = item[f]
        e["updated"] = max(e["updated"], item.get("updated") or 0)
        if e.get("github"):
            by_gh[e["github"]] = e
    recent, rules = read_recent(), read_ignore()
    items = [e for e in attach_subs(list(by_key.values())) if not (rules and ignored(e, rules))]
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


# ---------------------------------------------------------------- subprojects
# A subproject is one strand of work inside a bigger project: a git worktree of it (found automatically,
# e.g. ~/Projects/shop-checkout next to ~/Projects/shop) or a topic an agent named with `cproj sub`.

def worktree_of(d):
    """Main checkout of a git worktree folder — its .git is a file pointing into <main>/.git/worktrees/."""
    try:
        m = re.match(r"gitdir:\s*(.+)", (Path(d) / ".git").read_text().strip())
    except OSError:
        return None
    g = (Path(d) / m.group(1)).resolve() if m else None
    return str(g.parent.parent.parent) if g and g.parent.name == "worktrees" else None


def read_subs():
    try:
        return json.loads(SUBS.read_text())
    except (OSError, ValueError):
        return {}


def touch_sub(pkey, skey, name, desc=None, worktree=False):
    subs = read_subs()
    s = subs.setdefault(pkey, {}).setdefault(skey, {"name": name, "created": int(time.time())})
    s["last"] = int(time.time())
    if desc:
        s["desc"] = desc
    if worktree:
        s["worktree"] = True
    STATE.mkdir(parents=True, exist_ok=True)
    SUBS.write_text(json.dumps(subs, ensure_ascii=False, indent=1))


def short_name(child, parent):
    """`shop-checkout` in project `shop` → `checkout`."""
    rest = child[len(parent):].lstrip("-_. ") if child.lower().startswith(parent.lower()) else ""
    return rest or child


def attach_subs(items):
    """Fold worktree checkouts into their main project and add the topics agents created there."""
    by_path = {e["local"]: e for e in items if e.get("local")}
    out, topics = [], read_subs()
    for e in items:
        p = by_path.get(e.get("worktree_of"))
        if p is None or p is e:
            out.append(e)
            continue
        name = short_name(e["name"], p["name"])
        p.setdefault("subs", {})[key_of(name)] = {"name": name, "kind": "worktree", "local": e["local"],
                                                  "alias": e["key"], "updated": e["updated"], "desc": e.get("desc", "")}
        p["updated"] = max(p["updated"], e["updated"])
    for e in out:
        for k, t in topics.get(e["key"], {}).items():
            if t.get("worktree") and k not in e.get("subs", {}):
                continue  # the worktree is gone
            s = e.setdefault("subs", {}).setdefault(k, {"name": t["name"], "kind": "topic", "updated": 0})
            s["desc"] = t.get("desc") or s.get("desc", "")
            s["updated"] = max(s["updated"], t.get("last", 0))
    return out


def sub_items(items):
    """One matchable entry per subproject: `parent/sub`, carrying the parent's sources."""
    return [{**e, "key": f"{e['key']}/{k}", "name": f"{e['name']}/{s['name']}", "desc": s.get("desc", ""),
             "local": s.get("local") or e.get("local"), "updated": s["updated"] or e["updated"], "subs": None,
             "parent": e, "sub": k, "sub_name": s["name"], "alias": s.get("alias"), "kind": s["kind"]}
            for e in items for k, s in sorted((e.get("subs") or {}).items())]


def is_big(e):
    """Big = worth splitting into subprojects: it already has some, or sessions keep coming back to it."""
    return len(e.get("subs") or {}) >= 2 or sum(x["event"] == "pick" for x in read_events(e["key"])) >= CFG["big_after"]


def color_of(key):
    return CFG["colors"].get(key) or PALETTE[crc32(key.encode()) % len(PALETTE)]


def sgr(color, bg=False):
    """ANSI colour: a 256-colour number or #rrggbb."""
    c = str(color)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return f"{48 if bg else 38};2;{int(c[1:3], 16)};{int(c[3:5], 16)};{int(c[5:7], 16)}"
    return f"{48 if bg else 38};5;{int(c) if c.isdigit() else PALETTE[0]}"


def label(b):
    return b["name"] + (f"/{b['sub_name']}" if b.get("sub") else "")


def find_or_make_sub(p, name, desc=None):
    """The subproject of p that `name` means, else a new topic. → (entry, created?)"""
    hits = [x for x in sub_items([p]) if base_score({"key": x["sub"], "name": x["sub_name"]}, name) >= 80]
    if len(hits) == 1:
        if desc:
            touch_sub(p["key"], hits[0]["sub"], hits[0]["sub_name"], desc)
        return hits[0], False
    k = key_of(short_name(key_of(name), p["key"]))[:40].strip("-") or "misc"
    touch_sub(p["key"], k, k, desc)
    return {**p, "key": f"{p['key']}/{k}", "name": f"{p['name']}/{k}", "desc": desc or "", "subs": None,
            "updated": int(time.time()), "parent": p, "sub": k, "sub_name": k, "alias": None, "kind": "topic"}, True


def sub_cmd(query, desc=None):
    b = current_badge()
    if not b:
        return print("CPROJ: NO PROJECT active — pick one first (`cproj pick <name>`), then `cproj sub <topic>`.")
    p = next((e for e in load_index(cold=True) if e["key"] == b["key"]), None)
    if p is None:
        return print(f"CPROJ: ERROR — project {b['key']} is no longer in the index.")
    if not query:
        return print(f"CPROJ: SUBPROJECTS of {p['name']}\n" + (subs_block(p, b.get("sub"), 999) or "none yet"))
    if query in ("-", "none", "off"):
        switch_to(p)
        return print(f"CPROJ: SUB cleared — back to {p['name']}.")
    e, new = find_or_make_sub(p, query, desc)
    switch_to(e)
    print(f"CPROJ: SUB {label(e['parent'])}/{e['sub_name']} ({'new' if new else 'existing'})"
          + (f" — worktree {tilde(e['local'])}, work there" if e["kind"] == "worktree" else ""))


def color_cmd(args):
    """cproj color NAME [COLOR|-]: badge colour of a project (256-colour number or #rrggbb; - = automatic)."""
    name, color = (args + [None])[:2] if args else (None, None)
    if not name:
        return print("usage: cproj color NAME [COLOR|-]")
    sel, _ = resolve(load_index(cold=True), name)
    if not sel:
        return print(f"CPROJ: NO UNIQUE MATCH for “{name}”.")
    p = sel.get("parent") or sel
    if color:
        if color != "-" and not re.fullmatch(r"#[0-9a-fA-F]{6}|\d{1,3}", color):
            return print("CPROJ: ERROR — colour must be a 256-colour number (0–255) or #rrggbb.")
        f = Path(os.environ.get("CPROJ_CONFIG", CONFIG_DIR / "config.json"))
        try:
            cfg = json.loads(f.read_text())
        except FileNotFoundError:
            cfg = {}
        cfg.setdefault("colors", {}).pop(p["key"], None)
        if color != "-":
            cfg["colors"][p["key"]] = int(color) if color.isdigit() else color
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        CFG["colors"] = cfg["colors"]
        for bf in SESSIONS.glob("*.json"):  # running sessions pick the new colour up at once
            try:
                b = json.loads(bf.read_text())
                if b.get("key") == p["key"]:
                    bf.write_text(json.dumps({**b, "color": color_of(p["key"])}, ensure_ascii=False))
            except (OSError, ValueError):
                pass
    c = color_of(p["key"])
    print(f"CPROJ: COLOR {p['name']} = {c}  \033[1;38;5;16;{sgr(c, bg=True)}m ◆ {p['name']} \033[0m")


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
    n = len(e.get("subs") or {})
    desc = (f"[{n} subprojects] " if n else "") + (e.get("desc") or "")
    return f"{e['name']:<28} {where(e)}  {ago(e['updated']):>5}  {(e.get('status') or '')[:11]:<11} {desc[:width]}"


def score(e, q):
    split = 0
    if e.get("parent") and len(q.split()) > 1:  # "shop checkout" = project "shop", subproject "checkout"
        t = q.split()
        split = max(min(score(e["parent"], " ".join(t[:i])), score({"key": e["sub"], "name": e["sub_name"]},
                                                                    " ".join(t[i:]))) for i in range(1, len(t)))
        split = max(70, split - 10) if split >= 60 else 0
    return max(split, base_score(e, q))


def base_score(e, q):
    n, k = e["name"].lower(), key_of(e["key"])
    ql, qk = q.lower(), key_of(q)
    if ql in (n, k) or qk == k or (e.get("alias") and qk == e["alias"]):
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
    """Best project or subproject for a query; a subproject only competes where it matches better than its parent."""
    pool = items + sub_items(items)
    ranked = [(s, e) for s, e in sorted(((score(e, query), e) for e in pool), key=lambda t: -t[0])
              if s > 0 and not (e.get("parent") and score(e["parent"], query) >= s)]
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
    ids = {k: os.environ.get(k) for k in ("CPROJ_SESSION", "CODEX_THREAD_ID", "GEMINI_SESSION_ID",
                                          "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")}
    harness = (f"codex-{ids['CODEX_THREAD_ID']}" if ids["CODEX_THREAD_ID"] else
               f"gemini-{ids['GEMINI_SESSION_ID']}" if ids["GEMINI_SESSION_ID"] else
               "hermes" if os.environ.get("HERMES_INTERACTIVE") or os.environ.get("_HERMES_GATEWAY") else None)
    # Hermes shells carry no session id — "hermes" keeps them from borrowing an inherited Claude one
    return ids["CPROJ_SESSION"] or harness or ids["CLAUDE_CODE_SESSION_ID"] or ids["CLAUDE_SESSION_ID"] or "shared"


def current(fmt="plain"):
    """The active project of this slot — for prompts and status bars of other tools."""
    b = current_badge()
    if fmt == "json":
        return print(json.dumps(b or {}, ensure_ascii=False))
    if b:
        print(f"◆ {b['name']}" + (f" › {b['sub_name']}" if b.get("sub") else "") if fmt == "plain"
              else f"{label(b)}\t{b.get('local') or b.get('server') or b.get('github')}")


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


def set_badge(e, sid=None, keep=None):
    """e = a project or a subproject entry (sub_items); the badge keeps the parent's key and name.
    keep = the previous badge, whose start (time, HEAD) still counts — a topic named later in the same session."""
    sid = sid or session_id()
    if not sid:
        return False
    p, sub = e.get("parent") or e, e.get("sub")
    head = keep["head"] if keep else sh(["git", "-C", e["local"], "rev-parse", "HEAD"], 3).strip() if e.get("local") else ""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{sid}.json").write_text(json.dumps({
        "key": p["key"], "name": p["name"], "sub": sub, "sub_name": e.get("sub_name"), "color": color_of(p["key"]),
        "where": where(e), "local": e.get("local"), "server": e.get("server"),
        "github": e.get("github_name") or e.get("github"), "since": keep.get("since") or int(time.time()) if keep else int(time.time()),
        "head": head},
        ensure_ascii=False))
    mark_recent(p["key"])
    if sub:
        touch_sub(p["key"], sub, e["sub_name"], worktree=e.get("kind") == "worktree")
    log_event("pick", p["key"], p["name"], sid, **({"sub": sub} if sub else {}))
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
    return (f"CPROJ: {verb} {label(b)} (active since {t}; {work_summary(b) or 'no local checkout'})"
            + "".join(f"\n{k}: {b[k]}" for k in ("local", "server", "github") if b.get(k)))


def switch_to(sel):
    prev = current_badge()
    p = sel.get("parent") or sel
    if prev and (prev["key"] != p["key"] or prev.get("sub") and prev["sub"] != sel.get("sub")):
        print(left_project(prev, "LEFT"))
        log_event("off", prev["key"], prev["name"], **({"sub": prev["sub"]} if prev.get("sub") else {}))
        prev = None
    same_tree = prev and prev["key"] == p["key"] and prev.get("local") == sel.get("local") and prev.get("head") is not None
    return set_badge(sel, keep=prev if same_tree else None)


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
    sub = b.get("sub") if b and b["key"] == key else None
    log_event("handoff", key, b["name"] if b and b["key"] == key else key, text=text.strip(),
              **({"sub": sub} if sub else {}))
    print(f"CPROJ: HANDOFF saved for {key}" + (f"/{b['sub_name']}." if sub else "."))


def session_end():
    """SessionEnd hook: close an open project with a mechanical handoff."""
    try:
        sid = json.load(sys.stdin).get("session_id")
    except ValueError:
        return
    b = current_badge(sid)
    if b:
        log_event("end", b["key"], b["name"], sid, **({"sub": b["sub"]} if b.get("sub") else {}),
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
        c = color_of(b["key"])  # from the config, so a changed colour shows at once
        parts = [f"\033[1;38;5;16;{sgr(c, bg=True)}m ◆ {b['name']} \033[0m"
                 + (f"\033[1;{sgr(c)}m › {b['sub_name']}\033[0m" if b.get("sub") else "")
                 + f"\033[2m {loc}\033[0m"]
    else:  # nothing picked: the working directory may still tell — a worktree as parent › sub
        base = next((d for d in PROJECT_DIRS if cwd.startswith(f"{d}/")), None)
        name = cwd[len(str(base)) + 1:].split("/")[0] if base else ""
        main = worktree_of(base / name) if name else None
        if main:
            pname = Path(main).name
            name = f"{pname} › {short_name(name, pname)}"
        parts = [f"\033[{sgr(color_of(key_of(Path(main).name if main else name)))}m◇ {name}\033[0m" if name
                 else "\033[2m◇ no project\033[0m"]
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


def project_sessions(key, limit=3, sub=None, aliases=()):
    """Sessions that ran in the project's folders (aliases = its worktrees) or picked it (only this sub if given)."""
    idx, keys = transcript_index(), ({key} if not sub else set()) | set(aliases)
    sids = ({sid for sid, t in idx.items() if keys & set(t["keys"])}
            | {e["sid"] for e in read_events(key) if e.get("sid") and (not sub or e.get("sub") == sub)})
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


def subs_block(p, current=None, limit=15):
    subs = sorted((p.get("subs") or {}).items(), key=lambda t: -t[1]["updated"])
    if not subs:
        return ""
    out = [f"## Subprojects of {p['name']} (newest first; `cproj sub <name>` switches this session to one)"]
    for k, x in subs[:limit]:
        where_ = f"worktree {tilde(x['local'])}" if x["kind"] == "worktree" else "topic"
        out.append(f"- {x['name']} · {where_} · {ago(x['updated'])}" + (f" · {x['desc']}" if x.get("desc") else "")
                   + ("   ◀ this session" if k == current else ""))
    if len(subs) > limit:
        out.append(f"- … and {len(subs) - limit} older ones (`cproj sub` lists all)")
    return "\n".join(out)


def history_block(e):
    p, sub, out = e.get("parent") or e, e.get("sub"), []
    evs = [x for x in read_events(p["key"]) if x["event"] in ("handoff", "end")]
    notes = [x for x in evs if x.get("sub") == sub][-3:] if sub else evs[-4:]
    if sub:  # what happened elsewhere in the project still matters a little
        notes = [x for x in evs if x.get("sub") != sub][-1:] + notes
    if notes:
        out.append("## Recent handoffs")
        out += [f"- {since(x['ts'])} ({'session end' if x['event'] == 'end' else 'handoff'}"
                f"{' · ' + x['sub'] if x.get('sub') else ''}): {x['text']}" for x in reversed(notes)]
    aliases = [e["alias"]] if sub and e.get("alias") else [] if sub else [x["alias"] for x in (p.get("subs") or {}).values() if x.get("alias")]
    sess = project_sessions(p["key"], sub=sub, aliases=aliases)
    if sess:
        out.append("## Recent Claude sessions (resume in a new terminal)")
        out += [f"- {since(m)} · “{t['title'] or 'untitled'}” · `cd {tilde(t['start'])} && claude -r {sid}`"
                for m, sid, t in sess]
    return ("\n\n" + "\n".join(out)) if out else ""


def brief(e):
    p = e.get("parent") or e
    title = f"{p['name']} › {e['sub_name']}  (subproject, {e['kind']})" if e.get("sub") else e["name"]
    descs = [e.get("desc"), p.get("desc")] if e.get("sub") else [e.get("desc")]
    head = (f"# Project: {title}   [{where(e)} = local/server/GitHub]   last active: {since(e['updated'])}"
            + "".join(f"\n{d}" for d in dict.fromkeys(descs) if d)
            + (f"\nStatus: {p['status']}" if p.get("status") else ""))
    subs = subs_block(p, e.get("sub"))
    head += f"\n\n{subs}" if subs else ""
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
    return [["init", "-q"], ["symbolic-ref", "HEAD", "refs/heads/main"], *cfg, ["add", "-A"], ["commit", "-q", "-m", "Initial project skeleton"]]


def part_of(items, name):
    """`shop-payments` while `shop` exists: a subproject, not a new project. → (parent, rest) or None"""
    k = key_of(name)
    par = max((e for e in items if k.startswith(e["key"] + "-") and not e.get("temp")), key=lambda e: len(e["key"]),
              default=None)
    return (par, k[len(par["key"]) + 1:]) if par else None


def subproject_hint(hit):
    p, rest = hit
    return (f"CPROJ: SUBPROJECT OF {p['name']} — this is a part of {p['name']}, not a new project. "
            f"Use `cproj pick \"{p['name']} {rest}\"` (or `cproj sub {rest}` while {p['name']} is active); "
            f"`--force` creates a separate project anyway.")


def new_project(name, where_="local", github="none", desc="", force=False):
    items = load_index(cold=True)  # duplicates count in cold storage too
    k = key_of(name)
    hit = part_of(items, name)
    if hit and not force:
        return print(subproject_hint(hit))
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


# ---------------------------------------------------------------- temporary projects

def temp_project(name, desc=""):
    """A throwaway project (no git, no duplicate check) — e.g. one per agent session to resume."""
    if not name:
        return print("CPROJ: ERROR — temp needs a NAME.")
    d = TEMP_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    mark = d / TEMP_MARK
    if not mark.exists() or desc:
        mark.write_text(json.dumps({"created": int(time.time()), "desc": desc}, ensure_ascii=False))
    print(json.dumps({"key": key_of(name), "path": str(d)}))


def temp_entry(query):
    d = TEMP_DIR / query
    if (d / TEMP_MARK).is_file():
        return d
    hits = [p for p in TEMP_DIR.glob("*") if (p / TEMP_MARK).is_file() and key_of(p.name) == key_of(query)]
    return hits[0] if len(hits) == 1 else None


def drop_temp(query):
    d = temp_entry(query)
    if not d:
        return print(f"CPROJ: no temporary project “{query}” — normal projects are never deleted.")
    shutil.rmtree(d)
    print(f"CPROJ: DROPPED {d.name}")


def keep_temp(query):
    d = temp_entry(query)
    if not d:
        return print(f"CPROJ: no temporary project “{query}”.")
    target = PROJECT_DIRS[0] / d.name
    if target.exists():
        return print(f"CPROJ: ERROR — {tilde(target)} already exists.")
    shutil.move(str(d), str(target))
    (target / TEMP_MARK).unlink(missing_ok=True)
    print(f"CPROJ: KEPT {d.name} → {tilde(target)}")


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
    items += [x for x in sub_items(items) if x["kind"] == "worktree"]
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
            sv = None if e.get("parent") else server.get(e.get("server") or "")
            if sv and sv["head"] and sv["head"] != s["head"]:
                known = subprocess.run(["git", "-C", e["local"], "cat-file", "-e", sv["head"] + "^{commit}"],
                                       capture_output=True).returncode == 0
                notes.append("server behind local" if known else "server has commits local doesn't know")
            if sv and sv["dirty"]:
                notes.append(f"server: {sv['dirty']} uncommitted")
        mine = [p for p in prs if e.get("github") and p[0].lower() == e["github"] and not e.get("parent")]
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
        hit = rest and part_of(load_index(cold=True), rest)
        if hit:
            return print(subproject_hint(hit))
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
        if sel is None:  # "shop payments": an exactly named project plus a new topic
            t = query.split()
            for i in range(len(t) - 1, 0, -1):
                par = [e for e in items if base_score(e, " ".join(t[:i])) == 100]
                if len(par) == 1:
                    sel, cands = find_or_make_sub(par[0], " ".join(t[i:]))[0], []
                    break
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
    if not sel.get("sub") and is_big(sel):
        print(f"CPROJ: SUBPROJECT NEEDED — {sel['name']} is a big project. As soon as this session's topic is clear, "
              f"run `cproj sub <topic>`: an existing subproject if one fits (list below), else a new 1–3-word "
              f"kebab-case name without the project name or a date. Never create a separate project for it.")
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
    if cmd == "temp":
        return temp_project(re.sub(r"[^A-Za-z0-9._-]+", "-", query).strip("-"), opt("desc", ""))
    if cmd == "sub":
        return sub_cmd(query, opt("desc"))
    if cmd == "color":
        return color_cmd([a for a in args if not a.startswith("--")])
    if cmd == "drop":
        return drop_temp(query)
    if cmd == "keep":
        return keep_temp(query)
    if cmd == "list":
        items = load_index(cold=True if "--json" in args or "--all" in args else "only" if "--cold" in args else False)
        if "--json" in args:  # subprojects also as flat entries (worktrees under their folder key), marked "parent"
            flat = [{**x, "key": x.get("alias") or x["key"], "parent": x["parent"]["key"]} for x in sub_items(items)]
            return print(json.dumps(items + flat, ensure_ascii=False, indent=1))
        return print(HEADER + "\n" + "\n".join(row(e) for e in items))
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
