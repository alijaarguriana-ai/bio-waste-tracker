"""
Bio Waste Tracker - core logic (storage, validation, summary, insight engine).
Framework-free on purpose: used by the FastAPI app (main.py) and testable alone.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("BWT_TZ", "Europe/Berlin"))

TYPES = ("preparation_waste", "spoiled_stock", "plate_leftovers")
KINDS = ("disposal", "empty", "rejected", "overload", "fault")
LABELS = {
    "preparation_waste": "Preparation waste",
    "spoiled_stock": "Spoiled stock",
    "plate_leftovers": "Plate leftovers",
}

# --- Assumptions (state them in the pitch, tweak per kitchen) -------------
# Purchase value of the food that ends up in each stream, EUR per kg.
COST_EUR_PER_KG = {"preparation_waste": 2.0, "spoiled_stock": 4.0, "plate_leftovers": 4.5}
# ~2.5 kg CO2e per kg of food wasted (FAO: ~3.3 Gt CO2e for ~1.3 Gt wasted food).
CO2E_PER_KG = 2.5
OPERATING_DAYS_PER_YEAR = 300
HINT_WINDOW_S = 20  # a tablet tap within +-20 s of a disposal sets its type

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    ts           REAL NOT NULL,          -- unix seconds (resolved)
    day          TEXT NOT NULL,          -- local date YYYY-MM-DD
    weight_g     REAL NOT NULL,
    type         TEXT,
    type_source  TEXT,
    confidence   REAL,
    station_id   TEXT,
    note         TEXT,
    ts_estimated INTEGER DEFAULT 0,      -- 1 = device had no clock, ts reconstructed
    received_at  REAL NOT NULL,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day, ts);
CREATE TABLE IF NOT EXISTS type_hints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT,
    type       TEXT,
    ts         REAL,
    used       INTEGER DEFAULT 0
);
"""


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def today() -> str:
    return local_day(time.time())


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds")


class ValidationError(ValueError):
    pass


