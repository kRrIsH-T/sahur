// Sahur dashboard: talks to bolos/dashboard/server.py on this computer only.
"use strict";

const STATE = {
  ready: ["Ready", "#8f96a1"], listening: ["Listening", "#4cc2f5"], hearing: ["Hearing you", "#37d1a0"],
  transcribing: ["Writing it down", "#a48bfa"], understanding: ["Understanding", "#a48bfa"],
  thinking: ["Thinking", "#f2b64a"], confirming: ["Waiting for your yes", "#ff9d5c"],
  guiding: ["Showing you on screen", "#ff8a1f"], executing: ["Doing it", "#5ea8ff"], done: ["Done", "#4bd583"],
  error: ["Something went wrong", "#f47272"], warning: ["Needs a look", "#f2b64a"],
  microphone_unavailable: ["Check your microphone", "#f47272"],
  shortcut_unavailable: ["Check your keyboard shortcut", "#f47272"],
};
const BUSY = new Set(["listening", "hearing", "transcribing", "understanding", "thinking", "executing", "guiding"]);
const LANGUAGES = { hi: "Hindi", en: "English", kn: "Kannada", ta: "Tamil", te: "Telugu", mr: "Marathi",
  ml: "Malayalam", bn: "Bengali", gu: "Gujarati", pa: "Punjabi" };
const ACTIONS = { open_app: "Open an app", web_search: "Search the web", open_website: "Open a website",
  find_file: "Find or open a file", open_recent_download: "Open a recent download",
  read_screen: "Read the screen", fill_form: "Fill a form", play_media: "Play music or video",
  send_message: "Send a message", show_how: "Show how", multi: "Several steps", type_text: "Type text",
  answer: "Answer a question", media_control: "Control music or video", click: "Click a button",
  "not understood": "Not understood" };
const ICONS = { ok: "✓", warn: "!", fail: "×" };
const BARS = 44;
const GROUP_NAMES = { Sound: "Sound", Widget: "On screen", Voice: "Voice and language", Privacy: "Privacy",
  Brain: "Advanced" };

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
};

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Bolos": "1" },
  };
  let response;
  try {
    response = await fetch(path, { ...options, signal: body === undefined ? AbortSignal.timeout(4000) : undefined });
  } catch (error) {
    throw new Error("Couldn't reach Sahur. Open Sahur and try again.", { cause: error });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Sahur couldn't finish this. Try again.");
  return data;
}

let toastTimer;
function toast(text, bad = false) {
  const node = $("toast");
  node.textContent = text;
  node.className = bad ? "toast bad" : "toast";
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, bad ? 6000 : 2400);
}

// ------------------------------------------------------------------------------------------------------------
// Live state, checks, banners

let current = null;  // the last /api/state
let stateRequest = 0;
const controls = {};  // option key -> { row, sync(value) }

function showState(s) {
  const pill = $("state");
  let label, colour, busy = false;
  if (!s.running) {
    [label, colour] = ["Sahur is closed", "#f47272"];
  } else {
    [label, colour] = STATE[s.live.state] || ["Sahur is open", "#8f96a1"];
    busy = BUSY.has(s.live.state);
    if (s.dry_run) label += " (practice mode)";
  }
  $("state-text").textContent = label;
  pill.querySelector(".dot").style.background = colour;
  pill.classList.toggle("busy", busy);
}

function showChecks(s) {
  const blocked = s.checks.some((c) => c.level === "fail" && ["mic_error", "hotkey_error", "gnani"].includes(c.id));
  const hint = $("speak-hint");
  hint.hidden = !s.voice_commands || blocked;
  hint.textContent = `Tap ${pretty(s.values.hotkey)}, wait for “Listening”, then say what you want Sahur to do.`;
  $("retry-mic").hidden = !s.checks.some((c) => c.id === "mic_error");
  const list = $("checks");
  list.replaceChildren(...s.checks.map((c) => el("li", { class: c.level },
    el("span", { class: "icon", "aria-hidden": "true", text: ICONS[c.level] }),
    el("strong", { text: c.label }),
    el("small", { text: c.detail }))));
  const heard = s.live.heard;
  $("last").hidden = !heard;
  $("last-heard").textContent = heard || "";
}

