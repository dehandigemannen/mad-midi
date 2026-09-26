"use strict";

// ---- pywebview api helper -----------------------------------------------

function api(name, ...args) {
  if (!window.pywebview || !window.pywebview.api) {
    return Promise.reject(new Error("pywebview api not ready"));
  }
  return window.pywebview.api[name](...args);
}

// ---- role metadata --------------------------------------------------------

const PWM_PINS = new Set([2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 44, 45, 46]);
const RESERVED_PINS = new Set([0, 1, 13]);
const PIN_COUNT = 70;
const ALL_DEVICES_ID = "__all__";

const ROLE_LABELS = {
  unused: "Unused",
  button_note: "Button → Note",
  button_cc: "Button → CC",
  light_digital: "Light (on/off)",
  light_pwm: "Light/VU (PWM)",
  encoder_a: "Encoder (2-pin)",
  encoder_b: "Encoder B (paired)",
  pot_cc: "Pot/Fader → CC",
  button_shift: "Shift button",
};

const ROLE_FIELDS = {
  unused: [],
  button_note: ["channel", "number", "invert"],
  button_cc: ["channel", "number", "invert", "toggle"],
  light_digital: ["channel", "number", "in_min", "in_max", "invert"],
  light_pwm: [
    "channel",
    "number",
    "in_min",
    "in_max",
    "out_min",
    "out_max",
    "invert",
  ],
  encoder_a: ["channel", "number", "accel"], // shift fields are rendered separately, see renderEditor
  encoder_b: [],
  pot_cc: [
    "channel",
    "number",
    "in_min",
    "in_max",
    "out_min",
    "out_max",
    "invert",
  ],
  button_shift: ["invert"],
};

const NO_SHIFT_PIN = 255;

// ---- sequencer: default CC-number offsets per step function ---------------
// Distinct, non-overlapping ranges (8 slots each) so up to 8 steps' internal
// signals never collide with each other. Just defaults — since these are
// ordinary PinConfig fields, they can still be hand-edited in Table/Board.
const SEQ_BASE = {
  button: 0,
  encoder: 8,
  encoder_shifted: 16,
  pwm: 24,
  step_light: 32,
  enabled_light: 40,
};
const SEQ_MAX_STEPS = 8;
const SEQ_FN_LABELS = {
  button: "Button",
  encoder: "Encoder",
  shift: "Shift",
  pwm: "PWM (value)",
  step_light: "Step light",
  enabled_light: "Enabled light",
};
// Functions that need a genuinely PWM-capable pin, vs. functions that
// should steer clear of them so those pins stay free for PWM use.
const SEQ_FNS_NEEDING_PWM = new Set(["pwm"]);

const FIELD_META = {
  channel: {
    label: "MIDI Channel",
    type: "number",
    min: 1,
    max: 16,
    offset: -1,
  },
  in_min: { label: "In min", type: "number", min: 0, max: 255 },
  in_max: { label: "In max", type: "number", min: 0, max: 255 },
  out_min: { label: "Out min", type: "number", min: 0, max: 255 },
  out_max: { label: "Out max", type: "number", min: 0, max: 255 },
  invert: { label: "Invert", type: "checkbox" },
  toggle: { label: "Toggle (vs momentary)", type: "checkbox" },
  accel: { label: "Acceleration (faster turns = bigger steps)", type: "checkbox" },
};

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

function noteNumberToName(n) {
  const octave = Math.floor(n / 12) - 1; // MIDI note 60 = C4 (middle C)
  return `${NOTE_NAMES[n % 12]}${octave}`;
}

function buildNoteSelect(value, onChange) {
  const select = document.createElement("select");
  for (let n = 0; n <= 127; n++) {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = `${noteNumberToName(n)} (${n})`;
    if (n === value) opt.selected = true;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => onChange(parseInt(select.value, 10)));
  return select;
}

// Whether a pin's "number" field should currently be shown/entered as a
// note name rather than a plain CC number.
function usesNoteName(p) {
  if (p.role === "button_note") return true;
  if (p.role === "light_digital" || p.role === "light_pwm") return !p.match_cc;
  return false;
}

function displayNumber(p) {
  if (!ROLE_FIELDS[p.role].includes("number")) return ""; // e.g. button_shift sends no MIDI at all
  return usesNoteName(p) ? `${noteNumberToName(p.number)} (${p.number})` : String(p.number);
}

function isAnalogPin(pin) {
  return pin >= 54 && pin < PIN_COUNT;
}

function allowedRoles(pin) {
  if (RESERVED_PINS.has(pin)) return [];
  // A0-A15 are ordinary digital I/O pins that also happen to have ADC —
  // digitalWrite works on them the same as any other pin, so a plain on/off
  // light is a legitimate use (e.g. a sequencer step light), just not PWM
  // (the Mega has no PWM hardware on these pins).
  if (isAnalogPin(pin)) return ["unused", "pot_cc", "light_digital"];
  const roles = [
    "unused",
    "button_note",
    "button_cc",
    "button_shift",
    "light_digital",
  ];
  if (PWM_PINS.has(pin)) roles.push("light_pwm");
  if (pin < 53) roles.push("encoder_a");
  return roles;
}

function pinLabel(pin) {
  return isAnalogPin(pin) ? `A${pin - 54}` : `D${pin}`;
}

// ---- app state -----------------------------------------------------------

const state = {
  entries: [], // sidebar entries from list_entries(): {id, entry_type, nickname, device_id, connected, in_sync}
  selectedId: null, // an entry id, or ALL_DEVICES_ID, or null before first load
  pins: [], // array of PIN_COUNT pin config objects for the selected entry
  selectedPin: null,
  view: "table",
  monitorRows: 0,
  addDevicePorts: null, // non-null while the "+ Device" port picker is open
  reconnectingId: null, // entry id whose inline "connect to port" picker is open
  reconnectPorts: [],
  sequencer: null, // {channel, subdivision, steps: [...]} for the selected entry
  sequencerLive: null, // {device_id, running, current_step, step_enabled, bpm} pushed from a connected device, or null
  sequencerLiveAt: 0, // Date.now() of the last sequencerLive push, to spot the clock going quiet
  drone: null, // {enabled, channel, control_type, segments: [...]} for the selected entry
  droneLive: null, // {device_id, enabled: [...]} pushed from a connected device, or null
  fxRack: null, // {enabled, channel, segments: [...]} for the selected entry
  fxRackLive: null, // {device_id, enabled: [...]} pushed from a connected device, or null
  joystick: null, // {enabled, x_pin, y_pin, z_pin, button_pin} for the selected entry
  pinTest: blankPinTest(), // Test tab session, see syncPinTest()
  flashingId: null, // entry id currently mid flash_firmware() call, or null -- keeps the button disabled/labeled through the operation's own disconnect/reconnect
};

let saveTimer = null;

function pinByIndex(i) {
  return state.pins[i];
}

function selectedEntry() {
  return state.entries.find((e) => e.id === state.selectedId) || null;
}

// ---- role assignment (handles encoder pairing) ---------------------------

function setPinRole(pin, role) {
  const p = pinByIndex(pin);
  const prevRole = p.role;

  if (prevRole === "encoder_a" && role !== "encoder_a" && pin + 1 < PIN_COUNT) {
    const partner = pinByIndex(pin + 1);
    if (partner.role === "encoder_b") partner.role = "unused";
  }

  p.role = role;

  if (role === "encoder_a") {
    if (!p.in_min) p.in_min = NO_SHIFT_PIN; // sentinel: no shift button assigned yet
    if (pin + 1 < PIN_COUNT && !RESERVED_PINS.has(pin + 1)) {
      const partner = pinByIndex(pin + 1);
      partner.role = "encoder_b";
      partner.channel = p.channel;
      partner.number = p.number;
    }
  }

  scheduleAutoSave();
  renderAll();
}

function setPinField(pin, field, value) {
  const p = pinByIndex(pin);
  p[field] = value;
  if (
    p.role === "encoder_a" &&
    pin + 1 < PIN_COUNT &&
    (field === "channel" || field === "number")
  ) {
    pinByIndex(pin + 1)[field] = value;
  }
  scheduleAutoSave();
}

// ---- auto-save -------------------------------------------------------------

function scheduleAutoSave() {
  // Saves pins and every feature config together in one call — independent
  // debounced saves could race each other into corrupting the file when
  // they fire ~400ms apart (see update_entry_pins).
  if (!state.selectedId || state.selectedId === ALL_DEVICES_ID) return;
  const id = state.selectedId;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const pinsSnapshot = state.pins.map((p) => ({ ...p }));
    const sequencerSnapshot = JSON.parse(JSON.stringify(state.sequencer));
    const droneSnapshot = JSON.parse(JSON.stringify(state.drone));
    const fxRackSnapshot = JSON.parse(JSON.stringify(state.fxRack));
    const joystickSnapshot = JSON.parse(JSON.stringify(state.joystick));
    const status = await api(
      "update_entry_pins",
      id,
      pinsSnapshot,
      sequencerSnapshot,
      droneSnapshot,
      fxRackSnapshot,
      joystickSnapshot,
    );
    applyEntryStatus(status);
  }, 400);
}

function applyEntryStatus(status) {
  if (!status) return;
  const entry = state.entries.find((e) => e.id === status.id);
  if (!entry) return;
  if ("connected" in status) entry.connected = status.connected;
  if ("in_sync" in status) entry.in_sync = status.in_sync;
  renderSidebar();
  renderHeader();
}

// ---- value mirror (encoder/pot -> PWM VU meter), generalized --------------
// Any encoder_a/pot_cc pin can mirror its live value onto a light_pwm pin's
// brightness via PinConfig.mirror_pin -- not just a sequencer step's or a
// drone segment's. See app/value_mirror.py for the engine that drives this
// app-side (the firmware has no idea it's happening).

function linkMirror(controlPin, pwmPin) {
  if (controlPin === null || controlPin === undefined) return;
  pinByIndex(controlPin).mirror_pin = pwmPin;
}

function unlinkMirror(controlPin) {
  if (controlPin === null || controlPin === undefined) return;
  pinByIndex(controlPin).mirror_pin = NO_SHIFT_PIN;
}

// ---- shared pin release (sequencer step / drone / fx segment slots) -------
// Releases a pin previously used by any composite feature back to "unused",
// undoing any encoder-pairing or cross-pin references (shift-group, listen,
// mirror) it was part of.
function clearAssignedPin(pin) {
  const p = pinByIndex(pin);
  if (p.role === "encoder_a" && pin + 1 < PIN_COUNT) {
    const partner = pinByIndex(pin + 1);
    if (partner.role === "encoder_b") partner.role = "unused";
  }
  if (p.role === "button_shift") {
    state.pins.forEach((pp) => {
      if (pp.role === "encoder_a" && pp.in_min === pin) pp.in_min = NO_SHIFT_PIN;
      if (pp.role === "pot_cc" && pp.shift_pin === pin) pp.shift_pin = NO_SHIFT_PIN;
    });
  }
  // if this pin was a mirror target (a PWM meter) or a listen target (an
  // on/shift light), unlink whichever pin pointed at it
  state.pins.forEach((pp) => {
    if (pp.mirror_pin === pin) pp.mirror_pin = NO_SHIFT_PIN;
    if (pp.listen_pin === pin) pp.listen_pin = NO_SHIFT_PIN;
  });
  p.role = "unused";
}

// Wires a light pin to directly mirror a button/shift pin's physical state
// (firmware's listen_pin mechanism, roles.cpp's applyListenLights()) instead
// of matching an incoming Note/CC -- used for a segment's on/shift indicator
// lights (Drone, FX Rack), which need no MIDI channel/number at all.
function assignListenLight(pin, sourcePin) {
  const p = pinByIndex(pin);
  p.role = PWM_PINS.has(pin) ? "light_pwm" : "light_digital";
  if (p.role === "light_pwm") {
    p.in_min = 0;
    p.in_max = 127;
    p.out_min = 0;
    p.out_max = 255;
  }
  p.listen_pin = sourcePin === null || sourcePin === undefined ? NO_SHIFT_PIN : sourcePin;
}

// Shared by any feature with an encoder/pot value control plus an optional
// shift button (Sequencer steps, Drone segments, FX Rack's two encoders):
// makes the link (set automatically whichever order the two are assigned
// in) visible, since otherwise there's no way to tell that turning the
// control while holding shift actually sends a different CC.
function buildShiftLinkHint(obj, controlField, shiftField, controlType) {
  const hint = document.createElement("div");
  hint.className = "field-hint sequencer-link-hint";
  const hasControl = obj[controlField] !== null && obj[controlField] !== undefined;
  const hasShift = obj[shiftField] !== null && obj[shiftField] !== undefined;
  const noun = controlType === "pot" ? "pot" : "encoder";

  if (hasControl && hasShift) {
    const ctrl = pinByIndex(obj[controlField]);
    const shiftedCc = ctrl.role === "pot_cc" ? ctrl.shift_number : ctrl.out_max;
    hint.textContent = `✓ Shift linked — plain turn sends CC ${ctrl.number}, shift+turn sends CC ${shiftedCc} (both on channel ${ctrl.channel + 1})`;
  } else if (hasControl && !hasShift) {
    hint.textContent = `No shift assigned — this ${noun} always sends its plain CC.`;
  } else if (!hasControl && hasShift) {
    hint.textContent = `This shift button isn't linked to anything yet — assign this ${noun} to link it.`;
  } else {
    hint.hidden = true;
  }
  return hint;
}

