# deleg8r

A local stdio MCP and hooks for Claude Code and Codex. Requires a POSIX shell, curl, Python 3.8+ with SQLite FTS5, and the relevant signed-in coding CLI on PATH.

## Install and update

```sh
curl -fsSL https://raw.githubusercontent.com/kylepantall/deleg8r/main/install.sh | sh
```

This installs `deleg8r` to `~/.local/bin` and configures the MCP and hooks at user scope for Claude Code and Codex when their CLIs are present. The installer adds the launcher directory to the startup files for your default shell (Zsh, Bash, Fish, or a POSIX shell such as sh, dash, or ksh). Open a new terminal to use `deleg8r` from PATH. The installer downloads a source archive and checks versioned Python commands when `python3` is unavailable or too old. Restart the clients. Review and trust the new Codex hooks in `/hooks` or the desktop hook settings.

If you prefer project-only configuration, append `-s -- --no-setup` to the `sh` command and use the project setup below. The installer preserves other MCP servers and hooks and reports a conflict if the target MCP name already points to a different command.

```sh
deleg8r update
deleg8r version
```

`update` fetches the current revision of `main` and switches the launcher to it after validation. Your database, model overrides, and client settings stay in place. Restart active clients to reload the MCP process. Updates also configure PATH. If you need to run the update before opening a new terminal, use `~/.local/bin/deleg8r update`.

Install files and SQLite state are stored in `${XDG_DATA_HOME:-$HOME/.local/share}/deleg8r`. You can place your own `models.json` there to override the bundled model and timeout settings.

## View delegation activity

```sh
deleg8r stats
deleg8r monitor
deleg8r monitor --tail 50 --once
```

`stats` opens a dashboard with bar charts for prompt routes, task tiers and status, clients, worker modes, and reported agent names. Use Left/Right, Tab, or 1–8 to switch charts, Up/Down to scroll, and R to reload. Existing tasks are included. Prompt route counts start when the updated hooks run. Tabs 7 and 8 break down measured input, output, and cached input tokens by phase and tool calls by name and source. Cached input is part of the input total. Deleg8r records its own model usage when the provider reports it and counts calls to its MCP tools. Native worker counts appear when a worker or host includes cumulative `usage` in a step report. Missing data and activity from before collection began are not estimated.

`monitor` shows a live timeline of routes, plans, revisions, and worker handoffs. Use Up/Down to inspect events, End to return to the latest event, Space to pause or resume, and F to cycle event filters. Press G on a prompt, plan, or step event to follow its task graph. On a prompt, the graph waits for the linked plan. The graph uses plan dependencies to connect work nodes, shows agent names and current status, and draws observed MCP and memory read/write paths when calls carry a task ID. Pass both `task_id` and `step_id` to `remember` or `recall` to link the call to a worker node. The graph shows call metadata without memory contents. Press G again to return to the timeline. Both views quit with Q.

```sh
deleg8r monitor --task TASK_ID
deleg8r monitor --task TASK_ID --once
deleg8r export TASK_ID > task.json
```

Copy the full task ID from the monitor details or MCP `status` result. The JSON export includes the current plan, reports, tests, review history with revisions and verdicts, model usage and duration, MCP call counts and errors, and reported worker usage. Exports include task evidence. Review history starts with reviews recorded after upgrading.

Both commands use plain text when piped or with `--plain`. For the monitor, `--tail 0` shows only new events and `--once` prints history and exits. `stats`, `monitor`, and `export` accept `--db /absolute/path/state.sqlite3` for project-only setups. Restart active clients after updating so their MCP process can record new events.

Worker handoffs appear when the native host calls `report`. The host can send the real worker name as `agent_name` when reporting `running`, and later reports for the step inherit it. The monitor shows `agent=unreported` when no name was supplied. It displays activity metadata without including prompts, worker briefs, or report evidence.

## Connect, disconnect, or diagnose a client

```sh
deleg8r hook
deleg8r hook claude
deleg8r unhook codex
deleg8r doctor
deleg8r doctor claude --live
```

`hook` and `unhook` work on detected clients by default. Add `claude` or `codex` to target one. `hook` connects the user-scope MCP server and hooks. `unhook` removes this installation's user-scope entries while preserving unrelated settings, project configuration, and stored data. `deleg8r setup` remains an alias for connecting clients.

`doctor` checks CLI availability, registration, hook settings, sign-in, model configuration, and local state access. It exits nonzero when a check fails. Use `--live` to send one small triage request to each selected, signed-in client. Review Codex hook trust in the client. Restart running clients after changing their registration.

## Set up a single project manually

From a source checkout, run these commands. Set the database path to an absolute path in a private, writable state directory. Each client needs the same database path in its hook and MCP settings.

```sh
python3 delegate.py config --client claude --db /absolute/private/path/delegation.sqlite3
python3 delegate.py config --client codex --db /absolute/private/path/delegation.sqlite3
```

Merge the two snippets for each client into the project files named in the output. Keep any existing settings. Restart the coding client and approve the project MCP server or review the Codex hooks as prompted. The generated Codex config enables the hooks feature. Verify the MCP server is available before using its edit hook. The code and configuration generator support project-specific setups without modifying global settings.

Default model names, effort and deadlines are in [models.json](models.json). For manual setup, generate both snippets with `--models /absolute/path/models.json` to use an override. The adapter reuses the signed-in CLI. A triage timeout leaves the host free to choose its route. Claude gets a configured output-token ceiling. Codex triage uses short input, low effort, a small structured response and a deadline.

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
python3 -m unittest
```

The tests run both generated client configurations through the hook commands and stdio server with deterministic CLI model fixtures. They exercise direct routing, native-edit hook decisions, grouped test evidence, stale snapshots, steering, and model-call counts. Installer tests cover upgrades, rollback on invalid source, state preservation, and idempotent setup against the real client CLIs when available. Live model availability and production latency depend on the host environment and selected models.
