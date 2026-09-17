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
  encoder_a: ["channel", "number"], // shift fields are rendered separately, see renderEditor
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
  sequencerLive: null, // {device_id, running, current_step, step_enabled} pushed from a connected device, or null
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
  // Saves pins and sequencer together in one call — they used to be two
  // independent debounced saves and could race each other into corrupting
  // the file when both fired ~400ms apart (see update_entry_pins).
  if (!state.selectedId || state.selectedId === ALL_DEVICES_ID) return;
  const id = state.selectedId;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const pinsSnapshot = state.pins.map((p) => ({ ...p }));
    const sequencerSnapshot = JSON.parse(JSON.stringify(state.sequencer));
    const status = await api("update_entry_pins", id, pinsSnapshot, sequencerSnapshot);
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

// ---- sequencer: pin assignment (mirrors setPinRole's cleanup-then-assign) --

function blankSequencerStep(index) {
  return {
    note: 60 + index, // auto-assigned, still freely editable via the Note dropdown
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

// Releases a pin previously used by a sequencer function back to "unused",
// undoing any encoder-pairing/shift-linkage it was part of.
function clearSequencerPin(pin) {
  const p = pinByIndex(pin);
  if (p.role === "encoder_a" && pin + 1 < PIN_COUNT) {
    const partner = pinByIndex(pin + 1);
    if (partner.role === "encoder_b") partner.role = "unused";
  }
  if (p.role === "button_shift") {
    state.pins.forEach((pp) => {
      if (pp.role === "encoder_a" && pp.in_min === pin) pp.in_min = NO_SHIFT_PIN;
    });
  }
  p.role = "unused";
}

function assignSequencerPin(stepIndex, fn, pin) {
  const step = state.sequencer.steps[stepIndex];
  const field = fn + "_pin";
  const prevPin = step[field];
  if (prevPin !== null && prevPin !== undefined && prevPin !== pin) {
    clearSequencerPin(prevPin);
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
    } else if (fn === "shift") {
      p.role = "button_shift";
      if (step.encoder_pin !== null && step.encoder_pin !== undefined) {
        const enc = pinByIndex(step.encoder_pin);
        enc.in_min = pin;
        enc.out_min = ch;
        enc.out_max = SEQ_BASE.encoder_shifted + stepIndex;
      }
    } else if (fn === "step_light") {
      p.role = "light_digital";
      p.match_cc = true;
      p.channel = ch;
      p.number = SEQ_BASE.step_light + stepIndex;
    } else if (fn === "pwm") {
      // Value meter: mirrors the encoder's live CC output as PWM brightness.
      p.role = "light_pwm";
      p.match_cc = true;
      p.channel = ch;
      p.number = SEQ_BASE.pwm + stepIndex;
      p.in_min = 0;
      p.in_max = 127;
      p.out_min = 0;
      p.out_max = 255;
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
        if (pin !== null && pin !== undefined) clearSequencerPin(pin);
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
      if (enc.in_min !== NO_SHIFT_PIN) enc.out_min = value;
      if (step.encoder_pin + 1 < PIN_COUNT) pinByIndex(step.encoder_pin + 1).channel = value;
    }
  });
  scheduleAutoSave();
  renderAll();
}