// Shared by any feature where each item (a Sequencer step, a Drone segment)
// independently chooses whether its own value control is an encoder or a
// pot -- switching doesn't move that item's already-assigned pin (its
// actual role may now mismatch), so a hint below the picker says so.
function buildControlTypeSelect(currentType, onChange) {
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = "Value control";
  const select = document.createElement("select");
  [
    ["encoder", "Encoder (2-pin, relative)"],
    ["pot", "Pot/Fader (analog pin, absolute)"],
  ].forEach(([value, text]) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    if (value === (currentType || "encoder")) opt.selected = true;
    select.appendChild(opt);
  });
  select.addEventListener("change", () => onChange(select.value));
  wrap.appendChild(label);
  wrap.appendChild(select);
  return wrap;
}

// ---- sequencer: pin assignment (mirrors setPinRole's cleanup-then-assign) --

function blankSequencerStep(index) {
  return {
    note: 60 + index, // auto-assigned, still freely editable via the Note dropdown
    control_type: "encoder", // "encoder" | "pot" -- this step's own choice, independent of other steps
    button_pin: null,
    encoder_pin: null,
    shift_pin: null,
    pwm_pin: null,
    step_light_pin: null,
    enabled_light_pin: null,
  };
}

// Pins currently eligible for a given step's function: unused, or already
// holding that exact slot. Encoder additionally needs a free pin+1 (its
// encoder_b partner), same constraint setPinRole already applies elsewhere.
// Only the "pwm" function is pin-restricted — it needs actual PWM hardware,
// so it's confined to PWM_PINS. Every other function is a plain digital
// input and works fine on a PWM-capable pin (matches allowedRoles() for the
// main Table/Board view, which doesn't reserve PWM pins away from buttons
// either) — so a button/encoder/shift/step_light/enabled_light can freely
// take one. enabled_light additionally accepts analog pins (like
// step_light) since a plain on/off lamp works fine there too — see
// assignSequencerPin, which picks light_pwm vs light_digital based on
// whichever pin actually gets chosen.
function sequencerEligiblePins(stepIndex, fn) {
  const step = state.sequencer.steps[stepIndex];
  const currentlyAssigned = step[fn + "_pin"];
  const needsPwm = SEQ_FNS_NEEDING_PWM.has(fn);
  const isPotControl = fn === "encoder" && step.control_type === "pot";

  // Pot mode needs a single analog pin (A0-A15), not a digital pin pair --
  // a completely different eligibility rule from the encoder's, so it's
  // handled separately rather than folded into the loop below.
  if (isPotControl) {
    const result = [];
    for (let pin = 54; pin < PIN_COUNT; pin++) {
      if (pin === currentlyAssigned) {
        result.push(pin);
        continue;
      }
      if (pinByIndex(pin).role !== "unused") continue;
      result.push(pin);
    }
    return result;
  }

  // step_light/enabled_light can also live on an analog pin (plain
  // digitalWrite works there); every other function needs a digital pin
  // (button/shift/encoder for input, pwm for actual PWM hardware, which
  // analog pins don't have).
  const maxPin =
    fn === "encoder" ? 53 : fn === "step_light" || fn === "enabled_light" ? PIN_COUNT : 54;
  const result = [];
  for (let pin = 0; pin < maxPin; pin++) {
    if (RESERVED_PINS.has(pin)) continue;
    if (pin === currentlyAssigned) {
      result.push(pin);
      continue;
    }
    if (needsPwm && !PWM_PINS.has(pin)) continue;
    if (pinByIndex(pin).role !== "unused") continue;
    if (fn === "encoder") {
      if (pin + 1 >= PIN_COUNT || RESERVED_PINS.has(pin + 1)) continue;
      if (pinByIndex(pin + 1).role !== "unused") continue;
    }
    result.push(pin);
  }
  return result;
}

function assignSequencerPin(stepIndex, fn, pin) {
  const step = state.sequencer.steps[stepIndex];
  const field = fn + "_pin";
  const prevPin = step[field];
  if (prevPin !== null && prevPin !== undefined && prevPin !== pin) {
    clearAssignedPin(prevPin);
  }
  step[field] = pin === null || pin === undefined ? null : pin;

  if (pin !== null && pin !== undefined) {
    const p = pinByIndex(pin);
    const ch = state.sequencer.channel;
    if (fn === "button") {
      p.role = "button_cc";
      // A latching switch, not momentary — level mode (toggle=false) sends
      // the switch's actual current position on every edge, staying in
      // sync even across a reconnect; toggle=true only advances on a fresh
      // press edge and would drift from the switch's real position.
      p.toggle = false;
      p.channel = ch;
      p.number = SEQ_BASE.button + stepIndex;
    } else if (fn === "encoder") {
      if (step.control_type === "pot") {
        p.role = "pot_cc";
        p.channel = ch;
        p.number = SEQ_BASE.encoder + stepIndex;
        p.in_min = 0;
        p.in_max = 255;
        p.out_min = 0;
        p.out_max = 127;
        p.shift_pin = NO_SHIFT_PIN;
        if (step.shift_pin !== null && step.shift_pin !== undefined) {
          p.shift_pin = step.shift_pin;
          p.shift_channel = ch;
          p.shift_number = SEQ_BASE.encoder_shifted + stepIndex;
        }
      } else {
        p.role = "encoder_a";
        p.channel = ch;
        p.number = SEQ_BASE.encoder + stepIndex;
        p.in_min = NO_SHIFT_PIN;
        if (pin + 1 < PIN_COUNT && !RESERVED_PINS.has(pin + 1)) {
          const partner = pinByIndex(pin + 1);
          partner.role = "encoder_b";
          partner.channel = ch;
          partner.number = p.number;
        }
        if (step.shift_pin !== null && step.shift_pin !== undefined) {
          p.in_min = step.shift_pin;
          p.out_min = ch;
          p.out_max = SEQ_BASE.encoder_shifted + stepIndex;
        }
      }
      p.mirror_pin = NO_SHIFT_PIN;
      if (step.pwm_pin !== null && step.pwm_pin !== undefined) {
        linkMirror(pin, step.pwm_pin);
      }
    } else if (fn === "shift") {
      p.role = "button_shift";
      if (step.encoder_pin !== null && step.encoder_pin !== undefined) {
        const enc = pinByIndex(step.encoder_pin);
        if (step.control_type === "pot") {
          enc.shift_pin = pin;
          enc.shift_channel = ch;
          enc.shift_number = SEQ_BASE.encoder_shifted + stepIndex;
        } else {
          enc.in_min = pin;
          enc.out_min = ch;
          enc.out_max = SEQ_BASE.encoder_shifted + stepIndex;
        }
      }
    } else if (fn === "step_light") {
      p.role = "light_digital";
      p.match_cc = true;
      p.channel = ch;
      p.number = SEQ_BASE.step_light + stepIndex;
    } else if (fn === "pwm") {
      // Value meter: mirrors the encoder's live CC output as PWM brightness
      // (see linkMirror -- generalized value_mirror.py, not sequencer-only).
      p.role = "light_pwm";
      p.match_cc = true;
      p.channel = ch;
      p.number = SEQ_BASE.pwm + stepIndex;
      p.in_min = 0;
      p.in_max = 127;
      p.out_min = 0;
      p.out_max = 255;
      if (step.encoder_pin !== null && step.encoder_pin !== undefined) {
        linkMirror(step.encoder_pin, pin);
      }
    } else if (fn === "enabled_light") {
      // Tracks the step's on/off (enabled) state, not trigger flashes.
      // Any light-capable pin is allowed: a PWM-capable pin gets driven at
      // full analog brightness, everything else (including analog A0-A15
      // pins) gets a plain on/off digital lamp.
      p.match_cc = true;
      p.channel = ch;
      p.number = SEQ_BASE.enabled_light + stepIndex;
      if (PWM_PINS.has(pin)) {
        p.role = "light_pwm";
        p.in_min = 0;
        p.in_max = 127;
        p.out_min = 0;
        p.out_max = 255;
      } else {
        p.role = "light_digital";
      }
    }
  }

  scheduleAutoSave();
  renderAll();
}

function setStepCount(n) {
  n = Math.max(1, Math.min(SEQ_MAX_STEPS, n || 1));
  const steps = state.sequencer.steps;
  if (n < steps.length) {
    for (let i = n; i < steps.length; i++) {
      Object.keys(SEQ_FN_LABELS).forEach((fn) => {
        const pin = steps[i][fn + "_pin"];
        if (pin !== null && pin !== undefined) clearAssignedPin(pin);
      });
    }
    steps.length = n;
  } else {
    while (steps.length < n) steps.push(blankSequencerStep(steps.length));
  }
  scheduleAutoSave();
  renderAll();
}

function setSequencerChannel(value) {
  state.sequencer.channel = value;
  // keep every already-assigned sequencer pin's channel (and an active
  // shift group's shifted channel) in sync with the new setting
  state.sequencer.steps.forEach((step) => {
    ["button", "encoder", "pwm", "step_light", "enabled_light"].forEach((fn) => {
      const pin = step[fn + "_pin"];
      if (pin !== null && pin !== undefined) pinByIndex(pin).channel = value;
    });
    if (step.encoder_pin !== null && step.encoder_pin !== undefined) {
      const enc = pinByIndex(step.encoder_pin);
      if (enc.role === "pot_cc") {
        if (enc.shift_pin !== NO_SHIFT_PIN) enc.shift_channel = value;
      } else {
        if (enc.in_min !== NO_SHIFT_PIN) enc.out_min = value;
        if (step.encoder_pin + 1 < PIN_COUNT) pinByIndex(step.encoder_pin + 1).channel = value;
      }
    }
  });
  scheduleAutoSave();
  renderAll();
}

function setSequencerSubdivision(value) {
  state.sequencer.subdivision = value;
  scheduleAutoSave();
}

function setStepControlType(stepIndex, value) {
  // Switching doesn't move the step's already-assigned pin (its actual role
  // may now mismatch the new choice) -- reassign it below after switching,
  // same as the old device-wide toggle warned.
  state.sequencer.steps[stepIndex].control_type = value;
  scheduleAutoSave();
  renderAll();
}

function setStepNote(stepIndex, note) {
  state.sequencer.steps[stepIndex].note = note;
  scheduleAutoSave();
}

async function toggleStepEnabledUI(stepIndex) {
  await api("toggle_step_enabled", state.selectedId, stepIndex);
}

// ---- rendering: table view -------------------------------------------------

function renderTable() {
  const tbody = document.getElementById("pinTableBody");
  tbody.innerHTML = "";

  const groupRow = (label) => {
    const tr = document.createElement("tr");
    tr.className = "group-header";
    const td = document.createElement("td");
    td.colSpan = 11;
    td.textContent = label;
    tr.appendChild(td);
    tbody.appendChild(tr);
  };

  groupRow("Digital pins (0–53)");
  for (let i = 0; i < 54; i++) tbody.appendChild(buildTableRow(i));
  groupRow("Analog pins (A0–A15)");
  for (let i = 54; i < PIN_COUNT; i++) tbody.appendChild(buildTableRow(i));
}

function buildTableRow(pin) {
  const p = pinByIndex(pin);
  const tr = document.createElement("tr");
  tr.dataset.pin = pin;
  if (RESERVED_PINS.has(pin)) tr.classList.add("reserved");
  if (p.role === "encoder_b") tr.classList.add("encoder-b");
  if (state.selectedPin === pin) tr.classList.add("selected");

  const cell = (text) => {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  };

  tr.appendChild(
    (() => {
      const td = cell("");
      const span = document.createElement("span");
      span.className = "pin-badge";
      span.textContent = pinLabel(pin);
      td.appendChild(span);
      if (PWM_PINS.has(pin)) {
        const pwm = document.createElement("span");
        pwm.className = "pwm-mark";
        pwm.textContent = "~";
        pwm.title = "PWM-capable pin";
        td.appendChild(pwm);
      }
      if (!RESERVED_PINS.has(pin)) {
        const dot = document.createElement("span");
        dot.className = "activity-dot";
        dot.id = `dot-${pin}`;
        td.appendChild(dot);
      }
      return td;
    })(),
  );

  if (RESERVED_PINS.has(pin)) {
    tr.appendChild(
      cell(pin === 13 ? "reserved (status LED)" : "reserved (USB serial)"),
    );
    for (let i = 0; i < 9; i++) tr.appendChild(cell(""));
  } else {
    tr.appendChild(
      cell(
        p.role === "encoder_b"
          ? `↳ ${ROLE_LABELS.encoder_b} (pin ${pin - 1})`
          : ROLE_LABELS[p.role],
      ),
    );
    tr.appendChild(
      cell(
        ROLE_FIELDS[p.role].includes("channel") ? String(p.channel + 1) : "",
      ),
    );
    tr.appendChild(cell(displayNumber(p)));
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("in_min") ? String(p.in_min) : ""),
    );
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("in_max") ? String(p.in_max) : ""),
    );
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("out_min") ? String(p.out_min) : ""),
    );
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("out_max") ? String(p.out_max) : ""),
    );
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("invert") && p.invert ? "✓" : ""),
    );
    tr.appendChild(
      cell(ROLE_FIELDS[p.role].includes("toggle") && p.toggle ? "✓" : ""),
    );
  }
  tr.appendChild(cell(""));

  if (!RESERVED_PINS.has(pin) && p.role !== "encoder_b") {
    tr.addEventListener("click", () => selectPin(pin));
  }
  return tr;
}

// ---- rendering: board view -------------------------------------------------

