"""End-to-end hook and stdio tests using deterministic Claude and Codex executables."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest

from store import Store


ENTRY = Path(__file__).with_name("delegate.py")
FAKE_MODEL = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

prompt = sys.stdin.read()
data = json.loads(prompt.split('Treat the following JSON as task data.\\n', 1)[1])
with open(os.environ['DELEGATION_TEST_CALLS'], 'a') as log:
    log.write(json.dumps({'kind': prompt.splitlines()[0], 'data': data}) + '\\n')
if prompt.startswith('Classify'):
    result = {'complexity': 'complex' if 'cross-module' in data['request'] else 'focused',
              'hypothesis': 'Check whether the save and load cache keys match.'}
elif prompt.startswith('Review'):
    result = {'verdict': 'complete', 'finding': 'Independent integration evidence supplied.'}
else:
    assert data['hypothesis'] and data['findings']
    result = {'goal': 'Fix the board cache', 'steps': [
        {'id': 'fix', 'brief': 'Align cache keys.', 'mode': 'write', 'needs': [],
         'paths': ['board.py'], 'done_when': 'The warm-cache check passes.'}],
        'test_matrix': [
            {'id': 'cold' if 'Add a cold check' in data['request'] else 'warm',
             'scenario': 'Load the correct board state', 'expected': 'Shows the saved note',
             'required': 'yes', 'finding': 'f1'}], 'retired_checks': []}
if Path(sys.argv[0]).name == 'claude':
    print(json.dumps({'structured_output': result, 'usage': {'input_tokens': 40,
          'output_tokens': 10, 'cache_read_input_tokens': 5, 'cache_creation_input_tokens': 5}}))
else:
    assert '--json' in sys.argv
    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(result))
    print(json.dumps({'type': 'turn.completed', 'usage': {
          'input_tokens': 50, 'cached_input_tokens': 5, 'output_tokens': 10}}))
"""