function showBanners(s) {
  const restart = $("restart");
  const names = s.restart_needed.map((k) => s.options.find((o) => o.key === k)?.label).filter(Boolean);
  restart.hidden = names.length === 0;
  restart.textContent = names.length
    ? `Close and reopen Sahur to use the new settings for ${names.join(", ")}.` : "";
  $("settings-note").textContent = s.running
    ? "Changes are saved as you make them. Settings marked “After restart” take effect when you reopen Sahur."
    : "Changes are saved now and will take effect when you open Sahur.";
  const hello = $("test-welcome");
  hello.disabled = !s.buddy_available;
  hello.title = s.buddy_available ? "" : "Open Sahur with its desktop companion to preview it.";
}

function deviceLabel(kind, name) {
  if (name) return name;
  const fallback = current?.devices[kind].find((d) => d.default);
  return fallback ? `${fallback.name} (Windows default)` : "Windows default";
}

function showDevices(s) {
  $("mic-name").textContent = `Microphone: ${deviceLabel("input", s.values.input_device)}`;
  $("spk-name").textContent = `Speaker: ${deviceLabel("output", s.values.output_device)}`;
}

async function refreshState(forceKey) {
  const request = ++stateRequest;
  try {
    const s = await api("/api/state");
    if (request !== stateRequest) return;
    current = s;
    $("offline").hidden = true;
    showState(s);
    showChecks(s);
    showBanners(s);
    showDevices(s);
    if (!$("groups").childElementCount) buildSettings(s);
    else for (const o of s.options) {
      controls[o.key]?.sync(s.values[o.key], forceKey === o.key || forceKey === "all");
    }
  } catch (error) {
    if (request === stateRequest) $("offline").hidden = false;
  }
}

// ------------------------------------------------------------------------------------------------------------
// The microphone meter: a scrolling history of levels, like the status card's

const meterBars = [];
const levels = new Array(BARS).fill(0);
function buildMeter() {
  const meter = $("meter");
  for (let i = 0; i < BARS; i++) meterBars.push(meter.appendChild(el("span")));
}
function drawMeter(level, quiet) {
  levels.shift();
  levels.push(level);
  levels.forEach((value, i) => {
    const bar = meterBars[i];
    bar.style.height = `${Math.max(6, Math.round(Math.min(1, value) * 100))}%`;
    bar.classList.toggle("on", value > 0.04);
  });
  $("meter").classList.toggle("quiet", quiet);
  $("meter").setAttribute("aria-valuenow", String(Math.round(level * 100)));
}

let meterTimer;
let meterPending = false;
async function pollLevel() {
  clearTimeout(meterTimer);
  if (document.hidden || meterPending) return;
  meterPending = true;
  try {
    const data = await api("/api/level");
    // While Bol OS talks it ignores the microphone, so the bars go grey rather than looking broken.
    const speaking = data.state === "executing" || data.state === "done";
    drawMeter(data.level || 0, speaking);
    const hint = data.error || "Say something. The bars follow your voice.";
    if ($("meter-hint").textContent !== hint) $("meter-hint").textContent = hint;
  } catch (error) {
    drawMeter(0, true);
    $("meter-hint").textContent = "Waiting to reconnect to Sahur.";
  } finally {
    meterPending = false;
    meterTimer = setTimeout(pollLevel, 100);
  }
}

// ------------------------------------------------------------------------------------------------------------
// Settings

