#!/usr/bin/env python3
"""Automation Hub server: serves index.html and a JSON API backed by SQLite.

Standard library only. Normally started by runner.sh; can also be run directly:

    python3 server.py --host 127.0.0.1 --port 8080 --db data/hub.db --repo ..

Tables
    automations    catalog entries (playbooks, Terraform modules, rulebooks, scripts)
    files          current content of each file stored for an automation
    file_versions  every saved revision of a file (upload, edit, repo sync, restore)
    validations    latest syntax-check result for each automation
    workflows      workflow graphs (nodes and links) as JSON documents
    runs           workflow run history as JSON documents
    inventories    host inventories (Ansible INI text) that workflow nodes can target
    users          people who can sign in, with their role (see access.py)
    sessions       sign-in sessions (only a hash of each token is stored)
    approvals      approval steps waiting for, or decided by, an approver
"""
import argparse
import concurrent.futures
import getpass
import gzip
import hashlib
import io
import ipaddress
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import access  # noqa: E402
import checks  # noqa: E402

TYPES = {"ansible", "terraform", "eda", "shell", "python"}
SCHEMA_VERSION = 4
APPROVAL_ID = "__approval"   # built-in pseudo automation used by approval steps
COOKIE = "hub_session"
MAX_FILE = 512 * 1024          # per stored file
MAX_BODY = 32 * 1024 * 1024    # per request (restore of a full backup)
MAX_RUNS = 200

# Files picked up by "sync from repo". Secrets and state are never imported.
TEXT_SUFFIXES = {".yml", ".yaml", ".tf", ".hcl", ".j2", ".ini", ".cfg", ".conf", ".txt",
                 ".json", ".sh", ".py", ".ps1", ".md", ".example", ".toml", ".tpl"}
