"""Wire protocol shared with firmware/src/protocol.cpp, midi_io.cpp and
pin_config.h. Keep these in sync by hand if either side changes — there is
no code generation between the two right now.
"""

import struct

PIN_COUNT = 70  # Mega: 0-53 digital, 54-69 = A0-A15
CONFIG_FORMAT_VERSION = 6  # bumped: PinConfig grew listen_pin (LIGHT_DIGITAL/LIGHT_PWM direct-button-listen support)
RECORD_SIZE = 12
HEADER_SIZE = 6  # magic(2) + version(1) + pinCount(1) + deviceId(1) + checksum(1)
BLOB_SIZE = HEADER_SIZE + PIN_COUNT * RECORD_SIZE

RESERVED_PINS = {0, 1, 13}  # USB Serial0 RX/TX, status LED
NO_SHIFT_PIN = 255  # ENCODER_A in_min / POT_CC shift_pin / light listen_pin sentinel: no pin assigned

# --- PinRole ---------------------------------------------------------------

ROLE_UNUSED = 0
ROLE_BUTTON_NOTE = 1
ROLE_BUTTON_CC = 2
ROLE_LIGHT_DIGITAL = 3
ROLE_LIGHT_PWM = 4
ROLE_ENCODER_A = 5
ROLE_ENCODER_B = 6
ROLE_POT_CC = 7
ROLE_BUTTON_SHIFT = 8

ROLE_NAMES = {
    ROLE_UNUSED: "unused",
    ROLE_BUTTON_NOTE: "button_note",
    ROLE_BUTTON_CC: "button_cc",
    ROLE_LIGHT_DIGITAL: "light_digital",
    ROLE_LIGHT_PWM: "light_pwm",
    ROLE_ENCODER_A: "encoder_a",
    ROLE_ENCODER_B: "encoder_b",
    ROLE_POT_CC: "pot_cc",
    ROLE_BUTTON_SHIFT: "button_shift",
}
ROLE_BY_NAME = {v: k for k, v in ROLE_NAMES.items()}

FLAG_INVERT = 1 << 0
FLAG_TOGGLE = 1 << 1
# LIGHT_DIGITAL/LIGHT_PWM only: unset = number is a Note, set = number is a CC.
FLAG_MATCH_CC = 1 << 2
# ENCODER_A only: scale relative CC step size up the faster the encoder is turned.
FLAG_ACCEL_ENABLE = 1 << 3

# --- SysEx mode-switch trigger/ack (0x7D = MIDI-reserved non-commercial ID) -

SYSEX_ENTER_CONFIG = bytes([0xF0, 0x7D, 0x01, 0xF7])
SYSEX_CONFIG_ACK = bytes([0xF0, 0x7D, 0x02, 0xF7])

# --- Config-mode frame: [0xAA][CMD][LEN_LO][LEN_HI][PAYLOAD...][CHECKSUM] --

FRAME_START = 0xAA
CMD_PING = 0x01
CMD_READ_CONFIG = 0x02
CMD_WRITE_CONFIG = 0x03
CMD_RESUME_RUN = 0x04
CMD_SET_DEVICE_ID = 0x05


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    body = bytes([cmd, length & 0xFF, (length >> 8) & 0xFF]) + payload
    checksum = (256 - (sum(body) & 0xFF)) & 0xFF
    return bytes([FRAME_START]) + body + bytes([checksum])


