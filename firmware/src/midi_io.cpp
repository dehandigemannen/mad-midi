#include "midi_io.h"

void midiSendNoteOn(uint8_t channel, uint8_t note, uint8_t velocity) {
  Serial.write((uint8_t)(0x90 | (channel & 0x0F)));
  Serial.write((uint8_t)(note & 0x7F));
  Serial.write((uint8_t)(velocity & 0x7F));
}

void midiSendNoteOff(uint8_t channel, uint8_t note, uint8_t velocity) {
  Serial.write((uint8_t)(0x80 | (channel & 0x0F)));
  Serial.write((uint8_t)(note & 0x7F));
  Serial.write((uint8_t)(velocity & 0x7F));
}

void midiSendCC(uint8_t channel, uint8_t controller, uint8_t value) {
  Serial.write((uint8_t)(0xB0 | (channel & 0x0F)));
  Serial.write((uint8_t)(controller & 0x7F));
  Serial.write((uint8_t)(value & 0x7F));
}

static MidiNoteHandler noteHandler = nullptr;
static MidiCCHandler ccHandler = nullptr;

static uint8_t runningStatus = 0;
static uint8_t dataByte1 = 0;
static uint8_t dataBytesReceived = 0;

void midiParserInit(MidiNoteHandler nh, MidiCCHandler ch) {
  noteHandler = nh;
  ccHandler = ch;
  runningStatus = 0;
  dataBytesReceived = 0;
}

static void dispatch(uint8_t status, uint8_t d1, uint8_t d2) {
  uint8_t type = status & 0xF0;
  uint8_t channel = status & 0x0F;
  if (type == 0x90) {
    if (noteHandler) noteHandler(channel, d1, d2, d2 > 0);
  } else if (type == 0x80) {
    if (noteHandler) noteHandler(channel, d1, d2, false);
  } else if (type == 0xB0) {
    if (ccHandler) ccHandler(channel, d1, d2);
  }
}

void midiParserFeed(uint8_t b) {
  if (b & 0x80) {
    uint8_t type = b & 0xF0;
    if (type == 0x90 || type == 0x80 || type == 0xB0) {
      runningStatus = b;
      dataBytesReceived = 0;
    } else {
      runningStatus = 0; // unsupported status type in v1 (pitch bend, program change, ...)
    }
    return;
  }
  if (runningStatus == 0) return;
  if (dataBytesReceived == 0) {
    dataByte1 = b;
    dataBytesReceived = 1;
  } else {
    dispatch(runningStatus, dataByte1, b);
    dataBytesReceived = 0;
  }
}
