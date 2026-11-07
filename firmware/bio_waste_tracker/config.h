// =====================================================================
//  Bio Waste Tracker - configuration
//  Edit this file only. Everything tunable lives here.
// =====================================================================
#pragma once

// ---------- Network ----------------------------------------------------
// TIP: campus eduroam (WPA2-Enterprise) is painful on ESP32.
// Use a phone hotspot or the laptop hotspot for the demo.
#define WIFI_SSID        "BioWasteHotspot"
#define WIFI_PASSWORD    "changeme123"
// Laptop IP on the hotspot network + backend port
#define BACKEND_URL      "http://192.168.137.1:8000/api/events"
#define DEVICE_ID        "bin-01"          // becomes station_id
#define TZ_INFO          "CET-1CEST,M3.5.0,M10.5.0/3"   // Europe/Berlin
#define NTP_SERVER       "pool.ntp.org"

// ---------- Pins -------------------------------------------------------
#define PIN_HX_DOUT      16
#define PIN_HX_SCK       4
#define PIN_BTN_PREP     25   // preparation_waste  (to GND, internal pull-up)
#define PIN_BTN_SPOILED  26   // spoiled_stock
#define PIN_BTN_PLATE    27   // plate_leftovers
#define PIN_LED_OK       2    // on-board LED: blink = event recorded
#define PIN_LED_WAIT     15   // optional LED: "press a type button" (-1 to disable)
#define PIN_BUZZER       -1   // optional active buzzer (-1 to disable)

// ---------- Mechanics --------------------------------------------------
// WEIGH_MODE_CHUTE : weighing tray with a servo gate. Food is weighed,
//                    then the gate opens and food slides into the big bin.
// WEIGH_MODE_BUCKET: small insert bucket on the load cell, emptied by hand
//                    into the big bin when full (negative step = "empty").
#define WEIGH_MODE_CHUTE   1
#define WEIGH_MODE_BUCKET  2
#ifndef WEIGH_MODE
#define WEIGH_MODE         WEIGH_MODE_BUCKET
#endif

#define PIN_SERVO        18     // gate servo signal (CHUTE mode only)
#define SERVO_CLOSED_US  1000   // pulse width gate closed
#define SERVO_OPEN_US    2000   // pulse width gate open
#define GATE_OPEN_MS     1500   // how long the gate stays open
#define GATE_SETTLE_MS   1200   // ignore readings after closing (vibration)

// ---------- Calibration (overwritten by serial 'c' command, saved in NVS)
#define DEFAULT_SCALE    420.0f   // raw counts per gram (calibrate!)

// ---------- Signal processing ------------------------------------------
#define MEDIAN_N          5       // median window: kills single-sample spikes (bumps)
#define EMA_ALPHA         0.35f   // 1st-order IIR low-pass, tau ~ Ts*(1-a)/a
#define STABLE_MS         1200    // plateau must last this long ...
#define STABLE_BAND_G     4.0f    // ... with max-min below this band
#define MIN_DISPOSAL_G    10.0f   // smaller steps are noise / drift
#define ZERO_BAND_G       3.0f    // |level| below this when idle = "empty tray"
#define AZT_GAIN          0.10f   // auto-zero tracking gain (drift compensation)
#define MAX_LOAD_G        4500.0f // 5 kg cell: keep 10% headroom
#define HX_TIMEOUT_MS     5000    // HX711 silent this long = sensor fault (> tare time)

// ---------- Type assignment -------------------------------------------
#define TYPE_PRE_MS       10000   // button pressed up to 10 s BEFORE the tip counts
#define TYPE_WAIT_MS      6000    // after weighing, wait up to 6 s for a button
#define BTN_DEBOUNCE_MS   40
// Time-rule fallback when nobody presses a button (local hours, [start,end) )
// Outside these windows the fallback is preparation_waste with low confidence.
#define RULE_PREP_START   6
#define RULE_PREP_END     11
#define RULE_LUNCH_START  11
#define RULE_LUNCH_END    15
#define RULE_DINNER_START 17
#define RULE_DINNER_END   23

// ---------- Offline buffer ---------------------------------------------
#define QUEUE_FILE        "/queue.jsonl"
#define QUEUE_MAX_BYTES   200000  // ~1500 events before oldest are dropped
#define SEND_INTERVAL_MS  1000
#define HTTP_TIMEOUT_MS   2500