function setSequencerSubdivision(value) {
  state.sequencer.subdivision = value;
  scheduleAutoSave();
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
    hint.textContent = `Uses this pin + pin ${pin + 1} (A/B quadrature). Fixed 1:1, relative CC (1=CW, 127=CCW).`;
    fields.appendChild(hint);
  }
  if (p.role === "button_shift") {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent =
      "Pure modifier — sends no MIDI itself. Assign it as an encoder's shift button below.";
    fields.appendChild(hint);
  }
  ROLE_FIELDS[p.role].forEach((field) => {
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

  if (p.role === "encoder_a") renderEncoderShiftFields(pin, p, fields);
}

function renderNumberField(pin, p, fields) {
  const isDualRole = p.role === "light_digital" || p.role === "light_pwm";

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

function renderEncoderShiftFields(pin, p, fields) {
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
  select.value = validIds.has(p.in_min)
    ? String(p.in_min)
    : String(NO_SHIFT_PIN);

  select.addEventListener("change", () => {
    setPinField(pin, "in_min", parseInt(select.value, 10));
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
    chInput.value = p.out_min + 1;
    chInput.addEventListener("change", () => {
      const v = Math.max(1, Math.min(16, parseInt(chInput.value, 10) || 1)) - 1;
      setPinField(pin, "out_min", v);
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
    ccInput.value = p.out_max;
    ccInput.addEventListener("change", () => {
      const v = Math.max(0, Math.min(127, parseInt(ccInput.value, 10) || 0));
      setPinField(pin, "out_max", v);
    });
    ccRow.appendChild(ccLabel);
    ccRow.appendChild(ccInput);
    fields.appendChild(ccRow);
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
}

function buildSequencerPinSelect(stepIndex, fn, currentPin) {
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  const label = document.createElement("label");
  label.textContent = SEQ_FN_LABELS[fn];
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

// Makes the encoder/shift link (set automatically by assignSequencerPin,
// whichever order the two are assigned in) visible — otherwise there's no
// way to tell from this tab alone that turning the encoder while holding
// shift actually sends a different CC.
function buildEncoderShiftLinkHint(step) {
  const hint = document.createElement("div");
  hint.className = "field-hint sequencer-link-hint";
  const hasEncoder = step.encoder_pin !== null && step.encoder_pin !== undefined;
  const hasShift = step.shift_pin !== null && step.shift_pin !== undefined;

  if (hasEncoder && hasShift) {
    const enc = pinByIndex(step.encoder_pin);
    hint.textContent = `✓ Shift linked — plain turn sends CC ${enc.number}, shift+turn sends CC ${enc.out_max} (both on channel ${enc.channel + 1})`;
  } else if (hasEncoder && !hasShift) {
    hint.textContent = "No shift assigned — this encoder always sends its plain CC.";
  } else if (!hasEncoder && hasShift) {
    hint.textContent = "This shift button isn't linked to anything yet — assign this step's encoder to link it.";
  } else {
    hint.hidden = true;
  }
  return hint;
}

function renderSequencerSteps() {
  const container = document.getElementById("sequencerSteps");
  container.innerHTML = "";
  const live = state.sequencerLive;
  const entry = selectedEntry();
  const canToggleLive = !!(entry && entry.entry_type === "device" && entry.connected && live);

  state.sequencer.steps.forEach((step, i) => {
    const row = document.createElement("div");
    row.className = "sequencer-step";
    if (live && live.running && live.current_step === i) row.classList.add("playhead");

    const heading = document.createElement("div");
    heading.className = "sequencer-step-heading";
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

    row.appendChild(buildSequencerPinSelect(i, "button", step.button_pin));
    row.appendChild(buildSequencerPinSelect(i, "encoder", step.encoder_pin));
    row.appendChild(buildSequencerPinSelect(i, "shift", step.shift_pin));
    row.appendChild(buildSequencerPinSelect(i, "pwm", step.pwm_pin));
    row.appendChild(buildSequencerPinSelect(i, "step_light", step.step_light_pin));
    row.appendChild(buildSequencerPinSelect(i, "enabled_light", step.enabled_light_pin));
    row.appendChild(buildEncoderShiftLinkHint(step));

    container.appendChild(row);
  });
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
  if (!result.ok) alert("Could not reconnect: " + result.error);
  await refreshEntries();
  if (state.selectedId === id) await selectEntry(id);
}

async function refreshEntries() {
  state.entries = await api("list_entries");
  renderSidebar();
  renderHeader();
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
  if (id === ALL_DEVICES_ID) {
    state.pins = [];
    state.sequencer = { channel: 0, subdivision: "16th", steps: [] };
  } else {
    const dict = await api("get_entry", id);
    state.pins = dict.pins;
    state.sequencer = dict.sequencer;
  }
  renderAll();
}

async function readFromDevice() {
  const id = state.selectedId;
  const dict = await api("read_device_config", id);
  await refreshEntries();
  if (!dict) {
    alert("Failed to read config from device.");
    return;
  }
  if (state.selectedId === id) {
    state.pins = dict.pins;
    state.sequencer = dict.sequencer;
    state.selectedPin = null;
    renderAll();
  }
}

async function writeToDevice() {
  const id = state.selectedId;
  const ok = await api("write_device_config", id);
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
  }
  renderSidebar();
  renderHeader();
}

// ---- wiring -----------------------------------------------------------------

function initEventListeners() {
  document.getElementById("addDeviceBtn").addEventListener("click", toggleAddDevicePicker);
  document.getElementById("addDraftBtn").addEventListener("click", createDraft);

  document.getElementById("clearMonitorBtn").addEventListener("click", () => {
    document.getElementById("monitorLog").innerHTML = "";
    state.monitorRows = 0;
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.view = tab.dataset.view;
      document
        .querySelectorAll(".tab")
        .forEach((t) => t.classList.toggle("active", t === tab));
      document
        .getElementById("tableView")
        .classList.toggle("active", state.view === "table");
      document
        .getElementById("boardView")
        .classList.toggle("active", state.view === "board");
      document
        .getElementById("sequencerView")
        .classList.toggle("active", state.view === "sequencer");
    });
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
    e.detail.pins.forEach(({ pin, value }) => flashPin(pin, value));
  });

  window.addEventListener("sequencer_state", (e) => {
    const entry = selectedEntry();
    if (!entry || entry.entry_type !== "device" || e.detail.device_id !== entry.device_id) return;
    state.sequencerLive = e.detail;
    renderSequencerSteps();
  });
}

async function init() {
  initEventListeners();
  await refreshEntries();
  const first = sortedEntries()[0];
  await selectEntry(first ? first.id : ALL_DEVICES_ID);
  setInterval(refreshEntries, 1500);
}

window.addEventListener("pywebviewready", init);
