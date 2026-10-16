#include "roles.h"
#include "pin_config.h"
#include "midi_io.h"
#include <string.h>
#include <avr/interrupt.h>

static const unsigned long DEBOUNCE_MS = 10;

static void applyToOutputPin(uint8_t pin, uint16_t value, uint16_t inMin, uint16_t inMax);
static void applyListenLights(uint8_t sourcePin, bool pressed);

static uint8_t rawStable[PIN_COUNT];
static uint8_t lastRawLevel[PIN_COUNT];
static uint16_t lastChangeMs[PIN_COUNT];
static uint8_t toggleState[PIN_COUNT];
static int16_t potLastSent[PIN_COUNT];
// Pot movement threshold: potTracked only follows the raw ADC reading once
// it moves more than POT_HYSTERESIS counts away, and the CC is computed from
// potTracked rather than the raw reading. Without this, ADC noise (+-1-3
// counts, worse on spring-centred joysticks) sitting on the boundary between
// two CC values flips the output back and forth and floods the MIDI bus.
// 4 counts is half of one CC step at full 0-1023 range, so no resolution is
// lost there.
static const int16_t POT_HYSTERESIS = 4;
static int16_t potTracked[PIN_COUNT];
static bool potLastShifted[PIN_COUNT];
static volatile int8_t encoderAccum[PIN_COUNT];
static volatile uint8_t encoderPrevState[PIN_COUNT];

// Per-encoder acceleration state for FLAG_ACCEL_ENABLE, ported from the
// well-established ClickEncoder library's algorithm (0xPIT/encoder), whose
// constants were already tuned for a ~1ms service tick -- the same cadence
// TIMER1_COMPC_vect runs at here, so they're reused as-is rather than
// re-derived: decays by ENC_ACCEL_DEC every tick, bumped by ENC_ACCEL_INC on
// each confirmed step, capped at ENC_ACCEL_TOP. scanButtonsAndEncoders()
// turns the current value into a step multiplier via >>8 (0-12) when it
// drains a pending step.
static volatile uint16_t encoderAccel[PIN_COUNT];
static const uint16_t ENC_ACCEL_TOP = 3072;
static const uint16_t ENC_ACCEL_INC = 25;
static const uint16_t ENC_ACCEL_DEC = 2;

// Steps accumulated by ISR(TIMER1_COMPC_vect) below since the main loop last
// drained them: positive = CW steps pending, negative = CCW. Written only by
// the ISR, read-and-cleared only by scanButtonsAndEncoders().
static volatile int8_t encoderPending[PIN_COUNT];

// Direct port/bitmask for each configured encoder pin pair, precomputed by
// rolesInit() so the ISR never has to do digitalPinToPort()/
// digitalPinToBitMask() lookups itself on its ~1ms hot path.
struct EncoderIsrSlot
{
  uint8_t pin; // ENCODER_A pin; also indexes encoderPending/encoderAccum/encoderPrevState
  volatile uint8_t *portA;
  uint8_t maskA;
  volatile uint8_t *portB;
  uint8_t maskB;
};
static const uint8_t MAX_ENCODERS = PIN_COUNT / 2;
static EncoderIsrSlot encoderIsrSlots[MAX_ENCODERS];
static volatile uint8_t encoderIsrCount = 0;

// Standard quadrature transition table, indexed by (prevState<<2 | newState).
static const int8_t QUAD_TABLE[16] = {
    0, -1, 1, 0,
    1, 0, 0, -1,
    -1, 0, 0, 1,
    0, 1, -1, 0};

