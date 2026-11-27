"""Offline protocol/runtime tests. Fake Kubernetes is NOT an isolation smoke test."""

import base64
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

from django_ergo.analysis.client import ControllerClient
from django_ergo.analysis.controller import Controller
from django_ergo.analysis.kubernetes import KubernetesRuntime
from django_ergo.analysis.protocol import MAX_REQUEST
from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import encode
from django_ergo.analysis.protocol import open_envelope
from django_ergo.analysis.protocol import seal
from django_ergo.analysis.query import InvalidQueryError
from django_ergo.analysis.query import compile_select

KEY = b"offline-test-key-only-32-bytes-long"


def uid():
    return str(uuid.uuid4())


class Runtime:
    def __init__(self):
        self.pods = {}
        self.deleted = []
        self.hold_deletion = False
        self.launches = 0

    def launch(self, job, workspace, request):
        self.launches += 1
        self.pods[job] = "running"

    def status(self, job):
        return self.pods.get(job, "absent")

    def result(self, job):
        return {"ok": True, "result": {"output": "ok"}}

    def delete_job(self, job):
        if not self.hold_deletion:
            self.pods.pop(job, None)

    def delete_workspace(self, workspace):
        self.deleted.append(workspace)
        return True


@pytest.fixture
def broker(tmp_path):
    runtime = Runtime()
    controller = Controller(str(tmp_path / "ledger.sqlite"), runtime)
    body = {
        "workspace": uid(),
        "session": uid(),
        "user": "1",
        "action": "submit",
        "job": uid(),
        "request": {"op": "python", "code": "print(1)"},
    }
    return controller, runtime, body


def test_signed_envelopes_reject_tamper_expiry_replay_and_oversize(broker):
    controller, _, body = broker
    raw = seal(KEY, body)
    controller.authenticated(KEY, raw)
    with pytest.raises(ProtocolError, match="Replayed"):
        controller.authenticated(KEY, raw)
    value = json.loads(raw)
    value["message"]["body"]["user"] = "2"
    with pytest.raises(ProtocolError, match="Unauthenticated"):
        open_envelope(KEY, json.dumps(value).encode())
    with pytest.raises(ProtocolError, match="Expired"):
        open_envelope(KEY, seal(KEY, body, now=1))
    with pytest.raises(ProtocolError, match="byte limit"):
        encode({"data": "x" * MAX_REQUEST})


def test_ownership_idempotence_and_one_active_job(broker):
    controller, runtime, body = broker
    controller.dispatch(body)
    controller.dispatch(body)
    assert runtime.launches == 1
    with pytest.raises(ProtocolError, match="ownership"):
        controller.dispatch({**body, "user": "2"})
    with pytest.raises(ProtocolError, match="ownership"):
        controller.dispatch({**body, "session": uid()})
    with pytest.raises(ProtocolError, match="busy"):
        controller.dispatch({**body, "job": uid()})
    with pytest.raises(ProtocolError, match="conflict"):
        controller.dispatch({**body, "request": {"op": "list"}})
    with pytest.raises(ProtocolError, match="Unknown job"):
        controller.dispatch({**body, "action": "status", "job": uid()})


def test_cancellation_keeps_fence_until_actual_deletion(broker):
    controller, runtime, body = broker
    controller.dispatch(body)
    runtime.hold_deletion = True
    assert controller.dispatch({**body, "action": "cancel"})["state"] == "reaping"
    with pytest.raises(ProtocolError, match="busy"):
        controller.dispatch({**body, "job": uid()})
    runtime.hold_deletion = False
    assert controller.dispatch({**body, "action": "status"})["state"] == "cancelled"
    controller.dispatch({**body, "job": uid()})


def test_timeout_recovery_and_stale_cleanup(broker, tmp_path):
    controller, runtime, body = broker
    controller.dispatch(body)
    with controller.db:
        controller.db.execute("UPDATE jobs SET started=?", (time.time() - 100,))
    # A new process reads durable fences and reaps the old pod.
    restarted = Controller(str(tmp_path / "ledger.sqlite"), runtime)
    assert restarted.dispatch({**body, "action": "status"})["state"] == "timeout"
    with restarted.db:
        restarted.db.execute(
            "UPDATE workspaces SET touched=?", (time.time() - 31 * 86400,)
        )
    restarted.reconcile()
    assert body["workspace"] in runtime.deleted
    assert restarted.dispatch(body) == {"state": "retired"}


