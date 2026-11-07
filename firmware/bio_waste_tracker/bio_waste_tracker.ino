// =====================================================================
//  Bio Waste Tracker - ESP32 firmware  (D1 + D2)
//  WebMeister360 Corporate Innovation Challenge - Makeathon Reutlingen
//
//  Pipeline:  HX711 (bit-banged, 24 bit)
//          -> median filter      (kills single-sample spikes = bumps)
//          -> 1st-order IIR/EMA  (low-pass, removes vibration noise)
//          -> stability detector (max-min band over a time window)
//          -> event state machine (step detection against a reference
//             level, type assignment, transient rejection, gate control)
//          -> JSON line -> LittleFS queue -> HTTP POST (separate task)
//
//  No external libraries needed (Arduino-ESP32 core 2.x or 3.x).
//  Serial 115200 commands:  t = tare | c <grams> = calibrate |
//    r = stream raw/filtered | s = status | q = queue info |
//    x = clear queue | 1/2/3 = simulate type button
// =====================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <time.h>
#include "esp_arduino_version.h"
#include "config.h"

// ------------------------------------------------------------ types ----
enum WasteType : uint8_t { T_NONE = 0, T_PREP = 1, T_SPOILED = 2, T_PLATE = 3 };
static const char* typeName(WasteType t) {
  switch (t) {
    case T_PREP:    return "preparation_waste";
    case T_SPOILED: return "spoiled_stock";
    case T_PLATE:   return "plate_leftovers";
    default:        return "unknown";
  }
}

enum State : uint8_t { ST_IDLE, ST_AWAIT_TYPE, ST_GATE_OPEN, ST_GATE_SETTLE };

struct Pending {
  float    weightG;    // step size = disposal weight
  float    levelG;     // absolute plateau level
  uint32_t detectMs;
  time_t   epoch;      // 0 if NTP not yet synced
};

// ---------------------------------------------------------- globals ----
static Preferences prefs;
static SemaphoreHandle_t fsMutex;

static float   scaleCountsPerG = DEFAULT_SCALE;
static int32_t tareOffset      = 0;
static uint32_t bootId         = 0;
static uint32_t eventSeq       = 0;

static State   state  = ST_IDLE;
static Pending pending;
static float   refG   = 0.0f;       // reference level (empty tray / last plateau)
static uint32_t gateT = 0;
static bool    overloadLatched = false;
static bool    sensorFault     = false;
static bool    streamRaw       = false;

// buttons
struct Btn { uint8_t pin; WasteType type; bool stable; bool lastRaw; uint32_t tChange; };
static Btn btns[3] = {
  { PIN_BTN_PREP,    T_PREP,    true, true, 0 },
  { PIN_BTN_SPOILED, T_SPOILED, true, true, 0 },
  { PIN_BTN_PLATE,   T_PLATE,   true, true, 0 },
};
static volatile uint32_t btnSeq = 0, consumedSeq = 0;
static WasteType lastBtnType = T_NONE;
static uint32_t  lastBtnMs   = 0;

// =====================================================================
//  HX711 driver (bit-bang). Gain 128, channel A -> 25 clock pulses.
// =====================================================================
static portMUX_TYPE hxMux = portMUX_INITIALIZER_UNLOCKED;

static inline bool hxReady() { return digitalRead(PIN_HX_DOUT) == LOW; }

static int32_t hxReadRaw() {
  uint32_t v = 0;
  portENTER_CRITICAL(&hxMux);            // PD_SCK high > 60 us = power-down
  for (int i = 0; i < 24; i++) {
    digitalWrite(PIN_HX_SCK, HIGH); delayMicroseconds(1);
    v = (v << 1) | (uint32_t)digitalRead(PIN_HX_DOUT);
    digitalWrite(PIN_HX_SCK, LOW);  delayMicroseconds(1);
  }
  digitalWrite(PIN_HX_SCK, HIGH); delayMicroseconds(1);   // 25th pulse: gain 128
  digitalWrite(PIN_HX_SCK, LOW);  delayMicroseconds(1);
  portEXIT_CRITICAL(&hxMux);
  if (v & 0x800000UL) v |= 0xFF000000UL;  // sign-extend 24 -> 32 bit
  return (int32_t)v;
}

static int32_t hxAverage(int n) {
  int64_t sum = 0; int got = 0; uint32_t t0 = millis();
  while (got < n && millis() - t0 < (uint32_t)n * 200) {
    if (hxReady()) { sum += hxReadRaw(); got++; }
    delay(1);
  }
  return got ? (int32_t)(sum / got) : 0;
}

