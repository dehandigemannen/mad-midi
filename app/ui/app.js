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
  pins: [], // array of PIN_COUNT pin config objects — one shared editing buffer
  presetName: "untitled",
  selectedPin: null,
  devices: {}, // port -> status dict from list_devices()
  targetPort: null, // which connected device Read/Write/Flash/Assign-ID act on
  view: "table",
  monitorRows: 0,
};

function blankPins() {
  const pins = [];
  for (let i = 0; i < PIN_COUNT; i++) {
    pins.push({
      pin: i,
      role: "unused",
      channel: 0,
      number: 0,
      in_min: 0,
      in_max: 0,
      out_min: 0,
      out_max: 0,
      invert: false,
      toggle: false,
      match_cc: false,
    });
  }
  return pins;
}

function pinByIndex(i) {
  return state.pins[i];
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

// ---- top-level render ------------------------------------------------------

function renderAll() {
  renderTable();
  renderBoard();
  renderEditor();
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
  dev.textContent = evt.device_id ? `Device ${evt.device_id}` : evt.port;

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

// ---- connection / presets / device actions ---------------------------------

async function refreshPorts() {
  const select = document.getElementById("portSelect");
  const ports = await api("list_ports");
  const current = select.value;
  select.innerHTML = "";
  ports.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    select.appendChild(opt);
  });
  if (ports.includes(current)) select.value = current;
}

async function refreshPresets() {
  const select = document.getElementById("presetSelect");
  const presets = await api("list_presets");
  select.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "(load preset...)";
  select.appendChild(blank);
  presets.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
  });
}

function deviceLabel(status) {
  return status.device_id ? `Device ${status.device_id}` : `Unassigned`;
}

async function refreshDevices() {
  const list = await api("list_devices");
  state.devices = {};
  list.forEach((d) => {
    state.devices[d.port] = d;
  });

  if (!state.targetPort || !state.devices[state.targetPort]) {
    const ports = Object.keys(state.devices);
    state.targetPort = ports.length ? ports[0] : null;
  }
  renderDeviceBar();
}

function renderDeviceBar() {
  const list = document.getElementById("deviceList");
  list.innerHTML = "";
  const ports = Object.keys(state.devices);

  if (ports.length === 0) {
    const empty = document.createElement("div");
    empty.className = "device-empty";
    empty.textContent =
      "No devices connected — pick a port above and click Connect.";
    list.appendChild(empty);
    return;
  }

  ports.forEach((port) => {
    const d = state.devices[port];
    const chip = document.createElement("div");
    chip.className =
      "device-chip" + (port === state.targetPort ? " target" : "");

    const name = document.createElement("span");
    name.className = "dc-name";
    name.textContent = deviceLabel(d);
    chip.appendChild(name);

    const mode = document.createElement("span");
    mode.className = "dc-mode " + (d.mode === "config" ? "config" : "run");
    mode.textContent = d.mode || "run";
    chip.appendChild(mode);

    const portLabel = document.createElement("span");
    portLabel.className = "dc-port";
    portLabel.textContent = port;
    chip.appendChild(portLabel);

    chip.addEventListener("click", (e) => {
      if (e.target.closest("button, input")) return;
      state.targetPort = port;
      renderDeviceBar();
    });

    if (!d.device_id) {
      const idLabel = document.createElement("label");
      idLabel.className = "inline-label";
      idLabel.textContent = "New ID:";
      const idInput = document.createElement("input");
      idInput.type = "number";
      idInput.min = 1;
      idInput.max = 255;
      idInput.title = "Device ID to assign (1-255)";
      idLabel.appendChild(idInput);
      const assignBtn = document.createElement("button");
      assignBtn.textContent = "Assign ID";
      assignBtn.addEventListener("click", async () => {
        const id = parseInt(idInput.value, 10);
        if (!id || id < 1 || id > 255) return;
        const ok = await api("assign_device_id", port, id);
        if (!ok) alert("Failed to assign device id.");
        await refreshDevices();
      });
      chip.appendChild(idLabel);
      chip.appendChild(assignBtn);
    }

    const disconnectBtn = document.createElement("button");
    disconnectBtn.textContent = "✕";
    disconnectBtn.title = "Disconnect";
    disconnectBtn.addEventListener("click", async () => {
      await api("disconnect", port);
      await refreshDevices();
    });
    chip.appendChild(disconnectBtn);

    list.appendChild(chip);
  });
}