// Samples every configured encoder pair on a fixed ~1ms cadence instead of
// inline in the main loop: piggybacked on Timer1's Output Compare C, which
// the Arduino core already runs continuously (prescaler 64, 8-bit
// phase-correct PWM, ~2ms period) for analogWrite() on pins 11/12 via
// OCR1A/OCR1B. Channel C (OCR1C) is physically routed to pin 13, which is
// reserved for the status LED and never PWM'd, so it's unused -- enabling
// its compare-match interrupt just adds a periodic tick without touching
// WGM/CS/COM1A/COM1B, so it can't disturb the PWM waveform on 11/12.
//
// This replaces decoding the quadrature signal inline in
// scanButtonsAndEncoders(), which tied the sampling rate to however long the
// rest of loop() took that iteration (scanPots() in particular: analogRead()
// blocks for ~100us per configured pot). A slow iteration could let both
// quadrature bits change between samples, which QUAD_TABLE has no entry for
// (it's an invalid single-step transition) and silently drops -- read as
// missed/sluggish encoder turns, worse the more pots/buttons a board has
// configured. A steady hardware-timed tick decouples sampling from loop()
// timing entirely.
// Bumps a pin's acceleration counter on a confirmed step, capped at
// ENC_ACCEL_TOP. Called from the ISR below; kept as a tiny helper since both
// step directions need it.
static inline void bumpAccel(uint8_t pin)
{
  uint16_t accel = encoderAccel[pin];
  accel = (accel <= ENC_ACCEL_TOP - ENC_ACCEL_INC) ? (uint16_t)(accel + ENC_ACCEL_INC) : ENC_ACCEL_TOP;
  encoderAccel[pin] = accel;
}

ISR(TIMER1_COMPC_vect)
{
  for (uint8_t i = 0; i < encoderIsrCount; i++)
  {
    EncoderIsrSlot &slot = encoderIsrSlots[i];
    uint8_t a = (*slot.portA & slot.maskA) ? 1 : 0;
    uint8_t b = (*slot.portB & slot.maskB) ? 1 : 0;
    uint8_t state = (uint8_t)((a << 1) | b);
    uint8_t idx = (uint8_t)((encoderPrevState[slot.pin] << 2) | state) & 0x0F;
    int8_t movement = QUAD_TABLE[idx];
    encoderPrevState[slot.pin] = state;

    // Decay every tick regardless of movement, so acceleration falls back to
    // 0 shortly after the encoder slows down or stops.
    uint16_t accel = encoderAccel[slot.pin];
    encoderAccel[slot.pin] = (accel > ENC_ACCEL_DEC) ? (uint16_t)(accel - ENC_ACCEL_DEC) : 0;

    if (movement == 0)
      continue;

    int8_t accum = (int8_t)(encoderAccum[slot.pin] + movement);
    if (accum >= 4)
    {
      accum = 0;
      if (encoderPending[slot.pin] < 120)
        encoderPending[slot.pin]++;
      bumpAccel(slot.pin);
    }
    else if (accum <= -4)
    {
      accum = 0;
      if (encoderPending[slot.pin] > -120)
        encoderPending[slot.pin]--;
      bumpAccel(slot.pin);
    }
    encoderAccum[slot.pin] = accum;
  }
}

static void configurePinMode(uint8_t pin)
{
  const PinConfig &c = pinConfigs[pin];
  switch (c.role)
  {
  case ROLE_BUTTON_NOTE:
  case ROLE_BUTTON_CC:
  case ROLE_BUTTON_SHIFT:
  case ROLE_ENCODER_A:
  case ROLE_ENCODER_B:
    pinMode(pin, INPUT_PULLUP);
    break;
  case ROLE_LIGHT_DIGITAL:
  case ROLE_LIGHT_PWM:
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
    break;
  default:
    break; // ROLE_POT_CC needs no pinMode; ROLE_UNUSED left alone
  }
}

