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
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    kind TEXT NOT NULL,
    client TEXT NOT NULL,
    detail TEXT NOT NULL,
    task_id TEXT,
    revision INTEGER,
    step_id TEXT,
    agent_name TEXT
);
CREATE TABLE IF NOT EXISTS prompt_activity (
    event_id INTEGER PRIMARY KEY,
    thread TEXT NOT NULL,
    client TEXT NOT NULL,
    turn TEXT NOT NULL,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS prompt_activity_turn ON prompt_activity(thread,client,turn);
CREATE INDEX IF NOT EXISTS prompt_activity_task ON prompt_activity(task_id);
CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    client TEXT NOT NULL,
    phase TEXT NOT NULL,
    model TEXT NOT NULL,
    thread TEXT NOT NULL,
    turn TEXT,
    task_id TEXT,
    duration_ms INTEGER NOT NULL,
    usage TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_usage_task ON model_usage(task_id);
CREATE INDEX IF NOT EXISTS model_usage_turn ON model_usage(thread, client, turn);
CREATE TABLE IF NOT EXISTS worker_usage (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    agent_name TEXT,
    usage TEXT NOT NULL,
    PRIMARY KEY (task_id,revision,step_id)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY,
    task_id TEXT,
    name TEXT NOT NULL,
    step_id TEXT,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_task ON tool_calls(task_id);
CREATE TABLE IF NOT EXISTS task_reviews (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created REAL NOT NULL,
    snapshot TEXT NOT NULL,
    evidence TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_reviews_task ON task_reviews(task_id);
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

    def record_activity(self, kind, client, detail, *, task_id=None, revision=None, step_id=None, agent_name=None):
        return self.db.execute(
            """INSERT INTO activity (created,kind,client,detail,task_id,revision,step_id,agent_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (time.time(), kind, client, detail, task_id, revision, step_id, agent_name),
        ).lastrowid

    def record_model_usage(self, client, phase, model, thread, turn, task_id, duration_ms, usage):
        with self.db:
            return self.db.execute(
                """INSERT INTO model_usage (created,client,phase,model,thread,turn,task_id,duration_ms,usage)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (time.time(), client, phase, model, thread, turn, task_id, duration_ms, json.dumps(usage)),
            ).lastrowid

    def link_prompt(self, thread, client, turn, task_id, model_call):
        if model_call is not None:
            self.db.execute("UPDATE model_usage SET task_id=? WHERE id=?", (task_id, model_call))
        if turn:
            self.db.execute(
                "UPDATE prompt_activity SET task_id=? WHERE thread=? AND client=? AND turn=? AND task_id IS NULL",
                (task_id, thread, client, turn),
            )
            self.db.execute(
                """UPDATE model_usage SET task_id=?
                   WHERE thread=? AND client=? AND turn=? AND phase='triage' AND task_id IS NULL""",
                (task_id, thread, client, turn),
            )

    def record_tool_call(self, name, task_id, success, thread, client, step_id=None):
        with self.db:
            self.db.execute(
                """INSERT INTO tool_calls (name,task_id,step_id,success)
                   VALUES (?, (SELECT id FROM tasks WHERE id=? AND thread=? AND client=?), ?, ?)""",
                (name, task_id, thread, client, step_id, success),
            )

    def save_review(self, task, snapshot, evidence, result):
        with self.db:
            changed = self.db.execute(
                """INSERT INTO task_reviews (task_id,revision,created,snapshot,evidence,result)
                   SELECT id,revision,?,?,?,? FROM tasks WHERE id=? AND generation=?""",
                (time.time(), snapshot, evidence, json.dumps(result), task["id"], task["generation"]),
            ).rowcount
            if not changed:
                raise ValueError("Progress changed during review. Fetch status and retry.")
        return result

    def export_task(self, task_id):
        with self.db:
            self.db.execute("BEGIN")
            owner = self.db.execute("SELECT thread,client FROM tasks WHERE id=?", (task_id,)).fetchone()
            if owner is None:
                raise ValueError("Unknown task ID")
            task = self.get_task(owner["thread"], owner["client"], task_id)
            reviews = [dict(row) for row in self.db.execute(
                "SELECT revision,created,snapshot,evidence,result FROM task_reviews WHERE task_id=? ORDER BY id",
                (task_id,),
            )]
            for review in reviews:
                review["result"] = json.loads(review["result"])
            calls = [dict(row) for row in self.db.execute(
                """SELECT name,count(*) AS calls,sum(1-success) AS errors
                   FROM tool_calls WHERE task_id=? GROUP BY name ORDER BY name""", (task_id,),
            )]
            usage = [dict(row) for row in self.db.execute(
                """SELECT created,client,phase,model,duration_ms,usage FROM model_usage
                   WHERE task_id=? ORDER BY id""", (task_id,),
            )]
            workers = [dict(row) for row in self.db.execute(
                """SELECT revision,step_id,agent_name,usage FROM worker_usage
                   WHERE task_id=? ORDER BY revision,step_id""", (task_id,),
            )]
            for record in (*usage, *workers):
                record["usage"] = json.loads(record["usage"])
            return {"task_id": task_id, "client": task["client"], "tier": task["tier"],
                    "revision": task["revision"], "plan": task["plan"], "reports": task["reports"],
                    "tests": task["tests"], "reviews": reviews, "model_calls": usage, "tool_calls": calls,
                    "worker_usage": workers}

    def task_for_prompt(self, event_id):
        row = self.db.execute("SELECT task_id FROM prompt_activity WHERE event_id=?", (event_id,)).fetchone()
        return row["task_id"] if row else None

    def graph_activity(self, task_id):
        with self.db:
            self.db.execute("BEGIN")
            task = self.db.execute(
                """SELECT revision,plan,reports,(
                     SELECT result FROM task_reviews
                     WHERE task_id=tasks.id AND revision=tasks.revision ORDER BY id DESC LIMIT 1
                   ) AS review FROM tasks WHERE id=?""", (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("Unknown task ID")
            prompts = [row["detail"] for row in self.db.execute(
                """SELECT detail FROM prompt_activity JOIN activity ON activity.id=event_id
                   WHERE prompt_activity.task_id=? ORDER BY event_id""", (task_id,),
            )]
            calls = self.db.execute(
                """SELECT step_id,name,count(*) AS calls,sum(success) AS successes
                   FROM tool_calls WHERE task_id=? GROUP BY step_id,name ORDER BY step_id,name""", (task_id,),
            ).fetchall()
        return task, prompts, calls

    def set_prompt_gate(self, thread, turn, required, *, client, route):
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
            event_id = self.record_activity("route", client, route)
            self.db.execute("INSERT INTO prompt_activity VALUES (?,?,?,?,NULL)", (event_id, thread, client, turn))

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

    def create_task(self, thread, client, tier, plan, turn=None, model_call=None):
        task_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (task_id, thread, client, tier, 1, 1, json.dumps(plan), "{}"),
            )
            self.unlock_prompt(thread, turn, task_id)
            self.link_prompt(thread, client, turn, task_id, model_call)
            self.record_activity("plan", client, tier, task_id=task_id, revision=1)
        return self.get_task(thread, client, task_id)

    def revise(self, previous, tier, plan, turn=None, model_call=None):
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
            self.link_prompt(previous["thread"], previous["client"], turn, previous["id"], model_call)
            self.record_activity("revise", previous["client"], tier,
                                 task_id=previous["id"], revision=previous["revision"] + 1)
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

    def report(self, thread, client, task_id, revision, step_id, status, evidence, tests=(), agent_name=None, usage=None):
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
            agent_name = agent_name or previous.get("agent_name")
            if agent_name:
                update["agent_name"] = agent_name
            usage = usage if usage is not None else previous.get("usage")
            if usage is not None:
                update["usage"] = usage
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
            if usage is not None:
                self.db.execute(
                    """INSERT OR REPLACE INTO worker_usage VALUES (?,?,?,?,?)""",
                    (task_id, revision, step_id, agent_name, json.dumps(usage)),
                )
            if (status, agent_name) != (previous.get("status"), previous.get("agent_name")):
                self.record_activity("step", client, status, task_id=task_id, revision=revision,
                                     step_id=step_id, agent_name=agent_name)
        return self.get_task(thread, client, task_id)
