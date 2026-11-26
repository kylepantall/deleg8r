"""Install, configure and update deleg8r from its public source repository."""

import argparse
import contextlib
import fcntl
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
from collections import Counter
from datetime import datetime
from pathlib import Path

from delegate import TRIAGE, hook_configuration
from models import ROUTE, ask, model_settings
from store import Store


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


def stats(path):
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

    print(f"Deleg8r activity ({sum(tiers.values())} tasks)")
    chart("Recorded prompt routes", routes)
    chart("Task tiers", tiers)
    chart("Task status", task_states)
    chart("Tasks by client", clients)
    chart("Current steps by mode", modes)
    chart("Current steps by reported agent", agents)


def print_event(event):
    stamp = datetime.fromtimestamp(event["created"]).astimezone().isoformat(timespec="seconds")
    parts = [stamp, event["client"], event["kind"], event["detail"]]
    if event["task_id"]:
        parts.extend((event["task_id"], f"r{event['revision']}"))
    if event["step_id"]:
        agent = event["agent_name"] or "unreported"
        parts.extend((event["step_id"], f"agent={agent}"))
    print("  ".join(map(terminal_text, parts)), flush=True)


def monitor(path, tail, once):
    if not 0 <= tail <= 1000:
        raise ValueError("--tail must be between 0 and 1000")
    with contextlib.closing(Store(path).db) as db:
        cursor = db.execute("SELECT coalesce(max(id),0) FROM activity").fetchone()[0]
        recent = db.execute(
            "SELECT * FROM activity WHERE id<=? ORDER BY id DESC LIMIT ?", (cursor, tail)
        ).fetchall()
        for event in reversed(recent):
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
    watch.add_argument("--tail", type=int, default=20, help="Recent events to show before following (default: 20)")
    watch.add_argument("--once", action="store_true", help="Print recent events and exit")
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
            stats(database)
        else:
            monitor(database, args.tail, args.once)
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