function renderBoard() {
  const grid = document.getElementById("boardGrid");
  grid.innerHTML = "";

  const legend = document.createElement("div");
  legend.className = "board-legend";
  legend.innerHTML = '<span class="pwm-mark">~</span> = PWM-capable pin';
  grid.appendChild(legend);

  const addRow = (label, pins) => {
    const wrap = document.createElement("div");
    const rowLabel = document.createElement("div");
    rowLabel.className = "board-row-label";
    rowLabel.textContent = label;
    wrap.appendChild(rowLabel);
    const row = document.createElement("div");
    row.className = "board-row";
    pins.forEach((pin) => row.appendChild(buildPinBox(pin)));
    wrap.appendChild(row);
    grid.appendChild(wrap);
  };

  const lowDigital = [];
  for (let i = 0; i <= 13; i++) lowDigital.push(i);
  const highDigital = [];
  for (let i = 14; i <= 53; i++) highDigital.push(i);
  const analog = [];
  for (let i = 54; i < PIN_COUNT; i++) analog.push(i);

  addRow("Digital 0–13 (PWM: 2–13)", lowDigital);
  addRow("Digital 14–53 (PWM: 44–46)", highDigital);
  addRow("Analog A0–A15", analog);
}

function buildPinBox(pin) {
  const p = pinByIndex(pin);
  const box = document.createElement("div");
  box.className = "pin-box role-" + p.role;
  box.id = `pinbox-${pin}`;
  if (RESERVED_PINS.has(pin)) box.classList.add("reserved");
  if (p.role === "encoder_b") box.classList.add("encoder-b");
  if (state.selectedPin === pin) box.classList.add("selected");

  const pn = document.createElement("div");
  pn.className = "pn";
  pn.textContent = pinLabel(pin) + (PWM_PINS.has(pin) ? " ~" : "");
  box.appendChild(pn);
  if (PWM_PINS.has(pin)) {
    box.classList.add("pwm");
    box.title = "PWM-capable pin";
  }

  const role = document.createElement("div");
  role.className = "role";
  role.textContent = RESERVED_PINS.has(pin)
    ? pin === 13
      ? "LED"
      : "USB"
    : p.role === "unused"
      ? "–"
      : ROLE_LABELS[p.role].replace(" → ", "→");
  box.appendChild(role);

  if (!RESERVED_PINS.has(pin) && p.role !== "encoder_b") {
    box.addEventListener("click", () => selectPin(pin));
  }
  return box;
}

// ---- editor panel -----------------------------------------------------------

function selectPin(pin) {
  state.selectedPin = pin;
  renderAll();
}

function renderEditor() {
  const title = document.getElementById("editorTitle");
  const fields = document.getElementById("editorFields");
  fields.innerHTML = "";

  const pin = state.selectedPin;
  if (pin === null || pin === undefined) {
    title.textContent = "Select a pin";
    return;
  }
  const p = pinByIndex(pin);
  title.textContent = `Pin ${pinLabel(pin)} (Arduino #${pin})`;

  const roleRow = document.createElement("div");
  roleRow.className = "field-row";
  const roleLabel = document.createElement("label");
  roleLabel.textContent = "Role";
  const roleSelect = document.createElement("select");
  allowedRoles(pin).forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = ROLE_LABELS[r];
    if (r === p.role) opt.selected = true;
    roleSelect.appendChild(opt);
  });
  roleSelect.addEventListener("change", () =>
    setPinRole(pin, roleSelect.value),
  );
  roleRow.appendChild(roleLabel);
  roleRow.appendChild(roleSelect);
  fields.appendChild(roleRow);

  if (isAnalogPin(pin) && p.role === "pot_cc") {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      "In min/max are raw ADC ÷ 4 (0–255 covers the 0–1023 range).";
    fields.appendChild(hint);
  }
  if (p.role === "encoder_a") {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = `Uses this pin + pin ${pin + 1} (A/B quadrature). Relative CC (1=CW, 127=CCW per step); with Acceleration on, fast turns send a bigger step value instead of just 1/127.`;
    fields.appendChild(hint);
  }
  if (p.role === "button_shift") {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      "Pure modifier — sends no MIDI itself. Assign it as an encoder's shift button below.";
    fields.appendChild(hint);
  }
  const isListenLight =
    (p.role === "light_digital" || p.role === "light_pwm") && p.listen_pin !== NO_SHIFT_PIN;
  if (p.role === "light_digital" || p.role === "light_pwm") {
    renderListenField(pin, p, fields);
  }
  ROLE_FIELDS[p.role].forEach((field) => {
    if (isListenLight && field === "channel") return; // nothing to match against while listening
    if (field === "number") {
      renderNumberField(pin, p, fields);
      return;
    }
    const meta = FIELD_META[field];
    const row = document.createElement("div");
    row.className = "field-row" + (meta.type === "checkbox" ? " checkbox" : "");

    if (meta.type === "checkbox") {
      const label = document.createElement("label");
      label.className = "checkbox-label";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!p[field];
      input.addEventListener("change", () => {
        setPinField(pin, field, input.checked);
        renderAll();
      });
      const text = document.createElement("span");
      text.textContent = meta.label;
      label.appendChild(input);
      label.appendChild(text);
      row.appendChild(label);
    } else {
      const label = document.createElement("label");
      label.textContent = meta.label;
      const input = document.createElement("input");
      input.type = "number";
      input.min = meta.min;
      input.max = meta.max;
      const offset = meta.offset || 0;
      input.value = p[field] - offset;
      input.addEventListener("change", () => {
        let v = parseInt(input.value, 10) || 0;
        v = Math.max(meta.min, Math.min(meta.max, v)) - offset;
        setPinField(pin, field, v);
        renderAll();
      });
      row.appendChild(label);
      row.appendChild(input);
    }
    fields.appendChild(row);
  });

  if (p.role === "encoder_a")
    renderShiftFields(pin, p, fields, { pinField: "in_min", chField: "out_min", ccField: "out_max" });
  if (p.role === "pot_cc")
    renderShiftFields(pin, p, fields, { pinField: "shift_pin", chField: "shift_channel", ccField: "shift_number" });
  if (p.role === "encoder_a" || p.role === "pot_cc") renderMirrorField(pin, p, fields);
}

function renderNumberField(pin, p, fields) {
  const isDualRole = p.role === "light_digital" || p.role === "light_pwm";

  if (isDualRole && p.listen_pin !== NO_SHIFT_PIN) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = "Listening directly to a button — Note/CC matching is bypassed.";
    fields.appendChild(hint);
    return;
  }

  if (isDualRole) {
    const typeRow = document.createElement("div");
    typeRow.className = "field-row";
    const typeLabel = document.createElement("label");
    typeLabel.textContent = "Triggered by";
    const typeSelect = document.createElement("select");
    [["note", "Note"], ["cc", "CC"]].forEach(([value, text]) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = text;
      if ((value === "cc") === !!p.match_cc) opt.selected = true;
      typeSelect.appendChild(opt);
    });
    typeSelect.addEventListener("change", () => {
      setPinField(pin, "match_cc", typeSelect.value === "cc");
      renderAll();
    });
    typeRow.appendChild(typeLabel);
    typeRow.appendChild(typeSelect);
    fields.appendChild(typeRow);

    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = `Responds to an incoming ${p.match_cc ? "CC" : "Note"} with this channel + number.`;
    fields.appendChild(hint);
  }

  const row = document.createElement("div");
  row.className = "field-row";
  const label = document.createElement("label");
  const useNoteName = usesNoteName(p);
  label.textContent = useNoteName ? "Note" : "CC #";
  row.appendChild(label);

  if (useNoteName) {
    row.appendChild(buildNoteSelect(p.number, (v) => setPinField(pin, "number", v)));
  } else {
    const input = document.createElement("input");
    input.type = "number";
    input.min = 0;
    input.max = 127;
    input.value = p.number;
    input.addEventListener("change", () => {
      const v = Math.max(0, Math.min(127, parseInt(input.value, 10) || 0));
      setPinField(pin, "number", v);
    });
    row.appendChild(input);
  }
  fields.appendChild(row);
}

// Shared by encoder_a (in_min/out_min/out_max, its otherwise-unused value
// fields) and pot_cc (shift_pin/shift_channel/shift_number, dedicated fields
// since pots need in_min/in_max/out_min/out_max for ADC->CC scaling) --
// same shift-group UI, different backing fields per role.
function renderShiftFields(pin, p, fields, { pinField, chField, ccField }) {
  const shiftButtons = state.pins
    .map((pp, idx) => ({ idx, pp }))
    .filter(({ idx, pp }) => pp.role === "button_shift" && idx !== pin);

  const row = document.createElement("div");
  row.className = "field-row";
  const label = document.createElement("label");
  label.textContent = "Shift button (shift group)";
  const select = document.createElement("select");

  const noneOpt = document.createElement("option");
  noneOpt.value = String(NO_SHIFT_PIN);
  noneOpt.textContent = "None";
  select.appendChild(noneOpt);

  shiftButtons.forEach(({ idx }) => {
    const opt = document.createElement("option");
    opt.value = String(idx);
    opt.textContent = `Pin ${pinLabel(idx)} (#${idx})`;
    select.appendChild(opt);
  });

  const validIds = new Set([NO_SHIFT_PIN, ...shiftButtons.map((s) => s.idx)]);
  const currentShiftPin = p[pinField];
  select.value = validIds.has(currentShiftPin)
    ? String(currentShiftPin)
    : String(NO_SHIFT_PIN);

  select.addEventListener("change", () => {
    setPinField(pin, pinField, parseInt(select.value, 10));
    renderAll();
  });
  row.appendChild(label);
  row.appendChild(select);
  fields.appendChild(row);

  if (shiftButtons.length === 0) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      'No shift buttons configured yet — set another pin\'s role to "Shift button" first.';
    fields.appendChild(hint);
  }

  if (select.value !== String(NO_SHIFT_PIN)) {
    const chRow = document.createElement("div");
    chRow.className = "field-row";
    const chLabel = document.createElement("label");
    chLabel.textContent = "Shifted MIDI Channel";
    const chInput = document.createElement("input");
    chInput.type = "number";
    chInput.min = 1;
    chInput.max = 16;
    chInput.value = p[chField] + 1;
    chInput.addEventListener("change", () => {
      const v = Math.max(1, Math.min(16, parseInt(chInput.value, 10) || 1)) - 1;
      setPinField(pin, chField, v);
    });
    chRow.appendChild(chLabel);
    chRow.appendChild(chInput);
    fields.appendChild(chRow);

    const ccRow = document.createElement("div");
    ccRow.className = "field-row";
    const ccLabel = document.createElement("label");
    ccLabel.textContent = "Shifted CC #";
    const ccInput = document.createElement("input");
    ccInput.type = "number";
    ccInput.min = 0;
    ccInput.max = 127;
    ccInput.value = p[ccField];
    ccInput.addEventListener("change", () => {
      const v = Math.max(0, Math.min(127, parseInt(ccInput.value, 10) || 0));
      setPinField(pin, ccField, v);
    });
    ccRow.appendChild(ccLabel);
    ccRow.appendChild(ccInput);
    fields.appendChild(ccRow);
  }
}

// light_digital/light_pwm only: mirror a button/shift pin's live state
// directly (firmware's applyListenLights()) instead of matching an incoming
// Note/CC. Same picker pattern as renderShiftFields, different source-role
// filter and a single backing field (no per-target channel/number needed —
// there's nothing to send, just on/off to mirror).
function renderListenField(pin, p, fields) {
  const sourceButtons = state.pins
    .map((pp, idx) => ({ idx, pp }))
    .filter(({ pp }) => ["button_note", "button_cc", "button_shift"].includes(pp.role));

  const row = document.createElement("div");
  row.className = "field-row";
  const label = document.createElement("label");
  label.textContent = "Listen to button";
  const select = document.createElement("select");

  const noneOpt = document.createElement("option");
  noneOpt.value = String(NO_SHIFT_PIN);
  noneOpt.textContent = "None (match Note/CC instead)";
  select.appendChild(noneOpt);

  sourceButtons.forEach(({ idx, pp }) => {
    const opt = document.createElement("option");
    opt.value = String(idx);
    opt.textContent = `Pin ${pinLabel(idx)} (#${idx}, ${ROLE_LABELS[pp.role]})`;
    select.appendChild(opt);
  });

  const validIds = new Set([NO_SHIFT_PIN, ...sourceButtons.map((s) => s.idx)]);
  const currentListenPin = p.listen_pin;
  select.value = validIds.has(currentListenPin) ? String(currentListenPin) : String(NO_SHIFT_PIN);

  select.addEventListener("change", () => {
    setPinField(pin, "listen_pin", parseInt(select.value, 10));
    renderAll();
  });
  row.appendChild(label);
  row.appendChild(select);
  fields.appendChild(row);

  if (sourceButtons.length === 0) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      'No button pins configured yet — set another pin\'s role to a button/shift role first.';
    fields.appendChild(hint);
  }
}

// encoder_a/pot_cc only: pick a light_pwm pin whose brightness should track
// this control's live value (a VU meter) -- generalizes the sequencer/drone
// step's dedicated "pwm" pin-picker to any encoder or pot, in or out of a
// composite feature. Same picker pattern as renderShiftFields/
// renderListenField, backed by PinConfig.mirror_pin (see value_mirror.py).
function renderMirrorField(pin, p, fields) {
  const targets = state.pins
    .map((pp, idx) => ({ idx, pp }))
    .filter(({ idx, pp }) => pp.role === "light_pwm" && idx !== pin);

  const row = document.createElement("div");
  row.className = "field-row";
  const label = document.createElement("label");
  label.textContent = "Mirror value to PWM pin (VU meter)";
  const select = document.createElement("select");

  const noneOpt = document.createElement("option");
  noneOpt.value = String(NO_SHIFT_PIN);
  noneOpt.textContent = "None";
  select.appendChild(noneOpt);

  targets.forEach(({ idx }) => {
    const opt = document.createElement("option");
    opt.value = String(idx);
    opt.textContent = `Pin ${pinLabel(idx)} (#${idx})`;
    select.appendChild(opt);
  });

  const validIds = new Set([NO_SHIFT_PIN, ...targets.map((t) => t.idx)]);
  const current = p.mirror_pin;
  select.value = validIds.has(current) ? String(current) : String(NO_SHIFT_PIN);

  select.addEventListener("change", () => {
    setPinField(pin, "mirror_pin", parseInt(select.value, 10));
    renderAll();
  });
  row.appendChild(label);
  row.appendChild(select);
  fields.appendChild(row);

  if (targets.length === 0) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      'No PWM light pins configured yet — set another pin\'s role to "Light/VU (PWM)" first.';
    fields.appendChild(hint);
  }
}

