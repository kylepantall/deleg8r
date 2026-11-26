"""Installer lifecycle checks with disposable source archives and client settings."""

import hashlib
import json
import os
import shutil
import selectors
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from delegate import call_tool
from installer import FILES
from store import Store


SOURCE = Path(__file__).resolve().parent


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="deleg8r test ")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name).resolve()
        home = folder / "home"
        home.mkdir()
        self.codex = home / "codex"
        self.claude = home / "claude"
        self.source = folder / "source"
        self.source.mkdir()
        self.archives = folder / "archives"
        self.archives.mkdir()
        self.revision_file = folder / "revision.json"
        self.state = home / "app"
        self.launcher = home / "bin" / "deleg8r"
        self.env = dict(
            os.environ, HOME=str(home), CODEX_HOME=str(self.codex), CLAUDE_CONFIG_DIR=str(self.claude),
            XDG_CACHE_HOME=str(home / "cache"), XDG_CONFIG_HOME=str(home / "config"),
            DELEG8R_REVISION_URL=self.revision_file.as_uri(), DELEG8R_ARCHIVE_BASE=self.archives.as_uri(),
            DELEG8R_INSTALL_DIR=str(self.state), DELEG8R_BIN_DIR=str(self.launcher.parent),
            DELEG8R_PYTHON=os.environ.get("DELEG8R_TEST_PYTHON", sys.executable), PYTHONDONTWRITEBYTECODE="1",
        )
        self.env["SHELL"] = "/bin/sh"
        self.env.pop("ZDOTDIR", None)
        for name in FILES:
            shutil.copy2(SOURCE / name, self.source / name)
        self.original = self.publish("Initial fixture")

    def run_command(self, *argv, check=True, input=None):
        return subprocess.run(argv, env=self.env, cwd=self.env["HOME"], input=input,
                              text=True, capture_output=True, check=check, timeout=30)

    def publish(self, message):
        revision = hashlib.sha1(message.encode()).hexdigest()
        with tarfile.open(self.archives / revision, "w:gz") as archive:
            for name in FILES:
                archive.add(self.source / name, arcname=f"deleg8r-{revision}/{name}")
        self.revision_file.write_text(json.dumps({"sha": revision}))
        return revision

    def install(self, *flags):
        return self.run_command("sh", "-s", "--", *flags, input=(SOURCE / "install.sh").read_text())

    def test_install_update_and_failed_upgrade(self):
        self.install("--no-setup")
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), self.original)
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-11-25"}}
        response = self.run_command(self.launcher, "serve", "--client", "codex",
                                    input=json.dumps(request) + "\n")
        self.assertEqual(json.loads(response.stdout)["result"]["serverInfo"]["name"], "task-delegation")
        db = self.state / "state.sqlite3"
        before = db.read_bytes()
        custom = self.state / "models.json"
        custom.write_text("{}\n")
        (self.source / "README.md").write_text("Updated fixture\n")
        updated = self.publish("Update fixture")
        # The installed launcher retains its paths when update is called from an ordinary shell.
        self.env.pop("DELEG8R_INSTALL_DIR")
        self.env.pop("DELEG8R_BIN_DIR")
        self.run_command(self.launcher, "update")
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), updated)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(custom.read_text(), "{}\n")

        (self.source / "models.py").write_text("broken syntax =\n")
        self.publish("Broken release")
        failed = self.run_command(self.launcher, "update", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), updated)

    def test_install_configures_path_in_fresh_shells(self):
        home = Path(self.env["HOME"])
        self.launcher = home / "bin with 'quotes' and [brackets]" / "deleg8r"
        self.env["DELEG8R_BIN_DIR"] = str(self.launcher.parent)
        for shell, startup, modes in (
            ("bash", ".bash_login", ("-lic", "-ic")),
            ("zsh", "zsh-config/.zshrc", ("-lic", "-ic")),
            ("fish", "config/fish/config.fish", ("-lic", "-ic")),
            ("sh", ".profile", ("-lic",)),
        ):
            binary = shutil.which(shell)
            if not binary:
                continue
            with self.subTest(shell=shell):
                self.env["SHELL"] = binary
                self.env["ZDOTDIR"] = str(home / "zsh-config")
                profile = home / startup
                profile.parent.mkdir(parents=True, exist_ok=True)
                original = b"# Existing user settings"
                profile.write_bytes(original)
                profile.chmod(0o640)
                self.install("--no-setup")
                configured = profile.read_bytes()
                self.assertTrue(configured.startswith(original + b"\n"))
                self.assertEqual(profile.stat().st_mode & 0o777, 0o640)
                profile.write_bytes(original)
                self.run_command(self.launcher, "update")
                self.assertEqual(profile.read_bytes(), configured)
                self.run_command(self.launcher, "update")
                self.assertEqual(profile.read_bytes(), configured)
                for mode in modes:
                    search = "-s" if shell == "fish" else "-v"
                    command = f"command {search} deleg8r && deleg8r version"
                    output = self.run_command(binary, mode, command).stdout.splitlines()
                    self.assertEqual(output, [str(self.launcher), self.original])

    def report_activity(self, store, task, status, **details):
        return call_tool(store, {}, "codex", "report", {
            "thread": "test", "task_id": task["id"], "revision": 1,
            "step_id": "implement", "status": status, "evidence": "private evidence", **details,
        })

    def test_stats_charts_follow_task_progress(self):
        self.install("--no-setup")
        initial = self.run_command(self.launcher, "stats").stdout
        self.assertIn("0 tasks", initial)
        self.assertRegex(initial, r"completed +0")
        store = Store(self.state / "state.sqlite3")
        self.addCleanup(store.db.close)
        store.set_prompt_gate("test", "turn", True, client="codex", route="focused")
        task = store.create_task("test", "codex", "focused", {
            "steps": [{"id": "implement", "mode": "write", "needs": []}],
        })
        self.report_activity(store, task, "running", agent_name="Dev Agent")
        self.report_activity(store, task, "done")
        output = self.run_command(self.launcher, "stats").stdout
        for label in ("completed", "focused", "codex", "write", "Dev Agent"):
            self.assertRegex(output, rf"{label} +1 +#")
        self.assertNotIn("private evidence", output)

    def test_monitor_follows_reports_and_keeps_agent_names(self):
        self.install("--no-setup")
        database = self.state / "state.sqlite3"
        store = Store(database)
        self.addCleanup(store.db.close)
        task = store.create_task("test", "codex", "focused", {
            "steps": [{"id": "implement", "mode": "write", "needs": []}],
        })
        with subprocess.Popen([self.launcher, "monitor", "--tail", "0"], env=self.env,
                              stdout=subprocess.PIPE, text=True) as monitor:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(monitor.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(5), "Monitor did not start")
                    self.assertIn("Watching Deleg8r activity", monitor.stdout.readline())
                    self.report_activity(store, task, "running", agent_name="Dev Agent")
                    self.assertTrue(selector.select(5), "Monitor did not receive the live handoff")
                    line = monitor.stdout.readline()
                    for value in (task["id"], "implement", "running", "agent=Dev Agent"):
                        self.assertIn(value, line)
                    self.assertNotIn("private evidence", line)
                    self.report_activity(store, task, "done")
                    self.assertTrue(selector.select(5), "Monitor did not receive completion")
                    self.assertIn("agent=Dev Agent", monitor.stdout.readline())
            finally:
                monitor.terminate()
                monitor.wait(timeout=5)
        output = self.run_command(self.launcher, "monitor", "--once", "--tail", "1").stdout
        self.assertEqual(len(output.splitlines()), 1)
        self.assertIn("done", output)

    def test_activity_cli_opens_legacy_database(self):
        self.install("--no-setup")
        database = self.state / "state.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, thread TEXT NOT NULL,
                          client TEXT NOT NULL, tier TEXT NOT NULL, revision INTEGER NOT NULL,
                          generation INTEGER NOT NULL, plan TEXT NOT NULL, reports TEXT NOT NULL)""")
            row = (
                "legacy", "test", "claude", "complex", 1, 2,
                json.dumps({"steps": [{"id": "scout", "needs": [], "mode": "read"}]}),
                json.dumps({"scout": {"status": "done", "evidence": "original"}}),
            )
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", row)
        output = self.run_command(self.launcher, "stats", "--db", database).stdout
        for label in ("completed", "claude", "complex", "read"):
            self.assertRegex(output, rf"{label} +1 +#")
        self.assertEqual(self.run_command(self.launcher, "monitor", "--db", database, "--once").stdout, "")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "status", "arguments": {"thread": "test", "task_id": "legacy"},
        }}
        response = self.run_command(self.launcher, "serve", "--client", "claude",
                                    input=json.dumps(request) + "\n")
        status = json.loads(json.loads(response.stdout)["result"]["content"][0]["text"])
        self.assertEqual(status["plan"], json.loads(row[6]))
        self.assertEqual(status["reports"], json.loads(row[7]))
        self.assertEqual(db.execute("SELECT * FROM tasks").fetchone(), row)
        db.close()

    def test_discovers_versioned_python_without_git(self):
        commands = Path(self.env["HOME"]) / "commands"
        commands.mkdir()
        for command in ("sh", "curl", "mktemp", "rm"):
            (commands / command).symlink_to(shutil.which(command))
        unsupported = commands / "python3"
        unsupported.write_text("#!/bin/sh\nexit 1\n")
        unsupported.chmod(0o755)
        (commands / "python3.10").symlink_to(self.env.pop("DELEG8R_PYTHON"))
        self.env["PATH"] = str(commands)
        self.install("--no-setup")
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), self.original)

    @unittest.skipUnless(shutil.which("claude") and shutil.which("codex"), "needs both real client CLIs")
    def test_setup_preserves_configuration_and_is_idempotent(self):
        self.claude.mkdir()
        self.codex.mkdir()
        claude_settings = self.claude / "settings.json"
        codex_hooks = self.codex / "hooks.json"
        codex_config = self.codex / "config.toml"
        unrelated = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]}}
        for path in (claude_settings, codex_hooks):
            path.write_text(json.dumps(unrelated))
        codex_config.write_text('model = "gpt-6.1-sol"\n[features]\nundo = false\n')
        self.install()
        self.run_command(self.launcher, "setup")
        for path in (claude_settings, codex_hooks):
            settings = json.loads(path.read_text())["hooks"]
            self.assertEqual(settings["UserPromptSubmit"][0], unrelated["hooks"]["UserPromptSubmit"][0])
            self.assertEqual(len(settings["UserPromptSubmit"]), 2)
            self.assertEqual(len(settings["PreToolUse"]), 1)
        config = tomllib.loads(codex_config.read_text())
        self.assertEqual(config["model"], "gpt-6.1-sol")
        self.assertEqual(config["features"], {"undo": False, "hooks": True})
        server = config["mcp_servers"]["task_delegation"]
        self.assertEqual(server["command"], str(self.launcher))
        self.assertEqual(server["tool_timeout_sec"], 135)
        self.assertEqual(
            json.loads((self.claude / ".claude.json").read_text())["mcpServers"]["task_delegation"]["command"],
            str(self.launcher),
        )

    @unittest.skipUnless(shutil.which("claude") and shutil.which("codex"), "needs both real client CLIs")
    def test_doctor_hook_and_unhook(self):
        self.install("--no-setup")
        self.codex.mkdir()
        self.run_command("claude", "mcp", "add", "--scope", "user", "other", "--", "/bin/true")
        self.run_command("codex", "mcp", "add", "other", "--", "/bin/true")
        self.run_command(self.launcher, "hook")
        claude_registry = self.claude / ".claude.json"
        codex_config = self.codex / "config.toml"
        codex_hooks = self.codex / "hooks.json"
        claude_settings = self.claude / "settings.json"
        paths = (claude_registry, claude_settings, codex_config, codex_hooks)
        before = {path: path.read_bytes() for path in paths}
        report = self.run_command(self.launcher, "doctor", check=False)
        for client in ("claude", "codex"):
            self.assertIn(f"OK {client} MCP registration", report.stdout)
            self.assertIn(f"OK {client} hook registration (2/2 user hooks)", report.stdout)
            self.assertIn(f"OK {client} local MCP handshake", report.stdout)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

        event = {"hook_event_name": "UserPromptSubmit", "session_id": "installer-test",
                 "prompt": "Fix a typo in README.md."}
        legacy = self.run_command(self.launcher, "hook", "--client", "codex", input=json.dumps(event))
        self.assertIn("route direct", json.loads(legacy.stdout)["hookSpecificOutput"]["additionalContext"])
        database = self.state / "state.sqlite3"
        database_content = database.read_bytes()

        # Test a user hook in the same group as one of ours.
        previous = json.loads(codex_hooks.read_text())
        retained = {"type": "command", "command": "true"}
        previous["hooks"]["PreToolUse"][0]["hooks"].append(retained)
        codex_hooks.write_text(json.dumps(previous))
        self.run_command(self.launcher, "unhook", "codex")
        config = tomllib.loads(codex_config.read_text())
        self.assertEqual(set(config["mcp_servers"]), {"other"})
        self.assertTrue(config["features"]["hooks"])
        self.assertEqual(json.loads(codex_hooks.read_text())["hooks"]["PreToolUse"][0]["hooks"], [retained])
        self.assertIn("task_delegation", json.loads(claude_registry.read_text())["mcpServers"])
        detached = self.run_command(self.launcher, "doctor", "codex", check=False)
        self.assertEqual(detached.returncode, 1)
        self.assertIn("FAIL codex MCP registration", detached.stdout)
        self.assertIn("FAIL codex hook registration (0/2 user hooks)", detached.stdout)

        self.run_command(self.launcher, "hook", "codex")
        self.run_command(self.launcher, "unhook")
        self.run_command(self.launcher, "unhook")
        self.assertEqual(set(json.loads(claude_registry.read_text())["mcpServers"]), {"other"})
        self.assertEqual(set(tomllib.loads(codex_config.read_text())["mcp_servers"]), {"other"})
        self.assertEqual(database.read_bytes(), database_content)


if __name__ == "__main__":
    unittest.main()
