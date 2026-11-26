"""SQLite state shared by short-lived hooks and each client's MCP process."""

import json
import re
import sqlite3
import time
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    thread TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE(thread, key)
);
CREATE VIRTUAL TABLE IF NOT EXISTS note_search USING fts5(
    key, body, content='notes', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS notes_insert AFTER INSERT ON notes BEGIN
    INSERT INTO note_search(rowid, key, body) VALUES (new.id, new.key, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_update AFTER UPDATE ON notes BEGIN
    INSERT INTO note_search(note_search, rowid, key, body)
        VALUES ('delete', old.id, old.key, old.body);
    INSERT INTO note_search(rowid, key, body) VALUES (new.id, new.key, new.body);
END;
CREATE TABLE IF NOT EXISTS routes (
    fingerprint TEXT PRIMARY KEY,
    complexity TEXT NOT NULL,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    thread TEXT NOT NULL,
    client TEXT NOT NULL,
    tier TEXT NOT NULL,
    revision INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    plan TEXT NOT NULL,
    reports TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification (
    task_id TEXT PRIMARY KEY,
    results TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_gates (
    thread TEXT PRIMARY KEY,
    turn TEXT NOT NULL,
    task_id TEXT
);
"""


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=4)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def remember(self, thread, key, kind, text):
        with self.db:
            self.db.execute(
                """INSERT INTO notes(thread,key,kind,body,updated) VALUES (?,?,?,?,?)
                   ON CONFLICT(thread,key) DO UPDATE SET
                   kind=excluded.kind, body=excluded.body, updated=excluded.updated""",
                (thread, key, kind, text, time.time_ns()),
            )
        return {"key": key, "saved": True}

    def recall(self, thread, query, limit):
        output = "n.key, n.kind, n.body AS text"
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query)))[:12]
        if tokens:
            expression = " OR ".join(f'"{term}"' for term in tokens)
            rows = self.db.execute(
                f"""SELECT {output} FROM note_search JOIN notes n ON n.id=note_search.rowid
                    WHERE note_search MATCH ? AND n.thread=?
                    ORDER BY bm25(note_search, 2.0, 1.0), n.updated DESC LIMIT ?""",
                (expression, thread, limit),
            )
        elif query.strip():
            return []
        else:
            rows = self.db.execute(
                f"SELECT {output} FROM notes n WHERE n.thread=? ORDER BY n.updated DESC LIMIT ?",
                (thread, limit),
            )
        return [dict(row) for row in rows]

    def cached_route(self, fingerprint):
        entry = self.db.execute(
            "SELECT complexity FROM routes WHERE fingerprint=? AND created>?",
            (fingerprint, int(time.time()) - 3600),
        ).fetchone()
        if not entry:
            return None
        cached = entry["complexity"]
        return json.loads(cached) if cached.startswith("{") else None

    def cache_route(self, fingerprint, complexity):
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM routes WHERE created<=?", (now - 3600,))
            self.db.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?)", (fingerprint, json.dumps(complexity), now))

    def set_prompt_gate(self, thread, turn, required):
        with self.db:
            if required:
                self.db.execute(
                    "INSERT OR IGNORE INTO prompt_gates VALUES (?,?,NULL)", (thread, turn),
                )
                self.db.execute(
                    "UPDATE prompt_gates SET turn=?,task_id=NULL WHERE thread=? AND turn<>?",
                    (turn, thread, turn),
                )
            else:
                self.db.execute("DELETE FROM prompt_gates WHERE thread=?", (thread,))

    def unlock_prompt(self, thread, turn, task_id):
        if turn:
            self.db.execute(
                "UPDATE prompt_gates SET task_id=? WHERE thread=? AND turn=?",
                (task_id, thread, turn),
            )

    def edit_requires_plan(self, thread, turn):
        row = self.db.execute(
            "SELECT * FROM prompt_gates WHERE thread=? AND task_id IS NULL", (thread,),
        ).fetchone()
        return row is not None and (turn is None or row["turn"] == turn)

    def get_task(self, thread, client, task_id):
        row = self.db.execute(
            """SELECT tasks.*,COALESCE(verification.results,'{}') AS tests
               FROM tasks LEFT JOIN verification ON verification.task_id=tasks.id
               WHERE thread=? AND client=? AND id=?""", (thread, client, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown task in this thread")
        result = dict(row)
        for key in ("plan", "reports", "tests"):
            result[key] = json.loads(result[key])
        return result

    def create_task(self, thread, client, tier, plan, turn=None):
        task_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (task_id, thread, client, tier, 1, 1, json.dumps(plan), "{}"),
            )
            self.unlock_prompt(thread, turn, task_id)
        return self.get_task(thread, client, task_id)

    def revise(self, previous, tier, plan, turn=None):
        with self.db:
            changed = self.db.execute(
                """UPDATE tasks SET tier=?,revision=revision+1,generation=generation+1,plan=?,reports='{}'
                   WHERE id=? AND generation=?""",
                (tier, json.dumps(plan), previous["id"], previous["generation"]),
            ).rowcount
            if not changed:
                raise ValueError("Progress changed during replanning. Fetch status and retry.")
            old_rows = {row["id"]: row for row in previous["plan"].get("test_matrix", [])}
            retained = {
                row["id"]: previous["tests"][row["id"]] for row in plan["test_matrix"]
                if row == old_rows.get(row["id"]) and row["id"] in previous["tests"]
            }
            self.save_tests(previous["id"], retained)
            self.unlock_prompt(previous["thread"], turn, previous["id"])
        return self.get_task(previous["thread"], previous["client"], previous["id"])

    def save_tests(self, task_id, results):
        self.db.execute("INSERT OR REPLACE INTO verification VALUES (?,?)", (task_id, json.dumps(results)))

    def merge_tests(self, task, tests):
        ids = {row["id"] for row in task["plan"].get("test_matrix", [])}
        seen = set()
        merged = task["tests"].copy()
        for result in tests:
            checks = set(result["check_ids"])
            if checks - ids or checks & seen or len(checks) != len(result["check_ids"]):
                raise ValueError("Test results must refer to distinct matrix row IDs")
            seen.update(checks)
            record = {key: value for key, value in result.items() if key != "check_ids"}
            for check in checks:
                merged[check] = record
        return merged

    def record_integration(self, thread, client, task_id, revision, tests):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get_task(thread, client, task_id)
            if task["revision"] != revision:
                raise ValueError("Stale revision. Fetch status before reviewing.")
            merged = self.merge_tests(task, tests)
            if task["tests"] != merged:
                self.save_tests(task_id, merged)
                self.db.execute("UPDATE tasks SET generation=generation+1 WHERE id=?", (task_id,))
                task["tests"] = merged
                task["generation"] += 1
            return task

    def report(self, thread, client, task_id, revision, step_id, status, evidence, tests=()):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get_task(thread, client, task_id)
            if revision != task["revision"]:
                raise ValueError("Stale revision. Fetch status before reporting again.")
            steps = {step["id"]: step for step in task["plan"]["steps"]}
            if step_id not in steps:
                raise ValueError("Unknown step in this revision")
            reports = task["reports"]
            update = {"status": status, "evidence": evidence}
            previous = reports.get(step_id, {})
            merged = self.merge_tests(task, tests)
            if previous == update and task["tests"] == merged:
                return task
            if previous.get("status") == "done" and previous != update:
                raise ValueError("Completed steps require a new plan revision to change")
            if status in {"running", "done"} and not all(
                reports.get(dep, {}).get("status") == "done" for dep in steps[step_id]["needs"]
            ):
                raise ValueError("Finish this step's dependencies first")
            reports[step_id] = update
            self.db.execute(
                "UPDATE tasks SET reports=?,generation=generation+1 WHERE id=?",
                (json.dumps(reports), task_id),
            )
            self.save_tests(task_id, merged)
        return self.get_task(thread, client, task_id)