// ---- rendering: sequencer view ----------------------------------------------

function renderSequencerTab() {
  renderSequencerSettings();
  renderSequencerSteps();
}

function renderSequencerSettings() {
  const el = document.getElementById("sequencerSettings");
  el.innerHTML = "";
  const seq = state.sequencer;

  const countRow = document.createElement("div");
  countRow.className = "field-row";
  const countLabel = document.createElement("label");
  countLabel.textContent = "Steps";
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = 1;
  countInput.max = SEQ_MAX_STEPS;
  countInput.value = seq.steps.length;
  countInput.addEventListener("change", () => setStepCount(parseInt(countInput.value, 10)));
  countRow.appendChild(countLabel);
  countRow.appendChild(countInput);
  el.appendChild(countRow);

  const subRow = document.createElement("div");
  subRow.className = "field-row";
  const subLabel = document.createElement("label");
  subLabel.textContent = "Subdivision";
  const subSelect = document.createElement("select");
  [
    ["8th", "8th notes"],
    ["16th", "16th notes"],
    ["32nd", "32nd notes"],
    ["16th_triplet", "16th-note triplets"],
  ].forEach(([value, text]) => {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    if (value === seq.subdivision) opt.selected = true;
    subSelect.appendChild(opt);
  });
  subSelect.addEventListener("change", () => setSequencerSubdivision(subSelect.value));
  subRow.appendChild(subLabel);
  subRow.appendChild(subSelect);
  el.appendChild(subRow);

  const chRow = document.createElement("div");
  chRow.className = "field-row";
  const chLabel = document.createElement("label");
  chLabel.textContent = "MIDI Channel";
  const chInput = document.createElement("input");
  chInput.type = "number";
  chInput.min = 1;
  chInput.max = 16;
  chInput.value = seq.channel + 1;
  chInput.addEventListener("change", () => {
    const v = Math.max(1, Math.min(16, parseInt(chInput.value, 10) || 1)) - 1;
    setSequencerChannel(v);
  });
  chRow.appendChild(chLabel);
  chRow.appendChild(chInput);
  el.appendChild(chRow);

  const hint = document.createElement("div");
  hint.className = "field-hint";
  hint.textContent =
    "Slaved to external MIDI Clock/Start/Stop/Continue only — stays idle until a DAW sends one.";
  el.appendChild(hint);

  const status = document.createElement("div");
  status.id = "seqClockStatus";
  status.className = "seq-clock-status";
  el.appendChild(status);
  updateSeqClockStatus();
}

const SEQ_CLOCK_STALE_MS = 1500;

// Says which of the usual reasons the sequencer is silent applies: no clock
// from the DAW, transport stopped, or every step switched off.
function updateSeqClockStatus() {
  const el = document.getElementById("seqClockStatus");
  if (!el) return;
  const entry = selectedEntry();
  const live = state.sequencerLive;
  const fresh = live && Date.now() - state.sequencerLiveAt < SEQ_CLOCK_STALE_MS;
  let text;
  let kind = "warn";
  if (!entry || entry.entry_type !== "device") {
    text = "Sequencer runs on a connected device, not a draft.";
  } else if (!entry.connected) {
    text = "Device offline — connect it to run the sequencer.";
  } else if (!live || live.bpm == null) {
    text =
      `No MIDI clock yet. In Ableton: Settings → Link, Tempo & MIDI → MIDI Ports, turn on Sync for the ` +
      `output “Mad Midi ${entry.device_id} In”, then press play.`;
  } else if (!live.running) {
    text = fresh ? `Clock OK (${live.bpm} BPM) — stopped. Press play in the DAW.` : "Stopped.";
  } else if (!fresh) {
    text = "Clock stopped arriving — is the DAW still playing, with Sync on?";
  } else if (!live.step_enabled.some(Boolean)) {
    text = `Playing at ${live.bpm} BPM, but every step is off — switch some on below (or with their buttons).`;
  } else {
    text = `▶ Playing · ${live.bpm} BPM · step ${live.current_step + 1}`;
    kind = "ok";
  }
  el.textContent = text;
  el.className = `seq-clock-status ${kind}`;
}

function buildSequencerPinSelect(stepIndex, fn, currentPin) {
  const step = state.sequencer.steps[stepIndex];
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = fn === "encoder" && step.control_type === "pot" ? "Pot" : SEQ_FN_LABELS[fn];
  wrap.appendChild(label);

  const select = document.createElement("select");
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Unassigned";
  select.appendChild(noneOpt);

  const eligible = sequencerEligiblePins(stepIndex, fn);
  eligible.forEach((pin) => {
    const opt = document.createElement("option");
    opt.value = String(pin);
    opt.textContent = `${pinLabel(pin)} (#${pin})`;
    select.appendChild(opt);
  });
  select.value = currentPin === null || currentPin === undefined ? "" : String(currentPin);

  select.addEventListener("change", () => {
    assignSequencerPin(stepIndex, fn, select.value === "" ? null : parseInt(select.value, 10));
  });
  wrap.appendChild(select);

  if (eligible.length === 0 && (currentPin === null || currentPin === undefined)) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "No free pins available";
    wrap.appendChild(hint);
  }
  return wrap;
}

function renderSequencerSteps() {
  const container = document.getElementById("sequencerSteps");
  container.innerHTML = "";
  const live = state.sequencerLive;
  const entry = selectedEntry();
  const canToggleLive = !!(entry && entry.entry_type === "device" && entry.connected && live);

  state.sequencer.steps.forEach((step, i) => {
    const row = document.createElement("div");
    row.className = "segment-card";
    if (live && live.running && live.current_step === i) row.classList.add("playhead");

    const heading = document.createElement("div");
    heading.className = "segment-card-heading";
    heading.textContent = `Step ${i + 1}`;
    row.appendChild(heading);

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "checkbox-label";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = canToggleLive ? !!live.step_enabled[i] : false;
    enabledInput.disabled = !canToggleLive;
    enabledInput.addEventListener("change", () => toggleStepEnabledUI(i));
    enabledLabel.appendChild(enabledInput);
    enabledLabel.appendChild(document.createTextNode(" On"));
    row.appendChild(enabledLabel);
    if (!canToggleLive) {
      const hint = document.createElement("span");
      hint.className = "field-hint";
      hint.textContent = "Connect the device to toggle steps";
      row.appendChild(hint);
    }

    const noteRow = document.createElement("div");
    noteRow.className = "field-row";
    const noteLabel = document.createElement("label");
    noteLabel.textContent = "Note";
    noteRow.appendChild(noteLabel);
    noteRow.appendChild(buildNoteSelect(step.note, (v) => setStepNote(i, v)));
    row.appendChild(noteRow);

    row.appendChild(buildControlTypeSelect(step.control_type, (v) => setStepControlType(i, v)));
    row.appendChild(buildSequencerPinSelect(i, "button", step.button_pin));
    row.appendChild(buildSequencerPinSelect(i, "encoder", step.encoder_pin));
    row.appendChild(buildSequencerPinSelect(i, "shift", step.shift_pin));
    row.appendChild(buildSequencerPinSelect(i, "pwm", step.pwm_pin));
    row.appendChild(buildSequencerPinSelect(i, "step_light", step.step_light_pin));
    row.appendChild(buildSequencerPinSelect(i, "enabled_light", step.enabled_light_pin));
    row.appendChild(buildShiftLinkHint(step, "encoder_pin", "shift_pin", step.control_type));

    container.appendChild(row);
  });
}

// ---- drone: pin assignment + rendering -------------------------------------
// A drone segment's shape mirrors a sequencer step's control cluster
// (control/enable/shift/pwm/lights), but behaves differently: it never
// sends a Note, its enable button just gates whether its value control's CC
// reaches the DAW (app/gated_segments.py), and its on/shift lights are
// wired via listen_pin (assignListenLight) rather than matched CC/Note, so
// they track the button's physical/toggled state with zero MIDI round-trip.

const DRONE_BASE = { enable: 0, control: 8, control_shifted: 16, pwm: 24 };
const DRONE_MAX_SEGMENTS = 8;
const DRONE_SEGMENT_PIN_FIELDS = [
  "control_pin",
  "enable_pin",
  "shift_pin",
  "pwm_pin",
  "on_light_pin",
  "shift_light_pin",
];
const DRONE_FN_LABELS = {
  control: "Encoder",
  enable: "Enable",
  shift: "Shift",
  pwm: "PWM (VU meter)",
  on_light: "On light",
  shift_light: "Shift light",
};

function blankDroneSegment() {
  return {
    control_type: "encoder", // "encoder" | "pot" -- this segment's own choice, independent of other segments
    control_pin: null,
    enable_pin: null,
    shift_pin: null,
    pwm_pin: null,
    on_light_pin: null,
    shift_light_pin: null,
  };
}

// Same eligibility shape as sequencerEligiblePins, minus step_light (drones
// have no trigger flash, only the on/shift indicator lights).
function droneEligiblePins(segmentIndex, fn) {
  const seg = state.drone.segments[segmentIndex];
  const currentlyAssigned = seg[fn + "_pin"];
  const needsPwm = fn === "pwm";
  const isPotControl = fn === "control" && seg.control_type === "pot";

  if (isPotControl) {
    const result = [];
    for (let pin = 54; pin < PIN_COUNT; pin++) {
      if (pin === currentlyAssigned) {
        result.push(pin);
        continue;
      }
      if (pinByIndex(pin).role !== "unused") continue;
      result.push(pin);
    }
    return result;
  }

  const maxPin = fn === "control" ? 53 : fn === "on_light" || fn === "shift_light" ? PIN_COUNT : 54;
  const result = [];
  for (let pin = 0; pin < maxPin; pin++) {
    if (RESERVED_PINS.has(pin)) continue;
    if (pin === currentlyAssigned) {
      result.push(pin);
      continue;
    }
    if (needsPwm && !PWM_PINS.has(pin)) continue;
    if (pinByIndex(pin).role !== "unused") continue;
    if (fn === "control") {
      if (pin + 1 >= PIN_COUNT || RESERVED_PINS.has(pin + 1)) continue;
      if (pinByIndex(pin + 1).role !== "unused") continue;
    }
    result.push(pin);
  }
  return result;
}

function assignDronePin(segmentIndex, fn, pin) {
  const seg = state.drone.segments[segmentIndex];
  const field = fn + "_pin";
  const prevPin = seg[field];
  if (prevPin !== null && prevPin !== undefined && prevPin !== pin) {
    clearAssignedPin(prevPin);
  }
  seg[field] = pin === null || pin === undefined ? null : pin;

  if (pin !== null && pin !== undefined) {
    const p = pinByIndex(pin);
    const ch = state.drone.channel;
    if (fn === "control") {
      if (seg.control_type === "pot") {
        p.role = "pot_cc";
        p.channel = ch;
        p.number = DRONE_BASE.control + segmentIndex;
        p.in_min = 0;
        p.in_max = 255;
        p.out_min = 0;
        p.out_max = 127;
        p.shift_pin = NO_SHIFT_PIN;
        if (seg.shift_pin !== null && seg.shift_pin !== undefined) {
          p.shift_pin = seg.shift_pin;
          p.shift_channel = ch;
          p.shift_number = DRONE_BASE.control_shifted + segmentIndex;
        }
      } else {
        p.role = "encoder_a";
        p.channel = ch;
        p.number = DRONE_BASE.control + segmentIndex;
        p.in_min = NO_SHIFT_PIN;
        if (pin + 1 < PIN_COUNT && !RESERVED_PINS.has(pin + 1)) {
          const partner = pinByIndex(pin + 1);
          partner.role = "encoder_b";
          partner.channel = ch;
          partner.number = p.number;
        }
        if (seg.shift_pin !== null && seg.shift_pin !== undefined) {
          p.in_min = seg.shift_pin;
          p.out_min = ch;
          p.out_max = DRONE_BASE.control_shifted + segmentIndex;
        }
      }
      p.mirror_pin = NO_SHIFT_PIN;
      if (seg.pwm_pin !== null && seg.pwm_pin !== undefined) {
        linkMirror(pin, seg.pwm_pin);
      }
    } else if (fn === "enable") {
      // A drone never sends a Note -- enabling it just gates whether
      // control_pin's CC reaches the DAW (see app/gated_segments.py).
      // Toggle mode (not momentary) so the on-light reflects sustained
      // on/off state without holding the button down.
      p.role = "button_cc";
      p.toggle = true;
      p.channel = ch;
      p.number = DRONE_BASE.enable + segmentIndex;
      if (seg.on_light_pin !== null && seg.on_light_pin !== undefined) {
        assignListenLight(seg.on_light_pin, pin);
      }
    } else if (fn === "shift") {
      p.role = "button_shift";
      if (seg.control_pin !== null && seg.control_pin !== undefined) {
        const ctrl = pinByIndex(seg.control_pin);
        if (seg.control_type === "pot") {
          ctrl.shift_pin = pin;
          ctrl.shift_channel = ch;
          ctrl.shift_number = DRONE_BASE.control_shifted + segmentIndex;
        } else {
          ctrl.in_min = pin;
          ctrl.out_min = ch;
          ctrl.out_max = DRONE_BASE.control_shifted + segmentIndex;
        }
      }
      if (seg.shift_light_pin !== null && seg.shift_light_pin !== undefined) {
        assignListenLight(seg.shift_light_pin, pin);
      }
    } else if (fn === "pwm") {
      p.role = "light_pwm";
      p.match_cc = true;
      p.channel = ch;
      p.number = DRONE_BASE.pwm + segmentIndex;
      p.in_min = 0;
      p.in_max = 127;
      p.out_min = 0;
      p.out_max = 255;
      if (seg.control_pin !== null && seg.control_pin !== undefined) {
        linkMirror(seg.control_pin, pin);
      }
    } else if (fn === "on_light") {
      assignListenLight(pin, seg.enable_pin);
    } else if (fn === "shift_light") {
      assignListenLight(pin, seg.shift_pin);
    }
  }

  scheduleAutoSave();
  renderAll();
}

