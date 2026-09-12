#include "roles.h"
#include "pin_config.h"
#include "midi_io.h"
#include <string.h>

static const unsigned long DEBOUNCE_MS = 3;

static uint8_t rawStable[PIN_COUNT];
static uint16_t lastChangeMs[PIN_COUNT];
static uint8_t toggleState[PIN_COUNT];
static int16_t potLastSent[PIN_COUNT];
static int8_t encoderAccum[PIN_COUNT];
static uint8_t encoderPrevState[PIN_COUNT];

// Standard quadrature transition table, indexed by (prevState<<2 | newState).
static const int8_t QUAD_TABLE[16] = {
   0, -1,  1,  0,
   1,  0,  0, -1,
  -1,  0,  0,  1,
   0,  1, -1,  0
};

static void configurePinMode(uint8_t pin) {
  const PinConfig& c = pinConfigs[pin];
  switch (c.role) {
    case ROLE_BUTTON_NOTE:
    case ROLE_BUTTON_CC:
    case ROLE_BUTTON_SHIFT:
    case ROLE_ENCODER_A:
    case ROLE_ENCODER_B:
      pinMode(pin, INPUT_PULLUP);
      break;
    case ROLE_LIGHT_DIGITAL:
    case ROLE_LIGHT_PWM:
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);
      break;
    default:
      break; // ROLE_POT_CC needs no pinMode; ROLE_UNUSED left alone
  }
}

void rolesInit() {
  memset(rawStable, 0, sizeof(rawStable));
  memset(lastChangeMs, 0, sizeof(lastChangeMs));
  memset(toggleState, 0, sizeof(toggleState));
  for (uint8_t i = 0; i < PIN_COUNT; i++) potLastSent[i] = -1;
  memset(encoderAccum, 0, sizeof(encoderAccum));
  memset(encoderPrevState, 0, sizeof(encoderPrevState));

  for (uint8_t pin = 0; pin < PIN_COUNT; pin++) {
    if (isPinReserved(pin)) continue;
    configurePinMode(pin);
    uint8_t role = pinConfigs[pin].role;
    if (role == ROLE_BUTTON_NOTE || role == ROLE_BUTTON_CC || role == ROLE_BUTTON_SHIFT ||
        role == ROLE_ENCODER_A || role == ROLE_ENCODER_B) {
      rawStable[pin] = digitalRead(pin);
    }
  }
  for (uint8_t pin = 0; pin + 1 < PIN_COUNT; pin++) {
    if (pinConfigs[pin].role == ROLE_ENCODER_A && pinConfigs[pin + 1].role == ROLE_ENCODER_B) {
      encoderPrevState[pin] = (uint8_t)((rawStable[pin] << 1) | rawStable[pin + 1]);
    }
  }
}

void rolesReconfigure() {
  rolesInit();
}

static void handleButtonEdge(uint8_t pin, uint8_t stableLevel) {
  const PinConfig& c = pinConfigs[pin];
  // INPUT_PULLUP idle = HIGH, pressed pulls to LOW; FLAG_INVERT swaps that.
  bool pressed = (c.flags & FLAG_INVERT) ? (stableLevel == HIGH) : (stableLevel == LOW);

  if (c.role == ROLE_BUTTON_NOTE) {
    if (pressed) midiSendNoteOn(c.channel, c.number, 127);
    else midiSendNoteOff(c.channel, c.number, 0);
  } else if (c.role == ROLE_BUTTON_CC) {
    if (c.flags & FLAG_TOGGLE) {
      if (pressed) {
        toggleState[pin] = toggleState[pin] ? 0 : 127;
        midiSendCC(c.channel, c.number, toggleState[pin]);
      }
    } else {
      midiSendCC(c.channel, c.number, pressed ? 127 : 0);
    }
  }
}

static bool isShiftPinActive(uint8_t shiftPin) {
  if (shiftPin >= PIN_COUNT) return false;
  if (pinConfigs[shiftPin].role != ROLE_BUTTON_SHIFT) return false;
  bool invert = pinConfigs[shiftPin].flags & FLAG_INVERT;
  // INPUT_PULLUP idle = HIGH, pressed pulls to LOW; FLAG_INVERT swaps that.
  return invert ? (rawStable[shiftPin] == HIGH) : (rawStable[shiftPin] == LOW);
}

