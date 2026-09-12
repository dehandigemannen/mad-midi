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
  if (p.role === "unused" || p.role === "encoder_b") return "";
  return usesNoteName(p) ? `${noteNumberToName(p.number)} (${p.number})` : String(p.number);
}

function isAnalogPin(pin) {
  return pin >= 54 && pin < PIN_COUNT;
}

function allowedRoles(pin) {
  if (RESERVED_PINS.has(pin)) return [];
  if (isAnalogPin(pin)) return ["unused", "pot_cc"];
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
  if (!state.selectedId || state.selectedId === ALL_DEVICES_ID) return;
  const id = state.selectedId;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const pinsSnapshot = state.pins.map((p) => ({ ...p }));
    const status = await api("update_entry_pins", id, pinsSnapshot);
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
        p.role === "unused" || p.role === "encoder_b"
          ? ""
          : String(p.channel + 1),
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

function buildPortPicker(ports, onPick) {
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
    const btn = document.createElement("button");
    btn.textContent = port;
    btn.addEventListener("click", () => onPick(port));
    wrap.appendChild(btn);
  });
  return wrap;
}

function renderAddDevicePicker() {
  const container = document.getElementById("addDevicePicker");
  container.innerHTML = "";
  container.hidden = state.addDevicePorts === null;
  if (state.addDevicePorts === null) return;
  container.appendChild(buildPortPicker(state.addDevicePorts, adoptPort));
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
  if (entry) await selectEntry(entry.id);
}

async function createDraft() {
  const name = prompt("Draft name:", "Untitled draft");
  if (name === null) return;
  const entry = await api("create_draft", name);
  await refreshEntries();
  await selectEntry(entry.id);
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

    const flashBtn = document.createElement("button");
    flashBtn.textContent = "Flash firmware";
    flashBtn.disabled = !entry.connected;
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
  if (id === ALL_DEVICES_ID) {
    state.pins = [];
  } else {
    const dict = await api("get_entry", id);
    state.pins = dict.pins;
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
  if (!confirm("Flash firmware to this device? This takes a few seconds.")) return;
  const result = await api("flash_firmware", id);
  alert(result.ok ? "Flash succeeded." : "Flash failed:\n\n" + result.output);
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
}

async function init() {
  initEventListeners();
  await refreshEntries();
  const first = sortedEntries()[0];
  await selectEntry(first ? first.id : ALL_DEVICES_ID);
  setInterval(refreshEntries, 1500);
}

window.addEventListener("pywebviewready", init);
