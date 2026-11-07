#!/usr/bin/env python3
"""
Fake ESP32 for testing the backend + dashboard without hardware (stdlib only).

  python simulate_esp32.py --day            # back-fill a realistic kitchen day (station "sim-01")
  python simulate_esp32.py --live 8         # one event every 8 s, now
  python simulate_esp32.py --url http://192.168.137.1:8000/api/events --live 5

IMPORTANT for the pitch: simulated events use station_id "sim-01" so they can never be
confused with the real D2 log (station "bin-01"). Do not hand in simulated data as D2.
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

TYPES = ["preparation_waste", "spoiled_stock", "plate_leftovers"]
BOOT = random.randint(1000, 9999)
SEQ = 0


def post(url, ev):
    req = urllib.request.Request(url, data=json.dumps(ev).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        print("backend unreachable:", e.reason)
        return None


def event(kind, weight, etype=None, src="button", conf=1.0, ts=None, note="", station="sim-01"):
    global SEQ
    SEQ += 1
    return {"event_id": f"{station}-{BOOT}-{SEQ}", "kind": kind, "ts": int(ts or time.time()),
            "uptime_ms": SEQ * 1000, "boot": BOOT, "weight_g": round(weight, 1),
            "type": etype or "unknown", "type_source": src if kind == "disposal" else "sensor",
            "confidence": conf, "station_id": station, "note": note}


def kitchen_day():
    """A plausible service day: prep in the morning, lunch + dinner plate bursts,
    a fridge clean-out, plus the failures a real kitchen produces."""
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    at = lambda h, m: (d + timedelta(hours=h, minutes=m)).timestamp()
    evs = []
    for m in range(0, 150, 12):                                     # 07:00-09:30 trimmings
        evs.append(event("disposal", random.uniform(80, 320), "preparation_waste", ts=at(7, m)))
    evs.append(event("disposal", random.uniform(600, 1100), "spoiled_stock", ts=at(8, 5)))   # fridge check
    evs.append(event("disposal", random.uniform(250, 500), "spoiled_stock", ts=at(8, 9)))
    evs.append(event("rejected", 812, ts=at(9, 40), note="transient_returned_to_baseline"))
    for m in range(0, 140, 7):                                      # 12:00-14:20 lunch plates
        evs.append(event("disposal", random.uniform(90, 260), "plate_leftovers", ts=at(12, m)))
    evs.append(event("disposal", 1450, "preparation_waste", ts=at(14, 35)))                  # unsold pasta tray
    evs.append(event("disposal", 140, "preparation_waste", "time_rule", 0.6, ts=at(13, 3)))  # forgot button
    evs.append(event("empty", -3480, ts=at(15, 0), note="negative_step"))
    for m in range(0, 170, 6):                                      # 18:00-20:50 dinner plates
        evs.append(event("disposal", random.uniform(100, 300), "plate_leftovers", ts=at(18, m)))
    evs.append(event("overload", 4720, ts=at(21, 10), note="load_above_max"))
    return evs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/events")
    ap.add_argument("--day", action="store_true", help="back-fill one simulated kitchen day")
    ap.add_argument("--live", type=float, metavar="SECONDS", help="send a live event every N seconds")
    a = ap.parse_args()
    if a.day:
        evs = kitchen_day()
        ok = sum(1 for e in evs if post(a.url, e) in (200, 201))
        print(f"sent {ok}/{len(evs)} simulated events")
    if a.live:
        while True:
            t = random.choices(TYPES, weights=[3, 1, 4])[0]
            w = random.uniform(60, 400) if t != "spoiled_stock" else random.uniform(200, 900)
            src, conf = ("time_rule", 0.6) if random.random() < 0.15 else ("button", 1.0)
            e = event("disposal", w, t, src, conf)
            print(post(a.url, e), e["weight_g"], "g", t, src)
            time.sleep(a.live)


if __name__ == "__main__":
    main()