// =====================================================================
//  Signal conditioning
// =====================================================================
static float medBuf[MEDIAN_N]; static uint8_t medIdx = 0, medFill = 0;
static float ema = 0.0f; static bool emaInit = false;

#define WIN_CAP 160
static float    winV[WIN_CAP];
static uint32_t winT[WIN_CAP];
static uint16_t winHead = 0, winCount = 0;

static float medianPush(float x) {
  medBuf[medIdx] = x; medIdx = (medIdx + 1) % MEDIAN_N;
  if (medFill < MEDIAN_N) medFill++;
  float tmp[MEDIAN_N];
  for (uint8_t i = 0; i < medFill; i++) tmp[i] = medBuf[i];
  for (uint8_t i = 1; i < medFill; i++) {          // insertion sort (N=5)
    float k = tmp[i]; int8_t j = i - 1;
    while (j >= 0 && tmp[j] > k) { tmp[j + 1] = tmp[j]; j--; }
    tmp[j + 1] = k;
  }
  return tmp[medFill / 2];
}

static void windowPush(float v, uint32_t t) {
  winV[winHead] = v; winT[winHead] = t;
  winHead = (winHead + 1) % WIN_CAP;
  if (winCount < WIN_CAP) winCount++;
}

static void resetStability() { winCount = 0; }

// Stable = window spans >= STABLE_MS and (max - min) < STABLE_BAND_G.
// Returns the window mean as the plateau level.
static bool isStable(float* level) {
  if (winCount < 3) return false;
  uint32_t now = millis();
  float mn = 1e9f, mx = -1e9f, sum = 0; uint16_t n = 0;
  bool spans = false;
  for (uint16_t k = 0; k < winCount; k++) {
    uint16_t i = (winHead + WIN_CAP - 1 - k) % WIN_CAP;   // newest -> oldest
    if (now - winT[i] > STABLE_MS) { spans = true; break; }
    float v = winV[i];
    if (v < mn) mn = v;
    if (v > mx) mx = v;
    sum += v; n++;
  }
  if (!spans || n < 3) return false;
  if (mx - mn >= STABLE_BAND_G) return false;
  *level = sum / n;
  return true;
}

// =====================================================================
//  Gate servo (CHUTE mode)
// =====================================================================
static void servoWriteUs(uint16_t us) {
#if WEIGH_MODE == WEIGH_MODE_CHUTE
  uint32_t duty = (uint32_t)us * 65535UL / 20000UL;   // 50 Hz, 16 bit
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PIN_SERVO, duty);
#else
  ledcWrite(0, duty);
#endif
#endif
}
static void servoInit() {
#if WEIGH_MODE == WEIGH_MODE_CHUTE
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_SERVO, 50, 16);
#else
  ledcSetup(0, 50, 16); ledcAttachPin(PIN_SERVO, 0);
#endif
  servoWriteUs(SERVO_CLOSED_US);
#endif
}

// =====================================================================
//  Feedback (LED / buzzer)
// =====================================================================
static uint32_t okLedUntil = 0;
static void beep(uint16_t ms) {
  if (PIN_BUZZER < 0) return;
  digitalWrite(PIN_BUZZER, HIGH); delay(ms); digitalWrite(PIN_BUZZER, LOW);
}
static void flashOk() { okLedUntil = millis() + 800; }

// =====================================================================
//  Offline-first queue (LittleFS, one JSON object per line)
// =====================================================================
static bool removeFirstLineLocked() {
  File in = LittleFS.open(QUEUE_FILE, "r");
  if (!in) return false;
  File out = LittleFS.open("/queue.tmp", "w");
  in.readStringUntil('\n');                       // skip first line
  uint8_t buf[256];
  while (in.available()) { size_t n = in.read(buf, sizeof(buf)); out.write(buf, n); }
  in.close(); out.close();
  LittleFS.remove(QUEUE_FILE);
  LittleFS.rename("/queue.tmp", QUEUE_FILE);
  return true;
}

static void queueAppend(const char* line) {
  xSemaphoreTake(fsMutex, portMAX_DELAY);
  File f = LittleFS.open(QUEUE_FILE, "a");
  if (f && f.size() > QUEUE_MAX_BYTES) { f.close(); removeFirstLineLocked(); f = LittleFS.open(QUEUE_FILE, "a"); }
  if (f) { f.println(line); f.close(); }
  xSemaphoreGive(fsMutex);
}