def test_active_and_transfer_leases_exclude_cleanup(broker):
    controller, runtime, body = broker
    controller.dispatch(body)
    with controller.db:
        controller.db.execute("UPDATE workspaces SET touched=0")
    controller.reconcile()
    assert runtime.deleted == []
    controller.dispatch({**body, "action": "cancel"})
    lease = uid()
    controller.dispatch({**body, "action": "acquire", "lease": lease})
    with controller.db:
        controller.db.execute("UPDATE workspaces SET touched=0")
    controller.reconcile()
    assert runtime.deleted == []
    with pytest.raises(ProtocolError, match="lease"):
        controller.dispatch({**body, "job": uid()})
    controller.dispatch({**body, "job": uid(), "lease": lease})


def test_launch_ambiguity_preserves_durable_fence(broker, monkeypatch):
    controller, runtime, body = broker

    def ambiguous(*args):
        runtime.pods[body["job"]] = "running"
        raise TimeoutError

    monkeypatch.setattr(runtime, "launch", ambiguous)
    assert controller.dispatch(body)["state"] == "launching"
    with pytest.raises(ProtocolError, match="busy"):
        controller.dispatch({**body, "job": uid()})
    assert controller.dispatch({**body, "action": "cancel"})["state"] == "cancelled"


def test_http_client_checks_reply_binding_and_stream_cap(broker):
    controller, _, body = broker

    def handler(request):
        nonce, result = controller.authenticated(KEY, request.content)
        return httpx.Response(
            200, content=seal(KEY, {"request_nonce": nonce, "response": result})
        )

    client = ControllerClient(
        "https://controller.test", KEY, transport=httpx.MockTransport(handler)
    )
    assert client.call(body)["state"] == "running"
    client.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=seal(KEY, {"request_nonce": uid(), "response": {}})
        )
    )
    with pytest.raises(ProtocolError, match="bind"):
        client.call(body)
    client.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (MAX_REQUEST + 1))
    )
    with pytest.raises(ProtocolError, match="byte cap"):
        client.call(body)
    client.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"{}")
    )
    with pytest.raises(ProtocolError):
        client.call(body)


