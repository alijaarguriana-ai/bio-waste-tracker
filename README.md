# Bio Waste Tracker: WebMeister360 Corporate Innovation Challenge

Every disposal **weighed, typed and timed**. ESP32 + 5 kg load cell under a weighing chute, a 1-tap type interface, an offline-first data path, and a live dashboard that tells the kitchen what to change tomorrow morning.

```
 cook tips waste ─► weighing chute (load cell, HX711) ─► servo gate ─► big bin (never weighed)
                         │
           3 big buttons ┤ (or tablet /cook)
                         ▼
     ESP32: median → IIR low-pass → stability window → event state machine
                         │  JSON line
                         ▼
     LittleFS queue (survives Wi-Fi loss + power cuts) ──HTTP POST──► FastAPI + SQLite
                         └──── USB serial fallback (serial_bridge.py) ────┘      │ WebSocket
                                                                                 ▼
                                             Dashboard (projector)  ·  Cook tablet (/cook)
```

## Project structure

```
backend/            FastAPI server, SQLite store, local LLM client
  static/           dashboard, cook tablet and camera pages
  start.ps1         one-click start on Windows
firmware/
  bio_waste_tracker/  ESP32 sketch + config.h (Wi-Fi, pins, tuning)
tools/              ESP32 simulator and USB serial bridge
docker-compose.yml  containerised backend
```

## 1. Hardware wiring

| Part | ESP32 pin |
|---|---|
| HX711 DOUT / SCK | GPIO 16 / GPIO 4 (HX711 VCC → 3V3, GND → GND) |
| Load cell → HX711 | red E+, black E−, white A−, green A+ (swap A−/A+ if weight reads negative) |
| Button Prep / Spoiled / Plate | GPIO 25 / 26 / 27 → other leg to GND (internal pull-ups) |
| "Press a button" LED (optional) | GPIO 15 → 220 Ω → LED → GND |
| Gate servo (chute mode) | signal GPIO 18, **power from 5 V USB / external, not the 3V3 pin** |

**Mechanics:** the load cell is a cantilever bar: one end screwed to a base board, the tray on the other end (use spacers so the bar can bend). The tray sits *above/in front of* the bin. The servo flap is the tray's bottom or front edge. No flap? Set `WEIGH_MODE_BUCKET` in `config.h`: a small insert bucket on the cell that is emptied by hand (the negative step is logged as `empty`, never as a disposal).

## 2. Flash and calibrate (10 min)

1. Arduino IDE → board "ESP32 Dev Module" (core 2.x or 3.x, **no extra libraries**).
2. Edit `firmware/bio_waste_tracker/config.h`: hotspot SSID/password and `BACKEND_URL` = laptop IP. **Don't use eduroam**, use a phone hotspot.
3. Upload, open Serial Monitor at 115200, with the **tray empty** at power-on (auto-tare).
4. Put a known weight on the tray (a 500 g pasta pack, checked on a kitchen scale) → type `c 500` → saved to flash.
5. `r` streams raw/filtered values; `s` shows status; `1`/`2`/`3` simulate the buttons.

Tuning knobs (all in `config.h`): `STABLE_MS` / `STABLE_BAND_G` (how still a plateau must be), `MIN_DISPOSAL_G`, `TYPE_WAIT_MS`, the time-rule windows.

## 3. Backend + dashboard

```powershell
cd backend
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\start.ps1   # sets AI model + camera, starts on port 8000
```
Without the local AI: `python -m uvicorn main:app --host 0.0.0.0 --port 8000`. Local AI setup: see section 7.
- Dashboard (projector): `http://<laptop-ip>:8000/`
- Cook tablet / phone: `http://<laptop-ip>:8000/cook?station=bin-01`
- D2 log export: `/api/export.csv` (all events) or `/api/export.csv?disposals_only=1`
- Docker: `docker compose up --build`

No hardware yet? `python tools/simulate_esp32.py --day` back-fills a simulated kitchen day (station `sim-01`), `--live 8` sends a live event every 8 s. **Never hand in `sim-01` rows as D2.** For the real log, delete `backend/bwt.db` before the test run.

Wi-Fi dead during the pitch? Plug the ESP32 into the laptop and run `python tools/serial_bridge.py --port COM5`. The backend dedupes on `event_id`, so events arriving twice (USB + Wi-Fi) are stored once.

## 4. How the firmware decides what a disposal is

