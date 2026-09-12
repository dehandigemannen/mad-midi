#include <Arduino.h>
#include "pin_config.h"
#include "mode.h"
#include "midi_io.h"
#include "roles.h"
#include "protocol.h"

void setup() {
  Serial.begin(115200);
  loadConfigFromEEPROM();
  modeInit();
  rolesInit();
  protocolInit();
  midiParserInit(rolesHandleIncomingNote, rolesHandleIncomingCC);
}

void loop() {
  protocolUpdate();
  modeUpdate();
  if (currentMode == DeviceMode::RUN) {
    rolesScanInputs();
  }
}