function setDroneSegmentCount(n) {
  n = Math.max(1, Math.min(DRONE_MAX_SEGMENTS, n || 1));
  const segments = state.drone.segments;
  if (n < segments.length) {
    for (let i = n; i < segments.length; i++) {
      DRONE_SEGMENT_PIN_FIELDS.forEach((field) => {
        const pin = segments[i][field];
        if (pin !== null && pin !== undefined) clearAssignedPin(pin);
      });
    }
    segments.length = n;
  } else {
    while (segments.length < n) segments.push(blankDroneSegment());
  }
  scheduleAutoSave();
  renderAll();
}

function setDroneChannel(value) {
  state.drone.channel = value;
  state.drone.segments.forEach((seg) => {
    ["control", "enable", "pwm"].forEach((fn) => {
      const pin = seg[fn + "_pin"];
      if (pin !== null && pin !== undefined) pinByIndex(pin).channel = value;
    });
    if (seg.control_pin !== null && seg.control_pin !== undefined) {
      const ctrl = pinByIndex(seg.control_pin);
      if (ctrl.role === "pot_cc") {
        if (ctrl.shift_pin !== NO_SHIFT_PIN) ctrl.shift_channel = value;
      } else {
        if (ctrl.in_min !== NO_SHIFT_PIN) ctrl.out_min = value;
        if (seg.control_pin + 1 < PIN_COUNT) pinByIndex(seg.control_pin + 1).channel = value;
      }
    }
  });
  scheduleAutoSave();
  renderAll();
}

function setDroneSegmentControlType(segmentIndex, value) {
  state.drone.segments[segmentIndex].control_type = value;
  scheduleAutoSave();
  renderAll();
}

function renderDroneTab() {
  renderDroneSettings();
  renderDroneSegments();
}

function renderDroneSettings() {
  const el = document.getElementById("droneSettings");
  el.innerHTML = "";
  const d = state.drone;

  const countRow = document.createElement("div");
  countRow.className = "field-row";
  const countLabel = document.createElement("label");
  countLabel.textContent = "Segments";
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = 1;
  countInput.max = DRONE_MAX_SEGMENTS;
  countInput.value = d.segments.length;
  countInput.addEventListener("change", () => setDroneSegmentCount(parseInt(countInput.value, 10)));
  countRow.appendChild(countLabel);
  countRow.appendChild(countInput);
  el.appendChild(countRow);

  const chRow = document.createElement("div");
  chRow.className = "field-row";
  const chLabel = document.createElement("label");
  chLabel.textContent = "MIDI Channel";
  const chInput = document.createElement("input");
  chInput.type = "number";
  chInput.min = 1;
  chInput.max = 16;
  chInput.value = d.channel + 1;
  chInput.addEventListener("change", () => {
    const v = Math.max(1, Math.min(16, parseInt(chInput.value, 10) || 1)) - 1;
    setDroneChannel(v);
  });
  chRow.appendChild(chLabel);
  chRow.appendChild(chInput);
  el.appendChild(chRow);

  const hint = document.createElement("div");
  hint.className = "field-hint";
  hint.textContent =
    "Each segment's enable button toggles whether its value control's CC reaches your DAW — a drone never sends a Note. The on/shift lights mirror their button's physical state directly, with no DAW connection needed.";
  el.appendChild(hint);
}

function buildDronePinSelect(segmentIndex, fn, currentPin) {
  const seg = state.drone.segments[segmentIndex];
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = fn === "control" && seg.control_type === "pot" ? "Pot" : DRONE_FN_LABELS[fn];
  wrap.appendChild(label);

  const select = document.createElement("select");
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Unassigned";
  select.appendChild(noneOpt);

  const eligible = droneEligiblePins(segmentIndex, fn);
  eligible.forEach((pin) => {
    const opt = document.createElement("option");
    opt.value = String(pin);
    opt.textContent = `${pinLabel(pin)} (#${pin})`;
    select.appendChild(opt);
  });
  select.value = currentPin === null || currentPin === undefined ? "" : String(currentPin);

  select.addEventListener("change", () => {
    assignDronePin(segmentIndex, fn, select.value === "" ? null : parseInt(select.value, 10));
  });
  wrap.appendChild(select);

  if (eligible.length === 0 && (currentPin === null || currentPin === undefined)) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "No free pins available";
    wrap.appendChild(hint);
  }
  return wrap;
}

function renderDroneSegments() {
  const container = document.getElementById("droneSegments");
  container.innerHTML = "";
  const live = state.droneLive;
  const entry = selectedEntry();
  const showLive = !!(entry && entry.entry_type === "device" && entry.connected && live);

  state.drone.segments.forEach((seg, i) => {
    const row = document.createElement("div");
    row.className = "segment-card";
    if (showLive && live.enabled[i]) row.classList.add("active");

    const heading = document.createElement("div");
    heading.className = "segment-card-heading";
    heading.textContent = `Segment ${i + 1}`;
    row.appendChild(heading);

    const statusLabel = document.createElement("span");
    statusLabel.className = "field-hint";
    statusLabel.textContent = showLive
      ? live.enabled[i]
        ? "On (via physical button)"
        : "Off (via physical button)"
      : "Connect the device to see live status";
    row.appendChild(statusLabel);

    row.appendChild(buildControlTypeSelect(seg.control_type, (v) => setDroneSegmentControlType(i, v)));
    row.appendChild(buildDronePinSelect(i, "control", seg.control_pin));
    row.appendChild(buildDronePinSelect(i, "enable", seg.enable_pin));
    row.appendChild(buildDronePinSelect(i, "shift", seg.shift_pin));
    row.appendChild(buildDronePinSelect(i, "pwm", seg.pwm_pin));
    row.appendChild(buildDronePinSelect(i, "on_light", seg.on_light_pin));
    row.appendChild(buildDronePinSelect(i, "shift_light", seg.shift_light_pin));
    row.appendChild(buildShiftLinkHint(seg, "control_pin", "shift_pin", seg.control_type));

    container.appendChild(row);
  });
}

// ---- fx rack: pin assignment + rendering -----------------------------------
// Each segment is always two encoders + one enable button (gating both at
// once) + one shared shift button + on/shift lights -- no PWM meter.

const FX_BASE = { enable: 0, encoder1: 8, encoder1_shifted: 16, encoder2: 24, encoder2_shifted: 32 };
const FX_MAX_SEGMENTS = 8;
const FX_SEGMENT_PIN_FIELDS = [
  "encoder1_pin",
  "encoder2_pin",
  "enable_pin",
  "shift_pin",
  "on_light_pin",
  "shift_light_pin",
];
const FX_FN_LABELS = {
  encoder1: "Encoder 1",
  encoder2: "Encoder 2",
  enable: "Enable",
  shift: "Shift",
  on_light: "On light",
  shift_light: "Shift light",
};

function blankFxSegment() {
  return {
    encoder1_pin: null,
    encoder2_pin: null,
    enable_pin: null,
    shift_pin: null,
    on_light_pin: null,
    shift_light_pin: null,
  };
}

function fxEligiblePins(segmentIndex, fn) {
  const seg = state.fxRack.segments[segmentIndex];
  const currentlyAssigned = seg[fn + "_pin"];
  const isEncoder = fn === "encoder1" || fn === "encoder2";
  const maxPin = isEncoder ? 53 : fn === "on_light" || fn === "shift_light" ? PIN_COUNT : 54;

  const result = [];
  for (let pin = 0; pin < maxPin; pin++) {
    if (RESERVED_PINS.has(pin)) continue;
    if (pin === currentlyAssigned) {
      result.push(pin);
      continue;
    }
    if (pinByIndex(pin).role !== "unused") continue;
    if (isEncoder) {
      if (pin + 1 >= PIN_COUNT || RESERVED_PINS.has(pin + 1)) continue;
      if (pinByIndex(pin + 1).role !== "unused") continue;
    }
    result.push(pin);
  }
  return result;
}

function assignFxPin(segmentIndex, fn, pin) {
  const seg = state.fxRack.segments[segmentIndex];
  const field = fn + "_pin";
  const prevPin = seg[field];
  if (prevPin !== null && prevPin !== undefined && prevPin !== pin) {
    clearAssignedPin(prevPin);
  }
  seg[field] = pin === null || pin === undefined ? null : pin;

  if (pin !== null && pin !== undefined) {
    const p = pinByIndex(pin);
    const ch = state.fxRack.channel;
    if (fn === "encoder1" || fn === "encoder2") {
      const base = fn === "encoder1" ? FX_BASE.encoder1 : FX_BASE.encoder2;
      const shiftedBase = fn === "encoder1" ? FX_BASE.encoder1_shifted : FX_BASE.encoder2_shifted;
      p.role = "encoder_a";
      p.channel = ch;
      p.number = base + segmentIndex;
      p.in_min = NO_SHIFT_PIN;
      if (pin + 1 < PIN_COUNT && !RESERVED_PINS.has(pin + 1)) {
        const partner = pinByIndex(pin + 1);
        partner.role = "encoder_b";
        partner.channel = ch;
        partner.number = p.number;
      }
      if (seg.shift_pin !== null && seg.shift_pin !== undefined) {
        p.in_min = seg.shift_pin;
        p.out_min = ch;
        p.out_max = shiftedBase + segmentIndex;
      }
    } else if (fn === "enable") {
      // Gates BOTH encoders' CC from reaching the DAW at once (see
      // app/gated_segments.py) -- toggle mode, same as Drone's.
      p.role = "button_cc";
      p.toggle = true;
      p.channel = ch;
      p.number = FX_BASE.enable + segmentIndex;
      if (seg.on_light_pin !== null && seg.on_light_pin !== undefined) {
        assignListenLight(seg.on_light_pin, pin);
      }
    } else if (fn === "shift") {
      p.role = "button_shift";
      [
        ["encoder1_pin", FX_BASE.encoder1_shifted],
        ["encoder2_pin", FX_BASE.encoder2_shifted],
      ].forEach(([field2, shiftedBase]) => {
        const encPin = seg[field2];
        if (encPin !== null && encPin !== undefined) {
          const enc = pinByIndex(encPin);
          enc.in_min = pin;
          enc.out_min = ch;
          enc.out_max = shiftedBase + segmentIndex;
        }
      });
      if (seg.shift_light_pin !== null && seg.shift_light_pin !== undefined) {
        assignListenLight(seg.shift_light_pin, pin);
      }
    } else if (fn === "on_light") {
      assignListenLight(pin, seg.enable_pin);
    } else if (fn === "shift_light") {
      assignListenLight(pin, seg.shift_pin);
    }
  }

  scheduleAutoSave();
  renderAll();
}

function setFxSegmentCount(n) {
  n = Math.max(1, Math.min(FX_MAX_SEGMENTS, n || 1));
  const segments = state.fxRack.segments;
  if (n < segments.length) {
    for (let i = n; i < segments.length; i++) {
      FX_SEGMENT_PIN_FIELDS.forEach((field) => {
        const pin = segments[i][field];
        if (pin !== null && pin !== undefined) clearAssignedPin(pin);
      });
    }
    segments.length = n;
  } else {
    while (segments.length < n) segments.push(blankFxSegment());
  }
  scheduleAutoSave();
  renderAll();
}

function setFxChannel(value) {
  state.fxRack.channel = value;
  state.fxRack.segments.forEach((seg) => {
    ["encoder1", "encoder2", "enable"].forEach((fn) => {
      const pin = seg[fn + "_pin"];
      if (pin !== null && pin !== undefined) pinByIndex(pin).channel = value;
    });
    ["encoder1_pin", "encoder2_pin"].forEach((field) => {
      const encPin = seg[field];
      if (encPin !== null && encPin !== undefined) {
        const enc = pinByIndex(encPin);
        if (enc.in_min !== NO_SHIFT_PIN) enc.out_min = value;
        if (encPin + 1 < PIN_COUNT) pinByIndex(encPin + 1).channel = value;
      }
    });
  });
  scheduleAutoSave();
  renderAll();
}

function renderFxRackTab() {
  renderFxRackSettings();
  renderFxRackSegments();
}