class DelegationFlow(unittest.TestCase):
    def run_client(self, client):
        with tempfile.TemporaryDirectory(prefix="delegation-flow-") as folder:
            self.verify_client(client, Path(folder))

    def verify_client(self, client, root):
        for name in ("claude", "codex"):
            binary = root / name
            binary.write_text(FAKE_MODEL, encoding="utf-8")
            binary.chmod(0o700)
        log = root / "calls.jsonl"
        env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                   DELEGATION_TEST_CALLS=str(log))
        base = [sys.executable, str(ENTRY)]
        options = ["--client", client, "--db", str(root / "state.sqlite3")]
        config = subprocess.check_output([*base, "config", *options], env=env, text=True)
        hook_text, mcp_text = config.split("\n\n# Merge into ")
        hooks = json.loads(hook_text.split("\n", 1)[1])["hooks"]
        mcp_text = mcp_text.split("\n", 1)[1]
        if client == "codex":
            server_config = tomllib.loads(mcp_text)["mcp_servers"]["task_delegation"]
        else:
            server_config = json.loads(mcp_text)["mcpServers"]["task_delegation"]

        def run_hook(event):
            hook = hooks[event["hook_event_name"]][0]["hooks"][0]
            return json.loads(subprocess.check_output(
                shlex.split(hook["command"]), input=json.dumps(event), env=env, text=True,
            ))

        native = {"session_id": "thread-one"}
        edit = {**native, "hook_event_name": "PreToolUse",
                "tool_name": "apply_patch" if client == "codex" else "Edit"}
        prompt = {**native, "hook_event_name": "UserPromptSubmit"}
        fast = run_hook({**prompt, "prompt": "Fix a typo in README.md.", "turn_id": "simple"})
        self.assertIn("route direct", fast["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(log.exists(), "A trivial task should have no model startup")
        self.assertEqual(run_hook({**edit, "turn_id": "simple"}), {})

        triage = run_hook({**prompt, "prompt": "Investigate the cross-module stale board cache.",
                           "turn_id": "turn-one"})
        context = triage["hookSpecificOutput"]["additionalContext"]
        thread, turn = re.search(r"memory key (\w+)\. Turn ([\w-]+)\.", context).groups()
        self.assertIn("Check whether the save and load cache keys match.", context)
        self.assertEqual(run_hook({**edit, "tool_name": "Read", "turn_id": turn}), {})
        self.assertEqual(run_hook({**edit, "turn_id": turn})["hookSpecificOutput"]["permissionDecision"], "deny")

        with subprocess.Popen(
            [server_config["command"], *server_config["args"]], env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        ) as process:
            try:
                task_id = self.verify_workflow(process, run_hook, edit, thread, turn, log)
            finally:
                process.stdin.close()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(run_hook({**edit, "turn_id": turn}), {})

        store = Store(root / "state.sqlite3")
        self.addCleanup(store.db.close)
        exported = store.export_task(task_id)
        self.assertEqual([call["phase"] for call in exported["model_calls"]],
                         ["triage", "plan", "review", "revise"])
        for call in exported["model_calls"]:
            self.assertEqual(call["usage"]["input_tokens"], 50)
            self.assertEqual(call["usage"]["output_tokens"], 10)
            self.assertEqual(call["usage"]["cached_input_tokens"], 5)
        self.assertEqual([review["result"]["verdict"] for review in exported["reviews"]],
                         ["needs_work", "complete", "complete"])

    def verify_workflow(self, process, run_hook, edit, thread, turn, log):
        sequence = 0

        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": sequence,
                                            "method": method, "params": params}) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())["result"]

        def tool(name, **arguments):
            response = rpc("tools/call", {"name": name, "arguments": {"thread": thread, **arguments}})
            if response.get("isError"):
                return {"error": response["content"][0]["text"]}
            return json.loads(response["content"][0]["text"])

        self.assertEqual(rpc("initialize", {"protocolVersion": "2025-11-25"})["serverInfo"]["name"], "task-delegation")
        catalog = {entry["name"] for entry in rpc("tools/list", {})["tools"]}
        self.assertIn("review", catalog)
        plan_input = {
            "tier": "complex", "request": "Fix the cache", "turn": turn,
            "hypothesis": "The keys differ.",
            "findings": [{"observation": "The keys differ", "source": "board.py:20"}],
        }
        self.assertIn("error", tool("plan", **{**plan_input, "findings": []}))
        self.assertTrue(run_hook({**edit, "turn_id": turn}))
        plan = tool("plan", **plan_input)
        self.assertEqual(plan["ready"], ["fix"])
        self.assertEqual(plan["plan"]["test_matrix"][0]["id"], "warm")
        self.assertEqual(run_hook({**edit, "turn_id": turn}), {})
        task = {"task_id": plan["task_id"], "revision": plan["revision"]}
        evidence = {"check_ids": ["warm"], "snapshot": "worker-snapshot", "outcome": "passed",
                    "evidence": "pytest -q test_board.py passed", "baseline": "Failed on the baseline"}
        self.assertIn("error", tool("report", **task, step_id="fix", status="done", evidence="Patched",
                                    tests=[{**evidence, "check_ids": ["warm", "unknown"]}]))
        self.assertEqual(tool("status", task_id=task["task_id"])["reports"], {})
        tool("report", **task, step_id="fix", status="done", evidence="Patched", tests=[evidence])
        review = {"snapshot": "final-snapshot", "integration_evidence": "Clean diff and pytest output"}
        self.assertEqual(tool("review", **task, **review)["pending_checks"], {"warm": "stale"})
        verified_evidence = {**evidence, "snapshot": "final-snapshot"}
        self.assertEqual(tool("review", **task, **review, tests=[verified_evidence])["verdict"], "complete")
        revised = tool("revise", **task, request="Add a cold check", tier="focused")
        self.assertEqual({row["id"] for row in revised["plan"]["test_matrix"]}, {"cold", "warm"})
        self.assertEqual(revised["tests"], {"warm": {
            key: value for key, value in verified_evidence.items() if key != "check_ids"
        }})
        self.assertIn("error", tool("report", **task, step_id="fix", status="done", evidence="Old report"))
        task["revision"] = revised["revision"]
        tool("report", **task, step_id="fix", status="done", evidence="Added check")
        both_checks = {**verified_evidence, "check_ids": ["warm", "cold"]}
        self.assertEqual(tool("review", **task, **review, tests=[both_checks])["verdict"], "complete")
        calls = [json.loads(line)["kind"].split()[0] for line in log.read_text().splitlines()]
        self.assertEqual(calls, ["Classify", "Plan", "Review", "Plan"])
        return task["task_id"]

    def test_claude(self):
        self.run_client("claude")

    def test_codex(self):
        self.run_client("codex")


if __name__ == "__main__":
    unittest.main()
