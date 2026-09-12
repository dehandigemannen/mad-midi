#pragma once
#include <Arduino.h>

enum class DeviceMode : uint8_t { RUN, CONFIG };

extern DeviceMode currentMode;

void modeInit();
void modeUpdate();
void enterConfigMode();
void resumeRunMode();
void noteConfigActivity();
