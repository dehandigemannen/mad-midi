#pragma once
#include <Arduino.h>

void rolesInit();
void rolesReconfigure();
void rolesScanInputs();
void rolesHandleIncomingNote(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn);
void rolesHandleIncomingCC(uint8_t channel, uint8_t controller, uint8_t value);