function renderFxRackSettings() {
  const el = document.getElementById("fxRackSettings");
  el.innerHTML = "";
  const fx = state.fxRack;

  const countRow = document.createElement("div");
  countRow.className = "field-row";
  const countLabel = document.createElement("label");
  countLabel.textContent = "Segments";
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = 1;
  countInput.max = FX_MAX_SEGMENTS;
  countInput.value = fx.segments.length;
  countInput.addEventListener("change", () => setFxSegmentCount(parseInt(countInput.value, 10)));
  countRow.appendChild(countLabel);
  countRow.appendChild(countInput);
  el.appendChild(countRow);

  const chRow = document.createElement("div");
  chRow.className = "field-row";
  const chLabel = document.createElement("label");
  chLabel.textContent = "MIDI Channel";
  const chInput = document.createElement("input");
  chInput.type = "number";
  chInput.min = 1;
  chInput.max = 16;
  chInput.value = fx.channel + 1;
  chInput.addEventListener("change", () => {
    const v = Math.max(1, Math.min(16, parseInt(chInput.value, 10) || 1)) - 1;
    setFxChannel(v);
  });
  chRow.appendChild(chLabel);
  chRow.appendChild(chInput);
  el.appendChild(chRow);

  const hint = document.createElement("div");
  hint.className = "field-hint";
  hint.textContent =
    "Each segment always has two encoders. Its enable button gates both encoders' CC from reaching your DAW at once; the on/shift lights mirror their button's physical state directly.";
  el.appendChild(hint);
}

function buildFxPinSelect(segmentIndex, fn, currentPin) {
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = FX_FN_LABELS[fn];
  wrap.appendChild(label);

  const select = document.createElement("select");
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Unassigned";
  select.appendChild(noneOpt);

  const eligible = fxEligiblePins(segmentIndex, fn);
  eligible.forEach((pin) => {
    const opt = document.createElement("option");
    opt.value = String(pin);
    opt.textContent = `${pinLabel(pin)} (#${pin})`;
    select.appendChild(opt);
  });
  select.value = currentPin === null || currentPin === undefined ? "" : String(currentPin);

  select.addEventListener("change", () => {
    assignFxPin(segmentIndex, fn, select.value === "" ? null : parseInt(select.value, 10));
  });
  wrap.appendChild(select);

  if (eligible.length === 0 && (currentPin === null || currentPin === undefined)) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "No free pins available";
    wrap.appendChild(hint);
  }
  return wrap;
}

function renderFxRackSegments() {
  const container = document.getElementById("fxRackSegments");
  container.innerHTML = "";
  const live = state.fxRackLive;
  const entry = selectedEntry();
  const showLive = !!(entry && entry.entry_type === "device" && entry.connected && live);

  state.fxRack.segments.forEach((seg, i) => {
    const row = document.createElement("div");
    row.className = "segment-card";
    if (showLive && live.enabled[i]) row.classList.add("active");

    const heading = document.createElement("div");
    heading.className = "segment-card-heading";
    heading.textContent = `Segment ${i + 1}`;
    row.appendChild(heading);

    const statusLabel = document.createElement("span");
    statusLabel.className = "field-hint";
    statusLabel.textContent = showLive
      ? live.enabled[i]
        ? "On (via physical button)"
        : "Off (via physical button)"
      : "Connect the device to see live status";
    row.appendChild(statusLabel);

    row.appendChild(buildFxPinSelect(i, "encoder1", seg.encoder1_pin));
    row.appendChild(buildFxPinSelect(i, "encoder2", seg.encoder2_pin));
    row.appendChild(buildFxPinSelect(i, "enable", seg.enable_pin));
    row.appendChild(buildFxPinSelect(i, "shift", seg.shift_pin));
    row.appendChild(buildFxPinSelect(i, "on_light", seg.on_light_pin));
    row.appendChild(buildFxPinSelect(i, "shift_light", seg.shift_light_pin));
    row.appendChild(buildShiftLinkHint(seg, "encoder1_pin", "shift_pin", "encoder"));
    row.appendChild(buildShiftLinkHint(seg, "encoder2_pin", "shift_pin", "encoder"));

    container.appendChild(row);
  });
}

// ---- joystick: pin assignment + rendering ----------------------------------
// A single fixed X/Y/rotation + button widget, not a repeatable list. Every
// pin is an ordinary existing role (pot_cc for the three axes, button_note
// for the click) -- no engine at all, just a fixed-shape assignment UI plus
// a monitor visualization fed by the existing pin_activity event stream.

const JOYSTICK_AXIS_LABELS = { x: "X axis", y: "Y axis", z: "Rotation (Z)", button: "Button" };

function joystickEligiblePins(axis) {
  const currentlyAssigned = state.joystick[axis + "_pin"];
  const isAnalog = axis === "x" || axis === "y" || axis === "z";
  const result = [];
  if (isAnalog) {
    for (let pin = 54; pin < PIN_COUNT; pin++) {
      if (pin === currentlyAssigned) {
        result.push(pin);
        continue;
      }
      if (pinByIndex(pin).role !== "unused") continue;
      result.push(pin);
    }
  } else {
    for (let pin = 0; pin < 54; pin++) {
      if (RESERVED_PINS.has(pin)) continue;
      if (pin === currentlyAssigned) {
        result.push(pin);
        continue;
      }
      if (pinByIndex(pin).role !== "unused") continue;
      result.push(pin);
    }
  }
  return result;
}

function assignJoystickPin(axis, pin) {
  const field = axis + "_pin";
  const prevPin = state.joystick[field];
  if (prevPin !== null && prevPin !== undefined && prevPin !== pin) {
    clearAssignedPin(prevPin);
  }
  state.joystick[field] = pin === null || pin === undefined ? null : pin;

  if (pin !== null && pin !== undefined) {
    const p = pinByIndex(pin);
    if (axis === "button") {
      p.role = "button_note";
      p.channel = 0;
      p.number = 60; // default note, freely editable via Table/Board afterward
    } else {
      p.role = "pot_cc";
      p.channel = 0;
      p.number = axis === "x" ? 20 : axis === "y" ? 21 : 22;
      p.in_min = 0;
      p.in_max = 255;
      p.out_min = 0;
      p.out_max = 127;
      p.shift_pin = NO_SHIFT_PIN;
    }
  }

  scheduleAutoSave();
  renderAll();
}

function buildJoystickPinSelect(axis) {
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = JOYSTICK_AXIS_LABELS[axis];
  wrap.appendChild(label);

  const select = document.createElement("select");
  const noneOpt = document.createElement("option");
  noneOpt.value = "";
  noneOpt.textContent = "Unassigned";
  select.appendChild(noneOpt);

  const currentPin = state.joystick[axis + "_pin"];
  const eligible = joystickEligiblePins(axis);
  eligible.forEach((pin) => {
    const opt = document.createElement("option");
    opt.value = String(pin);
    opt.textContent = `${pinLabel(pin)} (#${pin})`;
    select.appendChild(opt);
  });
  select.value = currentPin === null || currentPin === undefined ? "" : String(currentPin);

  select.addEventListener("change", () => {
    assignJoystickPin(axis, select.value === "" ? null : parseInt(select.value, 10));
  });
  wrap.appendChild(select);

  // Same flag as the pin's own Invert checkbox -- flips it on the device, so
  // a stick wired backwards reads the right way round in the DAW too.
  const assigned = currentPin !== null && currentPin !== undefined ? pinByIndex(currentPin) : null;
  if (axis !== "button" && assigned && assigned.role === "pot_cc") {
    const invLabel = document.createElement("label");
    invLabel.className = "checkbox-label";
    const inv = document.createElement("input");
    inv.type = "checkbox";
    inv.checked = !!assigned.invert;
    inv.addEventListener("change", () => {
      setPinField(currentPin, "invert", inv.checked);
      renderAll();
    });
    const invText = document.createElement("span");
    invText.textContent = "Invert direction (needs Write to device)";
    invLabel.appendChild(inv);
    invLabel.appendChild(invText);
    wrap.appendChild(invLabel);
  }

  if (eligible.length === 0 && (currentPin === null || currentPin === undefined)) {
    const hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "No free pins available";
    wrap.appendChild(hint);
  }
  return wrap;
}

function renderJoystickTab() {
  const el = document.getElementById("joystickSettings");
  el.innerHTML = "";

  const hint = document.createElement("div");
  hint.className = "field-hint";
  hint.textContent =
    "X/Y/rotation are analog pots (A0–A15); the button is a plain digital input. Sends ordinary CC/Note like any other pin — no special engine involved.";
  el.appendChild(hint);

  ["x", "y", "z", "button"].forEach((axis) => {
    el.appendChild(buildJoystickPinSelect(axis));
  });

  const monitor = document.createElement("div");
  monitor.className = "joystick-monitor";
  const dot = document.createElement("div");
  dot.id = "joystickDot";
  dot.className = "joystick-dot";
  monitor.appendChild(dot);
  el.appendChild(monitor);

  // Rotation rests at the middle of its range and twists either way, so it
  // gets a centre-zero bar: fill grows left or right from the centre mark.
  const zWrap = document.createElement("div");
  zWrap.className = "joystick-z";
  const zLabels = document.createElement("div");
  zLabels.className = "joystick-z-labels";
  zLabels.innerHTML = "<span>◀ Left</span><span>Rotation</span><span>Right ▶</span>";
  zWrap.appendChild(zLabels);
  const zRow = document.createElement("div");
  zRow.className = "joystick-zbar-wrap";
  const zFill = document.createElement("div");
  zFill.id = "joystickZFill";
  zFill.className = "joystick-zbar-fill";
  zRow.appendChild(zFill);
  const zCentre = document.createElement("div");
  zCentre.className = "joystick-zbar-centre";
  zRow.appendChild(zCentre);
  zWrap.appendChild(zRow);
  const zValue = document.createElement("div");
  zValue.id = "joystickZValue";
  zValue.className = "joystick-z-value";
  zWrap.appendChild(zValue);
  el.appendChild(zWrap);
  positionJoystickZ(JOYSTICK_Z_CENTRE);
}

const JOYSTICK_Z_CENTRE = 64; // CC value the rotation axis rests at

function positionJoystickZ(value) {
  const fill = document.getElementById("joystickZFill");
  const label = document.getElementById("joystickZValue");
  if (!fill || !label) return;
  const offset = value - JOYSTICK_Z_CENTRE; // -64 (full left) .. +63 (full right)
  const pct = Math.min(50, Math.round((Math.abs(offset) / JOYSTICK_Z_CENTRE) * 50));
  fill.style.width = `${pct}%`;
  fill.style.left = offset < 0 ? `${50 - pct}%` : "50%";
  label.textContent = offset === 0 ? "Centre (64)" : `${offset > 0 ? "+" : ""}${offset} (${value})`;
}

function updateJoystickVisual(pin, value) {
  if (!state.joystick) return;
  const dot = document.getElementById("joystickDot");
  const zFill = document.getElementById("joystickZFill");
  if (dot && pin === state.joystick.x_pin) {
    dot.dataset.x = value;
    positionJoystickDot(dot);
  } else if (dot && pin === state.joystick.y_pin) {
    dot.dataset.y = value;
    positionJoystickDot(dot);
  } else if (zFill && pin === state.joystick.z_pin) {
    positionJoystickZ(value);
  }
}

function positionJoystickDot(dot) {
  const x = dot.dataset.x !== undefined ? parseInt(dot.dataset.x, 10) : 64;
  const y = dot.dataset.y !== undefined ? parseInt(dot.dataset.y, 10) : 64;
  dot.style.left = `${Math.round((x / 127) * 100)}%`;
  dot.style.top = `${Math.round((1 - y / 127) * 100)}%`;
}

// ---- rendering: test view -----------------------------------------------------
// Drives pins straight HIGH/LOW for checking wiring. While this tab is open the
// device is held in config mode (MIDI paused); leaving the tab, switching entry
// or disconnecting hands every tested pin back to its configured role.

function blankPinTest() {
  // status: idle | starting | active; levels: pin -> "high"/"low"/"input";
  // live: last {digital, analog} pushed by the device's pin_test_state poll
  return { entryId: null, status: "idle", error: null, levels: {}, live: null };
}

function pinTestWanted() {
  const entry = selectedEntry();
  return state.view === "test" && entry && entry.entry_type === "device" && entry.connected ? entry : null;
}

// Idempotent -- called from renderTabs() and refreshEntries(), so it starts or
// stops the device-side test session whenever the tab, selection, or
// connection state changes.
async function syncPinTest() {
  const pt = state.pinTest;
  const wanted = pinTestWanted();
  if (pt.entryId && (!wanted || wanted.id !== pt.entryId)) {
    const id = pt.entryId;
    state.pinTest = blankPinTest();
    renderTestTab();
    await api("stop_pin_test", id);
    return;
  }
  if (!wanted) {
    if (pt.error && state.view !== "test") state.pinTest = blankPinTest();
    return;
  }
  if (pt.status !== "idle" || pt.error) return; // already running, or failed until the tab is reopened
  pt.entryId = wanted.id;
  pt.status = "starting";
  renderTestTab();
  const result = await api("start_pin_test", wanted.id);
  if (state.pinTest !== pt) return; // stopped while we were waiting
  if (result.ok) {
    pt.status = "active";
  } else {
    state.pinTest = { ...blankPinTest(), error: result.error };
  }
  renderTestTab();
}

async function setTestPin(pin, level) {
  const pt = state.pinTest;
  if (pt.status !== "active") return;
  const ok = await api("test_pin", pt.entryId, pin, level);
  if (state.pinTest !== pt) return;
  if (!ok) {
    pt.error = `Device rejected ${pinLabel(pin)} → ${level}.`;
  } else if (level === "release") {
    delete pt.levels[pin];
  } else {
    pt.levels[pin] = level;
  }
  renderTestTab();
}

async function setAllTestPins(level) {
  const pins = level === "release" ? Object.keys(state.pinTest.levels).map(Number) : testablePins();
  for (const pin of pins) await setTestPin(pin, level);
}

function testablePins() {
  const pins = [];
  for (let i = 0; i < PIN_COUNT; i++) if (!RESERVED_PINS.has(i)) pins.push(i);
  return pins;
}

