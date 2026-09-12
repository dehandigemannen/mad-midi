#pragma once
#include <Arduino.h>

void midiSendNoteOn(uint8_t channel, uint8_t note, uint8_t velocity);
void midiSendNoteOff(uint8_t channel, uint8_t note, uint8_t velocity);
void midiSendCC(uint8_t channel, uint8_t controller, uint8_t value);

typedef void (*MidiNoteHandler)(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn);
typedef void (*MidiCCHandler)(uint8_t channel, uint8_t controller, uint8_t value);

// Feed only non-SysEx bytes received while in RUN mode (protocol.cpp filters
// the SysEx mode-switch trigger out before bytes reach this parser).
void midiParserInit(MidiNoteHandler noteHandler, MidiCCHandler ccHandler);
void midiParserFeed(uint8_t b);
