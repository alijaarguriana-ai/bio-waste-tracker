"""
AI consultant: turns today's raw log into advice for the restaurant owner,
using a LOCAL model through Ollama (default: gemma3:4b). No data leaves the laptop.

Config (environment variables, all optional):
  BWT_LLM_URL    default http://localhost:11434
  BWT_LLM_MODEL  default gemma3:4b          (check the exact name with `ollama list`)
  BWT_AI_LANG    default English            (language of the advice)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

from core import LABELS, TYPES, TZ

LLM_URL = os.environ.get("BWT_LLM_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("BWT_LLM_MODEL", "gemma3:4b")
AI_LANG = os.environ.get("BWT_AI_LANG", "English")
MAX_EVENTS_IN_PROMPT = 80          # keep the prompt small: 4B models get worse with long input

SYSTEM = f"""You are a food-waste consultant for a commercial kitchen (restaurant or hotel).
You receive today's measurements from a smart scale under the kitchen's food-waste bin.
Every disposal has a time, a weight in grams and an ORIGIN type:
- preparation_waste: trimmings and over-production from the kitchen
- spoiled_stock: food that spoiled in storage before being cooked
- plate_leftovers: what guests left on their plates
Your job: explain where the kitchen loses the most food and give concrete advice
the owner can apply tomorrow morning.

Strict rules:
- Use ONLY numbers that appear in the data. Never invent numbers, dishes or food names.
- Food names come ONLY from "food_g_by_type" and the food column of the log (recognised by the camera AI).
  If there is no food data, say that food recognition was not active. Never guess foods.
