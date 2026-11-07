# CLAUDE.md: Bio Waste Tracker (handoff from the claude.ai chat)

Read this first. It is the full context of the project so far. Talk to the team in **Italian**, step by step,
with exact commands (they use Windows + PowerShell with an **Italian keyboard**: `$` = Shift+4).
When something changes, tell them exactly which single file changed and where it goes. No new zip files.

## The challenge (Corporate Innovation Challenge, WebMeister360, Makeathon Reutlingen)
- Build a working system that records **every disposal** into a kitchen food-waste bin: weight in grams
  (of the disposal, not the bin), **type** = origin (`preparation_waste`, `spoiled_stock`, `plate_leftovers`),
  timestamp; daily totals per type and %; biggest loss stream + at least one concrete action for tomorrow.
- Record format: one line per disposal, `timestamp, weight_g, type` (CSV/JSON). Optional: station_id, confidence, image_id.
- Deliverables: D1 working hardware live with 3 disposals; D2 raw log of >= 20 test disposals (incl. failures);
  D3 summary + insight; D4 decisions explained; D5 scalability (10/100/1000 kitchens, cost per bin, install,
  maintenance, data flow); D6 real-kitchen problems solved/designed-around/remaining + top 3 risks with mitigation;
  D7 software dashboard ("X factor").
- **No-gos:** never put the whole bin on the 5 kg load cell; no step that needs a manual (one touch, wet gloves);
  camera: process on device, crop to the bin, store no faces, tell the team what is recorded;
  don't show only a chart: show the raw log including failures. "Honest accuracy beats a polished demo."
- Schedule: **Saturday 13:30 submission**, 15:00 pitch, 16:30 awards.

## Team idea / pitch line
"The scale says **how much**, the origin (tablet/buttons/time rule) says **where from**, the camera AI says **what**,
and a **local** AI writes the daily report for the owner. No data leaves the kitchen."
Recognising the food does NOT give the type (brief says so): the camera adds `food_item`, the type comes from input.

## Hardware (works, calibrated)
- ESP32 Dev Module (CP2102 driver installed, port COM7). Upload needs **BOOT held during "Connecting..."**
  (or BOOT+EN sequence).
- 5 kg bar load cell between two round **3D-printed plates** (sandwich with spacers), small container on top.
  The big blue bin stands NEXT to the scale (no-go otherwise). Base currently on styrofoam (better: rigid).
- HX711: red E+, black E-, white A-, green A+ (B-/B+ free). HX711 -> ESP32: VCC 3V3, GND GND, DT G16, SCK G4.
- Buttons (optional): G25 prep, G26 spoiled, G27 plate, to GND. Serial `1/2/3` simulate them.
- `config.h`: `WEIGH_MODE_BUCKET`, `HX_TIMEOUT_MS 5000`, `BACKEND_URL http://172.20.10.2:8000/api/events`,
  SSID "iPhone di Ari" (password kept local only, placeholder in git).
- Calibration stored in NVS: `scale=-442.728` (negative = cell mounted inverted, correct). Noise < +-0.5 g.
- Serial commands (115200): `t` tare, `c <grams>` calibrate, `r` stream, `s` status, `q` queue, `x` clear queue.
- Firmware logic: median -> EMA -> stability window (1.2 s, 4 g band) -> step >= 10 g = disposal;
  negative step = `empty`; lean that returns = `rejected`; no button within 6 s = time rule (low confidence);
  LittleFS offline queue, idempotent event_id, uptime-based timestamp reconstruction.
  Tested on a host simulator, then on real hardware.

## Network (phone hotspot "iPhone di Ari")
- Laptop: **172.20.10.2** (check with `ipconfig`, adapter "WiFi 2"). Android phone with **IP Webcam**: **172.20.10.3:8080**.
- Dashboard: laptop `http://localhost:8000` (never open 0.0.0.0), other devices `http://172.20.10.2:8000`,
  cook tablet `/cook`, browser camera page `/camera` (only if NOT using IP Webcam).

## Backend (FastAPI + SQLite + WebSocket), folder `backend/`
- Start (PowerShell in backend): `powershell -ExecutionPolicy Bypass -File .\start.ps1` where start.ps1 sets
  `BWT_LLM_MODEL=qwen3-vl:2b`, `BWT_VISION_MODEL=qwen3-vl:2b`, `BWT_CAM_URL=http://172.20.10.3:8080/shot.jpg`
  and runs uvicorn on port 8000 with `C:\Python314\python.exe`.
- **Python:** plain `python` in this terminal points to an unrelated virtualenv (`storefront`) without uvicorn.
  Always use `C:\Python314\python.exe` (start.ps1 already does).
- Port 8000 busy -> `Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }`
- Windows needed `tzdata` (in requirements). pip via `C:\Python314\python.exe -m pip`.
- Files: `core.py` (store, validation, summary, rule-based insight, food_g), `llm.py` (Ollama: daily report +
  `classify_food` vision, tolerant JSON parsing), `main.py` (routes), `static/` (dashboard, cook, camera).
- Endpoints: POST /api/events (ESP32, 409 on duplicate), POST /api/type-hint (tablet), GET /api/events,
  /api/summary, /api/export.csv (D2), /api/export.json, GET /api/ai-insight?refresh=1 (daily report),
  POST /api/events/{id}/image, POST /api/vision-test, GET /api/camera/test, GET /api/camera/reference, WS /ws.
- IP Webcam flow: on each disposal the backend pulls a snapshot, compares with the BEFORE photo, sets
  `food_item` + `food_confidence`. Images never stored.
- Local AI via Ollama: `qwen3-vl:2b` for both vision and report (gemma3:4b also installed as fallback;
  qwen3-vl:8b download was interrupted). The LLM only gets numbers computed by the backend.
- `simulate_esp32.py --day` / `--live 5` uses station "sim-01": never hand in simulated data as D2.

## Repo layout (reorganized Sat 2026-11-07)
`backend/`, `firmware/bio_waste_tracker/`, `tools/` at repo root (the old nested `def/def/` folder is gone).
A leftover `def/backend/` may still hold the old `bwt.db` (gitignored); it is not used anymore.

## Status (Sat 04:20)
Done: hardware calibrated, ESP32 -> backend -> dashboard live, AI report, camera food recognition (code tested
with mocks; real-model accuracy NOT measured yet), repo on GitHub (alijaarguriana-ai/bio-waste-tracker).
Decision: live local demo on the laptop + GitHub link in the final slide (no tunnel, github.io optional only).

## Next steps (in order)
1. Morning: stop server, delete `backend/bwt.db`, restart, tare with empty container, camera reference.
2. **>= 25 real disposals** simulating a kitchen day (prep morning, spoiled from crate, lunch/dinner plates)
   incl. deliberate failures (forgotten button, two quick tips, lean, emptying). Write down kitchen-scale
   weight and whether the camera got the food right, per disposal -> accuracy table (mean error g, detection
   rate, type accuracy, food accuracy, AI latency).
3. Export CSV (D2), generate daily report, screenshots, copy to USB stick.
4. Slides: problem -> idea -> live demo -> raw log + accuracy -> AI report -> D4 -> D5 -> D6 (texts in README
   section 5) -> GitHub link.
5. Rehearse demo 3x (start order + 3-disposal script), record backup video.
