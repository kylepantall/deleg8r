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
import tomllib
from pathlib import Path

from delegate import hook_configuration


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


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path}")
    return data


def install_release(source, root, bin_dir):
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        except sqlite3.OperationalError as error:
            raise ValueError("Python's SQLite library must support FTS5") from error
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
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


def settings_with_hooks(path, launcher, client):
    data = read_json(path)
    events = data.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise ValueError(f"Invalid hooks object in {path}")
    for event, (group,) in hook_configuration([str(launcher)], ["--client", client]).items():
        command = group["hooks"][0]["command"]
        entries = events.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"Invalid {event} hooks in {path}")
        installed = any(
            isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
            and any(isinstance(hook, dict) and hook.get("command") == command for hook in entry["hooks"])
            for entry in entries
        )
        if not installed:
            entries.append(group)
    return data


def matches_server(existing, launcher, client):
    return (
        isinstance(existing, dict) and existing.get("command") == str(launcher)
        and existing.get("args") == ["serve", "--client", client]
    )


def register_claude(root, launcher):
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(configured).expanduser() if configured else Path.home() / ".claude"
    registry = home / ".claude.json" if configured else Path.home() / ".claude.json"
    settings_path = home / "settings.json"
    existing = read_json(registry).get("mcpServers", {}).get(SERVER)
    if existing is not None and not matches_server(existing, launcher, "claude"):
        raise ValueError("Claude Code already has a different user MCP server named task_delegation")
    settings = settings_with_hooks(settings_path, launcher, "claude")
    if existing is None:
        subprocess.run(
            ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio", SERVER, "--",
             str(launcher), "serve", "--client", "claude"],
            cwd=root, check=True,
        )
    atomic_text(settings_path, json.dumps(settings, indent=2) + "\n")


def read_toml(path):
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def register_codex(root, launcher):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    config_path = home / "config.toml"
    settings_path = home / "hooks.json"
    config = read_toml(config_path)
    existing = config.get("mcp_servers", {}).get(SERVER)
    if existing is not None and not matches_server(existing, launcher, "codex"):
        raise ValueError("Codex already has a different user MCP server named task_delegation")
    settings = settings_with_hooks(settings_path, launcher, "codex")
    if existing is None:
        subprocess.run(
            ["codex", "mcp", "add", SERVER, "--", str(launcher), "serve", "--client", "codex"],
            cwd=root, check=True,
        )
    if not config.get("features", {}).get("hooks"):
        subprocess.run(["codex", "features", "enable", "hooks"], cwd=root, check=True)
    if read_toml(config_path)["mcp_servers"][SERVER].get("tool_timeout_sec") is None:
        original = config_path.read_text(encoding="utf-8")
        updated, count = re.subn(r"(?m)^\[mcp_servers\.task_delegation\][ \t]*$",
                                 r"\g<0>\ntool_timeout_sec = 135", original, count=1)
        if count != 1:
            raise ValueError(f"Could not set the MCP timeout in {config_path}")
        read = tomllib.loads(updated)
        if read["mcp_servers"][SERVER]["tool_timeout_sec"] != 135:
            raise ValueError("The Codex MCP timeout is invalid")
        atomic_text(config_path, updated)
    atomic_text(settings_path, json.dumps(settings, indent=2) + "\n")


def setup(root, bin_dir, client):
    launcher = bin_dir / APP
    clients = ("claude", "codex") if client == "auto" else (client,)
    configured = []
    failures = []
    for name in clients:
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


def main():
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--bin-dir", type=Path, default=default_bin())
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("_install")
    install.add_argument("--source", required=True, type=Path)
    install.add_argument("--no-setup", action="store_true")
    install.add_argument("--update", action="store_true")
    commands.add_parser("update", help="Install the latest version from GitHub")
    commands.add_parser("version", help="Show the installed Git revision")
    configuration = commands.add_parser("setup", help="Configure detected coding clients")
    configuration.add_argument("--client", choices=("auto", "claude", "codex"), default="auto")
    for action in ("serve", "hook", "pre-tool"):
        sub = commands.add_parser(action)
        sub.add_argument("--client", required=True, choices=("claude", "codex"))
    args = parser.parse_args()
    root, bin_dir = args.root, args.bin_dir
    release = Path(__file__).resolve().parent
    if args.action == "_install":
        revision = install_release(args.source, root, bin_dir)
        launcher = bin_dir / APP
        print(f"{'Updated' if args.update else 'Installed'} deleg8r {revision[:12]} at {launcher}", flush=True)
        if not args.no_setup:
            setup(root, bin_dir, "auto")
        if not shutil.which(APP):
            print(f"Add {bin_dir} to PATH or run {launcher} directly.")
    elif args.action == "update":
        env = dict(os.environ, DELEG8R_INSTALL_DIR=str(root), DELEG8R_BIN_DIR=str(bin_dir),
                   DELEG8R_PYTHON=sys.executable)
        os.execve("/bin/sh", ["sh", str(release / "install.sh"), "--update", "--no-setup"], env)
    elif args.action == "version":
        print((release / "revision").read_text(encoding="utf-8").strip())
    elif args.action == "setup":
        setup(root, bin_dir, args.client)
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
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"deleg8r: {exc}", file=sys.stderr)
        sys.exit(1)