static String queuePeek() {
  String s;
  xSemaphoreTake(fsMutex, portMAX_DELAY);
  File f = LittleFS.open(QUEUE_FILE, "r");
  if (f) { s = f.readStringUntil('\n'); f.close(); }
  xSemaphoreGive(fsMutex);
  s.trim();
  return s;
}

static size_t queueSize() {
  xSemaphoreTake(fsMutex, portMAX_DELAY);
  File f = LittleFS.open(QUEUE_FILE, "r");
  size_t n = 0;
  if (f) { while (f.available()) { if (f.read() == '\n') n++; } f.close(); }
  xSemaphoreGive(fsMutex);
  return n;
}

// Network task on core 0: never blocks the weighing loop on core 1.
static void netTask(void*) {
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {      // setAutoReconnect(true) retries by itself
      vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
      continue;
    }
    String line = queuePeek();
    if (line.length() == 0) { vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS)); continue; }

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.begin(BACKEND_URL);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Uptime", String(millis()));   // lets backend fix timestamps
    http.addHeader("X-Boot-Id", String(bootId));            // recorded without NTP
    int code = http.POST(line);
    http.end();

    if (code == 200 || code == 201 || code == 409) {         // 409 = duplicate, already stored
      xSemaphoreTake(fsMutex, portMAX_DELAY);
      removeFirstLineLocked();
      xSemaphoreGive(fsMutex);
      vTaskDelay(pdMS_TO_TICKS(20));                         // flush backlog quickly
    } else if (code == 400 || code == 422) {                 // malformed: drop it, don't block queue
      Serial.printf("[net] rejected by backend (%d), dropping: %s\n", code, line.c_str());
      xSemaphoreTake(fsMutex, portMAX_DELAY);
      removeFirstLineLocked();
      xSemaphoreGive(fsMutex);
    } else {
      Serial.printf("[net] POST failed (%d), will retry\n", code);
      vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS * 3));
    }
  }
}

// =====================================================================
//  Event emission
// =====================================================================
static time_t nowEpoch() {
  time_t t = time(nullptr);
  return (t > 1700000000) ? t : 0;     // 0 = clock not synced yet
}

static void emit(const char* kind, float weightG, WasteType type,
                 const char* source, float conf, time_t epoch, const char* note) {
  char line[360];
  snprintf(line, sizeof(line),
    "{\"event_id\":\"%s-%lu-%lu\",\"kind\":\"%s\",\"ts\":%ld,\"uptime_ms\":%lu,"
    "\"boot\":%lu,\"weight_g\":%.1f,\"type\":\"%s\",\"type_source\":\"%s\","
    "\"confidence\":%.2f,\"station_id\":\"%s\",\"note\":\"%s\"}",
    DEVICE_ID, (unsigned long)bootId, (unsigned long)(++eventSeq), kind,
    (long)epoch, (unsigned long)millis(), (unsigned long)bootId, weightG,
    typeName(type), source, conf, DEVICE_ID, note);
  Serial.print("EVT ");                 // serial_bridge.py picks these up (USB fallback)
  Serial.println(line);
  queueAppend(line);
}

// Fallback when nobody pressed a button: "when" gives context about "origin".
static WasteType timeRule(float* conf) {
  struct tm tmv;
  if (nowEpoch() == 0 || !getLocalTime(&tmv, 0)) { *conf = 0.30f; return T_PREP; }
  int h = tmv.tm_hour;
  if (h >= RULE_PREP_START  && h < RULE_PREP_END)    { *conf = 0.60f; return T_PREP;  }
  if (h >= RULE_LUNCH_START && h < RULE_LUNCH_END)   { *conf = 0.60f; return T_PLATE; }
  if (h >= RULE_DINNER_START && h < RULE_DINNER_END) { *conf = 0.60f; return T_PLATE; }
  *conf = 0.40f; return T_PREP;
}

static void recordDisposal(WasteType type, const char* source, float conf, float curLevel, bool curStable) {
  // refine: if the plateau settled a bit further, use the latest stable value
  if (curStable && fabsf(curLevel - pending.levelG) < MIN_DISPOSAL_G) {
    pending.levelG  = curLevel;
    pending.weightG = curLevel - refG;
  }
  emit("disposal", pending.weightG, type, source, conf, pending.epoch, "");
  refG = pending.levelG;
  flashOk(); beep(60);
}

static void afterCommit() {
#if WEIGH_MODE == WEIGH_MODE_CHUTE
  servoWriteUs(SERVO_OPEN_US);
  gateT = millis();
  state = ST_GATE_OPEN;
#else
  state = ST_IDLE;
#endif
}

