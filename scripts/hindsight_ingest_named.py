#!/usr/bin/env python3
"""Submit a reviewed named-chat export to one Hindsight bank, once per document.

Checkpoint records an intent before each POST. An interrupted/uncertain POST must
be reconciled manually; this tool never automatically replays it.
"""

# ruff: noqa: C901, PLR0912, PLR0915, TRY003, EM101, EM102, PLR2004, T201, S310
import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def request(url, payload=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid HTTP endpoint")
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if os.environ.get("HINDSIGHT_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["HINDSIGHT_API_KEY"]
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def save(path, state):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:18888")
    parser.add_argument("--batch-size", type=int, default=25, choices=range(1, 26))
    args = parser.parse_args()
    base = args.url.rstrip("/") + "/v1/default/banks/ergonaut"
    state = (
        json.loads(args.checkpoint.read_text())
        if args.checkpoint.exists()
        else {"batches": [], "skipped": []}
    )
    if any(b["status"] in ("intent", "submitted") for b in state["batches"]):
        raise SystemExit(
            "Uncertain prior submission: reconcile intent before proceeding; never replay automatically."
        )
    completed = {i for b in state["batches"] for i in b["ids"]} | set(state["skipped"])
    rows = json.loads((args.export / "summary.json").read_text())
    if any(r["bank"] not in ("ergonaut", None) or r["role"] != "chat" for r in rows):
        raise SystemExit("Export must contain named chats only, in ergonaut bank.")
    for row in rows:
        path = args.export / f"{row['bot']}-chat-{row['session']}.jsonl"
        if not path.exists():
            if row["documents"] and row["bank"]:
                raise SystemExit(f"Missing export: {path}")
            continue
        items = [json.loads(line) for line in path.read_text().splitlines()]
        if len(items) != row["documents"] or row["bank"] != "ergonaut":
            raise SystemExit(f"Invalid export: {path}")
        for start in range(0, len(items), args.batch_size):
            batch = []
            for item in items[start : start + args.batch_size]:
                docid = item["document_id"]
                if not docid.startswith("ergonaut-turn-"):
                    raise SystemExit("Invalid document ID")
                if docid in completed:
                    continue
                try:
                    request(base + "/documents/" + docid)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                else:
                    state["skipped"].append(docid)
                    completed.add(docid)
                    save(args.checkpoint, state)
                    continue
                batch.append(item)
            if not batch:
                continue
            record = {
                "chat": row["title"],
                "ids": [i["document_id"] for i in batch],
                "operation_id": str(uuid.uuid4()),
                "status": "intent",
            }
            state["batches"].append(record)
            save(args.checkpoint, state)
            response = request(
                base + "/memories",
                {"items": batch, "async": True, "operation_id": record["operation_id"]},
            )
            if (
                not response.get("success")
                or response.get("operation_id") != record["operation_id"]
            ):
                raise SystemExit(
                    "Unexpected response; reconcile checkpoint before continuing."
                )
            record["status"] = "submitted"
            save(args.checkpoint, state)
            print(
                f"Submitted {row['title']}: {len(batch)}, operation {record['operation_id']}",
                flush=True,
            )
            while True:
                op = request(base + "/operations/" + record["operation_id"])
                if op["status"] == "completed":
                    record["status"] = "completed"
                    save(args.checkpoint, state)
                    break
                if op["status"] in ("failed", "cancelled"):
                    raise SystemExit(
                        f"Operation {record['operation_id']} {op['status']}; inspect before continuing"
                    )
                time.sleep(10)
            # Shared bank has unrelated work and delayed retries. Wait for quiet or
            # bounded settlement; record remaining operations for operator review.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                pending = sum(
                    request(base + "/operations?status=" + s + "&limit=1")["total"]
                    for s in ("pending", "processing")
                )
                if not pending:
                    break
                time.sleep(15)
            record["queue_remaining"] = pending
            save(args.checkpoint, state)
            print(f"Settled {row['title']}: queue remaining {pending}", flush=True)
            return  # One bounded batch per invocation.


if __name__ == "__main__":
    main()
