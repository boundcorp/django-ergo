# ruff: noqa: TRY003, EM101, PLR2004, C901, PLR0912, PLR0915
"""Separate trusted controller, never hosted inside Django/Celery.

Single-process HTTP server + durable SQLite ledger. Run exactly one replica on
its own durable volume; restart recovery keeps workspace fences until pods are
confirmed terminated and absent. The key and Kubernetes credentials never enter job pods.
"""

import json
import logging
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path

from django_ergo.analysis.protocol import MAX_REQUEST
from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import encode
from django_ergo.analysis.protocol import identifier
from django_ergo.analysis.protocol import open_envelope
from django_ergo.analysis.protocol import seal

TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}


class Controller:
    def __init__(self, path, runtime, *, retention_days=30):
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("Invalid retention")
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.runtime = runtime
        self.retention = retention_days * 86400
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS nonces (id TEXT PRIMARY KEY, at INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, user TEXT, session TEXT, touched REAL, retired INTEGER DEFAULT 0, purged INTEGER DEFAULT 0, lease TEXT DEFAULT '', lease_until REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, workspace TEXT, request BLOB, state TEXT, started REAL, result BLOB, final TEXT DEFAULT '', stopped INTEGER DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(workspace) WHERE state NOT IN ('succeeded','failed','cancelled','timeout');
        """)
        # Upgrade the private ledger without interpreting old API absence as death.
        if "stopped" not in {
            row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")
        }:
            with self.db:
                self.db.execute("ALTER TABLE jobs ADD COLUMN stopped INTEGER DEFAULT 0")

    def authenticated(self, key, raw):
        message = open_envelope(key, raw)
        try:
            with self.db:
                self.db.execute("DELETE FROM nonces WHERE at < ?", (time.time() - 120,))
                self.db.execute(
                    "INSERT INTO nonces VALUES (?,?)", (message["nonce"], message["at"])
                )
        except sqlite3.IntegrityError as exc:
            raise ProtocolError("Replayed envelope") from exc
        return message["nonce"], self.dispatch(message["body"])

    def workspace(self, body):
        wid = identifier(body["workspace"])
        user, session = str(body["user"]), identifier(body["session"])
        if not user or len(user) > 200:
            raise ProtocolError("Invalid owner")
        row = self.db.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
        if row is None:
            if body["action"] == "inspect":
                return None
            if body["action"] not in {"submit", "acquire", "cancel"}:
                raise ProtocolError("Unknown workspace")
            with self.db:
                self.db.execute(
                    "INSERT INTO workspaces(id,user,session,touched) VALUES (?,?,?,?)",
                    (wid, user, session, time.time()),
                )
            row = self.db.execute(
                "SELECT * FROM workspaces WHERE id=?", (wid,)
            ).fetchone()
        if row["user"] != user or row["session"] != session:
            raise ProtocolError("Workspace ownership mismatch")
        if row["retired"]:
            return None
        return row

    def dispatch(self, body):
        if not isinstance(body, dict) or not {
            "workspace",
            "session",
            "user",
            "action",
        } <= set(body):
            raise ProtocolError("Malformed request")
        self.reconcile()
        workspace = self.workspace(body)
        if workspace is None:
            return {"state": "retired"}
        wid = workspace["id"]
        now = time.time()
        if body["action"] == "inspect":
            return {"state": "active"}
        with self.db:
            self.db.execute("UPDATE workspaces SET touched=? WHERE id=?", (now, wid))
        action = body["action"]
        if action in {"acquire", "release"}:
            lease = identifier(body["lease"])
            active = self.db.execute(
                "SELECT 1 FROM jobs WHERE workspace=? AND state NOT IN ('succeeded','failed','cancelled','timeout')",
                (wid,),
            ).fetchone()
            if active or (
                workspace["lease_until"] > now and workspace["lease"] != lease
            ):
                raise ProtocolError("Workspace busy")
            with self.db:
                self.db.execute(
                    "UPDATE workspaces SET lease=?,lease_until=?,touched=? WHERE id=?",
                    (
                        lease if action == "acquire" else "",
                        now + 300 if action == "acquire" else 0,
                        now,
                        wid,
                    ),
                )
            return {"state": "leased" if action == "acquire" else "released"}
        if action == "submit":
            if (
                workspace["lease_until"] > now
                and body.get("lease") != workspace["lease"]
            ):
                raise ProtocolError("Transfer lease held")
            job = identifier(body["job"])
            request = body["request"]
            if not isinstance(request, dict) or request.get("op") not in {
                "python",
                "dataset",
                "select",
                "list",
                "write",
                "read",
            }:
                raise ProtocolError("Unknown operation")
            request_bytes = encode(request, limit=64 * 1024)
            existing = self.db.execute(
                "SELECT * FROM jobs WHERE id=?", (job,)
            ).fetchone()
            if existing:
                if existing["workspace"] != wid or existing["request"] != request_bytes:
                    raise ProtocolError("Job identity conflict")
                return self.job_result(existing)
            try:
                with self.db:
                    self.db.execute(
                        "INSERT INTO jobs(id,workspace,request,state,started) VALUES (?,?,?,'launching',?)",
                        (job, wid, request_bytes, now),
                    )
                    self.db.execute(
                        "UPDATE workspaces SET touched=?,lease_until=CASE WHEN lease != '' THEN ? ELSE 0 END WHERE id=?",
                        (now, now + 300, wid),
                    )
            except sqlite3.IntegrityError as exc:
                raise ProtocolError("Workspace busy") from exc
            # Durable intent precedes side effect. If launch raises ambiguously, keep fence;
            # reconcile observes the deterministic pod name and eventually reaps it.
            try:
                self.runtime.launch(job, wid, request)
            except Exception:  # noqa: BLE001 — preserve durable fence across backend errors
                return {"job": job, "state": "launching"}
            with self.db:
                self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        elif action in {"status", "cancel"}:
            job = identifier(body["job"])
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row is not None and row["workspace"] != wid:
                raise ProtocolError("Unknown job")
            if row is None:
                # A lost submit reply does not tell the app whether launch happened.
                # If it did not, persist cancellation before a delayed submit arrives.
                if action != "cancel" or not isinstance(body.get("request"), dict):
                    raise ProtocolError("Unknown job")
                request_bytes = encode(body["request"], limit=64 * 1024)
                with self.db:
                    self.db.execute(
                        "INSERT INTO jobs(id,workspace,request,state,started,result,final,stopped) VALUES (?,?,?,'cancelled',?,?,'cancelled',1)",
                        (
                            job,
                            wid,
                            request_bytes,
                            now,
                            encode({"ok": False, "error": "Cancelled before launch"}),
                        ),
                    )
                row = self.db.execute(
                    "SELECT * FROM jobs WHERE id=?", (job,)
                ).fetchone()
            if action == "cancel" and row["state"] not in TERMINAL:
                with self.db:
                    self.db.execute(
                        "UPDATE jobs SET state='reaping',final='cancelled',result=? WHERE id=?",
                        (encode({"ok": False, "error": "Cancelled"}), job),
                    )
                self.reconcile()
        else:
            raise ProtocolError("Unknown action")
        row = self.db.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace=?", (job, wid)
        ).fetchone()
        return self.job_result(row)

    def job_result(self, row):
        value = {"job": row["id"], "state": row["state"]}
        if row["state"] in TERMINAL:
            value["response"] = json.loads(
                row["result"] or b'{"ok":false,"error":"Job failed"}'
            )
        return value

    def reconcile(self):
        now = time.time()
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE state NOT IN ('succeeded','failed','cancelled','timeout')"
        ).fetchall()
        for row in rows:
            phase = self.runtime.status(row["id"])
            state, final, result = row["state"], row["final"], row["result"]
            stopped = row["stopped"] or phase in {"succeeded", "failed"}
            if state != "reaping":
                if phase in {"succeeded", "failed"}:
                    try:
                        result = (
                            encode(self.runtime.result(row["id"]))
                            if phase == "succeeded"
                            else encode({"ok": False, "error": "Pod failed"})
                        )
                    except Exception:  # noqa: BLE001 — malformed/lost output cannot prevent reaping
                        result = encode(
                            {"ok": False, "error": "Executor result unavailable"}
                        )
                    final = (
                        "succeeded"
                        if phase == "succeeded" and json.loads(result).get("ok") is True
                        else "failed"
                    )
                    state = "reaping"
                elif now - row["started"] > 90:
                    state, final, result = (
                        "reaping",
                        "timeout",
                        encode({"ok": False, "error": "Job deadline exceeded"}),
                    )
                elif phase == "absent" and row["state"] == "running":
                    state, final, result = (
                        "reaping",
                        "failed",
                        encode({"ok": False, "error": "Pod disappeared"}),
                    )
                elif phase not in {"absent", "pending"}:
                    state = "running"
            # Commit termination evidence and reaping intent BEFORE API mutations.
            # A crash after finalizer removal must not erase the evidence; absence
            # alone (including a lost node or ambiguous launch) never releases a fence.
            with self.db:
                self.db.execute(
                    "UPDATE jobs SET state=?,final=?,result=?,stopped=? WHERE id=?",
                    (state, final, result, int(stopped), row["id"]),
                )
            if state == "reaping":
                self.runtime.delete_job(row["id"])
                if stopped:
                    self.runtime.release_job(row["id"])
                    if self.runtime.status(row["id"]) == "absent":
                        state = final
            with self.db:
                self.db.execute(
                    "UPDATE jobs SET state=? WHERE id=?", (state, row["id"])
                )
                self.db.execute(
                    "UPDATE workspaces SET touched=? WHERE id=?",
                    (now, row["workspace"]),
                )
        stale = self.db.execute(
            "SELECT * FROM workspaces w WHERE purged=0 AND touched < ? AND lease_until < ? AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace=w.id AND j.state NOT IN ('succeeded','failed','cancelled','timeout'))",
            (now - self.retention, now),
        ).fetchall()
        for row in stale:
            # Durable tombstone before storage deletion. Retried after a controller crash.
            with self.db:
                self.db.execute(
                    "UPDATE workspaces SET retired=1 WHERE id=?", (row["id"],)
                )
                self.db.execute("DELETE FROM jobs WHERE workspace=?", (row["id"],))
            if self.runtime.delete_workspace(row["id"]) is True:
                with self.db:
                    self.db.execute(
                        "UPDATE workspaces SET purged=1 WHERE id=?", (row["id"],)
                    )


def make_server(controller, key, host="127.0.0.1", port=8080):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log bodies, authentication, code, filenames or pod output.

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1" or not 0 < length <= MAX_REQUEST:
                    raise ProtocolError("Invalid request")  # noqa: TRY301
                raw = self.rfile.read(length)
                nonce, response = controller.authenticated(key, raw)
                data = seal(key, {"request_nonce": nonce, "response": response})
                self.send_response(200)
            except (ProtocolError, ValueError, KeyError, TypeError):
                data = b'{"error":"Rejected request"}'
                self.send_response(400)
            except Exception:  # noqa: BLE001 — preserve durable fence across backend errors
                data = b'{"error":"Controller unavailable"}'
                self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            return connection, address

        def service_actions(self):
            try:
                controller.reconcile()
            except Exception:  # noqa: BLE001 — preserve durable fence across backend errors
                logging.getLogger(__name__).error(  # noqa: TRY400 — exception may contain sensitive data
                    "Analysis reconciliation failed; fences retained"
                )

    return Server((host, port), Handler)


def serve(controller, key, host="127.0.0.1", port=8080):
    make_server(controller, key, host, port).serve_forever(poll_interval=1)


def main():
    from kubernetes import client
    from kubernetes import config

    from django_ergo.analysis.kubernetes import KubernetesRuntime

    if os.environ.get("ANALYSIS_CONTROLLER_APPROVED") != "yes":
        raise RuntimeError(
            "Infrastructure approval and prerequisite verification required"
        )
    config.load_incluster_config()
    runtime = KubernetesRuntime(
        core=client.CoreV1Api(),
        namespace=os.environ["ANALYSIS_NAMESPACE"],
        image=os.environ["ANALYSIS_IMAGE"],
        storage_class=os.environ["ANALYSIS_STORAGE_CLASS"],
        runtime_class=os.environ["ANALYSIS_RUNTIME_CLASS"],
    )
    controller = Controller(
        os.environ["ANALYSIS_LEDGER"],
        runtime,
        retention_days=int(os.environ.get("ANALYSIS_RETENTION_DAYS", "30")),
    )
    key = Path(os.environ["ANALYSIS_KEY_FILE"]).read_bytes()
    if len(key) < 32:
        raise RuntimeError("Invalid controller key")
    serve(controller, key, host="0.0.0.0")


if __name__ == "__main__":
    main()