// =====================================================================
//  Buttons (debounced, active LOW)
// =====================================================================
static void pressType(WasteType t) {
  lastBtnType = t; lastBtnMs = millis(); btnSeq++;
  Serial.printf("[btn] %s\n", typeName(t));
  beep(25);
}

static void pollButtons() {
  uint32_t now = millis();
  for (auto& b : btns) {
    bool raw = digitalRead(b.pin);
    if (raw != b.lastRaw) { b.lastRaw = raw; b.tChange = now; }
    if (now - b.tChange > BTN_DEBOUNCE_MS && raw != b.stable) {
      b.stable = raw;
      if (raw == LOW) pressType(b.type);      // falling edge = press
    }
  }
}

// =====================================================================
//  Main state machine
// =====================================================================
static void stateMachine(bool stable, float level) {
  uint32_t now = millis();

  // Overload protection (5 kg cell)
  if (stable && level > MAX_LOAD_G && !overloadLatched) {
    overloadLatched = true;
    emit("overload", level, T_NONE, "sensor", 1.0f, nowEpoch(), "load_above_max");
  }
  if (stable && level < MAX_LOAD_G * 0.9f) overloadLatched = false;

  switch (state) {
    case ST_IDLE: {
      if (!stable) break;
      float delta = level - refG;
      if (delta >= MIN_DISPOSAL_G) {
        pending = { delta, level, now, nowEpoch() };
        bool freshBtn = (btnSeq != consumedSeq) && (now - lastBtnMs <= TYPE_PRE_MS);
        if (freshBtn) {                                    // pressed BEFORE tipping
          consumedSeq = btnSeq;
          recordDisposal(lastBtnType, "button", 1.0f, level, stable);
          afterCommit();
        } else {
          consumedSeq = btnSeq;                            // discard stale presses
          state = ST_AWAIT_TYPE;
        }
      } else if (delta <= -MIN_DISPOSAL_G) {
        // Negative step is NOT a disposal: insert emptied / bin swap / item removed
        emit("empty", delta, T_NONE, "sensor", 1.0f, nowEpoch(), "negative_step");
        refG = level;
      } else {
        refG += AZT_GAIN * delta;                          // auto-zero tracking (drift)
      }
      break;
    }

    case ST_AWAIT_TYPE: {
      if (btnSeq != consumedSeq) {                         // pressed AFTER tipping
        consumedSeq = btnSeq;
        recordDisposal(lastBtnType, "button", 1.0f, level, stable);
        afterCommit();
        break;
      }
      if (stable) {
        float delta = level - refG;
        if (fabsf(delta) < MIN_DISPOSAL_G) {               // went back to baseline
          // lean / bump / hand on tray: never a disposal, but keep it in the raw log
          emit("rejected", pending.weightG, T_NONE, "sensor", 1.0f, pending.epoch, "transient_returned_to_baseline");
          state = ST_IDLE;
          break;
        }
        if (level - pending.levelG >= MIN_DISPOSAL_G) {    // second tip before any button
          float c; WasteType t = timeRule(&c);
          recordDisposal(t, "time_rule", c, pending.levelG, false);
          pending = { level - refG, level, now, nowEpoch() };
          break;                                           // stay in AWAIT for the 2nd one
        }
      }
      if (now - pending.detectMs > TYPE_WAIT_MS) {         // cook forgot the button
        float c; WasteType t = timeRule(&c);
        recordDisposal(t, "time_rule", c, level, stable);
        afterCommit();
      }
      break;
    }

    case ST_GATE_OPEN:
      if (now - gateT > GATE_OPEN_MS) {
        servoWriteUs(SERVO_CLOSED_US);
        gateT = now; resetStability();
        state = ST_GATE_SETTLE;
      }
      break;

    case ST_GATE_SETTLE:
      if (now - gateT > GATE_SETTLE_MS && stable) {
        if (level > 3 * MIN_DISPOSAL_G)
          Serial.printf("[warn] %.0f g residue on tray (sticky food?)\n", level);
        refG = level;                                      // new baseline incl. residue
        state = ST_IDLE;
      }
      break;
  }
}

// =====================================================================
//  Serial console (calibration + demo helpers)
// =====================================================================
static void doTare() {
  tareOffset = hxAverage(20);
  refG = 0; ema = 0; emaInit = false; medFill = 0; resetStability();
  prefs.putInt("offset", tareOffset);
  Serial.printf("[cal] tare offset = %ld\n", (long)tareOffset);
}