class FrameParser:
    """Incremental parser for config-mode frames arriving from the firmware."""

    WAIT_START, CMD, LEN_LO, LEN_HI, PAYLOAD, CHECKSUM = range(6)

    def __init__(self):
        self._reset()

    def _reset(self):
        self.state = self.WAIT_START
        self.cmd = 0
        self.length = 0
        self.payload = bytearray()

    def feed(self, byte: int):
        """Feed one byte. Returns (cmd, payload) when a full valid frame
        completes, else None (including on a checksum failure)."""
        if self.state == self.WAIT_START:
            if byte == FRAME_START:
                self.state = self.CMD
        elif self.state == self.CMD:
            self.cmd = byte
            self.state = self.LEN_LO
        elif self.state == self.LEN_LO:
            self.length = byte
            self.state = self.LEN_HI
        elif self.state == self.LEN_HI:
            self.length |= byte << 8
            self.payload = bytearray()
            self.state = self.PAYLOAD if self.length > 0 else self.CHECKSUM
        elif self.state == self.PAYLOAD:
            self.payload.append(byte)
            if len(self.payload) >= self.length:
                self.state = self.CHECKSUM
        elif self.state == self.CHECKSUM:
            body = bytes([self.cmd, self.length & 0xFF, (self.length >> 8) & 0xFF]) + bytes(self.payload)
            total = (sum(body) + byte) & 0xFF
            cmd, payload = self.cmd, bytes(self.payload)
            self._reset()
            if total == 0:
                return cmd, payload
        return None


class SysexAckDetector:
    """Matches an exact byte sequence anywhere in a stream (used to spot the
    firmware's config-mode ack while still scanning live MIDI bytes)."""

    def __init__(self, sequence: bytes):
        self.sequence = sequence
        self.pos = 0

    def feed(self, byte: int) -> bool:
        if byte == self.sequence[self.pos]:
            self.pos += 1
            if self.pos == len(self.sequence):
                self.pos = 0
                return True
        else:
            self.pos = 1 if byte == self.sequence[0] else 0
        return False


class RunningStatusMidiParser:
    """Mirrors firmware/src/midi_io.cpp's minimal channel-voice parser: Note
    On/Off and CC only, with running status. Anything else resets status."""

    def __init__(self):
        self.running_status = 0
        self.data1 = None

    def feed(self, byte: int):
        """Returns a complete 3-byte message when one finishes, else None."""
        if byte & 0x80:
            status_type = byte & 0xF0
            if status_type in (0x80, 0x90, 0xB0):
                self.running_status = byte
                self.data1 = None
            else:
                self.running_status = 0
            return None
        if not self.running_status:
            return None
        if self.data1 is None:
            self.data1 = byte
            return None
        message = bytes([self.running_status, self.data1, byte])
        self.data1 = None
        return message


# --- EEPROM config blob (de)serialization -----------------------------------

def pack_pin_config(role, channel, number, in_min, in_max, out_min, out_max, flags,
                     shift_pin, shift_channel, shift_number, listen_pin) -> bytes:
    return struct.pack("<12B", role, channel, number, in_min, in_max, out_min, out_max, flags,
                        shift_pin, shift_channel, shift_number, listen_pin)


def unpack_pin_config(data: bytes):
    return struct.unpack("<12B", data)


def build_config_blob(pins, device_id: int = 0) -> bytes:
    """pins: PIN_COUNT tuples of (role, channel, number, inMin, inMax, outMin, outMax, flags)."""
    if len(pins) != PIN_COUNT:
        raise ValueError(f"expected {PIN_COUNT} pin records, got {len(pins)}")
    records = b"".join(pack_pin_config(*p) for p in pins)
    checksum = sum(records) & 0xFF
    header = bytes([ord("M"), ord("M"), CONFIG_FORMAT_VERSION, PIN_COUNT, device_id & 0xFF, checksum])
    return header + records


def parse_config_blob(blob: bytes):
    """Returns (device_id, pins) where pins is a list of PIN_COUNT tuples."""
    if len(blob) != BLOB_SIZE:
        raise ValueError(f"expected {BLOB_SIZE} bytes, got {len(blob)}")
    if blob[0:2] != b"MM":
        raise ValueError("bad magic bytes")
    if blob[2] != CONFIG_FORMAT_VERSION:
        raise ValueError(f"unsupported config format version {blob[2]}")
    if blob[3] != PIN_COUNT:
        raise ValueError(f"unexpected pin count {blob[3]}")
    device_id = blob[4]
    records = blob[HEADER_SIZE:]
    if (sum(records) & 0xFF) != blob[5]:
        raise ValueError("checksum mismatch")
    pins = [
        unpack_pin_config(records[i * RECORD_SIZE:(i + 1) * RECORD_SIZE])
        for i in range(PIN_COUNT)
    ]
    return device_id, pins
