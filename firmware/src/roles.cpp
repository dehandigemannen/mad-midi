#include "roles.h"
#include "pin_config.h"
#include "midi_io.h"
#include <string.h>
#include <avr/interrupt.h>

static const unsigned long DEBOUNCE_MS = 10;

static uint8_t rawStable[PIN_COUNT];
static uint8_t lastRawLevel[PIN_COUNT];
static uint16_t lastChangeMs[PIN_COUNT];
static uint8_t toggleState[PIN_COUNT];
static int16_t potLastSent[PIN_COUNT];
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
  for (uint8_t i = 0; i < PIN_COUNT; i++)
    potLastSent[i] = -1;

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
}

void rolesReconfigure()
{
  rolesInit();
}

static void handleButtonEdge(uint8_t pin, uint8_t stableLevel)
{
  const PinConfig &c = pinConfigs[pin];
  // INPUT_PULLUP idle = HIGH, pressed pulls to LOW; FLAG_INVERT swaps that.
  bool pressed = (c.flags & FLAG_INVERT) ? (stableLevel == HIGH) : (stableLevel == LOW);

  if (c.role == ROLE_BUTTON_NOTE)
  {
    if (pressed)
      midiSendNoteOn(c.channel, c.number, 127);
    else
      midiSendNoteOff(c.channel, c.number, 0);
  }
  else if (c.role == ROLE_BUTTON_CC)
  {
    if (c.flags & FLAG_TOGGLE)
    {
      if (pressed)
      {
        toggleState[pin] = toggleState[pin] ? 0 : 127;
        midiSendCC(c.channel, c.number, toggleState[pin]);
      }
    }
    else
    {
      midiSendCC(c.channel, c.number, pressed ? 127 : 0);
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
      // Pure modifier: track debounced state only, no MIDI of its own.
      debounce(pin, nowMs);
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
      // ENC_ACCEL_* above). Capped well under 63 so it always stays a valid
      // positive two's-complement relative CC value.
      uint16_t multiplier = (c.flags & FLAG_ACCEL_ENABLE) ? (accel >> 8) : 0;
      uint8_t magnitude = (uint8_t)(1 + multiplier);
      if (magnitude > 63)
        magnitude = 63;
      uint8_t ccValue = pending > 0 ? magnitude : (uint8_t)(128 - magnitude);

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

    long mapped = map(raw, inMin, inMax, c.outMin, c.outMax);
    uint8_t lo = c.outMin < c.outMax ? c.outMin : c.outMax;
    uint8_t hi = c.outMin < c.outMax ? c.outMax : c.outMin;
    mapped = constrain(mapped, (long)lo, (long)hi);
    if (c.flags & FLAG_INVERT)
      mapped = (long)c.outMax + c.outMin - mapped;

    if (potLastSent[pin] != mapped)
    {
      potLastSent[pin] = (int16_t)mapped;
      midiSendCC(c.channel, c.number, (uint8_t)mapped);
    }
  }
}

void rolesScanInputs()
{
  scanButtonsAndEncoders();
  scanPots();
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

void rolesHandleIncomingNote(uint8_t channel, uint8_t note, uint8_t velocity, bool isOn)
{
  for (uint8_t pin = 0; pin < PIN_COUNT; pin++)
  {
    if (isPinReserved(pin))
      continue;
    const PinConfig &c = pinConfigs[pin];
    if (c.role != ROLE_LIGHT_DIGITAL && c.role != ROLE_LIGHT_PWM)
      continue;
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
    if (!(c.flags & FLAG_MATCH_CC))
      continue; // this pin is set to match Note, not CC
    if (c.channel != channel || c.number != controller)
      continue;
    applyToOutputPin(pin, value, c.inMin, c.inMax);
  }
}
