"""
Bio Waste Tracker - FastAPI backend (D2 + D7)

Run:   pip install -r requirements.txt
       uvicorn main:app --host 0.0.0.0 --port 8000
Open:  http://<laptop-ip>:8000/        dashboard (projector)
       http://<laptop-ip>:8000/cook    3-button tablet for the cooks
       http://<laptop-ip>:8000/docs    API docs
"""
import asyncio
import base64
import os
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import llm
from core import Store, ValidationError, today

HERE = Path(__file__).parent
store = Store(os.environ.get("BWT_DB", str(HERE / "bwt.db")))
app = FastAPI(title="Bio Waste Tracker")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


class Hub:
    """Fan-out of live updates to every open dashboard / tablet."""
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()


def _int_header(request: Request, name: str):
    v = request.headers.get(name)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


# ------------------------------------------------------------------ device API
@app.post("/api/events")
async def post_event(request: Request):
    """Called by the ESP32 (or serial_bridge.py). Idempotent on event_id."""
    try:
        payload = await request.json()
        ev, created = store.ingest(payload,
                                   device_uptime=_int_header(request, "x-device-uptime"),
                                   device_boot=_int_header(request, "x-boot-id"))
    except ValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except ValueError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not created:
        return JSONResponse({"status": "duplicate", "event": ev}, status_code=409)
    await hub.broadcast({"type": "event", "event": ev, "summary": store.summary(ev["day"])})
    if CAM_URL:
        _spawn(_cam_after_event(ev))
    return JSONResponse({"status": "stored", "event": ev}, status_code=201)


# ------------------------------------------------------------------ tablet API
@app.post("/api/type-hint")
async def post_type_hint(request: Request):
    """One tap on the cook tablet: {"station_id": "bin-01", "type": "plate_leftovers"}"""
    try:
        body = await request.json()
        updated = store.add_hint(body.get("station_id", "bin-01"), body.get("type"))
    except (ValidationError, ValueError, AttributeError) as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if updated:
        await hub.broadcast({"type": "event_updated", "event": updated,
                             "summary": store.summary(updated["day"])})
        return JSONResponse({"status": "applied", "event": updated})
    await hub.broadcast({"type": "hint", "station_id": body.get("station_id", "bin-01"),
                         "waste_type": body.get("type")})
    return JSONResponse({"status": "waiting_for_weight"})


# --------------------------------------------------------------- dashboard API
@app.get("/api/events")
async def get_events(request: Request):
    return JSONResponse(store.events(request.query_params.get("day")))


@app.get("/api/summary")
async def get_summary(request: Request):
    return JSONResponse(store.summary(request.query_params.get("day")))


@app.get("/api/days")
async def get_days(request: Request):
    return JSONResponse(store.days())