def test_kubernetes_spec_contains_boundary_constraints():
    runtime = KubernetesRuntime(
        core=None,
        namespace="analysis",
        image="registry/runner@sha256:" + "a" * 64,
        storage_class="hard-quota",
        runtime_class="gvisor",
    )
    spec = runtime.pod_spec(uid(), uid(), {"op": "python", "code": "print(1)"})["spec"]
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["activeDeadlineSeconds"] == 60
    assert spec["securityContext"]["runAsNonRoot"]
    container = spec["containers"][0]
    assert container["securityContext"] == {
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["limits"]["memory"] == "512Mi"
    assert {e["name"] for e in container["env"]} == {
        "ANALYSIS_REQUEST",
        "HOME",
        "MPLBACKEND",
        "OPENBLAS_NUM_THREADS",
    }
    assert all(
        "hostPath" not in volume and "secret" not in volume
        for volume in spec["volumes"]
    )
    assert len([v for v in spec["volumes"] if "persistentVolumeClaim" in v]) == 1
    with pytest.raises(ValueError, match="Pinned"):
        KubernetesRuntime(
            core=None,
            namespace="analysis",
            image="runner:latest",
            storage_class="x",
            runtime_class="gvisor",
        )


@pytest.mark.parametrize(
    "spec",
    [
        {"columns": ["x; DROP TABLE data"]},
        {"limit": True},
        {"limit": 1001},
        {"columns": ["x", "x"]},
        {"group_by": ["missing"]},
        {"aggregates": [{"op": "evil", "column": "x", "as": "a"}]},
        {"sort": [{"column": "x", "direction": "desc;--"}]},
        {"sql": "select * from data"},
        {"columns": []},
    ],
)
def test_bad_selects(spec):
    with pytest.raises(InvalidQueryError):
        compile_select(spec, ["x"])


@pytest.fixture
def executor(tmp_path, monkeypatch):
    from django_ergo.analysis import executor

    monkeypatch.setattr(executor, "ROOT", tmp_path)
    return executor


def test_all_readers_grouping_filters_and_count(executor):
    import pandas as pd

    frame = pd.DataFrame(
        {"region": ["west", "west", "east", "east"], "amount": [2, 3, 4, 1]}
    )
    for suffix in ("csv", "parquet", "xlsx"):
        path = executor.ROOT / ("data." + suffix)
        if suffix == "csv":
            frame.to_csv(path, index=False)
        elif suffix == "parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_excel(path, index=False)
        dataset = executor.run({"op": "dataset", "file": path.name})
        assert dataset["rows"] == 4
        result = executor.run(
            {
                "op": "select",
                "file": dataset["file"],
                "version": dataset["version"],
                "query": {
                    "group_by": ["region"],
                    "where": {"op": "ge", "column": "amount", "value": 2},
                    "aggregates": [{"op": "sum", "column": "amount", "as": "total"}],
                    "sort": [{"column": "total", "direction": "desc"}],
                    "limit": 1,
                },
            }
        )
        assert result["rows"] == [["west", 5]]
        assert result["total_rows"] == 2
        assert result["source_rows"] == 4
        assert result["next_offset"] == 1
        assert result["truncated"]


def test_stable_pagination_version_and_bytes(executor):
    (executor.ROOT / "data.csv").write_text("x,y\n2,b\n1,c\n1,a\n")
    dataset = executor.run({"op": "dataset", "file": "data.csv"})
    request = {
        "op": "select",
        "file": dataset["file"],
        "version": dataset["version"],
        "query": {"columns": ["x", "y"], "limit": 2},
    }
    result = executor.run(request)
    assert result["rows"] == [[1, "a"], [1, "c"]]
    assert executor.run({**request, "offset": 2})["rows"] == [[2, "b"]]
    import pandas as pd

    pd.DataFrame({"x": [9], "y": ["changed"]}).to_parquet(
        executor.ROOT / dataset["file"], index=False
    )
    with pytest.raises(ValueError, match="version changed"):
        executor.run(request)
    (executor.ROOT / "large.csv").write_text("x\n" + "z" * 50000 + "\n")
    with pytest.raises(ValueError, match="byte cap"):
        executor.run({"op": "select", "file": "large.csv", "query": {"columns": ["x"]}})


@pytest.mark.parametrize(
    "name", ["../secret", "/etc/passwd", "a/b", ".hidden", "a..b", "x\x00"]
)
def test_traversal_rejected(executor, name):
    with pytest.raises(ValueError, match="flat workspace"):
        executor.open_file(name)


def test_links_and_stream_caps(executor):
    (executor.ROOT / "source").write_bytes(b"hello")
    (executor.ROOT / "link").symlink_to(executor.ROOT / "source")
    with pytest.raises(OSError, match="Too many levels"):
        executor.open_file("link")
    import os

    os.link(executor.ROOT / "source", executor.ROOT / "hard")
    with pytest.raises(ValueError, match="one link"):
        executor.open_file("hard")
    with pytest.raises(ValueError, match="cap"):
        executor.run(
            {
                "op": "write",
                "file": "big",
                "offset": 0,
                "data": base64.b64encode(b"x" * 17000).decode(),
            }
        )
    result = executor.run(
        {
            "op": "write",
            "file": "new.csv",
            "offset": 0,
            "data": base64.b64encode(b"x\n1\n").decode(),
        }
    )
    assert result["bytes"] == 4
    with pytest.raises(ValueError, match="offset"):
        executor.run({"op": "write", "file": "new.csv", "offset": 1, "data": "eA=="})
    assert executor.run({"op": "read", "file": "new.csv", "offset": 0})["size"] == 4


def test_trusted_python_fixture_output_cap(executor):
    # Known constant fixture only. This local test does not establish isolation.
    result = executor.run({"op": "python", "code": "print('x' * 60000)"})
    assert result["truncated"] is True
    assert result["reason"] == "output_limit"
    assert len(result["output"]) <= executor.RESULT_BYTES


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ({"op": "eq", "column": "n", "value": 2}, [2]),
        ({"op": "ne", "column": "n", "value": 2}, [1, 3]),
        ({"op": "lt", "column": "n", "value": 2}, [1]),
        ({"op": "le", "column": "n", "value": 2}, [1, 2]),
        ({"op": "gt", "column": "n", "value": 2}, [3]),
        ({"op": "ge", "column": "n", "value": 2}, [2, 3]),
        ({"op": "in", "column": "n", "values": [1, 3]}, [1, 3]),
        ({"op": "is_null", "column": "n"}, [None]),
        ({"op": "not_null", "column": "n"}, [1, 2, 3]),
        ({"op": "not", "arg": {"op": "eq", "column": "n", "value": 2}}, [1, 3]),
        (
            {
                "op": "or",
                "args": [
                    {"op": "eq", "column": "n", "value": 1},
                    {"op": "eq", "column": "n", "value": 3},
                ],
            },
            [1, 3],
        ),
    ],
)
def test_every_predicate_with_null_semantics(predicate, expected):
    import duckdb

    compiled = compile_select({"columns": ["n"], "where": predicate}, ["n"])
    with duckdb.connect(config={"enable_external_access": False}) as con:
        con.execute("CREATE TABLE data(n INTEGER)")
        con.execute("INSERT INTO data VALUES (1),(2),(3),(NULL)")
        assert [
            r[0] for r in con.execute(compiled.sql, compiled.params).fetchall()
        ] == expected


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("count", 3),
        ("count_distinct", 2),
        ("sum", 4),
        ("avg", 4 / 3),
        ("min", 1),
        ("max", 2),
    ],
)
def test_every_aggregate(operation, expected):
    import duckdb

    compiled = compile_select(
        {"aggregates": [{"op": operation, "column": "n", "as": "answer"}]}, ["n"]
    )
    with duckdb.connect(config={"enable_external_access": False}) as con:
        con.execute("CREATE TABLE data(n INTEGER)")
        con.execute("INSERT INTO data VALUES (1),(1),(2),(NULL)")
        assert con.execute(compiled.sql, compiled.params).fetchone()[0] == expected