void rolesInit()
{
  memset(rawStable, 0, sizeof(rawStable));
  memset(lastRawLevel, 0, sizeof(lastRawLevel));
  memset(lastChangeMs, 0, sizeof(lastChangeMs));
  memset(toggleState, 0, sizeof(toggleState));
  memset(potLastShifted, 0, sizeof(potLastShifted));
  for (uint8_t i = 0; i < PIN_COUNT; i++)
  {
    potLastSent[i] = -1;
    potTracked[i] = -1;
  }

  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    configurePinMode(pin);
    uint8_t role = pinConfigs[pin].role;
    if (role == ROLE_BUTTON_NOTE || role == ROLE_BUTTON_CC || role == ROLE_BUTTON_SHIFT ||
        role == ROLE_ENCODER_A || role == ROLE_ENCODER_B)
    {
      rawStable[pin] = digitalRead(pin);
      lastRawLevel[pin] = rawStable[pin];
    }
  }

  // Rebuild the encoder ISR's pin table and per-encoder decode state
  // together, with the ISR paused throughout -- it reads all of this every
  // ~1ms and must never see it half-rebuilt.
  noInterrupts();
  TIMSK1 &= ~(1 << OCIE1C);
  for (uint8_t i = 0; i < PIN_COUNT; i++)
  {
    encoderAccum[i] = 0;
    encoderPrevState[i] = 0;
    encoderPending[i] = 0;
    encoderAccel[i] = 0;
  }
  encoderIsrCount = 0;
  for (uint8_t pin = 0; pin + 1 < PIN_COUNT; pin++)
  {
    if (pinConfigs[pin].role == ROLE_ENCODER_A && pinConfigs[pin + 1].role == ROLE_ENCODER_B)
    {
      encoderPrevState[pin] = (uint8_t)((rawStable[pin] << 1) | rawStable[pin + 1]);
      if (encoderIsrCount < MAX_ENCODERS)
      {
        EncoderIsrSlot &slot = encoderIsrSlots[encoderIsrCount++];
        slot.pin = pin;
        slot.portA = portInputRegister(digitalPinToPort(pin));
        slot.maskA = digitalPinToBitMask(pin);
        slot.portB = portInputRegister(digitalPinToPort(pin + 1));
        slot.maskB = digitalPinToBitMask(pin + 1);
      }
    }
  }
  OCR1C = 128;
  TIMSK1 |= (1 << OCIE1C);
  interrupts();

  // Sync any listen-mode lights to their button's actual current state right
  // away, rather than leaving them dark until the next physical edge.
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    uint8_t role = pinConfigs[pin].role;
    if (role != ROLE_BUTTON_NOTE && role != ROLE_BUTTON_CC && role != ROLE_BUTTON_SHIFT)
      continue;
    const PinConfig &c = pinConfigs[pin];
    // A toggle CC button's logical state is toggleState (just reset to 0
    // above, same as its CC output at boot), not the switch's raw position.
    bool on;
    if (role == ROLE_BUTTON_CC && (c.flags & FLAG_TOGGLE))
      on = toggleState[pin] != 0;
    else
      on = (c.flags & FLAG_INVERT) ? (rawStable[pin] == HIGH) : (rawStable[pin] == LOW);
    applyListenLights(pin, on);
  }
}

void rolesReconfigure()
{
  // A config write shouldn't flip every toggle button back to off: keep the
  // latched state of pins that are still toggle CC buttons (rolesInit()
  // clears it, and only toggle buttons ever set it, so pins that weren't
  // toggles before stay at 0).
  uint8_t kept[PIN_COUNT];
  memcpy(kept, toggleState, sizeof(kept));
  rolesInit();
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    const PinConfig &c = pinConfigs[pin];
    if (isPinReserved(pin) || c.role != ROLE_BUTTON_CC || !(c.flags & FLAG_TOGGLE))
      continue;
    toggleState[pin] = kept[pin];
    applyListenLights(pin, toggleState[pin] != 0);
  }
}

void rolesSendSnapshot()
{
  // Nothing here goes out on its own at boot or on connect, so without this
  // the app and the DAW only learn a switch's position from its next edge.
  // Note buttons are left out: replaying them would play notes.
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    const PinConfig &c = pinConfigs[pin];
    if (c.role == ROLE_BUTTON_CC)
    {
      uint8_t value;
      if (c.flags & FLAG_TOGGLE)
        value = toggleState[pin];
      else
      {
        bool pressed = (c.flags & FLAG_INVERT) ? (rawStable[pin] == HIGH) : (rawStable[pin] == LOW);
        value = pressed ? 127 : 0;
      }
      midiSendCC(c.channel, c.number, value);
    }
    else if (c.role == ROLE_POT_CC)
    {
      potLastSent[pin] = -1; // scanPots() resends it on the next pass
    }
  }
}