async function change(key, value) {
  const control = controls[key];
  if (control.pending) return;
  control.pending = true;
  control.row.classList.remove("saved");
  control.row.setAttribute("aria-busy", "true");
  control.error.hidden = true;
  try {
    const { changed } = await api("/api/settings", { changes: { [key]: value } });
    if (key in changed) current.values[key] = changed[key];
    control.row.classList.add("saved");
    setTimeout(() => control.row.classList.remove("saved"), 1800);
  } catch (error) {
    control.error.textContent = error.message;
    control.error.hidden = false;
  } finally {
    control.sync(current.values[key], true);
    control.row.removeAttribute("aria-busy");
    control.pending = false;
  }
  await refreshState(key);
}

function toggleControl(option, id) {
  const button = el("button", { type: "button", class: "switch", role: "switch", id,
    onclick: () => change(option.key, button.getAttribute("aria-checked") !== "true") });
  return { node: button, sync: (value) => {
    button.setAttribute("aria-checked", String(Boolean(value)));
    // The wake word needs a trained model: without one it can only be switched off.
    button.disabled = option.key === "wake_enabled" && !value && current && !current.wake_model;
  } };
}

function segmentsControl(option) {
  const group = el("div", { class: "segments", role: "group", "aria-label": option.label });
  const buttons = Object.entries(option.choices).map(([value, text]) => group.appendChild(
    el("button", { type: "button", text, "data-value": value, onclick: () => change(option.key, value) })));
  return { node: group, sync: (value) => buttons.forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.value === value))) };
}

function selectControl(option, id) {
  const select = el("select", { id, onchange: () => change(option.key, select.value) });
  for (const [value, text] of Object.entries(option.choices)) select.append(el("option", { value, text }));
  return { node: select, sync: (value, force) => { if (force || document.activeElement !== select) select.value = value; } };
}

function chipsControl(option) {
  const group = el("div", { class: "chips", role: "group", "aria-label": option.label });
  let chosen = [];
  const buttons = Object.entries(option.choices).map(([value, text]) => group.appendChild(
    el("button", { type: "button", text, "data-value": value, onclick: () => {
      const next = chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value];
      if (!next.length) return toast("Keep at least one language", true);
      change(option.key, next);
    } })));
  return { node: group, sync: (value) => {
    chosen = value;
    buttons.forEach((b) => b.setAttribute("aria-pressed", String(value.includes(b.dataset.value))));
  } };
}

function deviceControl(option, id) {
  const kind = option.kind === "input_device" ? "input" : "output";
  const select = el("select", { id, onchange: () => change(option.key, select.value) });
  return { node: select, sync: (value, force) => {
    if (!force && document.activeElement === select) return;
    const listed = current.devices[kind];
    const defaultName = listed.find((d) => d.default)?.name;
    const entries = [["", defaultName ? `Windows default (${defaultName})` : "Windows default"],
      ...listed.map((d) => [d.name, d.name])];
    if (value && !listed.some((d) => d.name === value)) entries.push([value, `${value} (not connected)`]);
    select.replaceChildren(...entries.map(([v, text]) => el("option", { value: v, text })));
    select.value = value;
  } };
}

const KEY_NAMES = { Space: "space", Enter: "enter", Tab: "tab", Backspace: "backspace", Insert: "insert",
  Delete: "delete", Home: "home", End: "end", PageUp: "pageup", PageDown: "pagedown", ArrowLeft: "left",
  ArrowUp: "up", ArrowRight: "right", ArrowDown: "down" };
function keyName(event) {
  if (KEY_NAMES[event.code]) return KEY_NAMES[event.code];
  if (/^Key[A-Z]$/.test(event.code)) return event.code.slice(3).toLowerCase();
  if (/^Digit\d$/.test(event.code)) return event.code.slice(5);
  if (/^F\d{1,2}$/.test(event.code)) return event.code.toLowerCase();
  return null;
}
function pretty(hotkey) {
  return hotkey.split("+").map((p) => (p.length <= 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1))).join(" + ");
}

