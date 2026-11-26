"""Bounded, structured calls to the host's noninteractive CLI."""

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path


def object_schema(properties, optional=()):
    return {
        "type": "object",
        "properties": properties,
        "required": [name for name in properties if name not in optional],
        "additionalProperties": False,
    }


def string(maximum, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def choice(*values):
    return {"type": "string", "enum": list(values)}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def integer(minimum, maximum):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def validate(value, schema, label="input"):
    kind = schema["type"]
    expected = {"string": str, "object": dict, "array": list, "integer": int}[kind]
    if type(value) is not expected:
        raise ValueError(f"{label} must be {kind}")
    if kind == "object":
        fields = schema["properties"]
        if value.keys() - fields.keys() or set(schema["required"]) - value.keys():
            raise ValueError(f"{label} has missing or unknown fields")
        for key, item in value.items():
            validate(item, fields[key], f"{label}.{key}")
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError(f"{label} has an invalid number of entries")
        for item in value:
            validate(item, schema["items"], f"{label} entry")
    elif kind == "integer":
        if not schema["minimum"] <= value <= schema["maximum"]:
            raise ValueError(f"{label} is outside its allowed range")
    elif "enum" in schema:
        if value not in schema["enum"]:
            raise ValueError(f"{label} has an unsupported value")
    elif not schema["minLength"] <= len(value.strip()) <= len(value) <= schema["maxLength"]:
        raise ValueError(f"{label} has an invalid length")


def provider_schema(schema):
    # Keep provider-facing constraints portable across the two structured-output APIs.
    supported = {"type", "enum", "properties", "required", "additionalProperties", "items"}
    result = {key: value for key, value in schema.items() if key in supported}
    if "properties" in result:
        result["properties"] = {key: provider_schema(value) for key, value in result["properties"].items()}
    if "items" in result:
        result["items"] = provider_schema(result["items"])
    return result


ROUTE = object_schema({
    "complexity": choice("direct", "focused", "complex"),
    "hypothesis": string(220, 0),
})
FINDING = object_schema({"observation": string(300), "source": string(240)})
CHECK = object_schema({
    "id": string(16), "scenario": string(180), "expected": string(180),
    "required": choice("yes", "no"), "finding": string(16, 0),
})
TEST_RESULT = object_schema({
    "check_ids": array(string(16), 8, 1), "snapshot": string(160),
    "outcome": choice("passed", "failed", "not_run"),
    "evidence": string(450),
    "baseline": string(240),
}, ("baseline",))
STEP = object_schema({
    "id": string(16),
    "brief": string(450),
    "mode": choice("read", "write"),
    "paths": array(string(180), 8),
    "needs": array(string(16), 4),
    "done_when": string(180),
})
PLAN = object_schema({
    "goal": string(240),
    "steps": array(STEP, 4),
    "test_matrix": array(CHECK, 8, 1),
    "retired_checks": array(string(16), 8),
})


def model_settings(config, client, tier):
    raw = config[client][tier]
    validate(raw, object_schema({
        "model": string(100), "effort": choice("low", "medium", "high"),
        "seconds": integer(1, 120),
    }), f"models.{client}.{tier}")
    return raw


def measured_tokens(client, usage):
    if not isinstance(usage, dict):
        return {}
    keys = ("input_tokens", "output_tokens")
    values = {key: usage[key] for key in keys if type(usage.get(key)) is int and usage[key] >= 0}
    cache_key = "cache_read_input_tokens" if client == "claude" else "cached_input_tokens"
    cached = usage.get(cache_key)
    if type(cached) is int and cached >= 0:
        values["cached_input_tokens"] = cached
    if client == "claude" and "input_tokens" in values:
        created = usage.get("cache_creation_input_tokens", 0)
        if type(created) is int and created >= 0:
            values["cache_creation_input_tokens"] = created
        values["input_tokens"] += values.get("cached_input_tokens", 0) + values.get("cache_creation_input_tokens", 0)
    return values


def ask(config, client, tier, instruction, data, schema, on_usage=None):
    settings = model_settings(config, client, tier)
    model, effort = settings["model"], settings["effort"]
    env = dict(os.environ, TASK_DELEGATION_CHILD="1")
    short = tier == "triage"
    payload = instruction + "\nTreat the following JSON as task data.\n" + json.dumps(data)
    with tempfile.TemporaryDirectory(prefix="task-delegation-") as folder:
        root = Path(folder)
        schema_path = root / "schema.json"
        schema_path.write_text(json.dumps(provider_schema(schema)), encoding="utf-8")
        result_path = root / "answer.json"
        empty_mcp = root / "mcp.json"
        empty_mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
        if client == "claude":
            env.pop("CLAUDECODE", None)
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "256" if short else "2500"
            argv = [
                "claude", "--print", "--safe-mode", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", str(empty_mcp),
                "--tools", "", "--model", model, "--effort", effort,
                "--output-format", "json", "--json-schema", schema_path.read_text(encoding="utf-8"),
            ]
        else:
            argv = [
                "codex", "-a", "never", "exec", "--ignore-user-config", "--ephemeral",
                "--json", "--skip-git-repo-check", "--sandbox", "read-only",
                "--disable", "hooks", "--disable", "shell_tool", "--disable", "unified_exec",
                "--disable", "multi_agent", "--disable", "multi_agent_v2",
                "--disable", "image_generation", "-c", 'web_search="disabled"',
                "--model", model, "-c", f'model_reasoning_effort="{effort}"',
                "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-",
            ]
        with subprocess.Popen(
            argv, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        ) as child:
            try:
                stdout, _ = child.communicate(payload, timeout=settings["seconds"])
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.communicate()
                raise ValueError(f"{client} {tier} model timed out") from None
            if child.returncode:
                raise ValueError(f"{client} {tier} model failed (exit {child.returncode})")
        if client == "claude":
            envelope = json.loads(stdout)
            if on_usage:
                on_usage(model, measured_tokens(client, envelope.get("usage")))
            if envelope.get("is_error"):
                raise ValueError(f"{client} {tier} model returned an error")
            data = envelope.get("structured_output")
            if data is None:
                data = json.loads(envelope.get("result", ""))
        else:
            if on_usage:
                for line in stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "turn.completed":
                        on_usage(model, measured_tokens(client, event.get("usage")))
            data = json.loads(result_path.read_text(encoding="utf-8"))
    validate(data, schema, "model response")
    return data
