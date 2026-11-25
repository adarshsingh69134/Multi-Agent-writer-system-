"""
FastAPI app for the Multi-Agent Report Writer System (4 agents).

Agents / workflow steps
  1. Researcher  (researcher_node)  plans key questions, searches web + arXiv
  2. Writer      (writer_node)      writes the report, revises from feedback
  3. Reviewer    (reviewer_node)    scores it; sends it back to the Writer or passes
  4. Final Editor (final_node)      combines notes, papers, draft and review into the final report

Run:  python app.py
  or: uvicorn app:app --reload --port 8000
Needs: pip install fastapi uvicorn

backend.py must expose:
  run_report_stream(topic, user_settings) -> generator of (event, data) pairs
  checkpointer (with load_state)

Endpoints
  GET  /health                       liveness check
  GET  /api/agents                   the three agents and their order
  GET  /api/reports/stream?topic=... Server-Sent Events, one event per agent step
  POST /api/reports                  runs the whole workflow, returns the final state
  GET  /api/reports/{topic}          loads a saved state
  GET  /                             serves frontend.html (plus /style.css and /script.js)

SSE events
  start     {"topic": ...}
  progress  {"node", "agent", "step", "total_steps", "message", "round", "update"}
  done      {"state": ..., "report": <final markdown>, "review": <reviewer text>}
            (report = final_report from the Final Editor, or the last draft if that step failed)
  error     {"message": ...}
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend import checkpointer, run_report_stream

logger = logging.getLogger("app")

HOST = "127.0.0.1"  # use "0.0.0.0" to allow other devices on your network
PORT = 8000
BASE_DIR = Path(__file__).resolve().parent

# The three agents, in workflow order. Keys match the node names in backend.py.
AGENTS: Dict[str, Dict[str, Any]] = {
    "researcher_node": {"agent": "Researcher", "step": 1, "role": "Plans key questions and gathers sources"},
    "writer_node": {"agent": "Writer", "step": 2, "role": "Writes and revises the report"},
    "reviewer_node": {"agent": "Reviewer", "step": 3, "role": "Scores the draft and returns feedback"},
    "final_node": {"agent": "Final Editor", "step": 4, "role": "Combines all information into the final report"},
}
TOTAL_STEPS = len(AGENTS)

app = FastAPI(title="Multi-Agent Report Writer", version="3.0.0")

# Lets the plain HTML frontend call the API from another origin or port.
# Tighten allow_origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------
class ReportRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)
    report_length: str = "detailed"  # short | detailed | comprehensive
    tone: str = "professional"


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def sse(event: str, data: Any) -> str:
    """Format one Server-Sent Event."""
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def settings_from(report_length: str, tone: str) -> Dict[str, str]:
    return {"report_length": report_length.strip().lower(), "tone": tone.strip()}


def describe_step(node: str, changes: Any) -> Dict[str, Any]:
    """Add agent name, step number and a readable message to a progress event."""
    info = AGENTS.get(node, {"agent": node, "step": 0})
    changes = changes if isinstance(changes, dict) else {}
    revision_round = changes.get("revision_round")

    if changes.get("error"):
        message = f"{info['agent']} failed: {changes['error']}"
    elif node == "researcher_node":
        message = "Researcher finished gathering sources"
    elif node == "writer_node":
        message = f"Writer finished draft {revision_round}" if revision_round else "Writer finished a draft"
    elif node == "reviewer_node":
        message = "Reviewer finished scoring the draft"
    elif node == "final_node":
        message = "Final Editor finished the combined report"
    else:
        message = f"{info['agent']} finished"

    return {
        "node": node,
        "agent": info["agent"],
        "step": info["step"],
        "total_steps": TOTAL_STEPS,
        "message": message,
        "round": revision_round,
        "update": changes,
    }


def sse_events(topic: str, settings: Dict[str, str]) -> Iterator[str]:
    """Wrap backend.run_report_stream() as SSE text."""
    try:
        for event, data in run_report_stream(topic, settings):
            if event == "progress":
                yield sse("progress", describe_step(data.get("node", ""), data.get("update")))
            elif event == "done":
                state = data.get("state", {})
                yield sse("done", {
                    "state": state,
                    "report": state.get("final_report") or state.get("draft", ""),
                    "review": (state.get("feedback") or {}).get("overall_score", ""),
                })
            else:
                yield sse(event, data)
    except Exception as exc:  # keep the browser informed instead of dropping the stream
        logger.exception("Streaming failed")
        yield sse("error", {"message": str(exc)})


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/agents")
def list_agents() -> Dict[str, Any]:
    """The agents in workflow order (useful for building the progress bar)."""
    return {
        "total_steps": TOTAL_STEPS,
        "agents": [{"node": node, **info} for node, info in AGENTS.items()],
    }


@app.get("/api/reports/stream")
def stream_report(
    topic: str = Query(..., min_length=3, max_length=300),
    report_length: str = "detailed",
    tone: str = "professional",
):
    """SSE endpoint. Frontend: new EventSource('/api/reports/stream?topic=...')"""
    return StreamingResponse(
        sse_events(topic, settings_from(report_length, tone)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # stops proxies from buffering the stream
        },
    )


@app.post("/api/reports")
def create_report(req: ReportRequest) -> Dict[str, Any]:
    """Non-streaming version: waits for the whole workflow, returns the final state."""
    final_state = None
    for event, data in run_report_stream(req.topic, settings_from(req.report_length, req.tone)):
        if event == "error":
            raise HTTPException(status_code=500, detail=data.get("message", "Workflow failed"))
        if event == "done":
            final_state = data.get("state")
    if final_state is None:
        raise HTTPException(status_code=500, detail="Workflow ended without a result")
    return final_state


@app.get("/api/reports/{topic}")
def get_saved_report(topic: str) -> Dict[str, Any]:
    saved = checkpointer.load_state(topic)
    if not saved:
        raise HTTPException(status_code=404, detail="No saved report for this topic")
    return saved


@app.get("/style.css", include_in_schema=False)
def serve_css():
    return FileResponse(BASE_DIR / "style.css", media_type="text/css")


@app.get("/script.js", include_in_schema=False)
def serve_js():
    return FileResponse(BASE_DIR / "script.js", media_type="application/javascript")


@app.get("/", include_in_schema=False)
def home():
    page = BASE_DIR / "frontend.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="frontend.html not found next to app.py")
    return FileResponse(page)


# ----------------------------------------------------------------------------
# Start the server with:  python app.py
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=HOST, port=PORT, reload=True)