"""
FastAPI backend for the Property Comparables frontend.

Endpoints:
  POST /api/search      → starts async job, returns {job_id}
  GET  /api/status/{id} → polls job status/progress/result
  GET  /                → serves static/index.html
"""

import asyncio
import uuid
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="PropComps API")

JOBS: dict[str, dict] = {}


class SearchRequest(BaseModel):
    address: str
    radius: float = 2.0
    tolerance: float = 0.20
    sold_days: int = 365


async def _execute_job(job_id: str, req: SearchRequest):
    def on_progress(msg: str):
        JOBS[job_id]["progress"] = msg

    try:
        from pipeline import run_pipeline
        result = await run_pipeline(
            req.address,
            req.radius,
            req.tolerance,
            req.sold_days,
            input_address=req.address,
            progress_cb=on_progress,
        )
        JOBS[job_id].update({"status": "done", "result": result})
    except Exception as exc:
        JOBS[job_id].update({"status": "error", "error": str(exc)})


@app.post("/api/search")
async def search(req: SearchRequest, background_tasks: BackgroundTasks):
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "progress": "Starting pipeline...", "result": None, "error": None}
    background_tasks.add_task(_execute_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return job


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