async function connectDevice() {
  const port = document.getElementById("portSelect").value;
  if (!port) return;
  await api("connect", port);
  await refreshDevices();
}

function pinsToDict() {
  return { name: state.presetName, pins: state.pins.map((p) => ({ ...p })) };
}

function loadPinsFromDict(dict) {
  state.presetName = dict.name;
  state.pins = dict.pins;
  document.getElementById("presetNameInput").value = state.presetName;
  state.selectedPin = null;
  renderAll();
}

function requireTarget() {
  if (!state.targetPort) {
    alert(
      "No target device selected — connect a Mega and select it in the device list first.",
    );
    return null;
  }
  return state.targetPort;
}

async function readFromDevice() {
  const port = requireTarget();
  if (!port) return;
  const ok = await api("enter_config", port);
  if (!ok) {
    alert("Could not enter config mode — is the device connected?");
    return;
  }
  const dict = await api("read_device_config", port);
  await api("resume_run", port);
  await refreshDevices();
  if (dict) loadPinsFromDict(dict);
  else alert("Failed to read config from device.");
}

async function writeToDevice() {
  const port = requireTarget();
  if (!port) return;
  const ok = await api("enter_config", port);
  if (!ok) {
    alert("Could not enter config mode — is the device connected?");
    return;
  }
  const success = await api("write_device_config", port, pinsToDict());
  await api("resume_run", port);
  await refreshDevices();
  alert(
    success
      ? "Config written to device."
      : "Write failed (version/checksum mismatch?).",
  );
}

async function flashFirmware() {
  const port = requireTarget();
  if (!port) return;
  if (
    !confirm(`Flash firmware to the Mega on ${port}? This takes a few seconds.`)
  )
    return;
  const result = await api("flash_firmware", port);
  alert(result.ok ? "Flash succeeded." : "Flash failed:\n\n" + result.output);
}

// ---- wiring -----------------------------------------------------------------

function initEventListeners() {
  document
    .getElementById("refreshPortsBtn")
    .addEventListener("click", refreshPorts);
  document
    .getElementById("connectBtn")
    .addEventListener("click", connectDevice);
  document
    .getElementById("readConfigBtn")
    .addEventListener("click", readFromDevice);
  document
    .getElementById("writeConfigBtn")
    .addEventListener("click", writeToDevice);
  document.getElementById("flashBtn").addEventListener("click", flashFirmware);
  document.getElementById("clearMonitorBtn").addEventListener("click", () => {
    document.getElementById("monitorLog").innerHTML = "";
    state.monitorRows = 0;
  });

  document.getElementById("newPresetBtn").addEventListener("click", () => {
    const name =
      document.getElementById("presetNameInput").value.trim() || "untitled";
    loadPinsFromDict({ name, pins: blankPins() });
  });

  document
    .getElementById("savePresetBtn")
    .addEventListener("click", async () => {
      state.presetName =
        document.getElementById("presetNameInput").value.trim() || "untitled";
      await api("save_preset", pinsToDict());
      await refreshPresets();
    });

  document
    .getElementById("presetSelect")
    .addEventListener("change", async (e) => {
      if (!e.target.value) return;
      const dict = await api("load_preset", e.target.value);
      loadPinsFromDict(dict);
      await refreshPresets();
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

  window.addEventListener("monitor_event", (e) => appendMonitorRow(e.detail));
  window.addEventListener("pin_activity", (e) => {
    // Only reflect activity from whichever device is currently selected as
    // the edit target, so pins from an unrelated second device don't flash.
    if (e.detail.port !== state.targetPort) return;
    e.detail.pins.forEach(({ pin, value }) => flashPin(pin, value));
  });
}

async function init() {
  initEventListeners();
  state.pins = blankPins();
  document.getElementById("presetNameInput").value = state.presetName;
  renderAll();

  await refreshPorts();
  await refreshPresets();
  await refreshDevices();
  setInterval(refreshDevices, 1500);
}

window.addEventListener("pywebviewready", init);
