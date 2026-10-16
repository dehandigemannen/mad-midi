#pragma once
#include <Arduino.h>

static const uint8_t PIN_COUNT = 70; // Mega: 0-53 digital, 54-69 = A0-A15
static const uint8_t CONFIG_FORMAT_VERSION = 6; // bumped: PinConfig grew listenPin (LIGHT_DIGITAL/LIGHT_PWM direct-button-listen support)

enum PinRole : uint8_t {
  ROLE_UNUSED = 0,
  ROLE_BUTTON_NOTE = 1,
  ROLE_BUTTON_CC = 2,
  ROLE_LIGHT_DIGITAL = 3,
  ROLE_LIGHT_PWM = 4,
  ROLE_ENCODER_A = 5,
  ROLE_ENCODER_B = 6,
  ROLE_POT_CC = 7,
  ROLE_BUTTON_SHIFT = 8,
};

enum PinFlags : uint8_t {
  FLAG_INVERT = 1 << 0,
  FLAG_TOGGLE = 1 << 1,
  // LIGHT_DIGITAL/LIGHT_PWM only: unset = number is a Note, set = number is a CC.
  // Without this a light would ambiguously match either a Note or a CC with
  // the same number.
  FLAG_MATCH_CC = 1 << 2,
  // ENCODER_A only: scale relative CC step size up the faster the encoder is
  // turned (see roles.cpp). Unset = fixed 65/63 per detent, unchanged.
  FLAG_ACCEL_ENABLE = 1 << 3,
};

// For ROLE_ENCODER_A, inMin/inMax/outMin/outMax are repurposed (encoders
// don't otherwise need a value range):
//   inMin  = shiftButtonPin (0-69 = a pin with ROLE_BUTTON_SHIFT; 255 = none)
//   inMax  = unused
//   outMin = shifted-state MIDI channel
//   outMax = shifted-state MIDI CC number
static const uint8_t NO_SHIFT_PIN = 255;

struct PinConfig {
  uint8_t role;
  uint8_t channel;
  uint8_t number;
  uint8_t inMin;
  uint8_t inMax;
  uint8_t outMin;
  uint8_t outMax;
  uint8_t flags;
  // ROLE_POT_CC only: shift support, same idea as ENCODER_A's shift fields
  // above but pots need inMin/inMax/outMin/outMax for their ADC->CC scaling
  // (see scanPots() in roles.cpp), so there's no spare field to repurpose --
  // these are dedicated instead.
  uint8_t shiftPin;     // 0-69 = a pin with ROLE_BUTTON_SHIFT; NO_SHIFT_PIN = none
  uint8_t shiftChannel; // shifted-state MIDI channel
  uint8_t shiftNumber;  // shifted-state MIDI CC number
  // LIGHT_DIGITAL/LIGHT_PWM only: mirror this pin's live button state
  // directly (0-69, a BUTTON_NOTE/BUTTON_CC/BUTTON_SHIFT pin) instead of
  // waiting for a matching Note/CC over MIDI -- see applyListenLights() in
  // roles.cpp. NO_SHIFT_PIN (reused as a generic "no pin" sentinel) = off,
  // falls back to ordinary Note/CC matching via FLAG_MATCH_CC.
  uint8_t listenPin;
};

static const uint8_t RECORD_SIZE = 12;
static const uint16_t HEADER_SIZE = 6; // magic(2) + version(1) + pinCount(1) + deviceId(1) + checksum(1)
static const uint16_t BLOB_SIZE = HEADER_SIZE + (uint16_t)PIN_COUNT * RECORD_SIZE;

extern PinConfig pinConfigs[PIN_COUNT];
extern uint8_t deviceId; // 0 = unassigned

bool isPinReserved(uint8_t pin);
void loadDefaultConfig();
bool loadConfigFromEEPROM();
void saveConfigToEEPROM();
uint8_t computeChecksum();
void setDeviceId(uint8_t id); // persists just this one byte

void serializeConfigBlob(uint8_t* buffer);
bool deserializeConfigBlob(const uint8_t* buffer, uint16_t len);