static void handleButtonEdge(uint8_t pin, uint8_t stableLevel)
{
  const PinConfig &c = pinConfigs[pin];
  // INPUT_PULLUP idle = HIGH, pressed pulls to LOW; FLAG_INVERT swaps that.
  bool pressed = (c.flags & FLAG_INVERT) ? (stableLevel == HIGH) : (stableLevel == LOW);

  if (c.role == ROLE_BUTTON_NOTE)
  {
    applyListenLights(pin, pressed);
    if (pressed)
      midiSendNoteOn(c.channel, c.number, 127);
    else
      midiSendNoteOff(c.channel, c.number, 0);
  }
  else if (c.role == ROLE_BUTTON_CC)
  {
    if (c.flags & FLAG_TOGGLE)
    {
      // Toggle mode only changes state on the press edge -- a listening
      // light must mirror that same latched value, not the raw (momentary)
      // press/release, or it'd go dark the instant the button is released.
      if (pressed)
      {
        toggleState[pin] = toggleState[pin] ? 0 : 127;
        midiSendCC(c.channel, c.number, toggleState[pin]);
        applyListenLights(pin, toggleState[pin] != 0);
      }
    }
    else
    {
      midiSendCC(c.channel, c.number, pressed ? 127 : 0);
      applyListenLights(pin, pressed);
    }
  }
}

static bool isShiftPinActive(uint8_t shiftPin)
{
  if (shiftPin >= PIN_COUNT)
    return false;
  if (pinConfigs[shiftPin].role != ROLE_BUTTON_SHIFT)
    return false;
  bool invert = pinConfigs[shiftPin].flags & FLAG_INVERT;
  // INPUT_PULLUP idle = HIGH, pressed pulls to LOW; FLAG_INVERT swaps that.
  return invert ? (rawStable[shiftPin] == HIGH) : (rawStable[shiftPin] == LOW);
}

// True debounce: a transition is only confirmed once the raw level has held
// steady for DEBOUNCE_MS, not just once it's merely *different* from the
// last confirmed level. The difference matters — a rate-limiter that
// accepts the first differing sample and only throttles the next one lets
// ordinary mechanical bounce register as a second real edge once the
// window elapses, which for a toggle-mode button can silently cancel a
// press out (flips twice, ends up back where it started). Returns true
// (and updates rawStable[pin]) exactly once per confirmed transition.
static bool debounce(uint8_t pin, uint16_t nowMs)
{
  uint8_t level = digitalRead(pin);
  if (level != lastRawLevel[pin])
  {
    lastRawLevel[pin] = level;
    lastChangeMs[pin] = nowMs;
    return false;
  }
  if (level != rawStable[pin] && (uint16_t)(nowMs - lastChangeMs[pin]) >= DEBOUNCE_MS)
  {
    rawStable[pin] = level;
    return true;
  }
  return false;
}

static void scanButtonsAndEncoders()
{
  uint16_t nowMs = (uint16_t)millis();
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    uint8_t role = pinConfigs[pin].role;

    if (role == ROLE_BUTTON_NOTE || role == ROLE_BUTTON_CC)
    {
      if (debounce(pin, nowMs))
        handleButtonEdge(pin, rawStable[pin]);
    }
    else if (role == ROLE_BUTTON_SHIFT)
    {
      // Pure modifier: track debounced state only, no MIDI of its own --
      // but a LIGHT_DIGITAL/LIGHT_PWM pin can still listen to it directly
      // via listenPin (see applyListenLights()).
      if (debounce(pin, nowMs))
        applyListenLights(pin, isShiftPinActive(pin));
    }
    else if (role == ROLE_ENCODER_A)
    {
      if (pin + 1 >= PIN_COUNT || pinConfigs[pin + 1].role != ROLE_ENCODER_B)
        continue;

      // Quadrature decoding happens in ISR(TIMER1_COMPC_vect) above, on a
      // fixed ~1ms tick independent of how long this loop() iteration
      // takes. Here we just drain whatever steps it's accumulated since we
      // last asked.
      noInterrupts();
      int8_t pending = encoderPending[pin];
      encoderPending[pin] = 0;
      uint16_t accel = encoderAccel[pin];
      interrupts();
      if (pending == 0)
        continue;

      const PinConfig &c = pinConfigs[pin];
      bool shifted = isShiftPinActive(c.inMin);
      uint8_t sendChannel = shifted ? c.outMin : c.channel;
      uint8_t sendNumber = shifted ? c.outMax : c.number;

      // Step magnitude: 1 unless FLAG_ACCEL_ENABLE is set, in which case it
      // scales with how fast the encoder is being turned (see encoderAccel /
      // ENC_ACCEL_* above). Sent as a binary-offset relative CC, the same
      // encoding Novation controllers use: 64 = no movement, 65..127 = CW
      // by (value - 64) steps, 1..63 = CCW by (64 - value) steps. Capped at
      // 63 so it always stays within 1..127.
      uint16_t multiplier = (c.flags & FLAG_ACCEL_ENABLE) ? (accel >> 8) : 0;
      uint8_t magnitude = (uint8_t)(1 + multiplier);
      if (magnitude > 63)
        magnitude = 63;
      uint8_t ccValue = pending > 0 ? (uint8_t)(64 + magnitude) : (uint8_t)(64 - magnitude);

      uint8_t steps = pending > 0 ? (uint8_t)pending : (uint8_t)(-pending);
      for (uint8_t i = 0; i < steps; i++)
        midiSendCC(sendChannel, sendNumber, ccValue);
    }
  }
}