class Store:
    def __init__(self, path: str = "bwt.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "food_item" not in cols:
            self.db.execute("ALTER TABLE events ADD COLUMN food_item TEXT")
        if "food_confidence" not in cols:
            self.db.execute("ALTER TABLE events ADD COLUMN food_confidence REAL")
        self.db.commit()
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ ingest
    def ingest(self, p: dict, device_uptime: int | None = None,
               device_boot: int | None = None, now: float | None = None) -> tuple[dict, bool]:
        """Validate + store one device event. Returns (event, created).
        created=False means duplicate (ESP32 retried a POST that already landed)."""
        now = now or time.time()
        try:
            event_id = str(p["event_id"])
            kind = str(p.get("kind", "disposal"))
            weight = float(p["weight_g"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValidationError(f"missing/invalid field: {e}")
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {KINDS}")
        etype = p.get("type")
        if kind == "disposal":
            if etype not in TYPES:
                raise ValidationError(f"type must be one of {TYPES}")
            if weight <= 0:
                raise ValidationError("a disposal must have a positive weight")
        else:
            etype = None

        # timestamp resolution: device clock > uptime reconstruction > arrival time
        ts = float(p.get("ts") or 0)
        estimated = 0
        if ts <= 0:
            estimated = 1
            up = p.get("uptime_ms")
            if device_uptime and up and device_boot is not None and int(p.get("boot", -1)) == device_boot:
                ts = now - max(0, device_uptime - int(up)) / 1000.0
            else:
                ts = now

        ev = {
            "event_id": event_id, "kind": kind, "ts": ts, "day": local_day(ts),
            "weight_g": round(weight, 1), "type": etype,
            "type_source": p.get("type_source", "sensor"),
            "confidence": float(p.get("confidence", 1.0)),
            "station_id": p.get("station_id", "bin-01"), "note": p.get("note", ""),
            "ts_estimated": estimated, "received_at": now, "raw": json.dumps(p),
        }

        with self.lock:
            dup = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if dup:                                   # idempotent retry from the device
                return self._public(dict(dup)), False
            # tablet tap BEFORE the weight arrived -> use it instead of the time rule
            if kind == "disposal" and ev["type_source"] == "time_rule":
                row = self.db.execute(
                    "SELECT id, type FROM type_hints WHERE used=0 AND station_id=? AND ABS(ts-?)<=? "
                    "ORDER BY ABS(ts-?) LIMIT 1",
                    (ev["station_id"], ts, HINT_WINDOW_S, ts)).fetchone()
                if row:
                    ev.update(type=row["type"], type_source="tablet", confidence=1.0)
                    self.db.execute("UPDATE type_hints SET used=1 WHERE id=?", (row["id"],))
            cur = self.db.execute(
                "INSERT OR IGNORE INTO events (event_id, kind, ts, day, weight_g, type, type_source,"
                " confidence, station_id, note, ts_estimated, received_at, raw) VALUES"
                " (:event_id,:kind,:ts,:day,:weight_g,:type,:type_source,:confidence,:station_id,"
                " :note,:ts_estimated,:received_at,:raw)", ev)
            self.db.commit()
            created = cur.rowcount == 1
        return self._public(ev), created

    def add_hint(self, station_id: str, etype: str, now: float | None = None) -> dict | None:
        """Tablet tap. If a time-rule disposal just happened, fix its type now;
        otherwise remember the tap for the next disposal. Returns updated event or None."""
        if etype not in TYPES:
            raise ValidationError(f"type must be one of {TYPES}")
        now = now or time.time()
        with self.lock:
            row = self.db.execute(
                "SELECT event_id FROM events WHERE kind='disposal' AND type_source='time_rule' "
                "AND station_id=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1",
                (station_id, now - HINT_WINDOW_S, now + 1)).fetchone()
            if row:
                self.db.execute(
                    "UPDATE events SET type=?, type_source='tablet', confidence=1.0 WHERE event_id=?",
                    (etype, row["event_id"]))
                self.db.commit()
                r = self.db.execute("SELECT * FROM events WHERE event_id=?", (row["event_id"],)).fetchone()
                return self._public(dict(r))
            self.db.execute("INSERT INTO type_hints (station_id, type, ts) VALUES (?,?,?)",
                            (station_id, etype, now))
            self.db.commit()
            return None

    # ----------------------------------------------------------------- queries
    @staticmethod
    def _public(ev: dict) -> dict:
        out = {k: ev[k] for k in ("event_id", "kind", "weight_g", "type", "type_source",
                                  "confidence", "station_id", "note", "ts_estimated", "day", "ts")}
        out["food_item"] = ev.get("food_item")
        out["food_confidence"] = ev.get("food_confidence")
        out["timestamp"] = iso(ev["ts"])
        return out

    def set_food(self, event_id: str, food: str, confidence: float) -> dict | None:
        """Result of the camera + vision model for one disposal."""
        with self.lock:
            self.db.execute("UPDATE events SET food_item=?, food_confidence=? WHERE event_id=?",
                            (food, round(float(confidence), 2), event_id))
            self.db.commit()
            r = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return self._public(dict(r)) if r else None

    def events(self, day: str | None = None) -> list[dict]:
        day = day or today()
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE day=? ORDER BY ts", (day,)).fetchall()
        return [self._public(dict(r)) for r in rows]

    def days(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT day FROM events ORDER BY day DESC")]

    def summary(self, day: str | None = None) -> dict:
        day = day or today()
        evs = self.events(day)
        disp = [e for e in evs if e["kind"] == "disposal"]
        totals = {t: 0.0 for t in TYPES}
        counts = {t: 0 for t in TYPES}
        by_hour = {t: [0.0] * 24 for t in TYPES}
        for e in disp:
            totals[e["type"]] += e["weight_g"]
            counts[e["type"]] += 1
            by_hour[e["type"]][datetime.fromtimestamp(e["ts"], TZ).hour] += e["weight_g"]
        food_g = {t: {} for t in TYPES}
        for e in disp:
            f = e.get("food_item")
            if f and f != "nothing":
                food_g[e["type"]][f] = round(food_g[e["type"]].get(f, 0) + e["weight_g"], 1)
        total = sum(totals.values())
        pct = {t: (round(100 * totals[t] / total, 1) if total else 0.0) for t in TYPES}
        eur = {t: round(totals[t] / 1000 * COST_EUR_PER_KG[t], 2) for t in TYPES}
        src = {}
        for e in disp:
            src[e["type_source"]] = src.get(e["type_source"], 0) + 1
        other = {k: sum(1 for e in evs if e["kind"] == k) for k in KINDS if k != "disposal"}
        s = {
            "day": day,
            "total_g": round(total, 1),
            "disposals": len(disp),
            "totals_g": {t: round(v, 1) for t, v in totals.items()},
            "percent": pct,
            "counts": counts,
            "eur": eur,
            "eur_total": round(sum(eur.values()), 2),
            "co2e_kg": round(total / 1000 * CO2E_PER_KG, 2),
            "by_hour_g": {t: [round(x, 1) for x in v] for t, v in by_hour.items()},
            "food_g": food_g,
            "food_recognised": sum(1 for e in disp if e.get("food_item")),
            "quality": {
                "type_sources": src,
                "button_or_tablet_pct": round(100 * (src.get("button", 0) + src.get("tablet", 0)) / len(disp), 1) if disp else 0.0,
                "non_disposal_events": other,
                "ts_estimated": sum(e["ts_estimated"] for e in evs),
            },
            "assumptions": {"eur_per_kg": COST_EUR_PER_KG, "co2e_per_kg": CO2E_PER_KG},
        }
        s["insight"] = insight(s)
        return s

    def export_csv(self, day: str | None = None, disposals_only: bool = False) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["timestamp", "weight_g", "type", "kind", "type_source", "confidence",
                    "station_id", "event_id", "ts_estimated", "note", "food_item", "food_confidence"])
        for e in self.events(day):
            if disposals_only and e["kind"] != "disposal":
                continue
            w.writerow([e["timestamp"], e["weight_g"], e["type"] or "", e["kind"], e["type_source"],
                        e["confidence"], e["station_id"], e["event_id"], e["ts_estimated"], e["note"],
                        e.get("food_item") or "", e.get("food_confidence") if e.get("food_confidence") is not None else ""])
        return buf.getvalue()


# --------------------------------------------------------------- insight engine
def insight(s: dict) -> dict:
    """Turn today's numbers into ONE action the kitchen can take tomorrow morning."""
    total = s["total_g"]
    if total <= 0:
        return {"biggest": None, "headline": "No disposals recorded yet today.",
                "action": "Tip the first waste onto the tray and press a type button.",
                "why": "", "saving_eur_year": 0}
    big = max(TYPES, key=lambda t: s["totals_g"][t])
    share = s["percent"][big]
    grams = s["totals_g"][big]
    hours = s["by_hour_g"][big]
    peak = max(range(24), key=lambda h: hours[h])
    n = max(1, s["counts"][big])
    avg = grams / n
    foods = s.get("food_g", {}).get(big) or {}
    top_food = max(foods, key=foods.get) if foods else None
    food_txt = f" Camera AI: mostly {top_food} ({foods[top_food]:.0f} g)." if top_food else ""

    if big == "plate_leftovers":
        cut = 10 if share < 35 else 15 if share < 50 else 20
        item = top_food if top_food else "side portion (pasta, rice, potatoes)"
        action = (f"Tomorrow at service: cut the standard {item} portion "
                  f"by {cut}% and offer free refills on request.")
        why = (f"Guests leave {share:.0f}% of today's waste on their plates "
               f"({grams/1000:.2f} kg in {n} disposals, peak around {peak}:00).")
        reduction = cut / 100 * 1.5   # leftovers shrink more than the portion cut (refills are rare)
    elif big == "preparation_waste":
        if avg >= 400:
            cut = 20
            action = (f"Tomorrow morning: cook the {peak}:00 batch in two smaller batches and "
                      f"reduce the first batch size by {cut}%; top up only when the tray is half empty.")
            why = (f"Prep waste is {share:.0f}% of today's waste and comes in large drops "
                   f"(avg {avg:.0f} g per disposal), the signature of over-production.")
            reduction = 0.5
        else:
            action = ("Tomorrow morning: collect vegetable trimmings in a separate tray and turn them "
                      "into stock / soup base for the next day's menu.")
            why = (f"Prep waste is {share:.0f}% of today's waste in many small drops "
                   f"(avg {avg:.0f} g, {n} disposals, peak around {peak}:00): trimmings, not leftovers.")
            reduction = 0.3
    else:
        action = ("Tomorrow at 8:00: 10-minute FIFO check of fridge and dry store, label every opened "
                  "container with the opening date, and lower the next order of the spoiled items by 25%.")
        why = (f"Spoiled stock is {share:.0f}% of today's waste ({grams/1000:.2f} kg): "
               f"food was bought and never cooked, the most expensive kind of waste.")
        reduction = 0.5

    why += food_txt
    saving_day = s["eur"][big] * reduction
    return {
        "biggest": big,
        "biggest_label": LABELS[big],
        "headline": f"Biggest loss stream: {LABELS[big]} ({share:.0f}%)",
        "action": action,
        "why": why,
        "saving_eur_year": round(saving_day * OPERATING_DAYS_PER_YEAR),
    }
