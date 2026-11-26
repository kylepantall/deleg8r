"""Installer lifecycle checks with a disposable Git remote and client settings."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from installer import FILES


SOURCE = Path(__file__).resolve().parent


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="deleg8r test ")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        home = folder / "home"
        home.mkdir()
        self.codex = home / "codex"
        self.claude = home / "claude"
        self.clone = folder / "remote"
        self.clone.mkdir()
        self.state = home / "app"
        self.launcher = home / "bin" / "deleg8r"
        self.env = dict(
            os.environ, HOME=str(home), CODEX_HOME=str(self.codex), CLAUDE_CONFIG_DIR=str(self.claude),
            XDG_CACHE_HOME=str(home / "cache"), XDG_CONFIG_HOME=str(home / "config"),
            DELEG8R_REPO_URL=self.clone.as_uri(), DELEG8R_INSTALL_DIR=str(self.state),
            DELEG8R_BIN_DIR=str(self.launcher.parent), DELEG8R_PYTHON=sys.executable,
            GIT_CONFIG_NOSYSTEM="1", PYTHONDONTWRITEBYTECODE="1",
        )
        self.git("init", "-q", "-b", "main")
        for name in FILES:
            shutil.copy2(SOURCE / name, self.clone / name)
        self.original = self.commit("Initial fixture")

    def run_command(self, *argv, check=True, input=None):
        return subprocess.run(argv, env=self.env, cwd=self.env["HOME"], input=input,
                              text=True, capture_output=True, check=check, timeout=30)

    def git(self, *args):
        return self.run_command("git", "-C", str(self.clone), *args).stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Installer test", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

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
        (self.clone / "README.md").write_text("Updated fixture\n")
        updated = self.commit("Update fixture")
        # The installed launcher retains its paths when update is called from an ordinary shell.
        self.env.pop("DELEG8R_INSTALL_DIR")
        self.env.pop("DELEG8R_BIN_DIR")
        self.run_command(self.launcher, "update")
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), updated)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(custom.read_text(), "{}\n")

        (self.clone / "models.py").write_text("broken syntax =\n")
        self.commit("Broken release")
        failed = self.run_command(self.launcher, "update", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.run_command(self.launcher, "version").stdout.strip(), updated)

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


if __name__ == "__main__":
    unittest.main()