function renderTestTab() {
  const settings = document.getElementById("testSettings");
  const grid = document.getElementById("testGrid");
  if (!settings || !grid) return;
  settings.innerHTML = "";
  grid.innerHTML = "";

  const entry = selectedEntry();
  const pt = state.pinTest;
  const active = pt.status === "active" && entry && pt.entryId === entry.id;

  const status = document.createElement("p");
  status.className = "field-hint test-status";
  if (pt.error) {
    status.classList.add("error");
    status.textContent = pt.error;
  } else if (!entry || entry.entry_type !== "device") {
    status.textContent = "Pin testing needs an adopted device.";
  } else if (!entry.connected) {
    status.textContent = "Connect the device to test its pins.";
  } else if (pt.status === "starting") {
    status.textContent = "Putting the device into test mode…";
  } else {
    status.textContent =
      "Test mode: MIDI is paused. H/L drive a pin, In reads it with a pull-up (for a button on a pin with no button role). Every pin shows its live level; analog pins also show their 0–1023 reading. Leaving this tab puts every pin back on its configured role.";
  }
  settings.appendChild(status);

  const allLow = document.createElement("button");
  allLow.textContent = "All low";
  allLow.disabled = !active;
  allLow.addEventListener("click", () => setAllTestPins("low"));
  settings.appendChild(allLow);

  const releaseAll = document.createElement("button");
  releaseAll.textContent = "Release all";
  releaseAll.disabled = !active || Object.keys(pt.levels).length === 0;
  releaseAll.addEventListener("click", () => setAllTestPins("release"));
  settings.appendChild(releaseAll);

  const addRow = (label, pins) => {
    const wrap = document.createElement("div");
    const rowLabel = document.createElement("div");
    rowLabel.className = "board-row-label";
    rowLabel.textContent = label;
    wrap.appendChild(rowLabel);
    const row = document.createElement("div");
    row.className = "board-row";
    pins.forEach((pin) => row.appendChild(buildTestPinBox(pin, active)));
    wrap.appendChild(row);
    grid.appendChild(wrap);
  };
  const range = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
  addRow("Digital 0–13", range(0, 13));
  addRow("Digital 14–53", range(14, 53));
  addRow("Analog A0–A15", range(54, PIN_COUNT - 1));
  updateTestLive();
}

function buildTestPinBox(pin, active) {
  const p = pinByIndex(pin);
  const level = state.pinTest.levels[pin];
  const reserved = RESERVED_PINS.has(pin);
  const box = document.createElement("div");
  box.className = "pin-box test-pin" + (level ? ` level-${level}` : "");
  if (reserved) box.classList.add("reserved");

  const pn = document.createElement("div");
  pn.className = "pn";
  pn.textContent = pinLabel(pin);
  box.appendChild(pn);

  const role = document.createElement("div");
  role.className = "role";
  role.textContent = reserved
    ? pin === 13 ? "LED" : "USB"
    : !p || p.role === "unused" ? "–" : ROLE_LABELS[p.role].replace(" → ", "→");
  box.appendChild(role);

  box.id = `testpin-${pin}`;
  const live = document.createElement("div");
  live.className = "test-live";
  const dot = document.createElement("span");
  dot.className = "test-dot";
  const text = document.createElement("span");
  text.className = "test-live-text";
  live.appendChild(dot);
  live.appendChild(text);
  box.appendChild(live);

  if (isAnalogPin(pin)) {
    const bar = document.createElement("div");
    bar.className = "test-abar";
    const fill = document.createElement("div");
    fill.className = "test-abar-fill";
    bar.appendChild(fill);
    box.appendChild(bar);
  }

  if (!reserved) {
    const btns = document.createElement("div");
    btns.className = "test-btns";
    const titles = { high: "Drive high", low: "Drive low", input: "Read as input with pull-up" };
    [["high", "H"], ["low", "L"], ["input", "In"]].forEach(([lvl, text]) => {
      const b = document.createElement("button");
      b.textContent = text;
      b.className = level === lvl ? "on" : "";
      b.disabled = !active;
      b.title = level === lvl ? "Release (back to its role)" : titles[lvl];
      b.addEventListener("click", () => setTestPin(pin, level === lvl ? "release" : lvl));
      btns.appendChild(b);
    });
    box.appendChild(btns);
  }
  return box;
}

const BUTTON_ROLES = new Set(["button_note", "button_cc", "button_shift"]);

// Patches the live readouts in place rather than re-rendering: this runs on
// every poll (~20x/s) and rebuilding the boxes would eat clicks mid-press.
function updateTestLive() {
  const live = state.pinTest.live;
  for (let pin = 0; pin < PIN_COUNT; pin++) {
    const box = document.getElementById(`testpin-${pin}`);
    if (!box) continue;
    const dot = box.querySelector(".test-dot");
    const text = box.querySelector(".test-live-text");
    const fill = box.querySelector(".test-abar-fill");
    if (!live) {
      box.classList.remove("live-high", "pressed");
      text.textContent = "";
      if (fill) fill.style.width = "0%";
      continue;
    }
    const high = live.digital[pin];
    const p = pinByIndex(pin);
    // Buttons are wired to GND with a pull-up, so pressed = LOW unless inverted.
    const isButton = p && BUTTON_ROLES.has(p.role);
    const pressed = isButton && (p.invert ? high : !high);
    box.classList.toggle("live-high", high);
    box.classList.toggle("pressed", pressed);
    dot.title = high ? "HIGH" : "LOW";
    if (isAnalogPin(pin)) {
      const v = live.analog[pin - 54];
      text.textContent = String(v);
      fill.style.width = `${Math.round((v / 1023) * 100)}%`;
    } else {
      text.textContent = isButton ? (pressed ? "pressed" : "–") : high ? "HIGH" : "LOW";
    }
  }
}

// ---- sidebar ----------------------------------------------------------------

function sortedEntries() {
  const devices = state.entries
    .filter((e) => e.entry_type === "device")
    .sort((a, b) => a.device_id - b.device_id);
  const drafts = state.entries
    .filter((e) => e.entry_type === "draft")
    .sort((a, b) => a.nickname.localeCompare(b.nickname));
  return [...devices, ...drafts];
}

function statusDotClass(entry) {
  if (entry.entry_type === "draft") return "dot draft";
  if (!entry.connected) return "dot offline";
  if (entry.in_sync === false) return "dot out-of-sync";
  return "dot in-sync";
}

function statusDotTitle(entry) {
  if (entry.entry_type === "draft") return "Draft (no device attached)";
  if (!entry.connected) return "Offline";
  if (entry.in_sync === false) return "Connected — out of sync with the device";
  return "Connected — in sync with the device";
}

function renderSidebar() {
  const list = document.getElementById("entryList");
  list.innerHTML = "";

  const allRow = document.createElement("div");
  allRow.className = "entry-row all-devices" + (state.selectedId === ALL_DEVICES_ID ? " selected" : "");
  allRow.textContent = "All devices";
  allRow.title = "Show MIDI traffic from every connected device";
  allRow.addEventListener("click", () => selectEntry(ALL_DEVICES_ID));
  list.appendChild(allRow);

  sortedEntries().forEach((entry) => {
    const row = document.createElement("div");
    row.className = "entry-row" + (state.selectedId === entry.id ? " selected" : "");

    const dot = document.createElement("span");
    dot.className = statusDotClass(entry);
    dot.title = statusDotTitle(entry);
    row.appendChild(dot);

    const name = document.createElement("span");
    name.className = "entry-name";
    name.textContent = entry.nickname;
    row.appendChild(name);

    if (entry.entry_type === "device") {
      const idTag = document.createElement("span");
      idTag.className = "entry-id";
      idTag.textContent = `#${entry.device_id}`;
      row.appendChild(idTag);
    } else {
      const tag = document.createElement("span");
      tag.className = "entry-id";
      tag.textContent = "draft";
      row.appendChild(tag);
    }

    if (entry.entry_type === "device" && !entry.connected) {
      const connectBtn = document.createElement("button");
      connectBtn.className = "entry-action";
      connectBtn.textContent = "Connect";
      connectBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleReconnectPicker(entry.id);
      });
      row.appendChild(connectBtn);
    }

    const renameBtn = document.createElement("button");
    renameBtn.className = "entry-action";
    renameBtn.textContent = "✎";
    renameBtn.title = "Rename";
    renameBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      renameEntry(entry.id, entry.nickname);
    });
    row.appendChild(renameBtn);

    const removeBtn = document.createElement("button");
    removeBtn.className = "entry-action";
    removeBtn.textContent = "✕";
    removeBtn.title = "Remove";
    removeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeEntry(entry.id);
    });
    row.appendChild(removeBtn);

    row.addEventListener("click", () => selectEntry(entry.id));
    list.appendChild(row);

    if (state.reconnectingId === entry.id) {
      list.appendChild(buildPortPicker(state.reconnectPorts, (port) => reconnectEntry(entry.id, port)));
    }
  });

  renderAddDevicePicker();
}

function buildPortPicker(ports, onPick, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "port-picker";
  if (ports.length === 0) {
    const empty = document.createElement("div");
    empty.className = "port-picker-empty";
    empty.textContent = "No serial ports found.";
    wrap.appendChild(empty);
    return wrap;
  }
  ports.forEach((port) => {
    const row = document.createElement("div");
    row.className = "port-picker-row";
    const btn = document.createElement("button");
    btn.textContent = port;
    btn.addEventListener("click", () => onPick(port));
    row.appendChild(btn);
    if (opts.withFlash) {
      const flashBtn = document.createElement("button");
      flashBtn.className = "port-picker-flash";
      flashBtn.textContent = "Flash";
      flashBtn.title =
        "Flash firmware directly to this port — use this first if the port doesn't adopt (blank or unresponsive board)";
      flashBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        flashPort(port);
      });
      row.appendChild(flashBtn);

      const draftBtn = document.createElement("button");
      draftBtn.className = "port-picker-draft";
      draftBtn.textContent = "New draft";
      draftBtn.title =
        "Start a draft pin map for this board now, without needing it to respond yet";
      draftBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        draftFromPort(port);
      });
      row.appendChild(draftBtn);
    }
    wrap.appendChild(row);
  });
  return wrap;
}

function renderAddDevicePicker() {
  const container = document.getElementById("addDevicePicker");
  container.innerHTML = "";
  container.hidden = state.addDevicePorts === null;
  if (state.addDevicePorts === null) return;
  container.appendChild(buildPortPicker(state.addDevicePorts, adoptPort, { withFlash: true }));
}

async function flashPort(port) {
  if (!confirm(`Flash firmware to ${port}? This takes a few seconds.`)) return;
  const result = await api("flash_port", port);
  alert(
    result.ok
      ? "Flash succeeded. Click the port again to adopt it."
      : "Flash failed:\n\n" + result.output
  );
}

async function toggleAddDevicePicker() {
  if (state.addDevicePorts !== null) {
    state.addDevicePorts = null;
  } else {
    state.addDevicePorts = await api("list_ports");
  }
  renderSidebar();
}

async function adoptPort(port) {
  state.addDevicePorts = null;
  const entry = await api("adopt_port", port);
  await refreshEntries();
  if (entry) {
    await selectEntry(entry.id);
  } else {
    alert(
      `${port} didn't respond as a Mad Midi device — it's probably running a ` +
        `different sketch (or none at all). Use "Flash" to put the firmware on it ` +
        `first, or "New draft" to start configuring its pin map now and adopt it later.`
    );
  }
}

async function createDraft(defaultName = "Untitled draft") {
  const name = prompt("Draft name:", defaultName);
  if (name === null) return;
  const entry = await api("create_draft", name);
  await refreshEntries();
  await selectEntry(entry.id);
}

async function draftFromPort(port) {
  const base = port.split("/").pop();
  await createDraft(`Draft (${base})`);
}

async function renameEntry(id, currentName) {
  const name = prompt("Rename to:", currentName);
  if (name === null || !name.trim()) return;
  await api("rename_entry", id, name.trim());
  await refreshEntries();
}

async function removeEntry(id) {
  if (!confirm("Remove this entry? This deletes its saved config permanently.")) return;
  await api("remove_entry", id);
  const wasSelected = state.selectedId === id;
  await refreshEntries();
  if (wasSelected) {
    const first = sortedEntries()[0];
    await selectEntry(first ? first.id : ALL_DEVICES_ID);
  }
}

async function toggleReconnectPicker(id) {
  if (state.reconnectingId === id) {
    state.reconnectingId = null;
  } else {
    state.reconnectingId = id;
    state.reconnectPorts = await api("list_ports");
  }
  renderSidebar();
}

async function reconnectEntry(id, port) {
  state.reconnectingId = null;
  const result = await api("reconnect_entry", id, port);
  if (!result.ok) {
    const isMismatch = result.error && result.error.includes("different id");
    if (isMismatch) {
      const proceed = confirm(
        `${result.error}\n\n` +
          `Flash this entry's firmware onto that board instead and make it take over as this device? ` +
          `This overwrites its firmware and device id — this cannot be undone. Are you sure?`
      );
      if (proceed) {
        const forced = await api("force_adopt_port", id, port);
        if (!forced.ok) alert("Force reflash failed:\n\n" + forced.output);
      }
    } else {
      alert("Could not reconnect: " + result.error);
    }
  }
  await refreshEntries();
  if (state.selectedId === id) await selectEntry(id);
}

async function refreshEntries() {
  state.entries = await api("list_entries");
  renderSidebar();
  renderHeader();
  syncPinTest();
}

// ---- feature toggles + tab bar ----------------------------------------------
// One place to turn Sequencer/Drone/FX Rack/Joystick on or off per entry.
// Table/Board are always available; the other four tabs only appear in the
// tab bar while their feature is enabled here.