static void scanPots()
{
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    if (pinConfigs[pin].role != ROLE_POT_CC)
      continue;

    const PinConfig &c = pinConfigs[pin];
    int raw = analogRead(pin); // pins 54-69 == A0-A15 in the Mega core
    uint16_t inMin = (uint16_t)c.inMin << 2;
    uint16_t inMax = (uint16_t)c.inMax << 2;
    if (inMax <= inMin)
    {
      inMin = 0;
      inMax = 1023;
    }

    // Past either end of the range, snap straight to the reading so the
    // threshold can never hold the output a step short of its min/max.
    int16_t tracked = potTracked[pin];
    if (tracked < 0 || raw <= (int)inMin || raw >= (int)inMax ||
        raw > tracked + POT_HYSTERESIS || raw < tracked - POT_HYSTERESIS)
    {
      tracked = (int16_t)raw;
      potTracked[pin] = tracked;
    }

    long mapped = map(tracked, inMin, inMax, c.outMin, c.outMax);
    uint8_t lo = c.outMin < c.outMax ? c.outMin : c.outMax;
    uint8_t hi = c.outMin < c.outMax ? c.outMax : c.outMin;
    mapped = constrain(mapped, (long)lo, (long)hi);
    if (c.flags & FLAG_INVERT)
      mapped = (long)c.outMax + c.outMin - mapped;

    bool shifted = isShiftPinActive(c.shiftPin);
    uint8_t sendChannel = shifted ? c.shiftChannel : c.channel;
    uint8_t sendNumber = shifted ? c.shiftNumber : c.number;

    // Re-send on a shift-state change even if the raw value hasn't moved --
    // otherwise the destination CC that shifting just switched to/from would
    // sit stale (never told the pot's current value) until the pot is
    // physically nudged. potLastShifted persists this alongside the value
    // dedup below.
    if (potLastSent[pin] != mapped || potLastShifted[pin] != shifted)
    {
      potLastSent[pin] = (int16_t)mapped;
      potLastShifted[pin] = shifted;
      midiSendCC(sendChannel, sendNumber, (uint8_t)mapped);
    }
  }
}

void rolesScanInputs()
{
  scanButtonsAndEncoders();
  scanPots();
}

// ---- pin test (config mode only) --------------------------------------------
// The app's Test tab drives pins straight HIGH/LOW regardless of their role.
// Every touched pin is remembered so rolesEndPinTest() can hand it back to
// its configured role on the way out of config mode -- an UNUSED pin would
// otherwise be left driving an output indefinitely.

static uint8_t testedPins[(PIN_COUNT + 7) / 8];

static void restoreTestedPin(uint8_t pin)
{
  pinMode(pin, INPUT);
  configurePinMode(pin);
}

bool rolesTestPin(uint8_t pin, uint8_t level)
{
  if (pin >= PIN_COUNT || isPinReserved(pin) || level > PIN_TEST_INPUT)
    return false;
  if (level == PIN_TEST_RELEASE)
  {
    restoreTestedPin(pin);
    return true;
  }
  testedPins[pin / 8] |= (uint8_t)(1 << (pin % 8));
  if (level == PIN_TEST_INPUT)
  {
    pinMode(pin, INPUT_PULLUP);
    return true;
  }
  pinMode(pin, OUTPUT);
  digitalWrite(pin, level == PIN_TEST_HIGH ? HIGH : LOW);
  return true;
}