function hotkeyControl(option, id) {
  let saved = "";
  const input = el("input", { type: "text", id, class: "hotkey", readonly: true,
    "aria-description": "Click, then press the new key combination. Escape cancels." });
  const stop = () => { input.classList.remove("capturing"); input.value = pretty(saved); };
  input.addEventListener("focus", () => { input.classList.add("capturing"); input.value = "Press the keys"; });
  input.addEventListener("blur", stop);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Tab") return;
    event.preventDefault();
    if (event.key === "Escape") return input.blur();
    const key = keyName(event);
    if (!key) return;  // a modifier on its own: keep waiting for the key
    const parts = [event.ctrlKey && "ctrl", event.altKey && "alt", event.shiftKey && "shift", event.metaKey && "win"]
      .filter(Boolean);
    if (!parts.length) { input.value = "Add Ctrl, Alt, Shift or Win"; return; }
    const combination = [...parts, key].join("+");
    input.blur();
    change(option.key, combination);
  });
  return { node: input, sync: (value) => { saved = value; if (document.activeElement !== input) input.value = pretty(value); } };
}

function buildControl(option, id) {
  switch (option.kind) {
    case "toggle": return toggleControl(option, id);
    case "choice": return Object.keys(option.choices).length <= 4 && option.key !== "decider"
      ? segmentsControl(option) : selectControl(option, id);
    case "choices": return chipsControl(option);
    case "hotkey": return hotkeyControl(option, id);
    case "input_device": case "output_device": return deviceControl(option, id);
    default: {
      const input = el("input", { type: "text", id, spellcheck: "false",
        onchange: () => change(option.key, input.value) });
      return { node: input, sync: (value, force) => {
        if (force || document.activeElement !== input) input.value = value;
      } };
    }
  }
}

function buildSettings(s) {
  const groups = new Map();
  for (const option of s.options) {
    if (!groups.has(option.group)) groups.set(option.group, []);
    groups.get(option.group).push(option);
  }
  const container = $("groups");
  for (const [group, options] of groups) {
    const box = container.appendChild(el("div", { class: "group" }, el("h3", { text: GROUP_NAMES[group] || group })));
    for (const option of options) {
      const id = `setting-${option.key}`;
      const control = buildControl(option, id);
      // Controls with long contents (device names, several or long choices) go under their label, full width.
      const choiceText = Object.values(option.choices || {}).join("").length;
      const wide = ["choices", "input_device", "output_device"].includes(option.kind)
        || (option.kind === "choice" && (control.node.tagName === "SELECT" || choiceText > 28));
      const labelled = ["toggle", "choice", "hotkey", "input_device", "output_device", "text"].includes(option.kind)
        && control.node.tagName !== "DIV";
      const name = labelled ? el("label", { for: id, text: option.label }) : el("span", { class: "name", text: option.label });
      if (!option.live) name.append(el("span", { class: "tag", text: "After restart" }));
      const error = el("p", { id: `${id}-error`, class: "error", role: "alert" });
      error.hidden = true;
      const help = option.help ? el("p", { id: `${id}-help`, class: "help", text: option.help }) : null;
      control.node.setAttribute("aria-describedby", [help?.id, error.id].filter(Boolean).join(" "));
      const row = box.appendChild(el("div", { class: wide ? "row stack" : "row" }, name, control.node,
        help, error));
      controls[option.key] = { row, error, sync: control.sync };
      control.sync(s.values[option.key]);
    }
  }
}

// ------------------------------------------------------------------------------------------------------------
// Sound checks, reset, archive