TEXT_NAMES = {".gitignore", "Makefile", "Dockerfile", "requirements.txt"}
SKIP_DIRS = {".git", ".terraform", "__pycache__", "node_modules", ".hub", "reports"}
SECRET_RE = re.compile(r"(^|/)(terraform\.tfvars|[^/]*\.auto\.tfvars|[^/]*\.tfstate(\.[^/]*)?|\.env|crash\.log)$")

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automations (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  type         TEXT NOT NULL,
  dir          TEXT NOT NULL,
  entry        TEXT NOT NULL,
  description  TEXT NOT NULL DEFAULT '',
  tags         TEXT NOT NULL DEFAULT '[]',
  vars         TEXT NOT NULL DEFAULT '',
  target_group TEXT NOT NULL DEFAULT '',
  action       TEXT NOT NULL DEFAULT '',
  updated      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  id            INTEGER PRIMARY KEY,
  automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,
  content       TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  size          INTEGER NOT NULL,
  updated       TEXT NOT NULL,
  UNIQUE (automation_id, path)
);
CREATE TABLE IF NOT EXISTS file_versions (
  id      INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  sha256  TEXT NOT NULL,
  size    INTEGER NOT NULL,
  source  TEXT NOT NULL,
  created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS file_versions_by_file ON file_versions(file_id, id);
CREATE TABLE IF NOT EXISTS validations (
  automation_id TEXT PRIMARY KEY REFERENCES automations(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  checked       TEXT NOT NULL,
  stale         INTEGER NOT NULL DEFAULT 0,
  result        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  doc     TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  workflow_id TEXT,
  started     TEXT NOT NULL,
  status      TEXT NOT NULL,
  doc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_started ON runs(started DESC);
CREATE TABLE IF NOT EXISTS inventories (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  doc     TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name       TEXT NOT NULL,
  role       TEXT NOT NULL,
  pw_hash    TEXT NOT NULL,
  disabled   INTEGER NOT NULL DEFAULT 0,
  created    TEXT NOT NULL,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created    TEXT NOT NULL,
  expires    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id                TEXT PRIMARY KEY,
  run_id            TEXT NOT NULL,
  workflow_id       TEXT,
  workflow_name     TEXT NOT NULL,
  node_id           TEXT NOT NULL,
  step_name         TEXT NOT NULL,
  instructions      TEXT NOT NULL DEFAULT '',
  requested_by      TEXT,
  requested_by_name TEXT NOT NULL,
  created           TEXT NOT NULL,
  timeout_at        REAL,
  last_seen         REAL NOT NULL,
  status            TEXT NOT NULL,
  decided_by        TEXT,
  decided_by_name   TEXT,
  decided_at        TEXT,
  comment           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS approvals_by_status ON approvals(status, created);
"""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def clean_path(path):
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if not parts or ".." in parts or len(p) > 300:
        raise ApiError(400, f"Invalid file path: {path!r}")
    return "/".join(parts)


def text_field(obj, key, required=False, default=""):
    v = obj.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise ApiError(400, f"{key} must be a string")
    if required and not v.strip():
        raise ApiError(400, f"{key} is required")
    return v


class Hub:
    def __init__(self, db_path, repo):
        self.db_path = Path(db_path)
        self.repo = Path(repo).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()
        with self.tx() as c:
            c.executescript(SCHEMA)
            # v4: who created each workflow and started each run (ownership is enforced here, not in the browser)
            for table, col in (("workflows", "created_by"), ("runs", "started_by")):
                if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.throttle = access.LoginThrottle()

    def migrate(self):
        """Upgrade databases created by earlier versions. The old file is copied first."""
        if not self.db_path.exists():
            return
        c = sqlite3.connect(self.db_path, timeout=15)
        try:
            if c.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                return
            row = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'automations'").fetchone()
            if not (row and "CHECK" in row[0]):
                return
            # v1 limited automation types with a CHECK constraint; SQLite can only drop it by rebuilding the table.
            backup = self.db_path.with_name(self.db_path.name + ".v1.bak")
            c.execute("VACUUM INTO ?", (str(backup),))
            c.execute("PRAGMA foreign_keys = OFF")
            c.executescript("""
                BEGIN;
                CREATE TABLE automations_v2 (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, dir TEXT NOT NULL, entry TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', vars TEXT NOT NULL DEFAULT '',
                  target_group TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL);
                INSERT INTO automations_v2 SELECT id, name, type, dir, entry, description, tags, vars, target_group, action, updated FROM automations;
                DROP TABLE automations;
                ALTER TABLE automations_v2 RENAME TO automations;
                COMMIT;
            """)
            bad = c.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"Migration left broken references ({len(bad)}); the original is in {backup}")
            print(f"Upgraded the database schema; the previous file was saved as {backup}", flush=True)
        finally:
            c.close()

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # ---------------------------------------------------------------- state
    @staticmethod
    def file_meta(c, aid):
        rows = c.execute("SELECT id, path, size, updated FROM files WHERE automation_id = ? ORDER BY path", (aid,))
        return [dict(r) for r in rows]

    def automation_dict(self, c, r):
        return {"id": r["id"], "name": r["name"], "type": r["type"], "dir": r["dir"], "entry": r["entry"],
                "description": r["description"], "tags": json.loads(r["tags"]), "vars": r["vars"],
                "group": r["target_group"], "action": r["action"], "updated": r["updated"],
                "files": self.file_meta(c, r["id"]), "validation": self.validation_summary(c, r["id"])}

    @staticmethod
    def validation_summary(c, aid):
        r = c.execute("SELECT status, checked, stale, result FROM validations WHERE automation_id = ?", (aid,)).fetchone()
        if not r:
            return None
        return {"status": r["status"], "checked": r["checked"], "stale": bool(r["stale"]), "counts": json.loads(r["result"])["counts"]}

    def state(self):
        with self.tx() as c:
            autos = [self.automation_dict(c, r) for r in c.execute("SELECT * FROM automations ORDER BY name")]
            wfs = [json.loads(r["doc"]) for r in c.execute("SELECT doc FROM workflows ORDER BY name")]
            runs = [json.loads(r["doc"]) for r in c.execute("SELECT doc FROM runs ORDER BY started DESC LIMIT ?", (MAX_RUNS,))]
            invs = [json.loads(r["doc"]) for r in c.execute("SELECT doc FROM inventories ORDER BY name")]
            init = c.execute("SELECT value FROM meta WHERE key = 'initialized'").fetchone()
            seed = c.execute("SELECT value FROM meta WHERE key = 'seed_version'").fetchone()
        return {"automations": autos, "workflows": wfs, "runs": runs, "inventories": invs,
                "initialized": bool(init) or bool(autos), "seedVersion": int(seed["value"]) if seed else 1,
                "db": str(self.db_path)}

    @staticmethod
    def upsert_automation(c, a):
        if not isinstance(a, dict):
            raise ApiError(400, "automation must be an object")
        t = text_field(a, "type", True)
        if t not in TYPES:
            raise ApiError(400, f"Unknown automation type: {t}")
        old = c.execute("SELECT type, dir, entry FROM automations WHERE id = ?", (a.get("id"),)).fetchone()
        if old and (old["type"], old["dir"], old["entry"]) != (a.get("type"), a.get("dir"), a.get("entry")):
            c.execute("UPDATE validations SET stale = 1 WHERE automation_id = ?", (a.get("id"),))
        tags = a.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
            raise ApiError(400, "tags must be a list of strings")
        c.execute(
            """INSERT INTO automations (id, name, type, dir, entry, description, tags, vars, target_group, action, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type, dir = excluded.dir,
                 entry = excluded.entry, description = excluded.description, tags = excluded.tags,
                 vars = excluded.vars, target_group = excluded.target_group, action = excluded.action,
                 updated = excluded.updated""",
            (text_field(a, "id", True), text_field(a, "name", True), t, text_field(a, "dir", True),
             text_field(a, "entry", True), text_field(a, "description"), json.dumps(tags),
             text_field(a, "vars"), text_field(a, "group"), text_field(a, "action"),
             text_field(a, "updated") or now()))

    @staticmethod
    def upsert_workflow(c, w, user=None):
        if not isinstance(w, dict) or not isinstance(w.get("nodes"), list) or not isinstance(w.get("edges"), list):
            raise ApiError(400, "workflow needs nodes and edges")
        wid = text_field(w, "id", True)
        old = c.execute("SELECT created_by, doc FROM workflows WHERE id = ?", (wid,)).fetchone()
        prev = json.loads(old["doc"]) if old else {}
        # The owner comes from the database (or the signed-in user), never from the browser's copy.
        owner = old["created_by"] if old else (user["id"] if user else w.get("createdBy"))
        w = {**w, "createdBy": owner, "createdByName": prev.get("createdByName") if old else (user["name"] if user else w.get("createdByName"))}
        if user:
            w["updatedByName"] = user["name"]
        c.execute("""INSERT INTO workflows (id, name, doc, updated, created_by) VALUES (?, ?, ?, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET name = excluded.name, doc = excluded.doc, updated = excluded.updated""",
                  (wid, text_field(w, "name") or "Untitled workflow", json.dumps(w), text_field(w, "updated") or now(), owner))

    @staticmethod
    def upsert_run(c, r, user=None):
        if not isinstance(r, dict):
            raise ApiError(400, "run must be an object")
        rid = text_field(r, "id", True)
        old = c.execute("SELECT started_by, doc FROM runs WHERE id = ?", (rid,)).fetchone()
        if old and user and old["started_by"] and old["started_by"] != user["id"] and user["role"] != "admin":
            raise ApiError(403, "This run was started by someone else")
        prev = json.loads(old["doc"]) if old else {}
        starter = (old["started_by"] if old and old["started_by"] else None) or (user["id"] if user else r.get("startedById"))
        r = {**r, "startedById": starter, "startedBy": prev.get("startedBy") or (user["name"] if user else r.get("startedBy"))}
        c.execute("""INSERT INTO runs (id, workflow_id, started, status, doc, started_by) VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET status = excluded.status, doc = excluded.doc""",
                  (rid, text_field(r, "workflowId"), text_field(r, "started", True), text_field(r, "status", True), json.dumps(r), starter))

    @staticmethod
    def upsert_inventory(c, inv):
        if not isinstance(inv, dict):
            raise ApiError(400, "inventory must be an object")
        content = text_field(inv, "content")
        if len(content.encode("utf-8")) > MAX_FILE:
            raise ApiError(413, f"Inventory is larger than {MAX_FILE // 1024} KB")
        c.execute("""INSERT INTO inventories (id, name, doc, updated) VALUES (?, ?, ?, ?)
                     ON CONFLICT(id) DO UPDATE SET name = excluded.name, doc = excluded.doc, updated = excluded.updated""",
                  (text_field(inv, "id", True), text_field(inv, "name") or "Untitled inventory", json.dumps(inv),
                   text_field(inv, "updated") or now()))

    UPSERT = {"automations": "upsert_automation", "workflows": "upsert_workflow", "runs": "upsert_run",
              "inventories": "upsert_inventory"}

    def sync(self, body, user):
        """Apply a batch of changes from the UI in one transaction, within the user's permissions."""
        ups, dels = body.get("upsert") or {}, body.get("delete") or {}
        p, admin = access.perms(user["role"]), user["role"] == "admin"
        for kind in ("automations", "inventories"):
            if (ups.get(kind) or dels.get(kind)) and "catalog" not in p:
                raise ApiError(403, f"Only admins can change {kind}")
        if (body.get("initialize") or "seedVersion" in body) and "catalog" not in p:
            raise ApiError(403, "Only admins can set up the catalog")
        with self.tx() as c:
            for kind in ("runs", "workflows", "automations", "inventories"):
                ids = [str(i) for i in dels.get(kind) or []]
                if kind == "runs" and "history" not in p:
                    ids = []  # browsers trim their run list; only admins delete history (the server trims it too)
                if kind == "workflows" and not admin:
                    for i in ids:
                        r = c.execute("SELECT created_by FROM workflows WHERE id = ?", (i,)).fetchone()
                        if r and r["created_by"] != user["id"]:
                            raise ApiError(403, "Only the workflow's creator or an admin can delete it")
                if ids:
                    c.executemany(f"DELETE FROM {kind} WHERE id = ?", [(i,) for i in ids])
            for kind in ("inventories", "automations", "workflows", "runs"):
                for obj in ups.get(kind) or []:
                    if kind in ("workflows", "runs"):
                        getattr(self, self.UPSERT[kind])(c, obj, user)
                    else:
                        getattr(self, self.UPSERT[kind])(c, obj)
            c.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started DESC LIMIT ?)", (MAX_RUNS,))
            if body.get("initialize"):
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('initialized', ?)", (now(),))
            if isinstance(body.get("seedVersion"), int):
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seed_version', ?)", (str(body["seedVersion"]),))
        return {"ok": True}

    def reset(self, body):
        """Replace everything (restore from backup, reset to sample data)."""
        with self.tx() as c:
            for t in ("runs", "workflows", "automations", "inventories"):
                c.execute(f"DELETE FROM {t}")
            for a in body.get("automations") or []:
                self.upsert_automation(c, a)
                for f in a.get("files") or []:
                    if isinstance(f, dict) and isinstance(f.get("content"), str):
                        self.save_file(c, a["id"], f.get("path"), f["content"], "restore")
            for w in body.get("workflows") or []:
                self.upsert_workflow(c, w)
            for r in body.get("runs") or []:
                self.upsert_run(c, r)
            for inv in body.get("inventories") or []:
                self.upsert_inventory(c, inv)
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('initialized', ?)", (now(),))
        return {"ok": True}

    def export(self):
        s = self.state()
        with self.tx() as c:
            for a in s["automations"]:
                a["files"] = [{"path": r["path"], "content": r["content"]} for r in
                              c.execute("SELECT path, content FROM files WHERE automation_id = ? ORDER BY path", (a["id"],))]
        del s["initialized"], s["db"], s["seedVersion"]
        for a in s["automations"]:
            a.pop("validation", None)
        s["exported"] = now()
        return s

    # ---------------------------------------------------------------- files
    @staticmethod
    def require_automation(c, aid):
        r = c.execute("SELECT * FROM automations WHERE id = ?", (aid,)).fetchone()
        if not r:
            raise ApiError(404, "Automation not found")
        return r

    @staticmethod
    def write_version(c, fid, content, sha, size, source, ts):
        c.execute("INSERT INTO file_versions (file_id, content, sha256, size, source, created) VALUES (?, ?, ?, ?, ?, ?)",
                  (fid, content, sha, size, source, ts))

    def save_file(self, c, aid, path, content, source):
        path = clean_path(path)
        if not isinstance(content, str):
            raise ApiError(400, "content must be text")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE:
            raise ApiError(413, f"{path} is larger than {MAX_FILE // 1024} KB")
        self.require_automation(c, aid)
        sha, ts = hashlib.sha256(data).hexdigest(), now()
        row = c.execute("SELECT id, sha256 FROM files WHERE automation_id = ? AND path = ?", (aid, path)).fetchone()
        if row and row["sha256"] == sha:
            return row["id"], "unchanged"
        if row:
            fid, status = row["id"], "updated"
            c.execute("UPDATE files SET content = ?, sha256 = ?, size = ?, updated = ? WHERE id = ?", (content, sha, len(data), ts, fid))
        else:
            status = "added"
            fid = c.execute("INSERT INTO files (automation_id, path, content, sha256, size, updated) VALUES (?, ?, ?, ?, ?, ?)",
                            (aid, path, content, sha, len(data), ts)).lastrowid
        self.write_version(c, fid, content, sha, len(data), source, ts)
        c.execute("UPDATE validations SET stale = 1 WHERE automation_id = ?", (aid,))
        return fid, status

    def put_file(self, aid, body):
        with self.tx() as c:
            fid, status = self.save_file(c, aid, body.get("path"), body.get("content"), str(body.get("source") or "edit")[:20])
            return {"id": fid, "status": status, "files": self.file_meta(c, aid)}

    def update_file(self, fid, body):
        with self.tx() as c:
            row = c.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
            if not row:
                raise ApiError(404, "File not found")
            path = clean_path(body.get("path") or row["path"])
            if path != row["path"]:
                if c.execute("SELECT 1 FROM files WHERE automation_id = ? AND path = ?", (row["automation_id"], path)).fetchone():
                    raise ApiError(409, f"{path} already exists")
                c.execute("UPDATE files SET path = ?, updated = ? WHERE id = ?", (path, now(), fid))
            content = body.get("content", row["content"])
            fid2, status = self.save_file(c, row["automation_id"], path, content, str(body.get("source") or "edit")[:20])
            return {"id": fid2, "status": status, "files": self.file_meta(c, row["automation_id"])}

    def get_file(self, fid):
        with self.tx() as c:
            r = c.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
            if not r:
                raise ApiError(404, "File not found")
            return dict(r)

    def delete_file(self, fid):
        with self.tx() as c:
            r = c.execute("SELECT automation_id FROM files WHERE id = ?", (fid,)).fetchone()
            if not r:
                raise ApiError(404, "File not found")
            c.execute("DELETE FROM files WHERE id = ?", (fid,))
            c.execute("UPDATE validations SET stale = 1 WHERE automation_id = ?", (r["automation_id"],))
            return {"files": self.file_meta(c, r["automation_id"])}

    def list_files(self, aid):
        with self.tx() as c:
            self.require_automation(c, aid)
            return self.file_meta(c, aid)

    def versions(self, fid):
        with self.tx() as c:
            rows = c.execute("SELECT id, sha256, size, source, created FROM file_versions WHERE file_id = ? ORDER BY id DESC", (fid,))
            return [dict(r) for r in rows]

    def version(self, vid):
        with self.tx() as c:
            r = c.execute("SELECT id, file_id, content, size, source, created FROM file_versions WHERE id = ?", (vid,)).fetchone()
            if not r:
                raise ApiError(404, "Version not found")
            return dict(r)

    def zip_files(self, aid):
        with self.tx() as c:
            a = self.require_automation(c, aid)
            rows = c.execute("SELECT path, content FROM files WHERE automation_id = ? ORDER BY path", (aid,)).fetchall()
        root = Path(a["dir"]).name or a["id"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for r in rows:
                z.writestr(f"{root}/{r['path']}", r["content"])
        return root + ".zip", buf.getvalue()

    # ----------------------------------------------------------- validation
    def _load(self, aid):
        with self.tx() as c:
            a = dict(self.require_automation(c, aid))
            files = [dict(r) for r in c.execute("SELECT path, content FROM files WHERE automation_id = ? ORDER BY path", (aid,))]
        a["group"] = a["target_group"]
        return a, files

    def validate(self, aid):
        a, files = self._load(aid)
        result = checks.validate_automation(a, files, self.repo)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM automations WHERE id = ?", (aid,)).fetchone():
                c.execute("""INSERT INTO validations (automation_id, status, checked, stale, result) VALUES (?, ?, ?, 0, ?)
                             ON CONFLICT(automation_id) DO UPDATE SET status = excluded.status, checked = excluded.checked,
                               stale = 0, result = excluded.result""",
                          (aid, result["status"], result["checked"], json.dumps(result)))
        return result

    def validate_many(self, ids=None):
        with self.tx() as c:
            ids = ids or [r["id"] for r in c.execute("SELECT id FROM automations")]
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            results = dict(zip(ids, ex.map(self.validate, ids)))
        return {"results": results}

    def last_validation(self, aid):
        with self.tx() as c:
            self.require_automation(c, aid)
            r = c.execute("SELECT result, stale FROM validations WHERE automation_id = ?", (aid,)).fetchone()
        return {**json.loads(r["result"]), "stale": bool(r["stale"])} if r else {"status": None}

    def check_content(self, body):
        with self.tx() as c:
            a = self.require_automation(c, text_field(body, "automationId", True))
        path = clean_path(body.get("path"))
        role = checks.ROLE.get(a["type"]) if path == a["entry"] else None
        return checks.check_file(path, text_field(body, "content"), role)

    def test_workflow(self, body):
        """Static test run: syntax, variables, inventories and tools for every node. Nothing is executed."""
        wf = body.get("workflow") or {}
        nodes = [n for n in wf.get("nodes") or [] if isinstance(n, dict)]
        with self.tx() as c:
            known = {r["id"] for r in c.execute("SELECT id FROM automations")}
        used = sorted({n.get("automationId") for n in nodes} & known)
        validations = self.validate_many(used)["results"] if used else {}
        out = {}
        for n in nodes:
            aid = n.get("automationId")
            if aid == APPROVAL_ID:
                out[n.get("id")] = {"issues": self.approval_readiness(n)}
                continue
            if aid not in known:
                out[n.get("id")] = {"issues": [checks.msg(None, "error", "The automation is not in the catalog")]}
                continue
            a, files = self._load(aid)
            v = validations[aid]
            out[n.get("id")] = {"issues": checks.node_readiness(a, n, files, v, self.repo), "validation": v["status"]}
        return {"nodes": out, "validations": validations, "tools": checks.tools(), "checked": now()}

    def approval_readiness(self, n):
        with self.tx() as c:
            approvers = c.execute("SELECT COUNT(*) FROM users WHERE disabled = 0 AND role IN ('admin', 'approver')").fetchone()[0]
        issues = []
        if not approvers:
            issues.append(checks.msg(None, "warning", "No active user can approve this step yet"))
        t = str(n.get("timeout") or "").strip()
        if t and not re.fullmatch(r"\d+(\.\d+)?", t):
            issues.append(checks.msg(None, "error", f"Timeout must be a number of minutes, not {t!r}"))
        return issues

    # ---------------------------------------------------------- users & sign-in
    @staticmethod
    def user_public(r):
        return {"id": r["id"], "username": r["username"], "name": r["name"], "role": r["role"],
                "roleLabel": access.ROLES.get(r["role"], {}).get("label", r["role"]), "disabled": bool(r["disabled"]),
                "created": r["created"], "lastLogin": r["last_login"]}

    @staticmethod
    def _new_session(c, uid):
        token = access.new_token()
        c.execute("INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
                  (access.token_hash(token), uid, now(), time.time() + access.SESSION_SECONDS))
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        return token

    def session_user(self, token):
        if not token:
            return None
        th = access.token_hash(token)
        with self.tx() as c:
            r = c.execute("""SELECT u.*, s.expires FROM sessions s JOIN users u ON u.id = s.user_id
                             WHERE s.token_hash = ? AND s.expires > ? AND u.disabled = 0""", (th, time.time())).fetchone()
            if not r:
                return None
            if r["expires"] - time.time() < access.SESSION_SECONDS - 3600:  # sliding expiry, refreshed hourly
                c.execute("UPDATE sessions SET expires = ? WHERE token_hash = ?", (time.time() + access.SESSION_SECONDS, th))
            return self.user_public(r)

    def session_info(self, user):
        with self.tx() as c:
            setup = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        return {"user": user, "setup": setup, "roles": {k: v["label"] for k, v in access.ROLES.items()}}

    @staticmethod
    def _check_new_user(body):
        username = text_field(body, "username", True).strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{2,64}", username):
            raise ApiError(400, "Usernames are 2-64 letters, digits, dots, dashes, underscores or @")
        name = (text_field(body, "name") or username).strip()[:80]
        problem = access.check_password_rules(body.get("password"))
        if problem:
            raise ApiError(400, problem)
        return username, name

    def setup(self, body):
        """Create the first admin. Only possible while there are no users at all."""
        username, name = self._check_new_user(body)
        with self.tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise ApiError(409, "The hub already has users; sign in instead")
            uid = "u-" + secrets.token_hex(6)
            c.execute("INSERT INTO users (id, username, name, role, pw_hash, created, last_login) VALUES (?, ?, ?, 'admin', ?, ?, ?)",
                      (uid, username, name, access.hash_password(body["password"]), now(), now()))
            token = self._new_session(c, uid)
            user = self.user_public(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
        return {"user": user, "_set_cookie": token}

    def login(self, body, client):
        username = text_field(body, "username").strip()
        keys = ("u:" + username.lower(), "ip:" + client)
        wait = self.throttle.blocked(*keys)
        if wait:
            raise ApiError(429, f"Too many failed sign-ins. Try again in {wait} seconds")
        with self.tx() as c:
            r = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            ok = access.verify_password(str(body.get("password") or ""), r["pw_hash"] if r else access.DUMMY_HASH)
            if not r or not ok or r["disabled"]:
                self.throttle.failed(*keys)
                raise ApiError(401, "Wrong username or password" if not (r and ok) else "This account is disabled")
            self.throttle.succeeded(*keys)
            c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), r["id"]))
            token = self._new_session(c, r["id"])
        return {"user": self.user_public(r), "_set_cookie": token}

    def logout(self, token):
        if token:
            with self.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash = ?", (access.token_hash(token),))
        return {"ok": True, "_clear_cookie": True}

    def list_users(self):
        with self.tx() as c:
            return [self.user_public(r) for r in c.execute("SELECT * FROM users ORDER BY role, username")]

    def create_user(self, body):
        username, name = self._check_new_user(body)
        role = text_field(body, "role", True)
        if role not in access.ROLES:
            raise ApiError(400, f"Unknown role: {role}")
        uid = "u-" + secrets.token_hex(6)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise ApiError(409, f"{username} already exists")
            c.execute("INSERT INTO users (id, username, name, role, pw_hash, created) VALUES (?, ?, ?, ?, ?, ?)",
                      (uid, username, name, role, access.hash_password(body["password"]), now()))
            return self.user_public(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())

    @staticmethod
    def _other_active_admins(c, uid):
        return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?", (uid,)).fetchone()[0]

    def update_user(self, uid, body, actor):
        with self.tx() as c:
            r = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not r:
                raise ApiError(404, "User not found")
            role = body.get("role", r["role"])
            disabled = bool(body.get("disabled", r["disabled"]))
            if role not in access.ROLES:
                raise ApiError(400, f"Unknown role: {role}")
            if r["role"] == "admin" and (role != "admin" or disabled) and not self._other_active_admins(c, uid):
                raise ApiError(409, "The hub needs at least one active admin")
            if uid == actor["id"] and disabled:
                raise ApiError(409, "You can't disable your own account")
            name = (text_field(body, "name") or r["name"]).strip()[:80]
            c.execute("UPDATE users SET name = ?, role = ?, disabled = ? WHERE id = ?", (name, role, int(disabled), uid))
            if body.get("password"):
                problem = access.check_password_rules(body["password"])
                if problem:
                    raise ApiError(400, problem)
                c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (access.hash_password(body["password"]), uid))
            if disabled or body.get("password") or role != r["role"]:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))  # signs them out everywhere
            return self.user_public(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())

    def delete_user(self, uid, actor):
        with self.tx() as c:
            r = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not r:
                raise ApiError(404, "User not found")
            if uid == actor["id"]:
                raise ApiError(409, "You can't delete your own account")
            if r["role"] == "admin" and not self._other_active_admins(c, uid):
                raise ApiError(409, "The hub needs at least one active admin")
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
        return {"ok": True}

    def change_password(self, user, body, token):
        with self.tx() as c:
            r = c.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not access.verify_password(str(body.get("current") or ""), r["pw_hash"]):
                raise ApiError(400, "The current password is wrong")
            problem = access.check_password_rules(body.get("password"))
            if problem:
                raise ApiError(400, problem)
            c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (access.hash_password(body["password"]), user["id"]))
            c.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], access.token_hash(token or "")))
        return {"ok": True}

    def reset_password_cli(self, username):
        with self.tx() as c:
            r = c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not r:
            sys.exit(f"No user called {username!r}")
        pw = getpass.getpass(f"New password for {username}: ")
        if pw != getpass.getpass("Repeat it: "):
            sys.exit("The passwords don't match")
        problem = access.check_password_rules(pw)
        if problem:
            sys.exit(problem)
        with self.tx() as c:
            c.execute("UPDATE users SET pw_hash = ?, disabled = 0 WHERE id = ?", (access.hash_password(pw), r["id"]))
            c.execute("DELETE FROM sessions WHERE user_id = ?", (r["id"],))
        print(f"Password for {username} changed; the account is enabled and signed out everywhere.")

    # --------------------------------------------------------------- approvals
    @staticmethod
    def approval_public(r):
        d = dict(r)
        d["launcherConnected"] = time.time() - r["last_seen"] < 20
        d["timeoutAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["timeout_at"])) if r["timeout_at"] else None
        del d["timeout_at"]
        return d

    @staticmethod
    def _expire(c):
        t = time.time()
        c.execute("UPDATE approvals SET status = 'expired', decided_at = ? WHERE status = 'pending' AND timeout_at IS NOT NULL AND timeout_at < ?", (now(), t))
        # The run waits in the launcher's browser; if that page has been gone for 10 minutes, the run is over.
        c.execute("UPDATE approvals SET status = 'abandoned', decided_at = ? WHERE status = 'pending' AND last_seen < ?", (now(), t - 600))

    def create_approval(self, body, user):
        t = body.get("timeoutMinutes")
        try:
            timeout_at = time.time() + 60 * float(t) if t not in (None, "") else None
        except (TypeError, ValueError):
            raise ApiError(400, "timeoutMinutes must be a number")
        aid = "ap-" + secrets.token_hex(6)
        with self.tx() as c:
            c.execute("""INSERT INTO approvals (id, run_id, workflow_id, workflow_name, node_id, step_name, instructions,
                           requested_by, requested_by_name, created, timeout_at, last_seen, status)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                      (aid, text_field(body, "runId", True), text_field(body, "workflowId"), text_field(body, "workflowName") or "Workflow",
                       text_field(body, "nodeId", True), text_field(body, "stepName") or "Approval", text_field(body, "instructions")[:4000],
                       user["id"], user["name"], now(), timeout_at, time.time()))
            return self.approval_public(c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

    def list_approvals(self, status):
        with self.tx() as c:
            self._expire(c)
            if status == "pending":
                rows = c.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created")
            else:
                rows = c.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT 200")
            return [self.approval_public(r) for r in rows]

    def get_approval(self, aid, user):
        with self.tx() as c:
            self._expire(c)
            r = c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
            if not r:
                raise ApiError(404, "Approval not found")
            if r["status"] == "pending" and r["requested_by"] == user["id"]:
                c.execute("UPDATE approvals SET last_seen = ? WHERE id = ?", (time.time(), aid))  # the launcher is still waiting
            return self.approval_public(r)

    def decide_approval(self, aid, body, user):
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise ApiError(400, "decision must be approve or reject")
        with self.tx() as c:
            self._expire(c)
            r = c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
            if not r:
                raise ApiError(404, "Approval not found")
            if r["status"] != "pending":
                raise ApiError(409, f"This approval is already {r['status']}")
            if r["requested_by"] == user["id"] and user["role"] != "admin":
                raise ApiError(403, "You can't approve a step in a run you launched; ask another approver")
            c.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_by_name = ?, decided_at = ?, comment = ? WHERE id = ?",
                      ("approved" if decision == "approve" else "rejected", user["id"], user["name"], now(),
                       text_field(body, "comment")[:2000], aid))
            return self.approval_public(c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

    def cancel_approval(self, aid, user):
        with self.tx() as c:
            r = c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
            if not r:
                raise ApiError(404, "Approval not found")
            if r["requested_by"] != user["id"] and user["role"] != "admin":
                raise ApiError(403, "Only the launcher or an admin can cancel this")
            if r["status"] == "pending":
                c.execute("UPDATE approvals SET status = 'cancelled', decided_at = ?, decided_by_name = ? WHERE id = ?", (now(), user["name"], aid))
            return self.approval_public(c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())

    @staticmethod
    def convert_inventory(body):
        """YAML inventory to INI; the browser converts the other upload formats itself."""
        try:
            return {"content": checks.yaml_inventory_ini(text_field(body, "content", True))}
        except ValueError as e:
            raise ApiError(400, str(e))

    # ------------------------------------------------------------ repo sync
    def repo_paths(self, base):
        rel = base.relative_to(self.repo).as_posix()
        try:
            out = subprocess.run(["git", "-C", str(self.repo), "ls-files", "-co", "--exclude-standard", "-z", "--", rel or "."],
                                 capture_output=True, check=True, timeout=30).stdout
            return [self.repo / p for p in out.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            return [p for p in base.rglob("*") if p.is_file()]

    def sync_repo(self, aid):
        with self.tx() as c:
            a = self.require_automation(c, aid)
        base = (self.repo / a["dir"]).resolve()
        if base != self.repo and self.repo not in base.parents:
            raise ApiError(400, f"{a['dir']} is outside the repository")
        result = {"id": aid, "added": 0, "updated": 0, "unchanged": 0, "skipped": []}
        if not base.is_dir():
            result["error"] = f"{a['dir']}/ not found in {self.repo}"
            return result
        with self.tx() as c:
            for p in sorted(self.repo_paths(base)):
                try:
                    rel = p.resolve().relative_to(base).as_posix()
                except ValueError:
                    continue  # symlink pointing outside the project
                parts = rel.split("/")
                if any(d in SKIP_DIRS for d in parts[:-1]) or not p.is_file():
                    continue
                if SECRET_RE.search(rel):
                    result["skipped"].append(f"{rel} (secret or state)")
                    continue
                if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in TEXT_NAMES:
                    continue
                if p.stat().st_size > MAX_FILE:
                    result["skipped"].append(f"{rel} (too large)")
                    continue
                try:
                    content = p.read_text("utf-8")
                except (UnicodeDecodeError, OSError):
                    result["skipped"].append(f"{rel} (not text)")
                    continue
                _, status = self.save_file(c, aid, rel, content, "repo")
                result[status] += 1
            result["files"] = self.file_meta(c, aid)
        return result

    def sync_repo_all(self):
        with self.tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM automations")]
        return {"results": [self.sync_repo(i) for i in ids]}


class Handler(BaseHTTPRequestHandler):
    server_version = "AutomationHub/1.0"
    protocol_version = "HTTP/1.1"
    hub: Hub = None
    verbose = False
    _index = {"mtime": None}

    # (method, path, permission, handler). None = no sign-in needed; other permissions are in access.ROLES.
    ROUTES = [
        ("GET", r"/api/session", None, lambda h, m, b: h.hub.session_info(h.user)),
        ("POST", r"/api/setup", None, lambda h, m, b: h.hub.setup(b)),
        ("POST", r"/api/login", None, lambda h, m, b: h.hub.login(b, h.client_address[0])),
        ("POST", r"/api/logout", None, lambda h, m, b: h.hub.logout(h.token)),
        ("POST", r"/api/me/password", "read", lambda h, m, b: h.hub.change_password(h.user, b, h.token)),
        ("GET", r"/api/users", "users", lambda h, m, b: h.hub.list_users()),
        ("POST", r"/api/users", "users", lambda h, m, b: h.hub.create_user(b)),
        ("PUT", r"/api/users/([\w-]+)", "users", lambda h, m, b: h.hub.update_user(m[1], b, h.user)),
        ("DELETE", r"/api/users/([\w-]+)", "users", lambda h, m, b: h.hub.delete_user(m[1], h.user)),
        ("GET", r"/api/approvals", "read", lambda h, m, b: h.hub.list_approvals(h.query.get("status"))),
        ("POST", r"/api/approvals", "workflows", lambda h, m, b: h.hub.create_approval(b, h.user)),
        ("GET", r"/api/approvals/([\w-]+)", "read", lambda h, m, b: h.hub.get_approval(m[1], h.user)),
        ("POST", r"/api/approvals/([\w-]+)/decide", "approve", lambda h, m, b: h.hub.decide_approval(m[1], b, h.user)),
        ("POST", r"/api/approvals/([\w-]+)/cancel", "read", lambda h, m, b: h.hub.cancel_approval(m[1], h.user)),
        ("GET", r"/api/state", "read", lambda h, m, b: h.hub.state()),
        ("POST", r"/api/sync", "read", lambda h, m, b: h.hub.sync(b, h.user)),
        ("POST", r"/api/reset", "backup", lambda h, m, b: h.hub.reset(b)),
        ("GET", r"/api/export", "backup", lambda h, m, b: h.hub.export()),
        ("POST", r"/api/sync-repo", "catalog", lambda h, m, b: h.hub.sync_repo_all()),
        ("GET", r"/api/automations/([\w.-]+)/files", "read", lambda h, m, b: h.hub.list_files(m[1])),
        ("PUT", r"/api/automations/([\w.-]+)/files", "catalog", lambda h, m, b: h.hub.put_file(m[1], b)),
        ("POST", r"/api/automations/([\w.-]+)/sync-repo", "catalog", lambda h, m, b: h.hub.sync_repo(m[1])),
        ("GET", r"/api/automations/([\w.-]+)/zip", "read", lambda h, m, b: h.hub.zip_files(m[1])),
        ("GET", r"/api/files/(\d+)", "read", lambda h, m, b: h.hub.get_file(int(m[1]))),
        ("PUT", r"/api/files/(\d+)", "catalog", lambda h, m, b: h.hub.update_file(int(m[1]), b)),
        ("DELETE", r"/api/files/(\d+)", "catalog", lambda h, m, b: h.hub.delete_file(int(m[1]))),
        ("GET", r"/api/files/(\d+)/versions", "read", lambda h, m, b: h.hub.versions(int(m[1]))),
        ("GET", r"/api/versions/(\d+)", "read", lambda h, m, b: h.hub.version(int(m[1]))),
        ("POST", r"/api/automations/([\w.-]+)/validate", "read", lambda h, m, b: h.hub.validate(m[1])),
        ("GET", r"/api/automations/([\w.-]+)/validation", "read", lambda h, m, b: h.hub.last_validation(m[1])),
        ("POST", r"/api/validate", "read", lambda h, m, b: h.hub.validate_many(b.get("ids"))),
        ("POST", r"/api/check-file", "read", lambda h, m, b: h.hub.check_content(b)),
        ("POST", r"/api/test-workflow", "read", lambda h, m, b: h.hub.test_workflow(b)),
        ("GET", r"/api/tools", "read", lambda h, m, b: checks.tools()),
        ("POST", r"/api/inventories/convert", "read", lambda h, m, b: h.hub.convert_inventory(b)),
    ]

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)

    def send(self, status, body, ctype="application/json", headers=None):
        if isinstance(body, dict) and ("_set_cookie" in body or "_clear_cookie" in body):
            token = body.pop("_set_cookie", None)
            body.pop("_clear_cookie", None)
            attrs = "; HttpOnly; SameSite=Strict; Path=/"
            cookie = f"{COOKIE}={token}{attrs}; Max-Age={access.SESSION_SECONDS}" if token else f"{COOKIE}={attrs}; Max-Age=0"
            headers = {**(headers or {}), "Set-Cookie": cookie, "Cache-Control": "no-store"}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "") and not ctype.startswith("application/zip"):
            body = gzip.compress(body, 6)
            headers = {**(headers or {}), "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def host_allowed(self):
        """Refuse unknown host names, so other web pages can't reach the API through DNS rebinding."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.find("]")] if host.startswith("[") else host.split(":")[0]
        if name == "localhost":
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return False

    def serve_index(self):
        path = HERE / "index.html"
        st = path.stat()
        cache = Handler._index
        if cache["mtime"] != st.st_mtime_ns:
            raw = path.read_bytes()
            cache.update(mtime=st.st_mtime_ns, raw=raw, gz=gzip.compress(raw, 9),
                         etag='"%s"' % hashlib.sha1(raw).hexdigest()[:16])
        common = {"ETag": cache["etag"], "Cache-Control": "no-cache"}
        if self.headers.get("If-None-Match") == cache["etag"]:
            self.send_response(304)
            for k, v in common.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = cache["gz"] if gz else cache["raw"]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for k, v in common.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_any(self):
        try:
            if not self.host_allowed():
                raise ApiError(403, "Unknown host")
            path = urlparse(self.path).path
            if self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
                return self.serve_index()
            if not path.startswith("/api/"):
                raise ApiError(404, "Not found")
            # Custom header forces a CORS preflight, which this server never approves,
            # so other sites open in the browser can't change the database.
            if self.command not in ("GET", "HEAD") and self.headers.get("X-Hub") != "1":
                raise ApiError(403, "Missing X-Hub header")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "Request too large")
            body = {}
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    raise ApiError(400, "Body must be JSON")
            method = "GET" if self.command == "HEAD" else self.command
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:
                pass
            self.token = jar[COOKIE].value if COOKIE in jar else None
            self.user = self.hub.session_user(self.token)
            self.query = {k: v for k, _, v in (p.partition("=") for p in urlparse(self.path).query.split("&") if p)}
            for m, pattern, perm, fn in self.ROUTES:
                match = re.fullmatch(pattern, path)
                if match and m == method:
                    if perm and not self.user:
                        raise ApiError(401, "Sign in to continue")
                    if perm and perm not in access.perms(self.user["role"]):
                        raise ApiError(403, f"Your role ({self.user['roleLabel']}) can't do this")
                    result = fn(self, match, body if isinstance(body, dict) else {})
                    if isinstance(result, tuple):  # (filename, bytes) download
                        name, data = result
                        return self.send(200, data, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
                    return self.send(200, result)
            raise ApiError(405 if any(re.fullmatch(p, path) for _, p, _, _ in self.ROUTES) else 404, "Not found")
        except ApiError as e:
            self.send(e.status, {"error": str(e)})
        except sqlite3.IntegrityError as e:
            self.send(409, {"error": f"Database constraint: {e}"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the server up, report the error to the UI
            self.log_error("%s %s failed: %r", self.command, self.path, e)
            self.send(500, {"error": f"Server error: {e}"})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = handle_any


def main():
    ap = argparse.ArgumentParser(description="Automation Hub server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(HERE / "data" / "hub.db"))
    ap.add_argument("--repo", default=str(HERE.parent), help="repository the hub imports project files from")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    ap.add_argument("--reset-password", metavar="USER", help="set a new password for USER (asks for it) and exit")
    args = ap.parse_args()

    Handler.hub = Hub(args.db, args.repo)
    if args.reset_password:
        return Handler.hub.reset_password_cli(args.reset_password)
    Handler.verbose = args.verbose
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Automation Hub listening on {args.host}:{args.port} (database {args.db}, repo {Handler.hub.repo})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
