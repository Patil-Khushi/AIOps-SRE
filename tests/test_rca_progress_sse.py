"""Tests for GET /api/rca/stream/{run_id} — the SSE route.

No timing dependency for the main-path tests: canned events (including the
terminal one) are pushed BEFORE the client connects, so the stream replays
history and then closes on its own — a finite read, not a stream that has to
be raced against a clock. Only the idle-timeout test needs real elapsed time,
and it uses a tiny env-configured timeout to stay fast.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.rca_agent.progress import RcaStage, StageEvent, StageOutcome
from demo.ui import rca_progress as mod


def _app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        mod.bootstrap_rca_progress()
        yield

    app = FastAPI(lifespan=_lifespan)
    mod.register_routes(app)
    return app


def _event(
    run_id: str, seq: int, stage: RcaStage, outcome: StageOutcome = StageOutcome.STARTED
) -> dict:
    return StageEvent(run_id=run_id, seq=seq, stage=stage, outcome=outcome, label="x").model_dump(
        mode="json"
    )


def _parse_sse(text: str) -> list[dict]:
    """Pull the `data:` payload out of each frame, ignoring `: ping` comments."""
    import json

    records = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                records.append(json.loads(line[len("data: ") :]))
    return records


def test_bad_uuid_is_rejected():
    with TestClient(_app()) as client:
        r = client.get("/api/rca/stream/not-a-uuid")
    assert r.status_code == 400


def test_replays_history_then_closes_on_terminal_event():
    run_id = str(uuid.uuid4())
    hub = mod.get_hub()
    hub.push(run_id, _event(run_id, 1, RcaStage.RECEIVED))
    hub.push(run_id, _event(run_id, 2, RcaStage.EVIDENCE))
    hub.push(run_id, _event(run_id, 3, RcaStage.COMPLETE, StageOutcome.OK))

    with TestClient(_app()) as client:
        r = client.get(f"/api/rca/stream/{run_id}")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    records = _parse_sse(r.text)
    assert [rec["stage"] for rec in records] == ["received", "evidence", "complete"]


def test_last_event_id_resumes_from_the_given_seq():
    run_id = str(uuid.uuid4())
    hub = mod.get_hub()
    hub.push(run_id, _event(run_id, 1, RcaStage.RECEIVED))
    hub.push(run_id, _event(run_id, 2, RcaStage.EVIDENCE))
    hub.push(run_id, _event(run_id, 3, RcaStage.COMPLETE, StageOutcome.OK))

    with TestClient(_app()) as client:
        r = client.get(f"/api/rca/stream/{run_id}", headers={"last-event-id": "1"})

    records = _parse_sse(r.text)
    assert [rec["seq"] for rec in records] == [2, 3]


def test_after_query_param_also_resumes():
    run_id = str(uuid.uuid4())
    hub = mod.get_hub()
    hub.push(run_id, _event(run_id, 1, RcaStage.RECEIVED))
    hub.push(run_id, _event(run_id, 2, RcaStage.COMPLETE, StageOutcome.OK))

    with TestClient(_app()) as client:
        r = client.get(f"/api/rca/stream/{run_id}?after=1")

    records = _parse_sse(r.text)
    assert [rec["seq"] for rec in records] == [2]


def test_unknown_run_id_idle_times_out_quickly(monkeypatch):
    monkeypatch.setenv("AIOPS_RCA_STREAM_HEARTBEAT", "0.05")
    monkeypatch.setenv("AIOPS_RCA_STREAM_IDLE_TIMEOUT", "0.15")
    run_id = str(uuid.uuid4())

    with TestClient(_app()) as client:
        r = client.get(f"/api/rca/stream/{run_id}")

    assert r.status_code == 200
    assert "event: timeout" in r.text
