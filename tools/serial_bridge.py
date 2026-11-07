#!/usr/bin/env python3
"""
USB fallback when Wi-Fi fails: reads "EVT {...}" lines from the ESP32 serial port
and forwards them to the backend. Also appends every line to a local JSONL file.

  pip install pyserial
  python serial_bridge.py --port COM5            (Windows)
  python serial_bridge.py --port /dev/ttyUSB0    (Linux / macOS: /dev/cu.usbserial-*)

Duplicates are harmless: the backend is idempotent on event_id, so the ESP32 may
also deliver the same event over Wi-Fi later.
"""
import argparse
import json
import urllib.error
import urllib.request

import serial  # pyserial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/events")
    ap.add_argument("--log", default="serial_events.jsonl")
    a = ap.parse_args()
    ser = serial.Serial(a.port, a.baud, timeout=1)
    print(f"listening on {a.port} -> {a.url}")
    with open(a.log, "a", encoding="utf-8") as log:
        while True:
            line = ser.readline().decode("utf-8", "replace").strip()
            if not line:
                continue
            if not line.startswith("EVT "):
                print("  ", line)                 # normal debug output
                continue
            payload = line[4:]
            log.write(payload + "\n"); log.flush()
            try:
                json.loads(payload)
                req = urllib.request.Request(a.url, data=payload.encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3) as r:
                    print("->", r.status, payload)
            except urllib.error.HTTPError as e:
                print("->", e.code, "(409 = already stored via Wi-Fi)" if e.code == 409 else "", payload)
            except Exception as e:
                print("!! not forwarded:", e, payload)


if __name__ == "__main__":
    main()
