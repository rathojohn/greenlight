import base64
import gzip
import http.client
import json
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from greenlight import analysis, otel
from greenlight.db import connect

ROOT = Path(__file__).resolve().parents[1]


class Collector:
    """Records OTLP/HTTP JSON requests like a Collector's otlp receiver would accept them."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def spans(self):
        return [s for r in self.requests if r["path"] == "/v1/traces"
                for rs in r["body"]["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]

    def metrics(self):
        return {m["name"]: m for r in self.requests if r["path"] == "/v1/metrics"
                for rm in r["body"]["resourceMetrics"] for sm in rm["scopeMetrics"] for m in sm["metrics"]}

    def close(self):
        self.server.shutdown()


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    db = tmp_path_factory.mktemp("otel") / "demo.db"
    subprocess.run([sys.executable, "-m", "greenlight.demo", "--db", str(db), "--days", "14"],
                   check=True, capture_output=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    return str(db)


@pytest.fixture
def collector(monkeypatch):
    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"):
        monkeypatch.delenv(var, raising=False)
    c = Collector()
    yield c
    c.close()


def fresh_copy(demo, tmp_path):
    dst = tmp_path / "copy.db"
    with closing(connect(demo, readonly=True)) as src, closing(connect(str(dst))) as out:
        src.backup(out)
    return str(dst)


def test_export_sends_semconv_traces_and_metrics(demo, collector, tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=abc%20def,team=ci")
    db = fresh_copy(demo, tmp_path)
    with closing(connect(db)) as conn:
        res = otel.export(conn, "me/game", collector.url)
    assert res["spans"] > 100 and res["rejected_spans"] == 0
    req = collector.requests[0]
    sent = {k.lower(): v for k, v in req["headers"].items()}  # header names are case-insensitive
    assert sent["x-api-key"] == "abc def" and sent["team"] == "ci"
    resource = otel.read_attrs(req["body"]["resourceSpans"][0]["resource"]["attributes"])
    assert resource["service.name"] == "game" and resource["vcs.repository.url.full"] == "https://github.com/me/game"

    spans = collector.spans()
    for s in spans:
        assert len(s["traceId"]) == 32 and len(s["spanId"]) == 16 and int(s["traceId"], 16)
    ids = {(s["traceId"], s["spanId"]) for s in spans}
    assert all((s["traceId"], s["parentSpanId"]) in ids for s in spans if "parentSpanId" in s)

    roots = [s for s in spans if s["name"].startswith("RUN ")]
    failed = next(s for s in roots if s["status"]["code"] == otel.ERROR)
    a = otel.read_attrs(failed["attributes"])
    assert failed["kind"] == otel.SERVER and a["cicd.pipeline.name"] == "CI" and a["cicd.pipeline.result"] == "failure"
    assert a["error.type"] == "failure" and a["cicd.pipeline.action.name"] == "RUN" and a["vcs.ref.head.revision"]
    jobs = [s for s in spans if s.get("parentSpanId") == failed["spanId"] and s["name"] != "queue"]
    ja = otel.read_attrs(jobs[0]["attributes"])
    assert {"cicd.pipeline.task.name", "cicd.pipeline.task.run.id", "cicd.pipeline.task.run.result"} <= set(ja)
    assert any(otel.read_attrs(j["attributes"]).get("cicd.pipeline.task.type") == "test" for j in jobs)

    tests = [s for s in spans if "test.case.name" in otel.read_attrs(s["attributes"])]
    fails = [s for s in tests if otel.read_attrs(s["attributes"])["test.case.result.status"] == "fail"]
    assert fails and all(s["events"][0]["name"] == "exception" for s in fails)
    assert any(otel.read_attrs(s["attributes"]).get("greenlight.test.classification") == "flaky" for s in fails)
    run_roots = [s for s in spans if "greenlight.gate.decision" in otel.read_attrs(s["attributes"])]
    assert {otel.read_attrs(s["attributes"])["greenlight.gate.decision"] for s in run_roots} >= {"PASS", "REAL_FAILURE"}
    deploys = [s for s in spans if s["name"].startswith("deploy ")]
    assert deploys and otel.read_attrs(deploys[0]["attributes"])["deployment.status"] == "succeeded"

    m = collector.metrics()
    h = m["cicd.pipeline.run.duration"]["histogram"]
    assert h["aggregationTemporality"] == 2
    for p in h["dataPoints"]:
        assert sum(int(c) for c in p["bucketCounts"]) == int(p["count"])
        assert len(p["bucketCounts"]) == len(p["explicitBounds"]) + 1
    assert {"cicd.pipeline.run.errors", "vcs.change.count", "greenlight.tests", "greenlight.dora.deployment_frequency",
            "greenlight.dora.lead_time_p50", "greenlight.runs.rerun_ratio"} <= set(m)

    # only new items go the second time; metrics are current values and always go
    with closing(connect(db)) as conn:
        again = otel.export(conn, "me/game", collector.url)
    assert again["spans"] == 0 and again["metrics"] == res["metrics"]


def test_payloads_match_the_otlp_protobuf_schema(demo, collector, tmp_path):
    pytest.importorskip("opentelemetry.proto")
    from google.protobuf import json_format
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    db = fresh_copy(demo, tmp_path)
    with closing(connect(db)) as conn:
        otel.export(conn, "me/game", collector.url, since_days=3)
    for r in collector.requests:
        body = r["body"]
        if r["path"] == "/v1/traces":
            # OTLP JSON writes ids as hex where protobuf's JSON mapping expects base64
            for rs in body["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    for s in ss["spans"]:
                        for k in ("traceId", "spanId", "parentSpanId"):
                            if k in s:
                                s[k] = base64.b64encode(bytes.fromhex(s[k])).decode()
            msg = json_format.ParseDict(body, ExportTraceServiceRequest())
            assert msg.resource_spans[0].scope_spans[0].spans[0].trace_id
        else:
            msg = json_format.ParseDict(body, ExportMetricsServiceRequest())
            assert msg.resource_metrics[0].scope_metrics[0].metrics


def test_test_runs_round_trip_through_otlp(demo, collector, tmp_path):
    db = fresh_copy(demo, tmp_path)
    with closing(connect(db)) as conn:
        otel.export(conn, None, collector.url, only={"tests"}, since_days=3)
        original = analysis.triage_run(conn)
    payload = {"resourceSpans": [rs for r in collector.requests if r["path"] == "/v1/traces"
                                 for rs in r["body"]["resourceSpans"]]}
    with closing(connect(str(tmp_path / "b.db"))) as other:
        added = otel.ingest_traces(other, payload)
        assert added["test_runs"] > 0 and added["test_results"] > 0
        assert otel.ingest_traces(other, payload) == {"test_results": 0, "test_runs": 0, "pipelines": 0, "jobs": 0}
        got = analysis.triage_run(other)
        assert got["decision"] == original["decision"]
        assert got["blocking"] == original["blocking"]
        assert got["run"]["commit_sha"] == original["run"]["commit_sha"]


def cicd_span(trace, span, name, attrs, parent=None, start=1_759_000_000_000_000_000, secs=60):
    s = {"traceId": trace, "spanId": span, "name": name, "startTimeUnixNano": str(start),
         "endTimeUnixNano": str(start + secs * 1_000_000_000), "attributes": otel.attrs(attrs)}
    if parent:
        s["parentSpanId"] = parent
    return s


def test_receives_cicd_spans_in_any_order(tmp_path):
    t = "ab" * 16
    job = cicd_span(t, "02" * 8, "build", {"cicd.pipeline.task.name": "build", "cicd.pipeline.task.run.id": "77",
                                            "cicd.pipeline.task.run.result": "failure"}, parent="01" * 8)
    run = cicd_span(t, "01" * 8, "RUN deploy", {"cicd.pipeline.name": "deploy", "cicd.pipeline.result": "failure",
                                                 "cicd.pipeline.run.id": "555", "vcs.ref.head.revision": "abc123",
                                                 "vcs.ref.head.name": "main"}, secs=120)
    wrap = lambda spans: {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": spans}]}]}  # noqa: E731
    with closing(connect(str(tmp_path / "r.db"))) as conn:
        otel.ingest_traces(conn, wrap([job]))      # the job arrives first
        otel.ingest_traces(conn, wrap([run]))
        p = conn.execute("SELECT * FROM pipelines").fetchone()
        assert (p["workflow"], p["status"], p["commit_sha"], p["duration_ms"]) == ("deploy", "failure", "abc123", 120000)
        j = conn.execute("SELECT * FROM jobs").fetchone()
        assert (j["name"], j["status"], j["pipeline_id"]) == ("build", "failure", p["pipeline_id"])


def test_receiver_endpoint(tmp_path):
    db = str(tmp_path / "recv.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), otel.make_receiver(db, token="tok"))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    span = cicd_span("cd" * 16, "03" * 8, "t", {"test.case.name": "pkg.mod::test_x", "test.case.result.status": "fail",
                                                  "vcs.ref.head.revision": "f00"})
    body = gzip.compress(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode())

    def post(path, data, ctype="application/json", auth="Bearer tok"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", path, body=data, headers={"Content-Type": ctype, "Content-Encoding": "gzip", "Authorization": auth})
        return c.getresponse().status

    try:
        assert post("/v1/traces", body, auth="Bearer nope") == 401
        assert post("/v1/traces", body, ctype="application/x-protobuf") == 415
        assert post("/v1/traces", b"not gzip") == 400
        assert post("/v1/traces", body) == 200
        assert post("/v1/metrics", body) == 200
        with closing(connect(db, readonly=True)) as conn:
            r = conn.execute("SELECT r.test_id, r.outcome, ru.commit_sha FROM results r JOIN runs ru USING (run_id)").fetchone()
            assert tuple(r) == ("pkg.mod::test_x", "fail", "f00")
    finally:
        server.shutdown()


def test_import_json_lines(tmp_path):
    lines = []
    for i, outcome in enumerate(("fail", "pass")):
        s = cicd_span("ef" * 16, f"{i:016x}", "x", {"test.case.name": "a::b", "test.case.result.status": outcome,
                                                     "vcs.ref.head.revision": "c1"}, start=1_759_000_000_000_000_000 + i)
        lines.append(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [s]}]}]}))
    f = tmp_path / "spans.jsonl"
    f.write_text("\n".join(lines) + "\n")
    with closing(connect(str(tmp_path / "i.db"))) as conn:
        assert otel.import_file(conn, str(f)) == {"test_results": 2, "test_runs": 1, "pipelines": 0, "jobs": 0}
        assert analysis.flake_stats(conn, 36500)["a::b"]["flip_shas"] == 1


def test_endpoint_and_resource_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example.com/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "https://m.example.com/v1/metrics")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "svc")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=ci,team=games")
    assert otel.endpoint("traces") == "https://otlp.example.com/v1/traces"
    assert otel.endpoint("metrics") == "https://m.example.com/v1/metrics"
    r = otel.read_attrs(otel.resource("o/r")["attributes"])
    assert r["service.name"] == "svc" and r["team"] == "games" and r["vcs.repository.name"] == "r"