| Real-kitchen problem | What the firmware does | Status |
|---|---|---|
| Bin weighs 20–30 kg | Only the chute/insert is weighed; 4.5 kg overload guard + `overload` event | ✅ solved |
| Bumps | Median filter (N=5) removes single-sample spikes; plateau must be stable 1.2 s within ±2 g | ✅ solved (tested: 500 g / 250 ms bump ignored) |
| Leaning on the tray | Weight returns to baseline before typing → logged as `rejected`, not counted | ✅ solved for leans < type timeout |
| Several disposals at once | Each new stable plateau ≥ 10 g above the last one = new disposal | ✅ solved if ~1.5 s between tips |
| Cook forgets the button | After 6 s: time rule (morning = prep, service = plate), `confidence` 0.3–0.6; tablet tap within 20 s corrects it | 🔧 designed around |
| Emptying / bin swap | Negative step = `empty` event, never a disposal | ✅ solved |
| Sensor drift | Auto-zero tracking of the baseline while idle; re-tare after every gate cycle | ✅ solved for slow drift |
| Patchy Wi-Fi / power cut | Every event goes to flash first; separate network task retries; idempotent `event_id`; timestamps reconstructed from uptime if NTP was missing | ✅ solved |
| Mixed items in one tip | One record, cook picks the dominant type | ⚠️ remains (accuracy cost: ~share of the minority item) |
| Liquids, non-food items | Not detected | ⚠️ remains |

Tested on a host simulator (the real sketch compiled against fake HX711/buttons/servo with a scripted kitchen scenario): button before tip, button after tip, forgotten button, 500 g bump, 4 s lean, two tips 3 s apart, 4.8 kg overload, bucket emptying. All produced the expected records. **Not yet run on a physical ESP32**: verify timing on the board tonight.

## 5. Pitch text: D4 / D5 / D6

**D4: Why a weighing chute and a 1-tap interface.** We never weigh the bin, we weigh the *disposal*. A 20–30 kg bin would overload the 5 kg cell and, worse, turn every tip into a small difference between two big noisy numbers. A chute in front of the bin starts from zero every time: the food lands, our firmware waits for a stable plateau (median filter + low-pass + 1.2 s stability window), records the step, and the gate drops it into the bin. The type cannot be measured: pasta from the pot and pasta from a plate are the same food with a different origin. So the cook tells us with one elbow-press on three big buttons, usable with wet gloves, before or after tipping. If they forget, a time rule fills in and the record is flagged with low confidence instead of being silently guessed. We tried and discarded putting the bin on the cell (overload), a camera (it recognises food, not origin, and raises privacy issues for staff) and a dial (two hands, too slow during the lunch rush).

**D5: Scalability: 10, 100, 1,000 kitchens.** Hardware cost per bin is about €45 at prototype volume (ESP32 €6, load cell + HX711 €8, three IP65 buttons €10, servo + chute €15, enclosure and PSU €8) and falls toward €30 at 1,000 units; a shared tablet per kitchen is optional. The unit clips onto the existing bin, so no kitchen re-design is needed. Installation takes about 20 minutes: a partner technician or the kitchen itself scans a QR claim code, the device joins Wi-Fi through a captive portal and calibrates with a reference weight. The backend is a stateless Docker container. At 10 kitchens one small VM is enough. At 100–1,000, devices publish over MQTT/TLS to a managed broker, ingestion scales horizontally, data lands in a multi-tenant time-series database (kitchen_id), and firmware updates ship over the air. Maintenance is mostly remote: the device already reports faults, overloads and drift, and a weekly 30-second check with a reference weight keeps calibration honest. The data flows from the bin to the kitchen dashboard first, then into WebMeister360's agent layer, where it is benchmarked across kitchens and turned into purchasing and menu actions.

**D6: Three biggest risks and how we engineer them away.** *(1) Sensor drift and mechanical noise:* load cells creep with temperature and grease. We use median + IIR filtering, a stability band before any record, auto-zero tracking of the baseline while idle, a re-tare after every gate cycle, and a reference-weight check at every shift change. *(2) Lost data from Wi-Fi drops and power cuts:* every event is written to the ESP32's flash before any send attempt. A separate network task drains the queue with retries, the backend is idempotent on event_id so retries never double-count, and timestamps are reconstructed from device uptime if the clock was never synced. A USB serial bridge is the last-resort path. *(3) Wrong type because cooks forget the button:* this is a human risk, not a technical one, so we make it visible instead of hiding it. Every record carries type_source and confidence, the dashboard shows the share typed by a person, a tablet tap can correct the last record within 20 s, and the time rule only fills gaps. If the typed share drops below 80%, that becomes the kitchen's first action item.