def test_sort_nulls_and_empty_group_results():
    import duckdb

    with duckdb.connect(config={"enable_external_access": False}) as con:
        con.execute("CREATE TABLE data(n INTEGER)")
        con.execute("INSERT INTO data VALUES (1),(3),(NULL)")
        compiled = compile_select(
            {"columns": ["n"], "sort": [{"column": "n", "direction": "desc"}]}, ["n"]
        )
        assert con.execute(compiled.sql, compiled.params).fetchall() == [
            (3,),
            (1,),
            (None,),
        ]
        compiled = compile_select(
            {
                "group_by": ["n"],
                "where": {"op": "gt", "column": "n", "value": 10},
                "aggregates": [{"op": "count", "column": "*", "as": "c"}],
            },
            ["n"],
        )
        assert con.execute(compiled.sql, compiled.params).fetchall() == []


def test_duckdb_external_sql_and_extensions_disabled():
    import duckdb

    with duckdb.connect(
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
    ) as con:
        with pytest.raises(duckdb.Error, match="disabled"):
            con.execute("SELECT * FROM read_csv('/etc/passwd')")
        with pytest.raises(duckdb.Error, match="disabled"):
            con.execute("INSTALL httpfs")


def test_malformed_or_lost_pod_result_cannot_block_reaping(broker, monkeypatch):
    controller, runtime, body = broker
    controller.dispatch(body)
    runtime.pods[body["job"]] = "succeeded"

    def lost_result(job):
        message = "fixture log transport failure"
        raise OSError(message)

    monkeypatch.setattr(runtime, "result", lost_result)
    response = controller.dispatch({**body, "action": "status"})
    assert response["state"] == "failed"
    assert response["response"]["error"] == "Executor result unavailable"
    assert runtime.pods == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        '{"ok":true,"result":[]}',
        '{"ok":true,"result":{"n":NaN}}',
        "x" * 66000,
    ],
)
def test_kubernetes_rejects_malformed_pod_outputs(raw):
    runtime = KubernetesRuntime(
        core=SimpleNamespace(read_namespaced_pod_log=lambda *args, **kwargs: raw),
        namespace="analysis",
        image="registry/runner@sha256:" + "a" * 64,
        storage_class="hard-quota",
        runtime_class="gvisor",
    )
    assert runtime.result(uid())["ok"] is False


def test_cleanup_crash_retries_storage_without_resurrecting_workspace(
    broker, monkeypatch
):
    controller, runtime, body = broker
    controller.dispatch(body)
    controller.dispatch({**body, "action": "cancel"})
    with controller.db:
        controller.db.execute("UPDATE workspaces SET touched=0")

    def deletion_error(workspace):
        message = "fixture storage unavailable after tombstone"
        raise OSError(message)

    monkeypatch.setattr(runtime, "delete_workspace", deletion_error)
    with pytest.raises(OSError, match="tombstone"):
        controller.reconcile()
    row = controller.db.execute("SELECT retired,purged FROM workspaces").fetchone()
    assert tuple(row) == (1, 0)
    monkeypatch.setattr(runtime, "delete_workspace", lambda workspace: True)
    controller.reconcile()
    row = controller.db.execute("SELECT retired,purged FROM workspaces").fetchone()
    assert tuple(row) == (1, 1)
    assert controller.dispatch(body) == {"state": "retired"}


def test_trusted_python_fixture_timeout_kills_child(executor, monkeypatch):
    moments = iter([0, 56])
    monkeypatch.setattr(
        executor, "time", SimpleNamespace(monotonic=lambda: next(moments))
    )
    result = executor.run({"op": "python", "code": "import time; time.sleep(20)"})
    assert result["reason"] == "timeout"
    assert result["exit_code"] != 0


def test_case_ambiguous_columns_and_aliases_rejected(executor):
    (executor.ROOT / "ambiguous.csv").write_text("amount,AMOUNT\n1,999\n")
    with pytest.raises(ValueError, match="ambiguous"):
        executor.run({"op": "dataset", "file": "ambiguous.csv"})
    with pytest.raises(InvalidQueryError, match="Duplicate"):
        compile_select(
            {
                "aggregates": [
                    {"op": "sum", "column": "x", "as": "total"},
                    {"op": "count", "column": "*", "as": "TOTAL"},
                ]
            },
            ["x"],
        )
