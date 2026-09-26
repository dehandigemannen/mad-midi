#include "protocol.h"
#include "mode.h"
#include "midi_io.h"
#include "pin_config.h"
#include "roles.h"

// SysEx trigger (valid MIDI, 0x7D = reserved non-commercial/private-use ID):
//   App -> Firmware: F0 7D 01 F7   (enter config mode)
//   Firmware -> App: F0 7D 02 F7   (ack)
static const uint8_t TRIGGER_SEQ[4] = { 0xF0, 0x7D, 0x01, 0xF7 };
static uint8_t triggerMatchPos = 0;

// Config-mode frame: [0xAA][CMD][LEN_LO][LEN_HI][PAYLOAD x LEN][CHECKSUM]
// CHECKSUM makes (CMD+LEN_LO+LEN_HI+sum(PAYLOAD)+CHECKSUM) mod 256 == 0.
static const uint8_t FRAME_START = 0xAA;
static const uint8_t CMD_PING = 0x01;
static const uint8_t CMD_READ_CONFIG = 0x02;
static const uint8_t CMD_WRITE_CONFIG = 0x03;
static const uint8_t CMD_RESUME_RUN = 0x04;
static const uint8_t CMD_SET_DEVICE_ID = 0x05;
static const uint8_t CMD_TEST_PIN = 0x06; // payload [pin, level]; level = PIN_TEST_LOW/HIGH/RELEASE/INPUT (roles.h)
static const uint8_t CMD_READ_PINS = 0x07; // reply = PIN_STATE_SIZE bytes (roles.h)

static const uint8_t FW_VERSION = 3; // 2: CMD_TEST_PIN, 3: CMD_READ_PINS + PIN_TEST_INPUT

enum class FrameState : uint8_t { WAIT_START, CMD, LEN_LO, LEN_HI, PAYLOAD, CHECKSUM };
static FrameState frameState = FrameState::WAIT_START;
static uint8_t frameCmd = 0;
static uint16_t frameLen = 0;
static uint16_t framePayloadReceived = 0;
static uint8_t framePayload[BLOB_SIZE]; // largest payload we ever need is a full config blob

static void sendSysexAck() {
  Serial.write((uint8_t)0xF0);
  Serial.write((uint8_t)0x7D);
  Serial.write((uint8_t)0x02);
  Serial.write((uint8_t)0xF7);
}

static void sendFrame(uint8_t cmd, const uint8_t* payload, uint16_t len) {
  uint16_t sum = (uint16_t)(cmd + (len & 0xFF) + ((len >> 8) & 0xFF));
  Serial.write(FRAME_START);
  Serial.write(cmd);
  Serial.write((uint8_t)(len & 0xFF));
  Serial.write((uint8_t)((len >> 8) & 0xFF));
  for (uint16_t i = 0; i < len; i++) {
    Serial.write(payload[i]);
    sum = (uint16_t)(sum + payload[i]);
  }
  Serial.write((uint8_t)((256 - (sum & 0xFF)) & 0xFF));
}

static void handleConfigCommand(uint8_t cmd, const uint8_t* payload, uint16_t len) {
  noteConfigActivity();
  switch (cmd) {
    case CMD_PING: {
      uint8_t reply[3] = { FW_VERSION, CONFIG_FORMAT_VERSION, deviceId };
      sendFrame(CMD_PING | 0x80, reply, 3);
      break;
    }
    case CMD_READ_CONFIG: {
      static uint8_t blob[BLOB_SIZE];
      serializeConfigBlob(blob);
      sendFrame(CMD_READ_CONFIG | 0x80, blob, BLOB_SIZE);
      break;
    }
    case CMD_WRITE_CONFIG: {
      if (deserializeConfigBlob(payload, len)) {
        rolesReconfigure();
        sendFrame(CMD_WRITE_CONFIG | 0x80, nullptr, 0);
      } else {
        uint8_t err[1] = { 1 };
        sendFrame((uint8_t)(CMD_WRITE_CONFIG | 0xC0), err, 1);
      }
      break;
    }
    case CMD_RESUME_RUN: {
      sendFrame(CMD_RESUME_RUN | 0x80, nullptr, 0);
      resumeRunMode();
      break;
    }
    case CMD_SET_DEVICE_ID: {
      if (len == 1) {
        setDeviceId(payload[0]);
        sendFrame(CMD_SET_DEVICE_ID | 0x80, nullptr, 0);
      } else {
        uint8_t err[1] = { 1 };
        sendFrame((uint8_t)(CMD_SET_DEVICE_ID | 0xC0), err, 1);
      }
      break;
    }
    case CMD_TEST_PIN: {
      if (len == 2 && rolesTestPin(payload[0], payload[1])) {
        sendFrame(CMD_TEST_PIN | 0x80, nullptr, 0);
      } else {
        uint8_t err[1] = { 1 };
        sendFrame((uint8_t)(CMD_TEST_PIN | 0xC0), err, 1);
      }
      break;
    }
    case CMD_READ_PINS: {
      uint8_t state[PIN_STATE_SIZE];
      rolesReadPins(state);
      sendFrame(CMD_READ_PINS | 0x80, state, PIN_STATE_SIZE);
      break;
    }
    default:
      break;
  }
}

static void feedConfigFrameByte(uint8_t b) {
  switch (frameState) {
    case FrameState::WAIT_START:
      if (b == FRAME_START) frameState = FrameState::CMD;
      break;
    case FrameState::CMD:
      frameCmd = b;
      frameState = FrameState::LEN_LO;
      break;
    case FrameState::LEN_LO:
      frameLen = b;
      frameState = FrameState::LEN_HI;
      break;
    case FrameState::LEN_HI:
      frameLen = (uint16_t)(frameLen | ((uint16_t)b << 8));
      framePayloadReceived = 0;
      if (frameLen > sizeof(framePayload)) {
        frameState = FrameState::WAIT_START; // malformed/oversized frame, drop
      } else if (frameLen == 0) {
        frameState = FrameState::CHECKSUM;
      } else {
        frameState = FrameState::PAYLOAD;
      }
      break;
    case FrameState::PAYLOAD:
      framePayload[framePayloadReceived++] = b;
      if (framePayloadReceived >= frameLen) frameState = FrameState::CHECKSUM;
      break;
    case FrameState::CHECKSUM: {
      uint16_t sum = (uint16_t)(frameCmd + (frameLen & 0xFF) + ((frameLen >> 8) & 0xFF) + b);
      for (uint16_t i = 0; i < frameLen; i++) sum = (uint16_t)(sum + framePayload[i]);
      frameState = FrameState::WAIT_START;
      if ((sum & 0xFF) == 0) {
        handleConfigCommand(frameCmd, framePayload, frameLen);
      }
      break;
    }
  }
}

static void feedRunModeByte(uint8_t b) {
  if (b == TRIGGER_SEQ[triggerMatchPos]) {
    triggerMatchPos++;
    if (triggerMatchPos == 4) {
      triggerMatchPos = 0;
      enterConfigMode();
      sendSysexAck();
    }
    return;
  }
  triggerMatchPos = (b == TRIGGER_SEQ[0]) ? 1 : 0;
  midiParserFeed(b);
}

void protocolInit() {
  triggerMatchPos = 0;
  frameState = FrameState::WAIT_START;
}

void protocolUpdate() {
  while (Serial.available() > 0) {
    uint8_t b = (uint8_t)Serial.read();
    if (currentMode == DeviceMode::RUN) {
      feedRunModeByte(b);
    } else {
      feedConfigFrameByte(b);
    }
  }
}
