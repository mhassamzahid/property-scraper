"""
FastAPI backend for the Property Comparables frontend.

Endpoints:
  POST /api/search               → starts async job, returns {job_id}
  GET  /api/status/{id}          → polls job status/progress/result
  GET  /api/property-history     → on-demand: load full sale history for a property
  GET  /                         → serves static/index.html
"""

import asyncio
import uuid
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Serialise all property-history requests so only one browser runs at a time
_history_lock = asyncio.Lock()

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


@app.get("/api/property-history")
async def property_history(url: str = Query(..., description="Redfin property URL")):
    """
    Navigate to a Redfin property page and return the full sale & listing history.
    Uses a fresh warmed browser session so Redfin's WAF lets the page load.
    """
    async with _history_lock:
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
            from pipeline import build_context, STEALTH_SCRIPT, fetch_property_history

            async with async_playwright() as pw:
                browser, ctx = await build_context(pw)
                try:
                    page = await ctx.new_page()
                    await stealth_async(page)
                    await page.add_init_script(STEALTH_SCRIPT)

                    # Warm Redfin session before navigating to the property page
                    await page.goto("https://www.redfin.com", wait_until="domcontentloaded", timeout=25_000)
                    await asyncio.sleep(2)

                    events = await fetch_property_history(page, url)
                    return {"events": events, "count": len(events), "status": "ok"}
                finally:
                    await ctx.close()
                    await browser.close()
        except Exception as exc:
            return JSONResponse({"events": [], "status": "error", "error": str(exc)}, status_code=200)


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