@app.get("/api/export.csv")
async def export_csv(request: Request):
    """D2 deliverable. ?day=YYYY-MM-DD  ?disposals_only=1"""
    day = request.query_params.get("day") or today()
    only = request.query_params.get("disposals_only") == "1"
    return PlainTextResponse(store.export_csv(day, only), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="bio_waste_log_{day}.csv"'})


@app.get("/api/export.json")
async def export_json(request: Request):
    day = request.query_params.get("day") or today()
    return JSONResponse(store.events(day), headers={
        "Content-Disposition": f'attachment; filename="bio_waste_log_{day}.json"'})


# ------------------------------------------------------------ camera food AI
_jobs: set = set()

# Server-side camera (e.g. Android app "IP Webcam"): the backend pulls a snapshot itself.
#   PowerShell:  $env:BWT_CAM_URL="http://<phone-ip>:8080/shot.jpg"
CAM_URL = os.environ.get("BWT_CAM_URL")
_cam_ref: dict = {}      # station_id -> base64 jpeg of the container before the next disposal


def _snapshot() -> str | None:
    try:
        with urllib.request.urlopen(CAM_URL, timeout=5) as r:
            return base64.b64encode(r.read()).decode()
    except Exception as e:
        print(f"[camera] snapshot failed: {e}")
        return None


def _spawn(coro):
    task = asyncio.create_task(coro)
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


async def _cam_after_event(ev: dict):
    st = ev["station_id"]
    if ev["kind"] == "empty":
        await asyncio.sleep(1.5)                     # hands out of the picture
        snap = await asyncio.to_thread(_snapshot)
        if snap:
            _cam_ref[st] = snap
        return
    if ev["kind"] != "disposal":
        return
    await asyncio.sleep(0.3)
    after = await asyncio.to_thread(_snapshot)
    if not after:
        await hub.broadcast({"type": "vision_error", "event_id": ev["event_id"], "error": "camera snapshot failed"})
        return
    before = _cam_ref.get(st)
    _cam_ref[st] = after
    await _vision_job(ev["event_id"], before, after)


@app.get("/api/camera/reference")
async def camera_reference(request: Request):
    """Take a fresh BEFORE picture (call with the container empty / as it is now)."""
    if not CAM_URL:
        return JSONResponse({"error": "BWT_CAM_URL not set"}, status_code=400)
    snap = await asyncio.to_thread(_snapshot)
    if not snap:
        return JSONResponse({"error": "camera not reachable"}, status_code=503)
    _cam_ref[request.query_params.get("station", "bin-01")] = snap
    return JSONResponse({"status": "reference taken", "bytes": len(snap)})


async def _vision_job(event_id: str, before: str, after: str):
    res = await asyncio.to_thread(llm.classify_food, before, after)
    if not res.get("ok"):
        await hub.broadcast({"type": "vision_error", "event_id": event_id, "error": res.get("error")})
        return
    ev = store.set_food(event_id, res["food"], res["confidence"])
    if ev:
        await hub.broadcast({"type": "event_updated", "event": ev, "vision": res,
                             "summary": store.summary(ev["day"])})


@app.post("/api/events/{event_id}/image")
async def post_event_image(request: Request):
    """Camera page sends BEFORE/AFTER crops (base64 JPEG). Processed locally, never stored."""
    event_id = request.path_params["event_id"]
    try:
        body = await request.json()
        before, after = body["before"], body["after"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "need before and after (base64 jpeg)"}, status_code=422)
    strip = lambda b: b.split(",", 1)[1] if b.startswith("data:") else b
    _spawn(_vision_job(event_id, strip(before), strip(after)))
    return JSONResponse({"status": "processing"}, status_code=202)


@app.post("/api/vision-test")
async def vision_test(request: Request):
    """Test the food recognition without the scale: one photo in, food out. Nothing stored."""
    try:
        img = (await request.json())["image"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False, "error": "need image (base64 jpeg)"}, status_code=422)
    img = img.split(",", 1)[1] if img.startswith("data:") else img
    res = await asyncio.to_thread(llm.classify_food, None, img)
    return JSONResponse(res, status_code=200 if res.get("ok") else 503)


@app.get("/api/camera/test")
async def camera_test(request: Request):
    """IP Webcam mode: take a snapshot from the phone and recognise it now."""
    if not CAM_URL:
        return JSONResponse({"ok": False, "error": "BWT_CAM_URL not set"}, status_code=400)
    snap = await asyncio.to_thread(_snapshot)
    if not snap:
        return JSONResponse({"ok": False, "error": f"camera not reachable at {CAM_URL}"}, status_code=503)
    res = await asyncio.to_thread(llm.classify_food, None, snap)
    return JSONResponse(res, status_code=200 if res.get("ok") else 503)


@app.get("/camera")
async def camera(request: Request):
    return FileResponse(str(HERE / "static" / "camera.html"))


# ------------------------------------------------------------ AI consultant
_ai_cache: dict = {}   # day -> (number_of_events, result)


@app.get("/api/ai-insight")
async def ai_insight(request: Request):
    """Local LLM (Ollama) reads today's raw log and writes advice. ?refresh=1 forces a new run."""
    day = request.query_params.get("day") or today()
    events = store.events(day)
    cached = _ai_cache.get(day)
    if cached and cached[0] == len(events) and request.query_params.get("refresh") != "1":
        return JSONResponse({**cached[1], "cached": True})
    result = await asyncio.to_thread(llm.generate, store.summary(day), events)
    if result.get("ok"):
        _ai_cache[day] = (len(events), result)
        await hub.broadcast({"type": "ai_insight", "day": day, "insight": result})
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    hub.clients.add(websocket)
    day = today()
    await websocket.send_json({"type": "snapshot", "events": store.events(day), "summary": store.summary(day)})
    try:
        while True:
            await websocket.receive_text()     # keep-alive pings from the browser
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(websocket)


# ---------------------------------------------------------------------- pages
@app.get("/")
async def dashboard(request: Request):
    return FileResponse(str(HERE / "static" / "dashboard.html"))


@app.get("/cook")
async def cook(request: Request):
    return FileResponse(str(HERE / "static" / "cook.html"))
