#include "pin_config.h"
#include <EEPROM.h>
#include <string.h>

PinConfig pinConfigs[PIN_COUNT];
uint8_t deviceId = 0;

static const uint16_t DEVICE_ID_EEPROM_OFFSET = 4; // header byte index: 'M','M',version,pinCount,deviceId,checksum

bool isPinReserved(uint8_t pin) {
  return pin == 0 || pin == 1 || pin == 13; // USB Serial0 RX/TX, status LED
}

void loadDefaultConfig() {
  for (uint8_t i = 0; i < PIN_COUNT; i++) {
    pinConfigs[i] = (PinConfig){ ROLE_UNUSED, 0, 0, 0, 0, 0, 0, 0 };
  }
}

uint8_t computeChecksum() {
  uint16_t sum = 0;
  for (uint8_t i = 0; i < PIN_COUNT; i++) {
    const PinConfig& c = pinConfigs[i];
    sum = (uint16_t)(sum + c.role + c.channel + c.number + c.inMin + c.inMax + c.outMin + c.outMax + c.flags);
  }
  return (uint8_t)(sum & 0xFF);
}

void serializeConfigBlob(uint8_t* buffer) {
  buffer[0] = 'M';
  buffer[1] = 'M';
  buffer[2] = CONFIG_FORMAT_VERSION;
  buffer[3] = PIN_COUNT;
  buffer[4] = deviceId;
  buffer[5] = computeChecksum();
  uint16_t offset = HEADER_SIZE;
  for (uint8_t i = 0; i < PIN_COUNT; i++) {
    const PinConfig& c = pinConfigs[i];
    buffer[offset++] = c.role;
    buffer[offset++] = c.channel;
    buffer[offset++] = c.number;
    buffer[offset++] = c.inMin;
    buffer[offset++] = c.inMax;
    buffer[offset++] = c.outMin;
    buffer[offset++] = c.outMax;
    buffer[offset++] = c.flags;
  }
}

static bool parseBlobInto(const uint8_t* buffer, uint16_t len, PinConfig* out, uint8_t* outDeviceId) {
  if (len != BLOB_SIZE) return false;
  if (buffer[0] != 'M' || buffer[1] != 'M') return false;
  if (buffer[2] != CONFIG_FORMAT_VERSION) return false;
  if (buffer[3] != PIN_COUNT) return false;

  uint16_t offset = HEADER_SIZE;
  uint16_t sum = 0;
  for (uint8_t i = 0; i < PIN_COUNT; i++) {
    out[i].role = buffer[offset++];
    out[i].channel = buffer[offset++];
    out[i].number = buffer[offset++];
    out[i].inMin = buffer[offset++];
    out[i].inMax = buffer[offset++];
    out[i].outMin = buffer[offset++];
    out[i].outMax = buffer[offset++];
    out[i].flags = buffer[offset++];
    sum = (uint16_t)(sum + out[i].role + out[i].channel + out[i].number + out[i].inMin + out[i].inMax + out[i].outMin + out[i].outMax + out[i].flags);
  }
  if ((uint8_t)(sum & 0xFF) != buffer[5]) return false;
  *outDeviceId = buffer[4];
  return true;
}

bool deserializeConfigBlob(const uint8_t* buffer, uint16_t len) {
  PinConfig temp[PIN_COUNT];
  uint8_t newDeviceId;
  if (!parseBlobInto(buffer, len, temp, &newDeviceId)) return false;
  memcpy(pinConfigs, temp, sizeof(temp));
  deviceId = newDeviceId;
  saveConfigToEEPROM();
  return true;
}

static bool headerStamped(const uint8_t* buffer) {
  return buffer[0] == 'M' && buffer[1] == 'M' && buffer[2] == CONFIG_FORMAT_VERSION && buffer[3] == PIN_COUNT;
}

bool loadConfigFromEEPROM() {
  uint8_t buffer[BLOB_SIZE];
  for (uint16_t i = 0; i < BLOB_SIZE; i++) {
    buffer[i] = EEPROM.read(i);
  }
  PinConfig temp[PIN_COUNT];
  uint8_t loadedDeviceId;
  if (!parseBlobInto(buffer, BLOB_SIZE, temp, &loadedDeviceId)) {
    loadDefaultConfig();
    // The pin records failed to validate (e.g. no WRITE_CONFIG has ever
    // happened, just a SET_DEVICE_ID) — but the device id byte isn't
    // covered by that checksum (see setDeviceId()), so don't throw away a
    // still-valid id along with the bad pin records. Only reset it to
    // unassigned when the header itself was never stamped at all.
    deviceId = headerStamped(buffer) ? buffer[DEVICE_ID_EEPROM_OFFSET] : 0;
    return false;
  }
  memcpy(pinConfigs, temp, sizeof(temp));
  deviceId = loadedDeviceId;
  return true;
}

void saveConfigToEEPROM() {
  uint8_t buffer[BLOB_SIZE];
  serializeConfigBlob(buffer);
  for (uint16_t i = 0; i < BLOB_SIZE; i++) {
    EEPROM.update(i, buffer[i]); // update() skips the write if unchanged, saves EEPROM wear
  }
}

void setDeviceId(uint8_t id) {
  deviceId = id;
  // Stamp the header (magic/version/pinCount) too, not just the id byte —
  // loadConfigFromEEPROM() only trusts a stored id when the header is
  // stamped, so without this a freshly-adopted device (SET_DEVICE_ID with
  // no WRITE_CONFIG yet) would read back as unassigned on its next boot.
  EEPROM.update(0, 'M');
  EEPROM.update(1, 'M');
  EEPROM.update(2, CONFIG_FORMAT_VERSION);
  EEPROM.update(3, PIN_COUNT);
  EEPROM.update(DEVICE_ID_EEPROM_OFFSET, id);
  // checksum (byte 5) is over the pin records only, so it doesn't need
  // recomputing here — until an actual WRITE_CONFIG, the pin records will
  // just fail their own checksum check and fall back to defaults, which is
  // fine (see loadConfigFromEEPROM()).
}
