#pragma once
#include <Arduino.h>

// CMD_TEST_PIN levels (see protocol.cpp)
static const uint8_t PIN_TEST_LOW = 0;
static const uint8_t PIN_TEST_HIGH = 1;
static const uint8_t PIN_TEST_RELEASE = 2; // hand the pin back to its configured role
static const uint8_t PIN_TEST_INPUT = 3;   // INPUT_PULLUP, for reading a button on a pin with no button role

// CMD_READ_PINS reply: digital levels as a bitmask (bit n = pin n), then the
// 16 analog pins (A0-A15) as little-endian 10-bit analogRead() values.
static const uint8_t PIN_STATE_DIGITAL_BYTES = 9; // (PIN_COUNT + 7) / 8
static const uint8_t PIN_STATE_ANALOG_COUNT = 16;
static const uint8_t PIN_STATE_SIZE = PIN_STATE_DIGITAL_BYTES + PIN_STATE_ANALOG_COUNT * 2;

void rolesInit();
void rolesReconfigure();
void rolesScanInputs();
void rolesHandleIncomingNote(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn);
void rolesHandleIncomingCC(uint8_t channel, uint8_t controller, uint8_t value);
bool rolesTestPin(uint8_t pin, uint8_t level); // false = reserved/out-of-range pin or bad level
void rolesReadPins(uint8_t* out); // fills PIN_STATE_SIZE bytes, see above
void rolesEndPinTest(); // restores every pin touched by rolesTestPin(); called on leaving config mode
