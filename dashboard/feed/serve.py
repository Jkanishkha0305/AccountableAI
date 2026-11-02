"""Live feed for the dashboard (NEXT_PUBLIC_FEED=live): the demo session over HTTP.

    python dashboard/feed/serve.py            # http://127.0.0.1:8765, rules-mode agents, free
    FEED_MESSY=1 python dashboard/feed/serve.py   # the same, on paperwork parsed from raw files

The Next.js app proxies /api/* here. One in-memory session; POST /api/reset rebuilds it.
Rules-mode agents by default (free). POST /api/live {"on": true} sends the next events to Claude
(Sonnet agents, verifier model stage), metered against FEED_BUDGET dollars (default 1.00, an estimated threshold);
past the cap everything falls back to rules. The briefing uses the free code briefing unless
DASHBOARD_BRIEF_MODEL=1 (one Sonnet call each).
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from session import DemoSession

app = FastAPI(title="AccountableAI dashboard feed")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_lock = threading.Lock()
_session: DemoSession | None = None


def session() -> DemoSession:
    global _session
    if _session is None:
        _session = DemoSession(budget=float(os.getenv("FEED_BUDGET", "1.0")), send_email=True)
    return _session


class ConsoleIn(BaseModel):
    preset: str | None = None
    params: dict | None = None
    text: str | None = None


class DecisionIn(BaseModel):
    approve: bool
    by: str = "controller.dana"
    identity_verified: bool = False


class ReplyIn(BaseModel):
    invoice_id: str
    text: str
    proof: Literal["none", "bank", "vendor"] = "none"          # none | bank | vendor


class ClockIn(BaseModel):
    days: int = 1


class LiveIn(BaseModel):
    on: bool


class BriefIn(BaseModel):
    question: str


@app.get("/api/state")
def state():
    with _lock:
        return session().snapshot()


@app.get("/api/presets")
def presets():
    from rails.console.console import PRESETS
    return [{"id": p.id, "label": p.label, "description": p.description, "params": p.params,
             "defaults": p.defaults} for p in PRESETS]


@app.post("/api/console")
def console(body: ConsoleIn):
    with _lock:
        return session().console(body.preset, body.params, body.text)


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: str, body: DecisionIn):
    with _lock:
        return session().decide(approval_id, body.approve, body.by, body.identity_verified)


@app.post("/api/reply")
def reply(body: ReplyIn):
    with _lock:
        return session().reply(body.invoice_id, body.text, body.proof)


@app.post("/api/clock")
def clock(body: ClockIn):
    with _lock:
        return session().advance(body.days)


@app.post("/api/live")
def live_mode(body: LiveIn):
    with _lock:
        r = session().set_live(body.on)
        return {**r, "state": session().snapshot()}


@app.post("/api/brief")
def brief(body: BriefIn):
    with _lock:
        return session().brief(body.question, use_model=os.getenv("DASHBOARD_BRIEF_MODEL") == "1")


@app.post("/api/loop/{step}")
def loop_step(step: Literal["start", "agreement", "chaser", "anomaly"]):
    """The three-agent loop, one agent per call (dashboard Live loop page). start = fresh session."""
    import loop
    with _lock:
        s = session()
        if step == "start":
            s.reset()
            return {"ok": True, "as_of": s.rails.now().isoformat(), "live": s.live}
        out = {"agreement": loop.agreement_step, "chaser": loop.chaser_step,
               "anomaly": loop.anomaly_step}[step](s)
        return {"ok": True, **out, "as_of": s.rails.now().isoformat()}


@app.get("/api/timeline")
def timeline(days: int = 15):
    """The investigation day by day: facts, agent decisions with reasoning, outcomes, people."""
    with _lock:
        return session().timeline(days)


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events, as they happen: `step` (a workflow step finished), `agent_started`
    (which agent runs next, on which invoice), `thinking` (a piece of Claude's summarized
    thinking, streamed), `decision` (the finished record: reasoning steps, mode, citations),
    and `action` (an action's start/finish). Connect directly to this server (not through
    the Next.js dev proxy, which may buffer the stream)."""
    hub = session().hub
    q = hub.add()

    async def events():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.to_thread(q.get, True, 10)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {msg['type']}\ndata: {json.dumps(msg, default=str)}\n\n"
        finally:
            hub.remove(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


SAMPLES = Path(__file__).parent / "samples"
_MEDIA = {".pdf": "application/pdf", ".eml": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8"}


def _sample(name: str) -> Path:
    """Only the files listed in samples/manifest.json (written by samples.py)."""
    listed = {s["file"] for s in json.loads((SAMPLES / "manifest.json").read_text())}
    if name not in listed:
        raise HTTPException(404, f"no sample called {name!r}")
    return SAMPLES / name


@app.get("/api/samples")
def samples():
    """Sample paperwork to drop into the demo: file, label, which flow it starts, what to expect."""
    return json.loads((SAMPLES / "manifest.json").read_text())


@app.get("/api/samples/{name}")
def sample_file(name: str):
    path = _sample(name)
    return FileResponse(path, media_type=_MEDIA.get(path.suffix, "application/octet-stream"),
                        headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.post("/api/samples/{name}/add")
def add_sample(name: str):
    """The file arrives: parsed by agents/extract, then run through the workflow."""
    with _lock:
        return session().add_file(_sample(name))


class Written(BaseModel):
    sender: str                                  # "keyway" or a vendor id
    subject: str = ""
    body: str


class Upload(BaseModel):
    name: str
    data: str                                    # base64


@app.post("/api/write")
def write(body: Written):
    """An email the judge writes as Keyway or a vendor, read like any other inbox file."""
    with _lock:
        return session().write_email(body.sender, body.subject, body.body)


@app.post("/api/upload")
def upload(body: Upload):
    """A file the judge brings (PDF, .eml, bank CSV, up to 2 MB), read like any other inbox file."""
    import base64
    import binascii
    if len(body.data) > 2_800_000:
        raise HTTPException(413, "Files up to 2 MB")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "not base64") from None
    with _lock:
        return session().upload(body.name, data)


@app.get("/api/simulation")
def simulation():
    """The 15-day script (simulation.py): day, label, what kind of step."""
    from simulation import SCRIPT
    return [{"index": i, "day": st["day"], "label": st["label"], "source": st["kind"]} for i, st in enumerate(SCRIPT)]


@app.post("/api/simulation/{index}")
def simulation_step(index: int):
    """Run one step of the script on the session: what ran, what was written, what changed."""
    from simulation import SCRIPT, run
    if not 0 <= index < len(SCRIPT):
        raise HTTPException(404, f"the script has {len(SCRIPT)} steps")
    with _lock:
        if index == 0:
            session().reset()
        return run(session(), index)


@app.post("/api/reset")
def reset():
    with _lock:
        session().reset()
        return session().snapshot()


# -- the UI revamp 2 frontend (ui-revamp-2/), served by this backend --------------------------
# Last, so every /api route above wins: the frontend at this server's root.
_UI = ROOT / "ui-revamp-2"
if _UI.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_UI), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    session()                                    # build before the first request
    uvicorn.run(app, host=os.getenv("FEED_HOST", "127.0.0.1"), port=int(os.getenv("PORT") or os.getenv("FEED_PORT", "8765")), log_level="warning")