- Be concrete and practical (portion sizes, batch cooking, stock rotation, ordering, menu).
- If there are fewer than 5 disposals, say that the data is not yet enough for firm conclusions.
- Write in {AI_LANG}. Reply with JSON only, no markdown."""

SCHEMA_HINT = """Reply with exactly this JSON structure:
{
  "headline": "max 15 words: the single most important finding",
  "diagnosis": "2-3 sentences explaining where and when food is lost, citing numbers from the data",
  "food_summary": "1-2 sentences: which foods were wasted most and in which stream (only from the food data)",
  "actions": [
    {"title": "short action name", "when": "tomorrow morning | this week | this month",
     "how": "1-2 sentences, concrete steps", "expected_impact": "qualitative or based on the data"}
  ],
  "owner_message": "2 sentences addressed directly to the restaurant owner",
  "data_quality": "1 sentence on how reliable today's data is (time-rule share, rejected events)"
}
Give exactly 3 actions, the most impactful first."""


def _build_prompt(summary: dict, events: list[dict]) -> str:
    disp = [e for e in events if e["kind"] == "disposal"]
    other = [e for e in events if e["kind"] != "disposal"]
    hours = {}
    for t in TYPES:
        nz = {f"{h}:00": g for h, g in enumerate(summary["by_hour_g"][t]) if g > 0}
        if nz:
            hours[t] = nz
    facts = {
        "day": summary["day"],
        "total_g": summary["total_g"],
        "disposals": summary["disposals"],
        "totals_g_by_type": summary["totals_g"],
        "percent_by_type": summary["percent"],
        "count_by_type": summary["counts"],
        "estimated_value_lost_eur": summary["eur_total"],
        "estimated_co2e_kg": summary["co2e_kg"],
        "grams_per_hour_by_type": hours,
        "how_types_were_assigned": summary["quality"]["type_sources"],
        "non_disposal_events": summary["quality"]["non_disposal_events"],
        "food_g_by_type": summary.get("food_g", {}),
        "disposals_with_food_recognised": summary.get("food_recognised", 0),
        "rule_based_suggestion": summary["insight"]["action"],
    }
    lines = ["time,weight_g,type,typed_by,food"]
    for e in disp[-MAX_EVENTS_IN_PROMPT:]:
        t = datetime.fromtimestamp(e["ts"], TZ).strftime("%H:%M")
        food = f",{e['food_item']}" if e.get("food_item") else ","
        lines.append(f"{t},{e['weight_g']},{e['type']},{e['type_source']}{food}")
    if len(disp) > MAX_EVENTS_IN_PROMPT:
        lines.insert(1, f"# only the last {MAX_EVENTS_IN_PROMPT} of {len(disp)} disposals shown")
    return (
        "TODAY'S SUMMARY (computed exactly by the system, trust these numbers):\n"
        + json.dumps(facts, indent=1)
        + "\n\nRAW DISPOSAL LOG (CSV):\n" + "\n".join(lines)
        + f"\n\n({len(other)} non-disposal events such as emptying or rejected bumps are excluded above.)\n\n"
        + SCHEMA_HINT
    )


def _parse(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)       # small models sometimes wrap JSON in prose
        if m:
            return json.loads(m.group(0))
        raise


def _norm(d):
    """Lower-case keys, spaces/dashes -> underscores, unwrap a single nested object."""
    if isinstance(d, list) and d and isinstance(d[0], dict):
        d = d[0]
    if not isinstance(d, dict):
        return {}
    d = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): v for k, v in d.items()}
    if len(d) == 1 and isinstance(next(iter(d.values())), dict):
        return _norm(next(iter(d.values())))
    return d


def _pick(d: dict, *names) -> str:
    for n in names:
        v = d.get(n)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return ""


def _clean(d) -> dict:
    d = _norm(d)
    acts = d.get("actions") or d.get("recommendations") or d.get("advice") or []
    if isinstance(acts, dict):
        acts = list(acts.values())
    out_acts = []
    for a in acts if isinstance(acts, list) else []:
        if isinstance(a, str):
            out_acts.append({"title": a, "when": "", "how": "", "expected_impact": ""})
        elif isinstance(a, dict):
            a = _norm(a)
            out_acts.append({"title": _pick(a, "title", "action", "name"),
                             "when": _pick(a, "when", "timing", "timeframe"),
                             "how": _pick(a, "how", "description", "details", "steps"),
                             "expected_impact": _pick(a, "expected_impact", "impact", "benefit")})
    return {
        "headline": _pick(d, "headline", "title", "summary", "key_finding"),
        "diagnosis": _pick(d, "diagnosis", "analysis", "explanation", "findings"),
        "food_summary": _pick(d, "food_summary", "foods", "food_analysis", "food"),
        "actions": out_acts[:3],
        "owner_message": _pick(d, "owner_message", "message", "message_to_owner", "message_for_owner"),
        "data_quality": _pick(d, "data_quality", "quality", "reliability"),
    }


def generate(summary: dict, events: list[dict], timeout: float = 180) -> dict:
    """Blocking call to Ollama. Returns {"ok": True, ...advice} or {"ok": False, "error": ...}."""
    if summary["disposals"] == 0:
        return {"ok": False, "error": "No disposals recorded today yet."}
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": _build_prompt(summary, events)}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3},
    }
    t0 = time.time()
    req = urllib.request.Request(f"{LLM_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        hint = " Check the model name with `ollama list`." if e.code == 404 else ""
        return {"ok": False, "error": f"Ollama returned {e.code}: {detail}{hint}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "error": f"Cannot reach Ollama at {LLM_URL} ({e}). Is `ollama serve` running?"}
    raw = (resp.get("message") or {}).get("content", "")
    try:
        advice = _clean(_parse(raw))
    except (ValueError, json.JSONDecodeError):
        return {"ok": False, "error": "The model did not return valid JSON. Try again. Raw: " + raw[:300]}
    if not (advice["headline"] or advice["diagnosis"] or advice["actions"]):
        return {"ok": False, "error": "Unexpected answer format. Try again. Raw: " + raw[:400]}
    return {"ok": True, "model": LLM_MODEL, "seconds": round(time.time() - t0, 1),
            "based_on_disposals": summary["disposals"], **advice}


# ---------------------------------------------------------------- vision
FOODS = [f.strip() for f in os.environ.get(
    "BWT_FOODS", "apple,pasta,bread,vegetables,potatoes,rice,meat,fish,salad,mixed,other,nothing").split(",")]

VISION_PROMPT = """You see two photos taken from above of the same small food-waste container in a kitchen.
Image 1 = BEFORE a disposal. Image 2 = AFTER the disposal.
Which food was ADDED between image 1 and image 2? Look only at what is new in image 2.
Answer with JSON only: {"food": "<one of: %s>", "confidence": <0.0-1.0>, "seen": "<max 8 words describing what was added>"}
If nothing new is visible, use "nothing". If several foods were added, use "mixed"."""


VISION_PROMPT_SINGLE = """You see a photo taken from above of a small food-waste container in a kitchen.
Which food was most recently thrown in (the food lying on top)?
Answer with JSON only: {"food": "<one of: %s>", "confidence": <0.0-1.0>, "seen": "<max 8 words describing it>"}
If the container is empty, use "nothing". If several foods are on top, use "mixed"."""


def classify_food(before_b64: str | None, after_b64: str, timeout: float = 120) -> dict:
    """Ask the local vision model what was added. Images stay in memory, never stored."""
    if before_b64:
        prompt, images = VISION_PROMPT % ", ".join(FOODS), [before_b64, after_b64]
    else:
        prompt, images = VISION_PROMPT_SINGLE % ", ".join(FOODS), [after_b64]
    body = {
        "model": os.environ.get("BWT_VISION_MODEL", LLM_MODEL),
        "messages": [{"role": "user", "content": prompt, "images": images}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    t0 = time.time()
    req = urllib.request.Request(f"{LLM_URL}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        d = _parse(resp["message"]["content"])
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Ollama returned {e.code}: {e.read().decode(errors='replace')[:200]}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "error": f"Cannot reach Ollama at {LLM_URL} ({e})"}
    except (KeyError, ValueError, json.JSONDecodeError):
        return {"ok": False, "error": "Vision model did not return valid JSON"}
    food = str(d.get("food", "other")).strip().lower()
    if food not in FOODS:
        food = "other"
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return {"ok": True, "food": food, "confidence": round(conf, 2),
            "seen": str(d.get("seen", ""))[:80], "seconds": round(time.time() - t0, 1)}