function wireButtons() {
  let checkingSound = false;
  document.querySelectorAll("[data-test]").forEach((button) => button.addEventListener("click", async () => {
    if (checkingSound) return;
    checkingSound = true;
    button.disabled = true;
    $("said").textContent = "Checking…";
    try {
      const { playing } = await api("/api/test", { what: button.dataset.test });
      $("said").textContent = playing === "chime" ? "Did you hear the chime? If it was quiet, turn up your volume."
        : playing === "welcome" ? "Look for Sahur on your desktop."
          : playing === "microphone" ? "Say something and check that the bars move."
            : `You should have heard “${playing}”`;
    } catch (error) {
      $("said").textContent = error.message;
    } finally {
      checkingSound = false;
      button.disabled = false;
      await refreshState();
    }
  }));
  $("reset").addEventListener("click", async () => {
    if (!confirm("Restore the original Sahur settings?")) return;
    try {
      await api("/api/reset", {});
      toast("Settings reset to the defaults");
    } catch (error) { toast(error.message, true); }
    refreshState("all");
  });
  $("archive").addEventListener("click", async () => {
    if (!confirm("Start the statistics afresh? The current history is kept in a renamed file next to it.")) return;
    try {
      const { archived } = await api("/api/history/archive", {});
      toast(archived ? `Statistics started afresh. Old history: ${archived}` : "There was no history yet");
    } catch (error) { toast(error.message, true); }
    refreshStats();
  });
}

// ------------------------------------------------------------------------------------------------------------
// Statistics

const seconds = (ms) => (ms == null ? "-" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const percent = (x) => (x == null ? "-" : `${Math.round(x * 100)}%`);

function kpis(stats) {
  const items = [
    ["Commands", String(stats.total), stats.today ? `${stats.today} today` : ""],
    ["Worked", percent(stats.success_rate), stats.corrected ? `${stats.corrected} corrected` : ""],
    ["Median time per command", seconds(stats.median_ms),
      stats.p90_ms != null ? `90% within ${seconds(stats.p90_ms)}` : ""],
    ["Decided on this PC", percent(stats.local_rate), "no System 2 needed"],
  ];
  $("kpis").replaceChildren(...items.map(([label, value, note]) =>
    el("div", {}, el("dt", { text: label }), el("dd", {}, value, note ? el("small", { text: note }) : null))));
}

function tip(event, text) {
  const node = $("tip");
  if (!text) { node.hidden = true; return; }
  node.textContent = text;
  node.hidden = false;
  const x = Math.min(event.clientX + 14, window.innerWidth - node.offsetWidth - 8);
  node.style.left = `${x}px`;
  node.style.top = `${event.clientY + 14}px`;
}

let lastDays = null;
function perDay(days) {
  lastDays = days;
  // Drawn at the box's own width, so the axis text keeps its size on a phone.
  const W = Math.max(280, Math.round($("per-day").clientWidth || 720)), H = 190, left = 28, bottom = 22, top = 8;
  const gap = 2;
  const max = Math.max(1, ...days.map((d) => d.ok + d.failed));
  const ticks = max <= 4 ? max : 4;
  const step = Math.ceil(max / ticks);
  const scaleMax = step * ticks;
  const plotH = H - bottom - top;
  const slot = (W - left) / days.length;
  const barW = Math.min(36, slot * 0.6);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Commands per day for the last ${days.length} days`);
  const add = (parent, tag, attrs, text) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    return parent.appendChild(node);
  };
  for (let i = 0; i <= ticks; i++) {
    const y = top + plotH - (i * step / scaleMax) * plotH;
    add(svg, "line", { class: "grid", x1: left, x2: W, y1: y, y2: y });
    add(svg, "text", { x: left - 8, y: y + 4, "text-anchor": "end" }, String(i * step));
  }
  days.forEach((d, i) => {
    const x = left + slot * i + (slot - barW) / 2;
    const g = add(svg, "g", { class: "col" });
    let y = top + plotH;
    // Worked sits on the baseline; what didn't work stacks on it, with a 2px gap between the two.
    for (const [value, kind, first] of [[d.ok, "worked", true], [d.failed, "failed", false]]) {
      if (!value) continue;
      const h = (value / scaleMax) * plotH - (first ? 0 : gap);
      y -= h + (first ? 0 : gap);
      add(g, "path", { d: roundedTop(x, y, barW, h, Math.min(4, h)), class: kind });
    }
    const date = new Date(`${d.date}T00:00:00`);
    const label = date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    const every = Math.ceil(days.length / Math.max(2, Math.floor((W - left) / 64)));
    if ((days.length - 1 - i) % every === 0) {
      add(svg, "text", { x: x + barW / 2, y: H - 6, "text-anchor": "middle" }, label);
    }
    const hit = add(svg, "rect", { class: "hit", x: left + slot * i, y: top, width: slot, height: plotH });
    const text = `${label}: ${d.ok} worked, ${d.failed} didn't`;
    hit.addEventListener("mousemove", (e) => tip(e, text));
    hit.addEventListener("mouseleave", (e) => tip(e, ""));
  });
  $("per-day").replaceChildren(svg);
}