static void scanButtonsAndEncoders() {
  uint16_t nowMs = (uint16_t)millis();
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++) {
    if (isPinReserved(pin)) continue;
    uint8_t role = pinConfigs[pin].role;

    if (role == ROLE_BUTTON_NOTE || role == ROLE_BUTTON_CC) {
      uint8_t level = digitalRead(pin);
      if (level != rawStable[pin] && (uint16_t)(nowMs - lastChangeMs[pin]) >= DEBOUNCE_MS) {
        rawStable[pin] = level;
        lastChangeMs[pin] = nowMs;
        handleButtonEdge(pin, level);
      }
    } else if (role == ROLE_BUTTON_SHIFT) {
      // Pure modifier: track debounced state only, no MIDI of its own.
      uint8_t level = digitalRead(pin);
      if (level != rawStable[pin] && (uint16_t)(nowMs - lastChangeMs[pin]) >= DEBOUNCE_MS) {
        rawStable[pin] = level;
        lastChangeMs[pin] = nowMs;
      }
    } else if (role == ROLE_ENCODER_A) {
      if (pin + 1 >= PIN_COUNT || pinConfigs[pin + 1].role != ROLE_ENCODER_B) continue;
      uint8_t a = digitalRead(pin);
      uint8_t b = digitalRead(pin + 1);
      uint8_t state = (uint8_t)((a << 1) | b);
      uint8_t idx = (uint8_t)((encoderPrevState[pin] << 2) | state) & 0x0F;
      int8_t movement = QUAD_TABLE[idx];
      encoderPrevState[pin] = state;
      if (movement != 0) {
        encoderAccum[pin] = (int8_t)(encoderAccum[pin] + movement);
        if (encoderAccum[pin] >= 4 || encoderAccum[pin] <= -4) {
          int8_t step = encoderAccum[pin] >= 4 ? 1 : -1;
          encoderAccum[pin] = 0;

          const PinConfig& c = pinConfigs[pin];
          bool shifted = isShiftPinActive(c.inMin);
          uint8_t sendChannel = shifted ? c.outMin : c.channel;
          uint8_t sendNumber = shifted ? c.outMax : c.number;
          midiSendCC(sendChannel, sendNumber, step > 0 ? 1 : 127);
        }
      }
    }
  }
}

static void scanPots() {
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++) {
    if (isPinReserved(pin)) continue;
    if (pinConfigs[pin].role != ROLE_POT_CC) continue;

    const PinConfig& c = pinConfigs[pin];
    int raw = analogRead(pin); // pins 54-69 == A0-A15 in the Mega core
    uint16_t inMin = (uint16_t)c.inMin << 2;
    uint16_t inMax = (uint16_t)c.inMax << 2;
    if (inMax <= inMin) { inMin = 0; inMax = 1023; }

    long mapped = map(raw, inMin, inMax, c.outMin, c.outMax);
    uint8_t lo = c.outMin < c.outMax ? c.outMin : c.outMax;
    uint8_t hi = c.outMin < c.outMax ? c.outMax : c.outMin;
    mapped = constrain(mapped, (long)lo, (long)hi);
    if (c.flags & FLAG_INVERT) mapped = (long)c.outMax + c.outMin - mapped;

    if (potLastSent[pin] != mapped) {
      potLastSent[pin] = (int16_t)mapped;
      midiSendCC(c.channel, c.number, (uint8_t)mapped);
    }
  }
}

void rolesScanInputs() {
  scanButtonsAndEncoders();
  scanPots();
}

static void applyToOutputPin(uint8_t pin, uint16_t value, uint16_t inMin, uint16_t inMax) {
  const PinConfig& c = pinConfigs[pin];
  if (inMax <= inMin) { inMin = 0; inMax = 127; }

  if (c.role == ROLE_LIGHT_DIGITAL) {
    bool on = value > inMin;
    if (c.flags & FLAG_INVERT) on = !on;
    digitalWrite(pin, on ? HIGH : LOW);
  } else if (c.role == ROLE_LIGHT_PWM) {
    long mapped = map((long)value, (long)inMin, (long)inMax, c.outMin, c.outMax);
    uint8_t lo = c.outMin < c.outMax ? c.outMin : c.outMax;
    uint8_t hi = c.outMin < c.outMax ? c.outMax : c.outMin;
    mapped = constrain(mapped, (long)lo, (long)hi);
    if (c.flags & FLAG_INVERT) mapped = (long)c.outMax + c.outMin - mapped;
    analogWrite(pin, (uint8_t)mapped); // caller must have assigned this role only to a PWM-capable pin
  }
}

void rolesHandleIncomingNote(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn) {
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++) {
    if (isPinReserved(pin)) continue;
    const PinConfig& c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM) continue;
    if (c.channel != channel || c.number != note) continue;
    applyToOutputPin(pin, isOn ? velocity : 0, c.inMin, c.inMax);
  }
}

void rolesHandleIncomingCC(uint8_t channel, uint8_t controller, uint8_t value) {
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++) {
    if (isPinReserved(pin)) continue;
    const PinConfig& c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM) continue;
    if (c.channel != channel || c.number != controller) continue;
    applyToOutputPin(pin, value, c.inMin, c.inMax);
  }
}