static void handleSerial() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n'); cmd.trim();
  if (cmd == "t") doTare();
  else if (cmd.startsWith("c ")) {
    float g = cmd.substring(2).toFloat();
    if (g <= 0) { Serial.println("usage: c 500  (known weight in grams on tray)"); return; }
    int32_t raw = hxAverage(20);
    scaleCountsPerG = (float)(raw - tareOffset) / g;
    prefs.putFloat("scale", scaleCountsPerG);
    Serial.printf("[cal] scale = %.3f counts/g (saved)\n", scaleCountsPerG);
    ema = 0; emaInit = false; medFill = 0; resetStability();
  }
  else if (cmd == "r") streamRaw = !streamRaw;
  else if (cmd == "s") Serial.printf("[status] state=%d ref=%.1fg ema=%.1fg wifi=%d queue=%u boot=%lu\n",
                                     state, refG, ema, WiFi.status() == WL_CONNECTED, (unsigned)queueSize(), (unsigned long)bootId);
  else if (cmd == "q") Serial.printf("[queue] %u events waiting\n", (unsigned)queueSize());
  else if (cmd == "x") { xSemaphoreTake(fsMutex, portMAX_DELAY); LittleFS.remove(QUEUE_FILE); xSemaphoreGive(fsMutex); Serial.println("[queue] cleared"); }
  else if (cmd == "1") pressType(T_PREP);
  else if (cmd == "2") pressType(T_SPOILED);
  else if (cmd == "3") pressType(T_PLATE);
}

// =====================================================================
//  setup / loop
// =====================================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== Bio Waste Tracker ===");

  pinMode(PIN_HX_DOUT, INPUT);
  pinMode(PIN_HX_SCK, OUTPUT); digitalWrite(PIN_HX_SCK, LOW);
  for (auto& b : btns) pinMode(b.pin, INPUT_PULLUP);
  pinMode(PIN_LED_OK, OUTPUT);
  if (PIN_LED_WAIT >= 0) pinMode(PIN_LED_WAIT, OUTPUT);
  if (PIN_BUZZER   >= 0) pinMode(PIN_BUZZER, OUTPUT);
  servoInit();

  fsMutex = xSemaphoreCreateMutex();
  if (!LittleFS.begin(true)) Serial.println("[fs] LittleFS mount failed");

  prefs.begin("bwt", false);
  scaleCountsPerG = prefs.getFloat("scale", DEFAULT_SCALE);
  bootId = prefs.getUInt("boot", 0) + 1;
  prefs.putUInt("boot", bootId);
  Serial.printf("[boot] id=%lu scale=%.3f queue=%u\n", (unsigned long)bootId, scaleCountsPerG, (unsigned)queueSize());

  delay(500);                       // let the load cell settle
  doTare();                         // tray must be EMPTY at power-on

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  configTzTime(TZ_INFO, NTP_SERVER);   // syncs in background once Wi-Fi is up

  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);
}

void loop() {
  static uint32_t lastSample = millis(), lastPrint = 0;
  uint32_t now = millis();

  handleSerial();
  pollButtons();

  if (hxReady()) {
    int32_t raw = hxReadRaw();
    float g = (float)(raw - tareOffset) / scaleCountsPerG;
    float m = medianPush(g);
    if (!emaInit) { ema = m; emaInit = true; }
    ema += EMA_ALPHA * (m - ema);
    windowPush(ema, now);
    lastSample = now;
    sensorFault = false;
    if (streamRaw && now - lastPrint > 200) {
      lastPrint = now;
      Serial.printf("raw=%ld g=%.1f med=%.1f ema=%.1f ref=%.1f\n", (long)raw, g, m, ema, refG);
    }
  }

  if (now - lastSample > HX_TIMEOUT_MS && !sensorFault) {
    sensorFault = true;                 // wiring / HX711 dead: say so, never fake data
    emit("fault", 0, T_NONE, "sensor", 1.0f, nowEpoch(), "hx711_timeout");
  }

  float level = 0;
  bool stable = !sensorFault && isStable(&level);
  stateMachine(stable, level);

  // LEDs: OK flash after an event; WAIT LED blinks while a type is expected
  digitalWrite(PIN_LED_OK, (now < okLedUntil || (sensorFault && (now / 150) % 2)) ? HIGH : LOW);
  if (PIN_LED_WAIT >= 0)
    digitalWrite(PIN_LED_WAIT, (state == ST_AWAIT_TYPE && (now / 250) % 2) ? HIGH : LOW);

  delay(2);
}