function roundedTop(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function bars(id, rows, valueText) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  $(id).replaceChildren(...rows.map((r) => {
    const fill = el("span", { class: "fill" });
    fill.style.width = `${Math.max(2, (r.count / max) * 78)}%`;  // CSSOM: the page's CSP has no inline styles
    return el("div", { class: "bar" },
      el("span", { class: "what", text: r.label, title: r.label }),
      el("span", { class: "track" }, fill,
        el("span", { class: "value" }, String(r.count), valueText ? el("small", { text: valueText(r) }) : null)));
  }));
}

function recent(rows) {
  const body = $("recent").querySelector("tbody");
  body.replaceChildren(...rows.map((r) => {
    const when = new Date(r.time);
    const today = new Date().toDateString() === when.toDateString();
    const time = when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    const stamp = today ? time : `${when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}, ${time}`;
    const failure = r.result || (r.outcome || "didn't work").replaceAll("_", " ");
    const result = r.corrected ? "Corrected" : r.ok ? "Worked" : failure[0].toUpperCase() + failure.slice(1);
    return el("tr", {},
      el("td", { class: "when", text: stamp }),
      el("td", { class: "said-cell", text: r.transcript, lang: r.language || undefined }),
      el("td", { class: "did", text: ACTIONS[r.action] || r.action.replaceAll("_", " ") || "-" }),
      el("td", { class: "by", text: r.source }),
      el("td", { class: "num", text: seconds(r.ms) }),
      el("td", {}, el("span", { class: r.ok ? "result" : "result bad", text: result })));
  }));
}

async function refreshStats() {
  let stats;
  try { stats = await api("/api/stats"); } catch (error) {
    $("stats-note").textContent = "Couldn't load your statistics. Sahur will try again shortly.";
    return;
  }
  const empty = stats.total === 0;
  $("stats-empty").hidden = !empty;
  $("stats-body").hidden = empty;
  $("stats-note").textContent = stats.first
    ? `From ${new Date(stats.first).toLocaleDateString(undefined, { day: "numeric", month: "long" })}, on this computer only.`
    : "Kept on this computer only.";
  if (empty) return;
  kpis(stats);
  $("per-day-title").textContent = `Commands per day, last ${stats.per_day.length} days`;
  perDay(stats.per_day);
  bars("sources", stats.sources.map((s) => ({ label: s.name, count: s.count, ms: s.median_ms })),
    (r) => (r.ms != null ? `  median ${seconds(r.ms)}` : ""));
  bars("actions", stats.actions.map((a) => ({ label: ACTIONS[a.name] || a.name.replaceAll("_", " "), count: a.count })));
  bars("languages", stats.languages.map((l) => ({ label: LANGUAGES[l.name] || l.name, count: l.count })));
  bars("triggers", stats.triggers.map((t) => ({ label: t.name, count: t.count })));
  recent(stats.recent);
}

// ------------------------------------------------------------------------------------------------------------

buildMeter();
wireButtons();
refreshState();
refreshStats();
pollLevel();
setInterval(refreshState, 5000);
setInterval(refreshStats, 15000);
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastDays) perDay(lastDays); }, 150);
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollLevel(); refreshState(); } });