## 6. Demo-day checklist
- [ ] Delete `bwt.db`, run ≥ 25 real disposals across a simulated day, include deliberate failures (forgotten button, double tip, lean, emptying).
- [ ] For each test disposal also note the reference-scale weight → mean error (g), detection rate, type accuracy. Put this table on a slide.
- [ ] Export CSV (D2) right after the test run; keep a copy on a USB stick.
- [ ] Live: 3 disposals with 3 different types, one *without* pressing a button → show the time rule + tablet correction.
- [ ] Backup video of a full cycle in case the hardware fails on stage.

## 7. Local AI: one model, two jobs (Ollama + qwen3-vl:2b)
Everything runs **on the laptop** through [Ollama](https://ollama.com). No image, number or log line leaves the kitchen.
One small vision-language model, **`qwen3-vl:2b`**, does both jobs:

| Job | Input | Output |
|---|---|---|
| Food recognition (camera AI) | BEFORE + AFTER photo of the container | `food_item` + `food_confidence`, saved on the disposal record |
| Owner report (AI consultant) | numbers computed by the backend + raw disposal log | headline, diagnosis, food summary, 3 actions, message for the owner |

Setup (once, PowerShell):
```powershell
winget install Ollama.Ollama      # then reopen PowerShell
ollama pull qwen3-vl:2b           # ~2 GB
ollama list                       # must show qwen3-vl:2b
```
Start the backend with `backend/start.ps1`. It sets the model and the camera:
```powershell
cd backend
powershell -ExecutionPolicy Bypass -File .\start.ps1
```
| Variable | Value in start.ps1 | Meaning |
|---|---|---|
| `BWT_LLM_MODEL` | `qwen3-vl:2b` | model for the owner report |
| `BWT_VISION_MODEL` | `qwen3-vl:2b` | model for food recognition (`gemma3:4b` works as a fallback) |
| `BWT_CAM_URL` | `http://172.20.10.3:8080/shot.jpg` | IP Webcam snapshot URL on the phone |
| `BWT_AI_LANG` | `English` (default) | language of the report |
| `BWT_AUTO_INSIGHT` | `1` (default) | `0` = report only on button press |
| `BWT_AUTO_INSIGHT_DELAY` | `8` s (default) | wait for more disposals before regenerating |

**Automatic report.** After every disposal, as soon as its food is recognised, the backend regenerates the owner report and pushes it to every open dashboard. Several quick disposals trigger one run, and two runs never overlap. The "Generate daily report" button still forces a fresh run.

**Honesty rules.** The model only receives numbers computed by the backend and is told never to invent figures or food names. qwen3's "thinking" mode is switched off (faster, clean JSON). The rule-based action card stays as the deterministic fallback when Ollama is off.

## 8. Camera food recognition: Android "IP Webcam" (main setup)
1. Install "IP Webcam" on an Android phone, join the same hotspot, set video resolution to **640×480** (faster inference), tap "Start server". It shows an address like `http://172.20.10.3:8080`.
2. Mount the phone above the container so the picture shows **only the container** (no faces, no hands at rest).
3. Start the backend with `start.ps1` (the URL is set there; change it if the phone's IP differs).
4. With the container empty open `http://localhost:8000/api/camera/reference` once (takes the BEFORE photo). After every `empty` event a new reference is taken automatically.
5. Quick test without the scale: point the camera at some food and open `http://localhost:8000/api/camera/test`.

Flow per disposal: ESP32 posts the weight → backend pulls a snapshot → qwen3-vl compares BEFORE/AFTER → `food_item` is saved and the dashboard updates live → the owner report is regenerated. Photos stay in memory and are **never stored**.

Food list: env `BWT_FOODS` (default apple,pasta,bread,vegetables,potatoes,rice,meat,fish,salad,mixed,other,nothing).
Origin type still comes from button/tablet/time rule: the camera says WHAT, not WHERE FROM.

### 8b. Alternative: browser camera page
Use this only if `BWT_CAM_URL` is **not** set (otherwise every disposal is analysed twice).
1. Phone as webcam for the laptop (e.g. Iriun Webcam on phone + Windows, same Wi-Fi).
2. On the laptop open `http://localhost:8000/camera` (cameras only work on localhost/HTTPS), pick the camera, adjust the green square to cover only the container.
3. On every disposal the page sends a BEFORE and AFTER crop to `POST /api/events/{id}/image`.
