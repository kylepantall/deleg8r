"""Install, configure and update deleg8r from its public source repository."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import heapq
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

from delegate import TRIAGE, hook_configuration
from models import ROUTE, ask, model_settings
from store import Store

try:
    import curses
except ImportError:
    curses = None


APP = "deleg8r"
SERVER = "task_delegation"
FILES = ("delegate.py", "models.py", "models.json", "store.py", "installer.py", "install.sh", "README.md")


def default_root():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return Path(os.environ.get("DELEG8R_INSTALL_DIR") or data / APP).expanduser().resolve()


def default_bin():
    return Path(os.environ.get("DELEG8R_BIN_DIR") or Path.home() / ".local" / "bin").expanduser().resolve()


def atomic_text(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path = path.resolve()
    if path.exists():
        mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def configure_shell_path(bin_dir):
    home = Path.home()
    shell = Path(os.environ.get("SHELL") or "/bin/sh").name
    directory = str(bin_dir)
    if any(character in directory for character in ":\r\n"):
        raise ValueError("The launcher directory cannot contain a colon or newline")

    if shell == "zsh":
        profiles = [Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"]
    elif shell == "bash":
        login_profiles = [home / name for name in (".bash_profile", ".bash_login", ".profile")]
        login = next((path for path in login_profiles if path.is_file()), home / ".bash_profile")
        profiles = [home / ".bashrc", login]
    elif shell == "fish":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        profiles = [config / "fish" / "config.fish"]
    elif shell in ("sh", "dash", "ksh"):
        profiles = [home / ".profile"]
    else:
        raise ValueError(f"Automatic PATH setup is unavailable for {shell}")

    if shell == "fish":
        quoted = "'" + directory.replace("\\", "\\\\").replace("'", "\\'") + "'"
        commands = (
            f"if not contains -- {quoted} $PATH\n"
            f"    set -gx PATH {quoted} $PATH\n"
            "end\n"
        )
    else:
        quoted = shlex.quote(directory)
        commands = (
            'case ":${PATH-}:" in\n'
            f'    *:{quoted}:*) ;;\n'
            f'    *) export PATH={quoted}${{PATH:+":$PATH"}} ;;\n'
            'esac\n'
        )

    start = "# >>> deleg8r PATH >>>"
    end = "# <<< deleg8r PATH <<<"
    block = f"{start}\n{commands}{end}\n"
    managed = re.compile(rf"^{re.escape(start)}\n.*?^{re.escape(end)}(?:\n|$)", re.M | re.S)
    for profile in profiles:
        existing = profile.read_bytes().decode("utf-8") if profile.exists() else ""
        if managed.search(existing):
            updated = managed.sub(lambda _: block, existing, count=1)
        else:
            separator = "\n" if existing and not existing.endswith("\n") else ""
            updated = existing + separator + block
        if updated != existing:
            atomic_text(profile, updated)


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path}")
    return data


def verify_sqlite():
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        except sqlite3.OperationalError as error:
            raise ValueError("Python's SQLite library must support FTS5") from error


def install_release(source, revision, root, bin_dir):
    verify_sqlite()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Could not identify the source revision")
    launcher = bin_dir / APP
    wrapper = "#!/bin/sh\nexec " + shlex.join([
        sys.executable, str(root / "current" / "installer.py"), "--root", str(root), "--bin-dir", str(bin_dir),
    ]) + ' "$@"\n'
    if launcher.is_symlink() or (launcher.exists() and launcher.read_text(encoding="utf-8") != wrapper):
        raise ValueError(f"{launcher} is already in use")
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = releases / revision
    with (root / "install.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not release.exists():
            with tempfile.TemporaryDirectory(prefix=".install-", dir=releases) as directory:
                staged = Path(directory) / "release"
                staged.mkdir()
                for name in FILES:
                    shutil.copy2(source / name, staged / name)
                    if name.endswith(".py"):
                        try:
                            compile((staged / name).read_text(encoding="utf-8"), name, "exec")
                        except SyntaxError as error:
                            raise ValueError(f"Invalid {name} in source revision {revision[:12]}") from error
                read_json(staged / "models.json")
                (staged / "revision").write_text(revision + "\n", encoding="utf-8")
                os.replace(staged, release)
        with tempfile.TemporaryDirectory(prefix=".switch-", dir=root) as directory:
            pointer = Path(directory) / "current"
            pointer.symlink_to(release, target_is_directory=True)
            os.replace(pointer, root / "current")
        if not launcher.exists():
            atomic_text(launcher, wrapper, 0o755)
    return revision


def hook_settings(path, launcher, client, install=None):
    data = read_json(path)
    events = data.get("hooks", {})
    if not isinstance(events, dict):
        raise ValueError(f"Invalid hooks object in {path}")
    present = []
    changed = False
    for event, (group,) in hook_configuration([str(launcher)], ["--client", client]).items():
        command = group["hooks"][0]["command"]
        entries = events.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Invalid {event} hooks in {path}")
        retained = []
        installed = False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                retained.append(entry)
                continue
            hooks = entry["hooks"]
            others = [hook for hook in hooks if not isinstance(hook, dict) or hook.get("command") != command]
            installed = installed or len(others) != len(hooks)
            if len(others) == len(hooks) or install is not False:
                retained.append(entry)
            elif others:
                retained.append({**entry, "hooks": others})
        if installed:
            present.append(event)
        if install is True and not installed:
            events[event] = [*entries, group]
            changed = True
        if install is False and installed:
            if retained:
                events[event] = retained
            else:
                events.pop(event)
            changed = True
    if changed:
        if events:
            data["hooks"] = events
        else:
            data.pop("hooks", None)
    return data, present, changed


def matches_server(existing, launcher, client):
    return (
        isinstance(existing, dict) and existing.get("command") == str(launcher)
        and existing.get("args") == ["serve", "--client", client]
    )


def claude_paths():
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(configured).expanduser() if configured else Path.home() / ".claude"
    registry = home / ".claude.json" if configured else Path.home() / ".claude.json"
    return registry, home / "settings.json"


def register_claude(root, launcher):
    _, settings_path = claude_paths()
    existing = registered_server(root, "claude")
    if existing is not None and not matches_server(existing, launcher, "claude"):
        raise ValueError("Claude Code already has a different user MCP server named task_delegation")
    settings, _, changed = hook_settings(settings_path, launcher, "claude", install=True)
    if existing is None:
        subprocess.run(
            ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", SERVER, "--",
             str(launcher), "serve", "--client", "claude"],
            cwd=root, check=True,
        )
    if changed:
        atomic_text(settings_path, json.dumps(settings, indent=2) + "\n")


def codex_json(root, *args):
    output = subprocess.check_output(["codex", "mcp", *args, "--json"], cwd=root, text=True)
    return json.loads(output)


def codex_paths():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return home / "config.toml", home / "hooks.json"


def registered_server(root, client):
    if client == "claude":
        registry, _ = claude_paths()
        return read_json(registry).get("mcpServers", {}).get(SERVER)
    entry = next((item for item in codex_json(root, "list") if item["name"] == SERVER), None)
    return entry.get("transport") if entry else None


def register_codex(root, launcher):
    config_path, settings_path = codex_paths()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = registered_server(root, "codex")
    if existing is not None and not matches_server(existing, launcher, "codex"):
        raise ValueError("Codex already has a different user MCP server named task_delegation")
    settings, _, changed = hook_settings(settings_path, launcher, "codex", install=True)
    if existing is None:
        subprocess.run(
            ["codex", "mcp", "add", SERVER, "--", str(launcher), "serve", "--client", "codex"],
            cwd=root, check=True,
        )
    subprocess.run(["codex", "features", "enable", "hooks"], cwd=root, check=True)
    if codex_json(root, "get", SERVER).get("tool_timeout_sec") is None:
        original = config_path.read_text(encoding="utf-8")
        updated, count = re.subn(r"(?m)^\[mcp_servers\.task_delegation\][ \t]*$",
                                 r"\g<0>\ntool_timeout_sec = 135", original, count=1)
        if count != 1:
            raise ValueError(f"Could not set the MCP timeout in {config_path}")
        atomic_text(config_path, updated)
        try:
            if codex_json(root, "get", SERVER).get("tool_timeout_sec") != 135:
                raise ValueError("The Codex MCP timeout is invalid")
        except (OSError, ValueError, subprocess.SubprocessError):
            atomic_text(config_path, original)
            raise
    if changed:
        atomic_text(settings_path, json.dumps(settings, indent=2) + "\n")


def client_names(client):
    return ("claude", "codex") if client == "auto" else (client,)


def unhook(root, bin_dir, client):
    failures = []
    found = False
    launcher = bin_dir / APP
    for name in client_names(client):
        if not shutil.which(name):
            if client != "auto":
                failures.append(f"{name} is not on PATH")
            continue
        found = True
        _, path = claude_paths() if name == "claude" else codex_paths()
        try:
            existing = registered_server(root, name)
            settings, _, changed = hook_settings(path, launcher, name, install=False)
            if changed:
                atomic_text(path, json.dumps(settings, indent=2) + "\n")
            if matches_server(existing, launcher, name):
                command = ["claude", "mcp", "remove", "--scope", "user", SERVER] if name == "claude" else [
                    "codex", "mcp", "remove", SERVER,
                ]
                subprocess.run(command, cwd=root, check=True)
                changed = True
            if existing is not None and not matches_server(existing, launcher, name):
                print(f"{name}: kept the existing MCP server owned by another installation.")
            print(f"{name}: {'unhooked' if changed else 'no deleg8r registration to remove'}.")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(f"{name}: {error}")
    if failures:
        raise ValueError("Unhook incomplete. " + " ".join(failures))
    if not found:
        print("No supported client CLIs on PATH. Existing client settings were preserved.")


def setup(root, bin_dir, client):
    launcher = bin_dir / APP
    configured = []
    failures = []
    for name in client_names(client):
        if not shutil.which(name):
            if client != "auto":
                failures.append(f"{name} is not on PATH")
            continue
        register = register_claude if name == "claude" else register_codex
        try:
            register(root, launcher)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(f"{name}: {error}")
        else:
            configured.append(name)
    if configured:
        print("Configured " + " and ".join(configured) + ". Restart the clients and review any hooks or approval prompts.")
    if failures:
        raise ValueError("Setup incomplete. " + " ".join(failures) + ". Run deleg8r setup to retry.")
    if not configured:
        print("Run deleg8r setup once Claude Code or Codex is on PATH.")


def doctor(root, bin_dir, release, client, live):
    healthy = True

    def check(label, ready, detail=""):
        nonlocal healthy
        healthy = healthy and ready
        print(f"{'OK' if ready else 'FAIL'} {label}" + (f" ({detail})" if detail else ""))

    verify_sqlite()
    check("Python and SQLite FTS5", True, sys.version.split()[0])
    check("curl", bool(shutil.which("curl")))
    db = root / "state.sqlite3"
    writable = os.access(root, os.W_OK | os.X_OK) and (not db.exists() or os.access(db, os.W_OK))
    check("state directory", writable, str(root))
    override = root / "models.json"
    model_path = override if override.is_file() else release / "models.json"
    config = read_json(model_path)
    found = False
    for name in client_names(client):
        binary = shutil.which(name)
        if not binary and client == "auto":
            print(f"SKIP {name} (CLI not on PATH)")
            continue
        found = True
        check(f"{name} CLI", bool(binary), binary or "missing")
        if not binary:
            continue
        try:
            _, path = claude_paths() if name == "claude" else codex_paths()
            settings, events, _ = hook_settings(path, bin_dir / APP, name)
            existing = registered_server(root, name)
            registered = matches_server(existing, bin_dir / APP, name)
            if name == "codex" and registered:
                registered = codex_json(root, "get", SERVER).get("enabled", True)
            check(f"{name} MCP registration", registered)
            check(f"{name} hook registration", len(events) == 2, f"{len(events)}/2 user hooks")
            if name == "codex":
                features = subprocess.check_output(["codex", "features", "list"], cwd=root, text=True,
                                                   stderr=subprocess.DEVNULL, timeout=8)
                hooks_enabled = any(
                    cells[0] == "hooks" and cells[-1] == "true"
                    for line in features.splitlines() if (cells := line.split())
                )
            else:
                hooks_enabled = settings.get("disableAllHooks") is not True
            check(f"{name} hooks enabled", hooks_enabled)
            auth = ["claude", "auth", "status"] if name == "claude" else ["codex", "login", "status"]
            status = subprocess.run(auth, cwd=root, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=8)
            check(f"{name} authentication", status.returncode == 0)
            models = [model_settings(config, name, tier)["model"] for tier in ("triage", "focused", "complex")]
            check(f"{name} model settings", True, ", ".join(models))
            with tempfile.TemporaryDirectory(prefix="deleg8r-doctor-") as folder:
                command = [
                    sys.executable, str(release / "delegate.py"), "serve", "--client", name,
                    "--db", str(Path(folder) / "state.sqlite3"), "--models", str(model_path),
                ]
                request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25"}}
                response = subprocess.run(command, input=json.dumps(request) + "\n", text=True,
                                          capture_output=True, check=True, timeout=8)
                server = json.loads(response.stdout)["result"]["serverInfo"]["name"]
                check(f"{name} local MCP handshake", server == "task-delegation")
            if live and status.returncode == 0:
                ask(config, name, "triage", TRIAGE, {"request": "Fix a typo in README.md."}, ROUTE)
                check(f"{name} live triage", True)
        except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
            check(f"{name} diagnostics", False, str(error))
    if not found:
        check("supported coding client", False, "install Claude Code or Codex")
    if shutil.which("codex") and client in ("auto", "codex"):
        print("Confirm Codex hook trust in /hooks or the desktop hook settings.")
    return healthy


def terminal_text(value):
    return "".join(character if character.isprintable() else "?" for character in value)


def chart(title, counts):
    print(f"\n{title}")
    if not counts:
        print("  No activity")
        return
    peak = max(counts.values(), default=0)
    for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        length = max(1, round(24 * count / peak)) if count else 0
        print(f"  {terminal_text(label):<18} {count:>6}  {'#' * length}")


def activity_counts(path):
    task_states = Counter({"completed": 0})
    tiers, clients, modes, agents = Counter(), Counter(), Counter(), Counter()
    with contextlib.closing(Store(path).db) as db:
        routes = Counter({
            row["detail"]: row["total"] for row in db.execute(
                "SELECT detail, count(*) AS total FROM activity WHERE kind='route' GROUP BY detail"
            )
        })
        for row in db.execute("SELECT client,tier,plan,reports FROM tasks"):
            tiers[row["tier"]] += 1
            clients[row["client"]] += 1
            steps = json.loads(row["plan"])["steps"]
            reports = json.loads(row["reports"])
            states = []
            for step in steps:
                modes[step["mode"]] += 1
                report = reports.get(step["id"], {})
                states.append(report.get("status", "pending"))
                if report.get("agent_name"):
                    agents[report["agent_name"]] += 1
            if not states:
                task_states["host-only"] += 1
            elif all(state == "done" for state in states):
                task_states["completed"] += 1
            elif "blocked" in states:
                task_states["blocked"] += 1
            elif all(state == "pending" for state in states):
                task_states["planned"] += 1
            else:
                task_states["in progress"] += 1

        tokens, tools = Counter(), Counter()
        token_labels = (("input_tokens", "input"), ("output_tokens", "output"),
                        ("cached_input_tokens", "cached"))
        for row in db.execute("""SELECT phase,usage FROM model_usage
                                 UNION ALL SELECT 'worker',usage FROM worker_usage"""):
            usage = json.loads(row["usage"])
            for field, label in token_labels:
                if field in usage:
                    tokens[f"{row['phase']} {label}"] += usage[field]
        for row in db.execute("SELECT name,count(*) AS calls FROM tool_calls GROUP BY name"):
            tools[f"MCP: {row['name']}"] = row["calls"]
        for row in db.execute("SELECT usage FROM worker_usage"):
            usage = json.loads(row["usage"])
            for tool in usage.get("tools", []):
                tools[f"Worker: {tool['name']}"] += tool["calls"]

    return sum(tiers.values()), (
        ("Recorded prompt routes", routes), ("Task tiers", tiers),
        ("Task status", task_states), ("Tasks by client", clients),
        ("Current steps by mode", modes), ("Current steps by reported agent", agents),
        ("Measured tokens by phase", tokens), ("Tool calls by name and source", tools),
    )


def terminal_dashboard(view, *args):
    if not curses or not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") in (None, "", "dumb"):
        return False
    try:
        curses.wrapper(view, *args)
    except curses.error:
        return False
    except KeyboardInterrupt:
        pass
    return True


def dashboard_colors():
    if not curses.has_colors():
        return curses.A_BOLD, curses.A_NORMAL, curses.A_BOLD, curses.A_NORMAL
    curses.start_color()
    background = curses.COLOR_BLACK
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        pass
    for number, color in enumerate((curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_GREEN, curses.COLOR_YELLOW), 1):
        curses.init_pair(number, color, background)
    return tuple(curses.color_pair(number) | curses.A_BOLD for number in range(1, 5))


def screen_line(screen, row, text, style=0, column=0):
    height, width = screen.getmaxyx()
    if 0 <= row < height and column < width - 1:
        try:
            screen.addnstr(row, column, str(text), width - column - 1, style)
        except curses.error:
            pass


def stats_dashboard(screen, path):
    accents = dashboard_colors()
    total, charts = activity_counts(path)
    tabs = "Routes  Tiers  Status  Clients  Modes  Agents  Tokens  Tools".split()
    selected = offset = 0
    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        screen_line(screen, 0, f" DELEG8R / STATS   {total} tasks", accents[0])
        position = 1
        for index, tab in enumerate(tabs):
            label = f" {index + 1}:{tab} "
            if index == 4:
                position = 1
            screen_line(screen, 2 + index // 4, label,
                        curses.A_REVERSE if index == selected else accents[0], position)
            position += len(label) + 1
        title, counts = charts[selected]
        screen_line(screen, 4, title, curses.A_BOLD, 1)
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        visible = max(1, height - 8)
        offset = min(offset, max(0, len(ordered) - visible))
        peak = max(counts.values(), default=0)
        bar_width = max(0, min(42, width - 35))
        if not ordered:
            screen_line(screen, 6, "No activity for this view", column=1)
        for row, (label, count) in enumerate(ordered[offset:offset + visible], 6):
            screen_line(screen, row, f"{terminal_text(label)[:22]:22} {count:>6}", column=1)
            if peak and count and bar_width:
                length = max(1, round(bar_width * count / peak))
                screen_line(screen, row, "█" * length, accents[selected % len(accents)], 32)
        screen_line(screen, height - 1, " ←/→ Tabs   1–8 Jump   ↑/↓ Scroll   r Reload   q Quit ", curses.A_REVERSE)
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_RIGHT, 9, curses.KEY_LEFT) or ord("1") <= key <= ord("8"):
            if ord("1") <= key <= ord("8"):
                selected = key - ord("1")
            else:
                selected = (selected + (-1 if key == curses.KEY_LEFT else 1)) % len(charts)
            offset = 0
        elif key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif key == ord("r"):
            total, charts = activity_counts(path)


def stats(path, plain=False):
    if not plain and terminal_dashboard(stats_dashboard, path):
        return
    total, charts = activity_counts(path)
    print(f"Deleg8r activity ({total} tasks)")
    for title, counts in charts:
        chart(title, counts)


def print_event(event):
    stamp = datetime.fromtimestamp(event["created"]).astimezone().isoformat(timespec="seconds")
    parts = [stamp, event["client"], event["kind"], event["detail"]]
    if event["task_id"]:
        parts.extend((event["task_id"], f"r{event['revision']}"))
    if event["step_id"]:
        agent = event["agent_name"] or "unreported"
        parts.extend((event["step_id"], f"agent={agent}"))
    print("  ".join(map(terminal_text, parts)), flush=True)


def recent_events(db, tail, cursor):
    newest = db.execute("SELECT coalesce(max(id),0) FROM activity").fetchone()[0]
    rows = db.execute(
        "SELECT * FROM activity WHERE id>? AND id<=? ORDER BY id DESC LIMIT ?", (cursor, newest, tail)
    ).fetchall()
    return reversed(rows), newest


EVENT_STYLES = {"route": ("●", 0), "plan": ("◆", 1), "revise": ("◇", 3), "step": ("■", 2)}


def task_graph(store, task_id):
    task, prompts, calls = store.graph_activity(task_id)
    reports = json.loads(task["reports"])
    steps = json.loads(task["plan"])["steps"]
    plan = f"Plan r{task['revision']}"
    nodes = {}
    for step in steps:
        step_id = step["id"]
        report = reports.get(step_id, {})
        agent = report.get("agent_name")
        state = report.get("status", "pending")
        nodes[step_id] = f"{step_id} · {agent} ({state})" if agent else f"{step_id} ({state})"

    def edge(source, link, target):
        return f"[{source}] ── {link} ──▶ [{target}]"

    lines = [edge(f"Prompt ({route})", "route", plan) for route in prompts]
    if not lines:
        lines = [f"[{plan}]  Prompt unavailable"]
    terminal_steps = set(nodes)
    for step in steps:
        target = nodes[step["id"]]
        if not step["needs"]:
            lines.append(edge(plan, "dispatch", target))
        for predecessor in step["needs"]:
            terminal_steps.discard(predecessor)
            lines.append(edge(nodes[predecessor], "dependency", target))

    verdict = json.loads(task["review"])["verdict"] if task["review"] else "pending"
    leaves = [nodes[step["id"]] for step in steps if step["id"] in terminal_steps] or [plan]
    lines.extend(edge(source, "review", f"Review ({verdict})") for source in leaves)

    lines.extend(("", "MCP and memory flow"))
    if not calls:
        lines.append("No task-scoped calls yet")
    for call in calls:
        actor = nodes.get(call["step_id"], call["step_id"]) or "Host"
        label = f"{call['name']} ×{call['calls']}"
        if call["calls"] != call["successes"]:
            label += f" ({call['calls'] - call['successes']} errors)"
        if call["name"] == "recall" and call["successes"]:
            lines.append(f"[Memory] ── {label} ──▶ [MCP] ── result ──▶ [{actor}]")
        elif call["name"] == "remember" and call["successes"]:
            lines.append(f"[{actor}] ── {label} ──▶ [MCP] ── write ──▶ [Memory]")
        else:
            lines.append(edge(actor, label, "MCP"))
    return lines


# pip install textual
def textual_graph(store, task_id, prompt_id=None):
    """Open the optional Textual view. Import it only for interactive graphs."""
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import ScrollableContainer
    from textual.geometry import Offset, Region
    from textual.widgets import Footer, Static

    node_width, node_height = 22, 5
    directions = ((1, 0), (0, 1), (0, -1), (-1, 0))
    # Direction flags are ordered east, south, north, west.
    glyphs = {
        (1, 0, 0, 1): "─", (0, 1, 1, 0): "│",
        (1, 1, 0, 0): "┌", (0, 1, 0, 1): "┐",
        (1, 0, 1, 0): "└", (0, 0, 1, 1): "┘",
        (1, 1, 1, 0): "├", (0, 1, 1, 1): "┤",
        (1, 1, 0, 1): "┬", (1, 0, 1, 1): "┴", (1, 1, 1, 1): "┼",
    }

    class NodeWidget(Static):
        def __init__(self, x: int, y: int) -> None:
            super().__init__(classes="graph-node", markup=False)
            self.x, self.y = x, y
            self.styles.offset = Offset(x, y)

        def set_content(self, title: str, value: str) -> None:
            text = Text(overflow="ellipsis", no_wrap=True)
            text.append(terminal_text(title) + "\n", style="bold")
            text.append(terminal_text(value) + "\n")
            text.append(f"({self.x}, {self.y})", style="dim")
            self.update(text)

    class GraphCanvas(Static):
        def __init__(self) -> None:
            super().__init__(id="graph-canvas")
            self.nodes: dict[str, NodeWidget] = {}
            self.active = 0
            self.snapshot: tuple = ()
            self.edges = Static(id="graph-edges", markup=False)

        def compose(self) -> ComposeResult:
            yield self.edges

        async def sync(self, descriptions: dict, links: tuple) -> None:
            snapshot = (descriptions, links)
            if self.snapshot == snapshot:
                return
            self.snapshot = snapshot
            for key in list(self.nodes):
                if key not in descriptions:
                    await self.nodes.pop(key).remove()
            for index, key in enumerate(descriptions):
                if key in self.nodes:
                    continue
                column, row = index % 3, index // 3
                x, y = 4 + 31 * column, 2 + 8 * row
                # Keep nodes already moved by the user in their current positions.
                while any(abs(node.x - x) < node_width + 3 and abs(node.y - y) < node_height
                          for node in self.nodes.values()):
                    y += 8
                node = self.nodes[key] = NodeWidget(x, y)
                await self.mount(node)
            self.active = min(self.active, max(0, len(self.nodes) - 1))
            self.draw()

        def select(self, index: int) -> None:
            if self.nodes and -1 <= index < len(self.nodes):
                self.active = index % len(self.nodes)
                self.draw()
                node = list(self.nodes.values())[self.active]
                self.reveal(node)

        def move(self, dx: int, dy: int) -> None:
            if not self.nodes:
                return
            selected = list(self.nodes.values())[self.active]
            x, y = max(3, selected.x + dx), max(1, selected.y + dy)
            if any(node is not selected and abs(node.x - x) < node_width + 3
                   and abs(node.y - y) < node_height for node in self.nodes.values()):
                return
            selected.x, selected.y = x, y
            selected.styles.offset = Offset(x, y)
            self.draw()
            self.reveal(selected)

        def reveal(self, node: NodeWidget) -> None:
            self.call_after_refresh(self.parent.scroll_to_region,
                                    Region(node.x, node.y, node_width, node_height), animate=False)

        def route(self, start: tuple[int, int], end: tuple[int, int], blocked: set[tuple[int, int]],
                  wires: dict, bounds: Region) -> list[tuple[int, int]]:
            # Prefer short routes, but avoid existing wires when a small detour works.
            queue = [(0, start)]
            previous = {start: None}
            cost = {start: 0}
            while queue:
                distance, point = heapq.heappop(queue)
                if distance != cost[point]:
                    continue
                if point == end:
                    path = [point]
                    while previous[path[-1]] is not None:
                        path.append(previous[path[-1]])
                    return path[::-1]
                x, y = point
                for dx, dy in directions:
                    next_point = (x + dx, y + dy)
                    if next_point in blocked or not bounds.contains(*next_point):
                        continue
                    next_cost = distance + (5 if next_point in wires else 1)
                    if next_point not in cost or next_cost < cost[next_point]:
                        previous[next_point] = point
                        cost[next_point] = next_cost
                        heapq.heappush(queue, (next_cost, next_point))
            return []

        def draw(self) -> None:
            descriptions, links = self.snapshot
            width = max(96, max((node.x + node_width + 4 for node in self.nodes.values()), default=0))
            height = max(22, max((node.y + node_height + 3 for node in self.nodes.values()), default=0))
            self.styles.width = self.edges.styles.width = width
            self.styles.height = self.edges.styles.height = height
            occupied = set()
            for index, (key, node) in enumerate(self.nodes.items()):
                node.set_class(index == self.active, "selected")
                node.set_content(*descriptions[key])
                occupied.update((x, y) for x in range(node.x, node.x + node_width)
                                for y in range(node.y, node.y + node_height))
                occupied.update(((node.x - 1, node.y + 2), (node.x + node_width, node.y + 2)))
            marks: dict[tuple, set] = {}
            arrows = set()
            for source, destination in links:
                parent, child = self.nodes[source], self.nodes[destination]
                output = (parent.x + node_width - 1, parent.y + 2)
                arrow = (child.x - 1, child.y + 2)
                start, end = (output[0] + 2, output[1]), (arrow[0] - 1, arrow[1])
                path = self.route(start, end, occupied - {end}, marks, Region(0, 0, width, height))
                if not path:
                    continue
                path = [output, (output[0] + 1, output[1]), *path, arrow]
                arrows.add(arrow)
                for (x, y), (nx, ny) in zip(path, path[1:]):
                    marks.setdefault((x, y), set()).add((nx - x, ny - y))
                    marks.setdefault((nx, ny), set()).add((x - nx, y - ny))
            rows = [[" "] * width for _ in range(height)]
            for (x, y), connections in marks.items():
                shape = tuple(direction in connections for direction in directions)
                rows[y][x] = "►" if (x, y) in arrows else glyphs.get(shape, "─" if shape[0] or shape[3] else "│")
            self.edges.update(Text("\n".join("".join(row) for row in rows), no_wrap=True))

    class NodeGraphApp(App[bool]):
        CSS = """
        Screen { background: #101722; }
        #graph-heading { height: 2; padding: 0 1; color: #84e6d5; text-style: bold; }
        ScrollableContainer { height: 1fr; overflow: auto auto; }
        #graph-canvas { layers: edges nodes; }
        #graph-edges { layer: edges; position: absolute; color: #678cb0; }
        .graph-node { layer: nodes; position: absolute; width: 22; height: 5;
                      padding: 0 1; border: round #546d8c; background: #1b2839; color: #e3eefb; }
        .graph-node.selected { border: round #66f4cc; background: #24394a; }
        """
        BINDINGS = [
            Binding("left", "move(-1, 0)", show=False, priority=True),
            Binding("right", "move(1, 0)", show=False, priority=True),
            Binding("up", "move(0, -1)", "Move Node", key_display="Arrows", priority=True),
            Binding("down", "move(0, 1)", show=False, priority=True),
            Binding("tab", "next_node", "Next Node", priority=True),
            Binding("g", "timeline", "Timeline"),
            Binding("q", "quit", "Quit"),
            *(Binding(str(index), f"select_node({index - 1})", show=False) for index in range(1, 10)),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.task_id = task_id
            self.canvas = GraphCanvas()
            self.heading = Static(id="graph-heading", markup=False)

        def compose(self) -> ComposeResult:
            yield self.heading
            yield ScrollableContainer(self.canvas)
            yield Footer()

        async def on_mount(self) -> None:
            await self.refresh_task()
            self.set_interval(0.5, self.refresh_task)

        async def refresh_task(self) -> None:
            if self.task_id is None:
                self.task_id = store.task_for_prompt(prompt_id)
            if self.task_id is None:
                self.heading.update("Task graph · Waiting for a plan")
                return
            self.heading.update(f"Task graph · {self.task_id}")
            descriptions, links = graph_canvas_data(store, self.task_id)
            await self.canvas.sync(descriptions, links)

        def action_move(self, dx: int, dy: int) -> None:
            self.canvas.move(dx, dy)

        def action_next_node(self) -> None:
            if self.canvas.nodes:
                self.canvas.select((self.canvas.active + 1) % len(self.canvas.nodes))

        def action_select_node(self, index: int) -> None:
            self.canvas.select(index)

        def action_timeline(self) -> None:
            self.exit(True)

    return NodeGraphApp()


def graph_canvas_data(store, task_id):
    task, prompts, calls = store.graph_activity(task_id)
    steps = json.loads(task["plan"])["steps"]
    reports = json.loads(task["reports"])
    verdict = json.loads(task["review"])["verdict"] if task["review"] else "pending"
    nodes = {"@prompt": ("Prompt", prompts[-1] if prompts else "Route unavailable"),
             "@plan": (f"Plan r{task['revision']}", f"{len(steps)} worker steps")}
    links = [("@prompt", "@plan")]
    parents = set()
    for step in steps:
        key = step["id"]
        report = reports.get(key, {})
        agent = report.get("agent_name")
        status = report.get("status", "pending")
        nodes[key] = (agent or key, f"{key} · {status}" if agent else status)
        parents.update(step["needs"])
        links.extend((parent, key) for parent in step["needs"] or ["@plan"])
    nodes["@review"] = ("Review", verdict)
    links.extend((leaf, "@review") for leaf in [step["id"] for step in steps if step["id"] not in parents] or ["@plan"])

    if calls:
        nodes["@mcp"] = ("MCP", f"{sum(call['calls'] for call in calls)} calls")
    memory = Counter()
    for call in calls:
        actor = call["step_id"] or "@host"
        if actor not in nodes:
            nodes[actor] = ("Host" if actor == "@host" else actor, "MCP caller")
        tool, count = call["name"], call["calls"]
        key = f"@call:{actor}:{tool}"
        nodes[key] = (tool, f"{count} calls · {count - call['successes']} errors")
        if tool == "recall" and call["successes"]:
            memory["reads"] += call["successes"]
            links.extend((("@memory", "@mcp"), ("@mcp", key), (key, actor)))
        else:
            links.extend(((actor, key), (key, "@mcp")))
            if tool == "remember" and call["successes"]:
                memory["writes"] += call["successes"]
                links.append(("@mcp", "@memory"))
    if memory:
        nodes["@memory"] = ("Memory", f"{memory['reads']} reads · {memory['writes']} writes")
    return nodes, tuple(dict.fromkeys(links))


def graph_dashboard(screen, store, task_id, prompt_id=None):
    accents = dashboard_colors()
    offset = 0
    horizontal = 0
    screen.timeout(500)
    while True:
        if task_id is None:
            task_id = store.task_for_prompt(prompt_id)
        lines = task_graph(store, task_id) if task_id else ["Waiting for a plan linked to this prompt…"]
        height, _ = screen.getmaxyx()
        visible = max(1, height - 4)
        offset = min(offset, max(0, len(lines) - visible))
        screen.erase()
        screen_line(screen, 0, " DELEG8R / TASK GRAPH", accents[0])
        screen_line(screen, 1, task_id or f"Prompt event {prompt_id}", column=1)
        for y, line in enumerate(lines[offset:offset + visible], 2):
            screen_line(screen, y, terminal_text(line)[horizontal:], accents[2] if "▶" in line else curses.A_BOLD, 1)
        screen_line(screen, height - 1, " ↑/↓ Scroll   ←/→ Pan   g Timeline   q Quit ", curses.A_REVERSE)
        key = screen.getch()
        if key in (ord("q"), 27):
            return False
        if key == ord("g"):
            return True
        if key == curses.KEY_DOWN:
            offset += 1
        elif key == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif key == curses.KEY_RIGHT:
            horizontal = min(max(map(len, lines), default=0), horizontal + 8)
        elif key == curses.KEY_LEFT:
            horizontal = max(0, horizontal - 8)


def monitor_dashboard(screen, store, tail, task_id=None, canvas=False):
    if task_id and not graph_dashboard(screen, store, task_id):
        return
    db = store.db
    accents = dashboard_colors()
    recent, cursor = recent_events(db, tail, 0)
    events = deque(recent, maxlen=1000)
    filters = ("all", *EVENT_STYLES)
    selected_filter = 0
    selected_id = None
    paused = False
    screen.timeout(500)
    while True:
        if not paused:
            recent, cursor = recent_events(db, events.maxlen, cursor)
            events.extend(recent)
        filtered = [event for event in events if filters[selected_filter] in ("all", event["kind"])]
        selected = next((index for index, event in enumerate(filtered) if event["id"] == selected_id),
                        len(filtered) - 1)
        height, width = screen.getmaxyx()
        visible = max(1, height - 9)
        first = max(0, min(selected, len(filtered) - visible))
        screen.erase()
        state = "PAUSED" if paused else "LIVE"
        screen_line(screen, 0, f" DELEG8R / MONITOR   {state}   Filter: {filters[selected_filter]}   {len(filtered)} events",
                    accents[3 if paused else 2])
        screen_line(screen, 2, "Timeline", curses.A_BOLD, 1)
        for column, event in enumerate(filtered[-max(1, width - 13):], 11):
            symbol, color = EVENT_STYLES[event["kind"]]
            screen_line(screen, 2, symbol, accents[color], column)
        screen_line(screen, 3, "● Route   ◆ Plan   ◇ Revision   ■ Step", column=1)
        for row, event in enumerate(filtered[first:first + visible], 5):
            symbol, color = EVENT_STYLES[event["kind"]]
            timestamp = datetime.fromtimestamp(event["created"]).strftime("%H:%M:%S")
            subject = event["step_id"] or (event["task_id"] or "")[:8]
            agent = (event["agent_name"] or "unreported") if event["step_id"] else ""
            line = f"{symbol} {timestamp}  {event['client']:7} {event['kind']:6} {event['detail']:11} {subject:16} {agent}"
            style = accents[color] | (curses.A_REVERSE if row - 5 + first == selected else 0)
            screen_line(screen, row, terminal_text(line), style, 1)
        if filtered:
            event = filtered[selected]
            identity = f"{event['client']}  {event['kind']} {event['detail']}"
            if event["step_id"]:
                identity += f"  step={event['step_id']}  agent={event['agent_name'] or 'unreported'}"
            screen_line(screen, height - 4, terminal_text(identity), curses.A_BOLD, 1)
            if event["task_id"]:
                screen_line(screen, height - 3, f"Task {event['task_id']}  revision {event['revision']}", column=1)
        else:
            screen_line(screen, 5, "Waiting for matching activity…", column=1)
        screen_line(screen, height - 1, " ↑/↓ Inspect   End Latest   Space Pause   f Filter   g Graph   q Quit ", curses.A_REVERSE)
        key = screen.getch()
        if key in (ord("q"), 27):
            return
        if key == ord(" "):
            paused = not paused
        elif key == ord("f"):
            selected_filter = (selected_filter + 1) % len(filters)
            selected_id = None
        elif key == ord("g") and filtered:
            event = filtered[selected]
            if event["task_id"] or event["kind"] == "route":
                if canvas:
                    return event["task_id"], event["id"]
                if not graph_dashboard(screen, store, event["task_id"], event["id"]):
                    return
        elif key == curses.KEY_END:
            selected_id = None
        elif filtered and key in (curses.KEY_UP, curses.KEY_DOWN):
            index = max(0, min(len(filtered) - 1, selected + (-1 if key == curses.KEY_UP else 1)))
            selected_id = filtered[index]["id"]


def monitor(path, tail, once, plain=False, task_id=None, classic=False):
    if not 0 <= tail <= 1000:
        raise ValueError("--tail must be between 0 and 1000")
    store = Store(path)
    with contextlib.closing(store.db) as db:
        if not once and not plain:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
            if not classic and interactive and os.environ.get("TERM") not in (None, "", "dumb"):
                try:
                    import textual
                except ImportError:
                    textual = None
                if textual is not None:
                    selected = (task_id, None) if task_id else None
                    while True:
                        if selected:
                            if not textual_graph(store, *selected).run():
                                return
                        if not curses:
                            break
                        try:
                            selected = curses.wrapper(monitor_dashboard, store, tail, None, True)
                        except KeyboardInterrupt:
                            return
                        except curses.error:
                            break
                        if not selected:
                            return
            if terminal_dashboard(monitor_dashboard, store, tail, task_id):
                return
        if task_id:
            print("\n".join(map(terminal_text, task_graph(store, task_id))))
            return
        recent, cursor = recent_events(db, tail, 0)
        for event in recent:
            print_event(event)
        if once:
            return
        print("Watching Deleg8r activity. Press Ctrl-C to stop.", flush=True)
        try:
            while True:
                batch = db.execute(
                    "SELECT * FROM activity WHERE id>? ORDER BY id LIMIT 200", (cursor,)
                ).fetchall()
                for event in batch:
                    print_event(event)
                    cursor = event["id"]
                if len(batch) < 200:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


def main():
    if sys.version_info < (3, 8):
        raise ValueError("Python 3.8 or newer is required")
    parser = argparse.ArgumentParser(prog=APP, description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--bin-dir", type=Path, default=default_bin())
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("_install")
    install.add_argument("--source", required=True, type=Path)
    install.add_argument("--revision")
    install.add_argument("--no-setup", action="store_true")
    install.add_argument("--update", action="store_true")
    commands.add_parser("update", help="Install the latest version from GitHub")
    commands.add_parser("version", help="Show the installed source revision")
    statistics = commands.add_parser("stats", help="Show terminal charts of delegation activity")
    watch = commands.add_parser("monitor", help="Watch delegation events and agent handoffs")
    for view in (statistics, watch):
        view.add_argument("--db", type=Path, help="Use a project-specific delegation database")
        view.add_argument("--plain", action="store_true", help="Use plain-text output in a terminal")
    watch.add_argument("--tail", type=int, default=20, help="Recent events to show before following (default: 20)")
    watch.add_argument("--classic", action="store_true", help="Use the classic terminal graph")
    watch.add_argument("--task", help="Open the dependency graph for this task ID")
    watch.add_argument("--once", action="store_true", help="Print recent events and exit")
    export = commands.add_parser("export", help="Export one task and its reviews as JSON")
    export.add_argument("task_id", help="Task ID from the monitor or MCP status")
    export.add_argument("--db", type=Path, help="Use a project-specific delegation database")
    configuration = commands.add_parser("setup", help="Alias for hook")
    configuration.add_argument("--client", choices=("auto", "claude", "codex"), default="auto")
    hook = commands.add_parser("hook", help="Connect Claude Code, Codex, or both")
    hook.add_argument("target", nargs="?", choices=("claude", "codex"))
    # Existing installations invoke this exact form for UserPromptSubmit.
    hook.add_argument("--client", choices=("claude", "codex"), help=argparse.SUPPRESS)
    detach = commands.add_parser("unhook", help="Remove deleg8r's client registration and hooks")
    detach.add_argument("target", nargs="?", choices=("claude", "codex"))
    diagnostics = commands.add_parser("doctor", help="Check installation, client wiring and login")
    diagnostics.add_argument("target", nargs="?", choices=("claude", "codex"))
    diagnostics.add_argument("--live", action="store_true", help="Also run a small live model request")
    for action in ("serve", "pre-tool"):
        sub = commands.add_parser(action)
        sub.add_argument("--client", required=True, choices=("claude", "codex"))
    args = parser.parse_args()
    root, bin_dir = args.root, args.bin_dir
    release = Path(__file__).resolve().parent
    if args.action == "_install":
        revision = args.revision
        if revision is None:
            # An installed older version updates by cloning the repository.
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source, text=True).strip()
        revision = install_release(args.source, revision, root, bin_dir)
        launcher = bin_dir / APP
        print(f"{'Updated' if args.update else 'Installed'} deleg8r {revision[:12]} at {launcher}", flush=True)
        try:
            configure_shell_path(bin_dir)
        except (OSError, ValueError) as error:
            print(f"deleg8r: {error}. Add {bin_dir} to PATH or run {launcher} directly.", file=sys.stderr)
        else:
            if not shutil.which(APP):
                print("Open a new terminal to use deleg8r from PATH.")
        if not args.no_setup:
            setup(root, bin_dir, "auto")
    elif args.action == "update":
        env = dict(os.environ, DELEG8R_INSTALL_DIR=str(root), DELEG8R_BIN_DIR=str(bin_dir),
                   DELEG8R_PYTHON=sys.executable)
        os.execve("/bin/sh", ["sh", str(release / "install.sh"), "--update", "--no-setup"], env)
    elif args.action == "version":
        print((release / "revision").read_text(encoding="utf-8").strip())
    elif args.action in ("stats", "monitor"):
        database = args.db.expanduser() if args.db else root / "state.sqlite3"
        if args.action == "stats":
            stats(database, args.plain)
        else:
            monitor(database, args.tail, args.once, args.plain, args.task, args.classic)
    elif args.action == "export":
        database = args.db.expanduser() if args.db else root / "state.sqlite3"
        store = Store(database)
        with contextlib.closing(store.db):
            print(json.dumps(store.export_task(args.task_id), indent=2, ensure_ascii=False))
    elif args.action == "setup":
        setup(root, bin_dir, args.client)
    elif args.action == "hook" and not args.client:
        setup(root, bin_dir, args.target or "auto")
    elif args.action == "unhook":
        unhook(root, bin_dir, args.target or "auto")
    elif args.action == "doctor":
        sys.exit(0 if doctor(root, bin_dir, release, args.target or "auto", args.live) else 1)
    else:
        command = [sys.executable, str(release / "delegate.py"), args.action,
                   "--client", args.client, "--db", str(root / "state.sqlite3")]
        models = root / "models.json"
        if models.is_file():
            command.extend(["--models", str(models)])
        os.execv(sys.executable, command)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"deleg8r: {exc}", file=sys.stderr)
        sys.exit(1)
