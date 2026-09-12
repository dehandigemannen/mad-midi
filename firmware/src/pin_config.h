#pragma once
#include <Arduino.h>

static const uint8_t PIN_COUNT = 70; // Mega: 0-53 digital, 54-69 = A0-A15
static const uint8_t CONFIG_FORMAT_VERSION = 3; // bumped: header gained deviceId, encoder fields reinterpreted for shift, FLAG_MATCH_CC added

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
};

static const uint8_t RECORD_SIZE = 8;
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