const TAB_DEFS = [
  { key: "table", label: "Table", viewId: "tableView", always: true },
  { key: "board", label: "Board", viewId: "boardView", always: true },
  { key: "sequencer", label: "Sequencer", viewId: "sequencerView", stateKey: "sequencer" },
  { key: "drone", label: "Drone", viewId: "droneView", stateKey: "drone" },
  { key: "fx_rack", label: "FX Rack", viewId: "fxRackView", stateKey: "fxRack" },
  { key: "joystick", label: "Joystick", viewId: "joystickView", stateKey: "joystick" },
  { key: "test", label: "Test", viewId: "testView", deviceOnly: true },
];

function setFeatureEnabled(stateKey, value) {
  state[stateKey].enabled = value;
  scheduleAutoSave();
  renderAll();
}

function buildFeatureToggles() {
  const wrap = document.createElement("div");
  wrap.className = "feature-toggles";
  [
    ["sequencer", "Sequencer"],
    ["drone", "Drone"],
    ["fxRack", "FX Rack"],
    ["joystick", "Joystick"],
  ].forEach(([stateKey, label]) => {
    const lbl = document.createElement("label");
    lbl.className = "checkbox-label feature-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!(state[stateKey] && state[stateKey].enabled);
    input.addEventListener("change", () => setFeatureEnabled(stateKey, input.checked));
    const text = document.createElement("span");
    text.textContent = label;
    lbl.appendChild(input);
    lbl.appendChild(text);
    wrap.appendChild(lbl);
  });
  return wrap;
}

// Rebuilds the tab bar from TAB_DEFS (only showing tabs whose feature is
// enabled, always showing Table/Board), and syncs each view div's "active"
// class to state.view. Falls back to "table" if the currently active view's
// feature just got disabled.
function renderTabs() {
  const container = document.getElementById("viewTabs");
  if (!container) return;
  const entry = selectedEntry();
  const isDevice = !!entry && entry.entry_type === "device";
  const visible = TAB_DEFS.filter(
    (t) => t.always || (t.deviceOnly ? isDevice : state[t.stateKey] && state[t.stateKey].enabled)
  );
  if (!visible.some((t) => t.key === state.view)) {
    state.view = "table";
  }
  container.innerHTML = "";
  visible.forEach((def) => {
    const btn = document.createElement("button");
    btn.className = "tab" + (state.view === def.key ? " active" : "");
    btn.textContent = def.label;
    btn.addEventListener("click", () => {
      state.view = def.key;
      renderTabs();
    });
    container.appendChild(btn);
  });
  TAB_DEFS.forEach((def) => {
    const el = document.getElementById(def.viewId);
    if (el) el.classList.toggle("active", state.view === def.key);
  });
  syncPinTest();
}

// ---- header (per-entry actions) ---------------------------------------------

function renderHeader() {
  const el = document.getElementById("entryHeader");
  const prevApplySelect = el.querySelector(".eh-apply-select");
  const prevApplyValue = prevApplySelect ? prevApplySelect.value : null;
  el.innerHTML = "";
  const entry = selectedEntry();

  if (!entry) {
    el.textContent = state.selectedId === ALL_DEVICES_ID ? "Showing MIDI traffic from all devices" : "";
    return;
  }

  const name = document.createElement("span");
  name.className = "eh-name";
  name.textContent = entry.nickname;
  el.appendChild(name);

  el.appendChild(buildFeatureToggles());

  const resetBtn = document.createElement("button");
  resetBtn.textContent = "Reset profile";
  resetBtn.title = "Clear every pin role and sequencer setup back to blank";
  resetBtn.addEventListener("click", () => resetProfile(entry.id));
  el.appendChild(resetBtn);

  if (entry.entry_type === "device") {
    const badge = document.createElement("span");
    badge.className = "eh-badge " + (entry.connected ? "online" : "offline");
    badge.textContent = entry.connected ? "Online" : "Offline";
    el.appendChild(badge);

    const readBtn = document.createElement("button");
    readBtn.textContent = "Read from device";
    readBtn.disabled = !entry.connected;
    readBtn.addEventListener("click", readFromDevice);
    el.appendChild(readBtn);

    const writeBtn = document.createElement("button");
    writeBtn.id = "writeBtn";
    writeBtn.textContent = "Write to device";
    writeBtn.disabled = !entry.connected;
    writeBtn.className =
      entry.connected && entry.in_sync === false ? "out-of-sync" : "";
    writeBtn.addEventListener("click", writeToDevice);
    el.appendChild(writeBtn);

    const flashing = state.flashingId === entry.id;
    const flashBtn = document.createElement("button");
    flashBtn.textContent = flashing ? "Flashing…" : "Flash firmware";
    flashBtn.disabled = flashing || !entry.connected;
    flashBtn.addEventListener("click", flashFirmware);
    el.appendChild(flashBtn);
  } else {
    const applyLabel = document.createElement("label");
    applyLabel.className = "inline-label";
    applyLabel.textContent = "Apply to:";
    const select = document.createElement("select");
    select.className = "eh-apply-select";
    const devices = state.entries.filter((e) => e.entry_type === "device");
    if (devices.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "(no devices adopted yet)";
      select.appendChild(opt);
      select.disabled = true;
    } else {
      devices.forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.id;
        opt.textContent = d.nickname;
        select.appendChild(opt);
      });
      if (devices.some((d) => d.id === prevApplyValue)) select.value = prevApplyValue;
    }
    applyLabel.appendChild(select);
    el.appendChild(applyLabel);

    const applyBtn = document.createElement("button");
    applyBtn.textContent = "Apply";
    applyBtn.disabled = devices.length === 0;
    applyBtn.addEventListener("click", () => applyDraftToDevice(entry.id, select.value));
    el.appendChild(applyBtn);
  }
}

async function applyDraftToDevice(draftId, deviceId) {
  if (!deviceId) return;
  await api("apply_draft", draftId, deviceId);
  await refreshEntries();
}

// ---- entry selection / editing buffer ---------------------------------------

async function selectEntry(id) {
  state.selectedId = id;
  state.selectedPin = null;
  state.sequencerLive = null;
  state.droneLive = null;
  state.fxRackLive = null;
  if (id === ALL_DEVICES_ID) {
    state.pins = [];
    state.sequencer = { channel: 0, subdivision: "16th", enabled: true, steps: [] };
    state.drone = { enabled: false, channel: 0, segments: [] };
    state.fxRack = { enabled: false, channel: 0, segments: [] };
    state.joystick = { enabled: false, x_pin: null, y_pin: null, z_pin: null, button_pin: null };
  } else {
    const dict = await api("get_entry", id);
    state.pins = dict.pins;
    state.sequencer = dict.sequencer;
    state.drone = dict.drone;
    state.fxRack = dict.fx_rack;
    state.joystick = dict.joystick;
  }
  renderAll();
}

async function readFromDevice() {
  const id = state.selectedId;
  const dict = await api("read_device_config", id);
  state.pinTest = blankPinTest(); // the read ended any test session; syncPinTest() restarts it if still on the tab
  await refreshEntries();
  if (!dict) {
    alert("Failed to read config from device.");
    return;
  }
  if (state.selectedId === id) {
    state.pins = dict.pins;
    state.sequencer = dict.sequencer;
    state.drone = dict.drone;
    state.fxRack = dict.fx_rack;
    state.joystick = dict.joystick;
    state.selectedPin = null;
    renderAll();
  }
}

async function resetProfile(id) {
  if (
    !confirm(
      "Reset this profile? Every pin role and feature setup (sequencer/drone/FX rack/joystick) are cleared back to blank. This cannot be undone (unless you still have it on the device to read back). Are you sure?"
    )
  )
    return;
  const dict = await api("reset_entry_pins", id);
  await refreshEntries();
  if (state.selectedId === id) {
    state.pins = dict.pins;
    state.sequencer = dict.sequencer;
    state.drone = dict.drone;
    state.fxRack = dict.fx_rack;
    state.joystick = dict.joystick;
    state.selectedPin = null;
    renderAll();
  }
}

async function writeToDevice() {
  const id = state.selectedId;
  const ok = await api("write_device_config", id);
  state.pinTest = blankPinTest(); // see readFromDevice()
  await refreshEntries();
  if (!ok) alert("Write failed (version/checksum mismatch?).");
}

async function flashFirmware() {
  const id = state.selectedId;
  if (state.flashingId) return; // already mid-flash somewhere -- ignore a stray double-click
  if (
    !confirm(
      "Flash firmware to this device, then reconnect it and write its current config back? This takes a few seconds."
    )
  )
    return;
  state.flashingId = id;
  renderAll();
  try {
    const result = await api("flash_firmware", id);
    await refreshEntries();
    if (!result.ok) {
      alert("Flash failed:\n\n" + result.output);
    } else if (result.reconnect_error) {
      alert("Flash succeeded, but " + result.reconnect_error + ".");
    } else {
      alert("Flash succeeded — config written to the device.");
    }
  } finally {
    state.flashingId = null;
    state.pinTest = blankPinTest(); // see readFromDevice()
    renderAll();
  }
}

// ---- monitor ---------------------------------------------------------------

const MAX_MONITOR_ROWS = 500;

function appendMonitorRow(evt) {
  const log = document.getElementById("monitorLog");
  const row = document.createElement("div");
  row.className = "mline";

  const t = document.createElement("span");
  t.className = "t";
  t.textContent = new Date().toLocaleTimeString();

  const dir = document.createElement("span");
  dir.className = "dir " + evt.direction;
  dir.textContent = evt.direction === "in" ? "←" : "→";

  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = evt.kind === "midi" ? "MIDI" : "CTRL";

  const dev = document.createElement("span");
  dev.className = "dev";
  dev.textContent = evt.device_id ? `Device ${evt.device_id}` : "?";

  const decoded = document.createElement("span");
  decoded.className = "decoded";
  decoded.textContent = evt.decoded || "";

  const raw = document.createElement("span");
  raw.className = "raw";
  raw.textContent = evt.raw
    .map((b) => b.toString(16).padStart(2, "0"))
    .join(" ");

  row.appendChild(t);
  row.appendChild(dir);
  row.appendChild(kind);
  row.appendChild(dev);
  row.appendChild(decoded);
  row.appendChild(raw);
  log.appendChild(row);

  state.monitorRows++;
  if (state.monitorRows > MAX_MONITOR_ROWS) {
    log.removeChild(log.firstChild);
    state.monitorRows--;
  }
  log.scrollTop = log.scrollHeight;
}

function flashPin(pin, value) {
  const dot = document.getElementById(`dot-${pin}`);
  const box = document.getElementById(`pinbox-${pin}`);
  [dot, box].forEach((el) => {
    if (!el) return;
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 150);
  });
}

// ---- top-level render ------------------------------------------------------

function renderAll() {
  const entry = selectedEntry();
  const hasConfig = !!entry;
  document.getElementById("main").hidden = !hasConfig;
  document.getElementById("mainEmpty").hidden = hasConfig;
  if (hasConfig) {
    renderTable();
    renderBoard();
    renderEditor();
    renderSequencerTab();
    renderDroneTab();
    renderFxRackTab();
    renderJoystickTab();
    renderTestTab();
  }
  renderTabs();
  renderSidebar();
  renderHeader();
}

// ---- wiring -----------------------------------------------------------------

function initEventListeners() {
  document.getElementById("addDeviceBtn").addEventListener("click", toggleAddDevicePicker);
  document.getElementById("addDraftBtn").addEventListener("click", () => createDraft());

  document.getElementById("clearMonitorBtn").addEventListener("click", () => {
    document.getElementById("monitorLog").innerHTML = "";
    state.monitorRows = 0;
  });

  window.addEventListener("monitor_event", (e) => {
    const evt = e.detail;
    if (state.selectedId !== ALL_DEVICES_ID) {
      const entry = selectedEntry();
      if (!entry || entry.entry_type !== "device" || evt.device_id !== entry.device_id) return;
    }
    appendMonitorRow(evt);
  });

  window.addEventListener("pin_activity", (e) => {
    const entry = selectedEntry();
    if (!entry || entry.entry_type !== "device" || e.detail.device_id !== entry.device_id) return;
    e.detail.pins.forEach(({ pin, value }) => {
      flashPin(pin, value);
      updateJoystickVisual(pin, value);
    });
  });

  window.addEventListener("sequencer_state", (e) => {
    const entry = selectedEntry();
    if (!entry || entry.entry_type !== "device" || e.detail.device_id !== entry.device_id) return;
    state.sequencerLive = e.detail;
    state.sequencerLiveAt = Date.now();
    renderSequencerSteps();
    updateSeqClockStatus();
  });

  window.addEventListener("drone_state", (e) => {
    const entry = selectedEntry();
    if (!entry || entry.entry_type !== "device" || e.detail.device_id !== entry.device_id) return;
    state.droneLive = e.detail;
    renderDroneSegments();
  });

  window.addEventListener("pin_test_state", (e) => {
    const entry = selectedEntry();
    const pt = state.pinTest;
    if (!entry || pt.status !== "active" || pt.entryId !== entry.id || e.detail.device_id !== entry.device_id) return;
    pt.live = e.detail;
    updateTestLive();
  });

  window.addEventListener("fx_state", (e) => {
    const entry = selectedEntry();
    if (!entry || entry.entry_type !== "device" || e.detail.device_id !== entry.device_id) return;
    state.fxRackLive = e.detail;
    renderFxRackSegments();
  });
}

async function init() {
  initEventListeners();
  await refreshEntries();
  const first = sortedEntries()[0];
  await selectEntry(first ? first.id : ALL_DEVICES_ID);
  setInterval(refreshEntries, 1500);
  setInterval(updateSeqClockStatus, 500); // catches the clock going quiet, which sends no event
}

window.addEventListener("pywebviewready", init);