void rolesReadPins(uint8_t *out)
{
  memset(out, 0, PIN_STATE_DIGITAL_BYTES);
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    // Straight from the port register rather than digitalRead(), which
    // would switch off PWM on any timer pin it touches.
    if (*portInputRegister(digitalPinToPort(pin)) & digitalPinToBitMask(pin))
      out[pin / 8] |= (uint8_t)(1 << (pin % 8));
  }
  for (uint8_t i = 0; i < PIN_STATE_ANALOG_COUNT; i++)
  {
    uint16_t v = (uint16_t)analogRead(A0 + i);
    out[PIN_STATE_DIGITAL_BYTES + i * 2] = (uint8_t)(v & 0xFF);
    out[PIN_STATE_DIGITAL_BYTES + i * 2 + 1] = (uint8_t)(v >> 8);
  }
}

void rolesEndPinTest()
{
  bool any = false;
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (!(testedPins[pin / 8] & (1 << (pin % 8))))
      continue;
    restoreTestedPin(pin);
    any = true;
  }
  if (!any)
    return;
  memset(testedPins, 0, sizeof(testedPins));
  // Driven encoder/button pins fed the encoder ISR and debouncer junk while
  // under test; a full re-init resyncs all of that from the real pin levels.
  rolesInit();
}

static void applyToOutputPin(uint8_t pin, uint16_t value, uint16_t inMin, uint16_t inMax)
{
  const PinConfig &c = pinConfigs[pin];
  if (inMax <= inMin)
  {
    inMin = 0;
    inMax = 127;
  }

  if (c.role == ROLE_LIGHT_DIGITAL)
  {
    bool on = value > inMin;
    if (c.flags & FLAG_INVERT)
      on = !on;
    digitalWrite(pin, on ? HIGH : LOW);
  }
  else if (c.role == ROLE_LIGHT_PWM)
  {
    long mapped = map((long)value, (long)inMin, (long)inMax, c.outMin, c.outMax);
    uint8_t lo = c.outMin < c.outMax ? c.outMin : c.outMax;
    uint8_t hi = c.outMin < c.outMax ? c.outMax : c.outMin;
    mapped = constrain(mapped, (long)lo, (long)hi);
    if (c.flags & FLAG_INVERT)
      mapped = (long)c.outMax + c.outMin - mapped;
    analogWrite(pin, (uint8_t)mapped); // caller must have assigned this role only to a PWM-capable pin
  }
}

// Drives every LIGHT_DIGITAL/LIGHT_PWM pin whose listenPin targets sourcePin
// (a button/shift pin that just changed state), bypassing MIDI entirely.
// Reuses applyToOutputPin so a listen-mode light gets the same in/out
// mapping (dimming, invert, etc.) a CC-matched one would -- just fed
// 0/127 directly instead of waiting for a matching Note/CC to arrive.
static void applyListenLights(uint8_t sourcePin, bool pressed)
{
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    const PinConfig &c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM)
      continue;
    if (c.listenPin != sourcePin)
      continue;
    applyToOutputPin(pin, pressed ? 127 : 0, c.inMin, c.inMax);
  }
}

void rolesHandleIncomingNote(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn)
{
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    const PinConfig &c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM)
      continue;
    if (c.listenPin != NO_SHIFT_PIN)
      continue; // direct-listen mode: driven by applyListenLights(), not incoming MIDI
    if (c.flags & FLAG_MATCH_CC)
      continue; // this pin is set to match CC, not Note
    if (c.channel != channel || c.number != note)
      continue;
    applyToOutputPin(pin, isOn ? velocity : 0, c.inMin, c.inMax);
  }
}

void rolesHandleIncomingCC(uint8_t channel, uint8_t controller, uint8_t value)
{
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    const PinConfig &c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM)
      continue;
    if (c.listenPin != NO_SHIFT_PIN)
      continue; // direct-listen mode: driven by applyListenLights(), not incoming MIDI
    if (!(c.flags & FLAG_MATCH_CC))
      continue; // this pin is set to match Note, not CC
    if (c.channel != channel || c.number != controller)
      continue;
    applyToOutputPin(pin, value, c.inMin, c.inMax);
  }
}
