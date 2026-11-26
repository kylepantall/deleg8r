# Task delegation MCP

A local stdio MCP and project hooks for Claude Code and Codex. Requires Python 3.11+, SQLite with FTS5, and the relevant signed-in coding CLI on PATH. The hook commands target POSIX shells.

## Set up a project

Run these commands from this directory. Set the database path to an absolute path in a private, writable state directory. Each client needs the same database path in its hook and MCP settings.

```sh
python3 delegate.py config --client claude --db /absolute/private/path/delegation.sqlite3
python3 delegate.py config --client codex --db /absolute/private/path/delegation.sqlite3
```

Merge the two snippets for each client into the project files named in the output. Keep any existing settings. Restart the coding client and approve the project MCP server or review the Codex hooks as prompted. The generated Codex config enables the hooks feature. Verify the MCP server is available before using its edit hook. The code and configuration generator support project-specific setups without modifying global settings.

Model names, effort and deadlines are in [models.json](models.json). Generate both snippets with `--models /absolute/path/models.json` to use an override. The adapter reuses the signed-in CLI. A triage timeout leaves the host free to choose its route. Claude gets a configured output-token ceiling. Codex triage uses short input, low effort, a small structured response and a deadline.

## How a request flows

| Existing action | Bundled data |
|---|---|
| Prompt routing | Greeting, thanks and explicit typo-only prompts take the local fast path. Other requests get one lean routing call. A delegated route includes a one-line hypothesis or a question for a scout. |
| `plan` or `revise` | The host brings the hypothesis and sourced findings from read-only investigation. The selected planner returns work assignments and a small matrix together. A matrix row names a scenario and expected result, whether it is required, and the finding it covers when relevant. |
| `report` | A worker's progress can include results with matrix IDs, a code snapshot, an outcome, command or manual-check evidence, and an optional baseline. One result can cover several rows when the same evidence establishes the same outcome for each. |
| `review` | The host bundles final integration evidence and any final test results. The server checks finished steps and current passing results for all required rows locally. It asks the selected model for additional review on complex tasks after those checks pass. |

The native host dispatches workers and relays progress. The configured edit hooks gate common native file-edit tools in the routed chat until a valid plan for the turn is stored. Other execution surfaces, including shell commands and changes in worker sessions with a separate identity, may still be available under the host's permissions. The hook is a workflow checkpoint. Keep scouts read-only and use your host's permission settings for enforcement across execution surfaces.

Pass the turn identifier from the hook context when planning or revising. Keep durable decisions and constraints in `remember` and fetch targeted notes with `recall`. The MCP stores compact records in SQLite. Use a fresh database to clear state between experiments.

When requirements change, revise the plan. Existing matrix rows carry forward unless superseded. An unchanged row keeps its last result, but final review requires a matching snapshot. Use an identifier for the code actually tested, including uncommitted content. The MCP records the host's test evidence and snapshot claims. Run tests through the native host or CI and include actual output or an artifact reference. Missing, failed, skipped and stale required checks leave the task unverified.

## Verify locally

```sh
python3 test_flow.py
```

The tests run both generated client configurations through the real hook commands and stdio server with deterministic CLI model fixtures. They exercise direct routing, native-edit hook decisions, grouped test evidence, stale snapshots, steering, and model-call counts. Live model availability and production latency depend on the host environment and selected models.
