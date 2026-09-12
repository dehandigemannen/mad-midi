#include "mode.h"

static const uint8_t LED_PIN = 13;
static const unsigned long CONFIG_TIMEOUT_MS = 15000UL;
static const unsigned long CONFIG_BLINK_INTERVAL_MS = 300UL;

DeviceMode currentMode = DeviceMode::RUN;
static unsigned long lastConfigActivity = 0;
static unsigned long lastBlinkToggle = 0;
static bool ledState = false;

void modeInit() {
  pinMode(LED_PIN, OUTPUT);
  currentMode = DeviceMode::RUN;
  digitalWrite(LED_PIN, HIGH);
}

void enterConfigMode() {
  currentMode = DeviceMode::CONFIG;
  lastConfigActivity = millis();
  lastBlinkToggle = millis();
  ledState = false;
  digitalWrite(LED_PIN, LOW);
}

void resumeRunMode() {
  currentMode = DeviceMode::RUN;
  digitalWrite(LED_PIN, HIGH);
}

void noteConfigActivity() {
  lastConfigActivity = millis();
}

void modeUpdate() {
  if (currentMode != DeviceMode::CONFIG) return;

  if (millis() - lastConfigActivity > CONFIG_TIMEOUT_MS) {
    resumeRunMode();
    return;
  }
  if (millis() - lastBlinkToggle > CONFIG_BLINK_INTERVAL_MS) {
    lastBlinkToggle = millis();
    ledState = !ledState;
    digitalWrite(LED_PIN, ledState ? HIGH : LOW);
  }
}
