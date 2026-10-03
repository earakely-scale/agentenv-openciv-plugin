// The play UI (docs/play.md): a person plays one seat through the env's play API, with the game's own flow and
// hotkeys. Every action goes through the same env calls as an agent's tools. Needs map.js.
"use strict";

const TOKEN = new URLSearchParams(location.hash.slice(1)).get("token") || new URLSearchParams(location.search).get("token");
const S = {
  view: null, world: null, cam: new Camera(), painter: new PlayPainter(), sel: null, mode: null, hover: null,
  waited: new Set(), sites: null, sitesFor: null, busy: false, turn: null, gameId: null, dialog: null,
  tileInfo: new Map(), drag: null, confirmEnd: false, lastEventsTurn: null,
  art: null, artState: null,   // the client's art (art.js): null until loaded; artState "loading", "on", "off", "none"
};
// The client's art when the env has it: the game's own terrain, cities and units. The plain map otherwise, or when
// the player turns the art off (key T, remembered in this browser).
function loadArt() {
  if (S.artState) return;
  S.artState = "loading";
  Art.load("play/art/").then(art => {
    S.art = art;
    S.artState = localStorage.getItem("openciv3-art") === "off" ? "off" : "on";
    if (S.artState === "on") useArt(true);
    renderBar();
  }).catch(() => { S.artState = "none"; renderBar(); });
}
function useArt(on) {
  if (!S.art) return;
  S.artState = on ? "on" : "off";
  localStorage.setItem("openciv3-art", S.artState);
  S.painter = on ? new ArtPainter(S.art) : new PlayPainter();
  if (S.world && S.view) S.world.update(S.view.map, palette());
  if (on && S.cam.hw === 44) S.cam.hw = 56;   // the art reads best near the game's own zoom
  document.body.classList.toggle("art", on);
  if (on && !$("#art-style")) {   // the HUD's art, as CSS: the sheets' URLs carry the art's id
    const u = n => `url("${S.art.url(S.art.m.sheets[n])}")`, st = document.createElement("style");
    st.id = "art-style";
    st.textContent = `body.art .status { background-image: ${u("status_box")}; }
      body.art .status .dome { background-image: ${u("next_turn")}; }
      body.art .minimap { background-image: ${u("minimap_box")}; }
      body.art .commands button.icon { background-image: ${u("buttons")}; }
      body.art .commands button.icon:hover:not(:disabled) { background-image: ${u("buttons_hover")}; }
      body.art .commands button.icon:active:not(:disabled) { background-image: ${u("buttons_pressed")}; }`;
    document.head.appendChild(st);
  }
  renderBar(); renderStatus(); renderCommands(); draw();
}
const artOn = () => S.artState === "on" && !!S.art;
// Battles the seat saw (known_map battles, or its own attack's result) play once on the art map, in order, the camera
// following them; the ones already shown are remembered for the game, so a reload doesn't replay them.
function playBattles(list) {
  if (!artOn() || !list || !list.length || !S.painter.queueBattles) return;
  const key = `openciv3-battles-${S.gameId}`, seen = new Set(JSON.parse(sessionStorage.getItem(key) || "[]"));
  const fresh = list.filter(b => !seen.has(b.id)).sort((a, b) => a.id - b.id);
  if (!fresh.length) return;
  for (const b of fresh) seen.add(b.id);
  sessionStorage.setItem(key, JSON.stringify([...seen].slice(-200)));
  S.painter.onBattle = b => centerOn(b.defender.x, b.defender.y, true);
  S.painter.queueBattles(fresh);
  draw();
}

// The unit orders, as the game's command bar shows them: [order, label, key shown, key code]
const ORDERS = [
  ["found_city", "Build city", "B"], ["goto", "Go to", "G"], ["explore", "Explore", "X"], ["auto_work", "Automate", "A"],
  ["build_road", "Road", "R"], ["build_mine", "Mine", "M"], ["irrigate", "Irrigate", "I"], ["clear_forest", "Clear", "⇧C"],
  ["fortify", "Fortify", "F"], ["wake", "Wake", "⇧W"], ["hold", "Skip turn", "Space"], ["bombard", "Bombard", "⇧B"],
  ["disband", "Disband", "⇧D"],
];
// The client's order buttons (NormButtons.png and its hover and pressed sheets): order -> [column, row] of 32x32.
const BUTTON_CELL = {hold: [0, 0], wait: [1, 0], fortify: [2, 0], disband: [3, 0], goto: [4, 0], explore: [5, 0],
  bombard: [3, 1], found_city: [5, 2], build_road: [6, 2], build_railroad: [7, 2], build_mine: [1, 3], irrigate: [2, 3],
  clear_forest: [3, 3], auto_work: [7, 3]};
const KEYS = {b: "found_city", g: "goto", x: "explore", a: "auto_work", r: "build_road", m: "build_mine", i: "irrigate",
  C: "clear_forest", f: "fortify", W: "wake", " ": "hold", B: "bombard", D: "disband"};
// Arrow keys and the number pad move the active unit a tile, as in the game (8 is north).
const MOVE_KEYS = {ArrowUp: "N", ArrowDown: "S", ArrowLeft: "W", ArrowRight: "E", Numpad8: "N", Numpad2: "S", Numpad4: "W",
  Numpad6: "E", Numpad7: "NW", Numpad9: "NE", Numpad1: "SW", Numpad3: "SE", Home: "NW", PageUp: "NE", End: "SW", PageDown: "SE"};
const HOT = new Set(["unit_lost", "city_destroyed", "civ_destroyed", "war_declared", "disorder", "disorder_started",
  "city_starved", "gold_stolen", "threat", "defenseless", "settle_failed", "goto_blocked", "attacked"]);

// ======================================================================== the play API

async function api(path, body) {
  const r = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: {"X-OpenCiv3-Token": TOKEN || "", ...(body ? {"Content-Type": "application/json"} : {})},
    body: body ? JSON.stringify(body) : undefined});
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok || (data && data.ok === false)) {
    const err = (data && data.error) || {code: `http_${r.status}`, message: r.status === 401
      ? "This link doesn't play a seat in the current game. Ask for a new one (agent-env openciv3 play)." : `The env answered ${r.status}.`};
    throw Object.assign(new Error(err.message), err, {status: r.status});
  }
  return data;
}
async function act(tool, args = {}, {quiet = false} = {}) {
  if (S.busy) return null;
  S.busy = true; renderCommands();
  try {
    const res = await api("play/api/act", {tool, args});
    if (res.result && res.result.battle) playBattles([res.result.battle]);
    if (!quiet && res.message) toast(res.message);
    await loadView();
    return res;
  } catch (e) {
    if (e.code === "at_peace") { await offerWar(e, tool, args); return null; }
    toast(e.message, true);
    await loadView().catch(() => {});
    return null;
  } finally {
    S.busy = false; renderCommands();
  }
}

// ======================================================================== shell

function shell() {
  $("#play").innerHTML = `
    <canvas id="map" aria-label="The map, as your civilization knows it"></canvas>
    <div class="bar" id="bar"></div>
    <div class="waiting" id="waiting" hidden></div>
    <div class="minimap" id="minimapbox"><canvas id="minimap"></canvas></div>
    <div class="commands" id="commands"></div>
    <div class="status" id="status"></div>
    <div class="toasts" id="toasts"></div>
    <div class="tip" id="tip" hidden></div>
    <div id="dialog"></div>`;
  mapEvents(); keys();
  new ResizeObserver(() => draw()).observe($("#play"));
}
function centerMessage(html) { $("#play").innerHTML = `<div class="center">${html}</div>`; }

// The env's messages are written for agents: drop the hints that name tool calls, e.g. "(change it with
// set_production(city="c1", item=...))", which a person does with the UI instead.
const plain = text => String(text).replace(/\s*\([^()]*\b[a-z_]+\([^()]*\)[^()]*\)/g, "")
  .replace(/\s*—?\s*consider [a-z_]+\([^)]*\)/g, "").replace(/\s+(?:with|using|via|call) [a-z_]+\([^)]*\)/g, "");
function toast(text, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : ""); el.textContent = plain(text);
  $("#toasts").appendChild(el);
  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 450); }, err ? 6000 : 3500);
}

// ======================================================================== loading the view

async function loadView() {
  const v = await api("play/api/view");
  const newGame = v.game.id !== S.gameId, newTurn = v.game.turn !== S.turn;
  S.view = v; S.gameId = v.game.id;
  const colors = palette(v);
  if (!S.world) S.world = new World(v.map, colors); else S.world.update(v.map, colors);
  if (newGame) { S.waited.clear(); S.sel = null; centerOnHome(); }
  if (newTurn) { S.turn = v.game.turn; S.waited.clear(); S.tileInfo.clear(); S.sites = null; }
  const units = myUnits();
  if (S.sel && !units.find(u => u.id === S.sel)) S.sel = null;
  if (!S.sel || !canAct(unit(S.sel))) selectNext({scroll: !newGame});
  renderBar(); renderStatus(); renderCommands(); renderWaiting(); draw();
  if (newTurn && !newGame) turnReport();
  else if (newGame && v.state.turn > 0 && (v.state.last_events || []).length) turnReport();
  if (v.game.game_over) gameOver();
  if (v.game.art) loadArt();
  playBattles(v.map.battles);
  return v;
}
// The civs' colours: with the game's art, the client's own (known_map players[].color), so the map looks as it does
// in the game; else the viewer's, as the live view and recordings show them.
function palette(v = S.view) {
  if (!artOn()) return v.colors;
  const own = Object.fromEntries((v.map.players || []).filter(p => p.color).map(p => [String(p.index), p.color]));
  return {...v.colors, ...own};
}
function civColor(civ, fallback) {
  if (!artOn()) return fallback;
  const p = (S.view.map.players || []).find(q => q.civ === civ);
  return p?.color || fallback;
}
const state = () => S.view.state;
const myUnits = () => state().units || [];
const unit = id => myUnits().find(u => u.id === id) || null;
const canAct = u => !!u && u.needs_orders && !S.view.game.ended && !S.view.game.game_over;
const ended = () => S.view.game.ended || S.view.game.game_over;

function centerOnHome() {
  const s = state(), home = (s.cities || []).find(c => c.capital) || (s.cities || [])[0] || myUnits()[0];
  if (home) { S.cam.cx = home.x; S.cam.cy = home.y; return; }
  const b = S.world.bounds(); if (b) { S.cam.cx = (b.x0 + b.x1) / 2; S.cam.cy = (b.y0 + b.y1) / 2; }
}
function centerOn(x, y, onlyIfOff = false) {
  if (onlyIfOff) {
    const r = $("#map").getBoundingClientRect(), [sx, sy] = S.cam.screen(x, y, r.width, r.height, S.world.wrap ? S.world.W : 0);
    if (sx > r.width * 0.15 && sx < r.width * 0.85 && sy > r.height * 0.2 && sy < r.height * 0.75) return;
  }
  S.cam.cx = x; S.cam.cy = y; draw();
}

// The next unit that needs orders, as the game picks it: units told to wait come back once the rest have moved.
function selectNext({scroll = true} = {}) {
  const ready = myUnits().filter(canAct);
  let next = ready.find(u => !S.waited.has(u.id) && u.id !== S.sel) || ready.find(u => !S.waited.has(u.id));
  if (!next && ready.length) { S.waited.clear(); next = ready[0]; }
  select(next ? next.id : null, {scroll});
}
function select(id, {scroll = true} = {}) {
  S.sel = id; S.mode = null; S.confirmEnd = false;
  const u = unit(id);
  if (u && scroll) centerOn(u.x, u.y, true);
  if (u && (u.orders || []).includes("settle") && S.sitesFor !== u.id) loadSites(u);
  if (!u || !(u.orders || []).includes("settle")) S.sites = null;
  renderStatus(); renderCommands(); draw();
}
async function loadSites(u) {
  S.sitesFor = u.id;
  try { const r = await api(`play/api/sites?unit=${encodeURIComponent(u.id)}`); if (S.sel === u.id) { S.sites = r.sites || []; draw(); } }
  catch (e) { S.sites = null; }
}

// ======================================================================== the top bar, the status box, the commands

function renderBar() {
  const v = S.view, s = v.state, g = v.game, me = g.me, rs = s.rates || {}, res = s.research || {};
  const pct = res.cost ? Math.min(100, (res.beakers || 0) / res.cost * 100) : 0;
  $("#bar").innerHTML = `
    <div class="civ"><span class="sw" style="background:${civColor(me.civ, me.color)}"></span>${esc(me.civ)} <small>${esc(me.label || "")}</small></div>
    <div class="turn">T${g.turn} <small>/ ${g.turn_limit}</small></div>
    <div class="stat"><span class="l">Government</span><span class="v">${esc(s.government)}${s.anarchy_until ? ` <span class="bad">until T${s.anarchy_until}</span>` : ""}</span></div>
    <div class="stat"><span class="l">Gold</span><span class="v gold">${s.gold} <span class="muted">${s.gold_per_turn >= 0 ? "+" : ""}${s.gold_per_turn}</span></span></div>
    <div class="stat"><span class="l">Tax · Sci · Lux</span><span class="v">${rs.tax ?? "-"} · ${rs.science ?? "-"} · ${rs.luxury ?? "-"}</span></div>
    <div class="stat research"><span class="l">Research</span><span class="v">${res.current ? `${esc(res.current)} <span class="muted">${res.turns_left ?? "?"} t</span>` : '<span class="bad">nothing</span>'}</span>
      <div class="meter"><i style="width:${pct.toFixed(0)}%"></i></div></div>
    <div class="stat"><span class="l">Score</span><span class="v">${s.score.total}</span></div>
    <div class="grow"></div>
    <div class="seats">${g.seats.filter(x => x.civ !== me.civ).map(x => `<span class="seat" title="${esc(x.human ? "a person" : "an agent")}">
      <span class="sw" style="background:${civColor(x.civ, x.color)}"></span>${esc(x.label || x.civ)}${x.human ? " 👤" : ""}
      <span class="st ${x.ended ? "done" : "play"}">${x.defeated ? "out" : x.ended ? "✓" : "●"}</span></span>`).join("")}</div>
    <div class="adv">
      <button data-a="domestic" title="Domestic advisor: taxes and government (F1)">Domestic <kbd>F1</kbd></button>
      <button data-a="foreign" title="Foreign advisor: war and peace (F4)">Foreign <kbd>F4</kbd></button>
      <button data-a="science" title="Science advisor: research (F6)">Science <kbd>F6</kbd></button>
      ${S.art ? `<button data-a="art" title="The game's art, or the plain map (T)">${S.artState === "on" ? "Plain map" : "Game art"} <kbd>T</kbd></button>` : ""}
    </div>`;
  for (const b of $$("#bar .adv button")) b.onclick = () => b.dataset.a === "art" ? useArt(S.artState !== "on") : advisor(b.dataset.a);
}

function renderStatus() {
  if (!S.view) return;
  if (artOn()) return renderArtStatus();
  const s = state(), g = S.view.game, u = unit(S.sel);
  let body;
  if (g.game_over) body = `<div class="enter" style="animation:none">Game over</div>`;
  else if (g.ended) body = `<div class="note">Your turn is over. Waiting for ${g.waiting_for.map(seatName).map(esc).join(", ") || "the others"}…</div>`;
  else if (u) {
    const hp = u.hp_max ? u.hp / u.hp_max * 100 : 100, found = u.can_found_city;
    body = `<div class="unit">${esc(u.type)} <span style="font-weight:500;font-size:12px">${esc(u.id)}</span></div>
      <div class="hp"><i style="width:${hp}%"></i></div>
      <div class="row"><span>Moves ${fmtMoves(u.moves_left)}/${u.moves_max}</span><span>HP ${u.hp}/${u.hp_max}</span><span>(${u.x},${u.y})</span></div>
      <div class="note">${esc(statusText(u))}</div>
      ${found && (u.orders || []).includes("found_city") ? `<div class="note">${found.ok ? "Can build a city here (B)" : esc(found.reason)}</div>` : ""}
      ${(u.attack_targets || []).length ? `<div class="note">Can attack: ${u.attack_targets.map(t => `${esc(t.defender || t.city || t.owner)} ${Math.round(t.win_chance * 100)}%`).join(", ")}</div>` : ""}`;
  } else {
    const pending = myUnits().filter(canAct).length;
    body = pending ? `<div class="note">${pending} unit${pending > 1 ? "s" : ""} waiting for orders</div>`
      : `<div class="enter">Press <b>Enter</b> for the next turn</div>`;
  }
  $("#status").innerHTML = `<div class="hd"><span>${esc(g.me.civ)} · ${esc(s.government)} · T${g.turn}</span>
    <span>${s.gold} gold</span></div><div class="bd">${body}
    <div class="actions" style="margin-top:8px"><button id="endturn" ${ended() ? "disabled" : ""}>End turn <kbd>Enter</kbd></button>
    ${u ? `<button id="waitbtn">Wait <kbd>W</kbd></button>` : ""}</div></div>`;
  $("#endturn").onclick = () => endTurn(true);
  if ($("#waitbtn")) $("#waitbtn").onclick = waitUnit;
}
// The client's status scroll (LowerRightInfoBox.cs): the active unit at the top right, with its picture; the civ,
// the treasury and the research in the middle; the next-turn dome on the brass ball, lit while the turn can end.
function renderArtStatus() {
  const s = state(), g = S.view.game, u = unit(S.sel), res = s.research || {};
  const pending = myUnits().filter(canAct).length;
  const hint = g.game_over ? "Game over" : g.ended ? "Please wait..." : !u && !pending ? "ENTER or SPACEBAR for next turn" : "";
  const where = u ? (() => { const c = (s.cities || []).find(c => c.x === u.x && c.y === u.y); const t = S.world.tile(u.x, u.y);
    return c ? c.name : t ? (t.overlay || t.terrain).replace(/^./, ch => ch.toUpperCase()) : ""; })() : "";
  $("#status").innerHTML = `<button class="dome ${hint && !g.ended && !g.game_over ? "lit" : ""}" id="endturn" title="End the turn (Enter)" ${ended() ? "disabled" : ""}></button>
    ${hint ? `<div class="hint">${esc(hint)}</div>` : ""}
    ${u ? `<canvas class="thumb" width="70" height="60"></canvas>
      <div class="unitline" style="top:18px">${esc(u.type)} <span class="uid">${esc(u.id)}</span></div>
      <div class="unitline" style="top:32px">HP ${u.hp}/${u.hp_max} · ${fmtMoves(u.moves_left)}/${u.moves_max}</div>
      <div class="unitline" style="top:46px">${esc(where)}</div>
      <div class="unitline small" style="top:60px">${esc(statusText(u))}</div>` : ""}
    <div class="mid" style="top:80px">${esc(s.civ)} - ${esc(s.government)}${s.anarchy_until ? ` (until T${s.anarchy_until})` : ""}</div>
    <div class="mid" style="top:94px">Turn ${g.turn}  ${s.gold} Gold (${s.gold_per_turn >= 0 ? "+" : ""}${s.gold_per_turn} per turn)</div>
    <div class="mid" style="top:108px">${res.current ? `${esc(res.current)} (${res.turns_left ?? "--"} turns)` : "Not selected (-- turns)"}</div>`;
  $("#endturn").onclick = () => endTurn(true);
  const thumb = $("#status .thumb");
  if (thumb && u) {
    const ua = S.art.unit(u.type, renderStatus), g2 = thumb.getContext("2d");
    if (ua) {
      const cell = S.art.unitCell(ua, u.status === "fortified" ? "fortify" : "default", "SE", 0, rgb(S.world.color(S.world.me)));
      const [ax, ay] = ua.spec.anchor;
      g2.drawImage(cell, 35 - ax, 44 - ay);
    }
  }
}
const fmtMoves = m => Number.isInteger(m) ? m : (Math.round(m * 3) / 3).toFixed(1);
function statusText(u) {
  const st = u.status || "idle";
  if (st === "goto" || st === "settle") return `${st === "settle" ? "Going to found a city at" : "Going to"} (${u.target?.x},${u.target?.y})`;
  if (st.startsWith("working:")) return `Working: ${st.slice(8).replace("_", " ")}`;
  return {idle: "Waiting for orders", fortified: "Fortified", exploring: "Exploring", auto_work: "Automated", done: "No moves left"}[st] || st;
}

function renderCommands() {
  const el = $("#commands"); if (!el || !S.view) return;
  const u = unit(S.sel);
  if (!u || ended()) { el.hidden = true; return; }
  el.hidden = false;
  const have = new Set(u.orders || []);
  const art = artOn();
  el.classList.toggle("art", art);
  el.innerHTML = ORDERS.filter(([o]) => have.has(o)).map(([o, label, k]) => art && BUTTON_CELL[o]
    ? `<button class="icon" data-o="${o}" ${S.busy ? "disabled" : ""} title="${esc(label)} (${k})" aria-label="${esc(label)}"
        style="background-position:-${BUTTON_CELL[o][0] * 32}px -${BUTTON_CELL[o][1] * 32}px"><kbd>${esc(k)}</kbd></button>`
    : `<button data-o="${o}" ${S.busy ? "disabled" : ""} title="${esc(label)} (${k})"><b>${esc(label)}</b><kbd>${esc(k)}</kbd></button>`).join("")
    + (art ? `<button class="icon" data-o="wait" title="Wait: come back to this unit later (W)" aria-label="Wait"
        style="background-position:-32px 0"><kbd>W</kbd></button>`
      : `<span class="sep"></span><button data-o="wait" title="Come back to this unit later (W)"><b>Wait</b><kbd>W</kbd></button>
       <button data-o="center" title="Centre on the unit (C)"><b>Centre</b><kbd>C</kbd></button>`);
  for (const b of $$("button", el)) b.onclick = () => command(b.dataset.o);
}

// A seat by its player's name, with the civ: "sol (America)".
function seatName(civ) {
  const x = S.view.game.seats.find(s => s.civ === civ);
  return x && x.label && x.label !== civ ? `${x.label} (${civ})` : civ;
}
function renderWaiting() {
  const g = S.view.game, el = $("#waiting");
  el.hidden = !g.ended || g.game_over;
  if (!el.hidden) {
    const names = g.waiting_for.map(seatName);
    el.innerHTML = `<span class="dot"></span><span>Waiting for ${names.map(esc).join(", ") || "the turn to end"}…</span>`;
  }
}

// ======================================================================== orders

async function command(order) {
  const u = unit(S.sel); if (!u) return;
  if (order === "wait") return waitUnit();
  if (order === "center") return centerOn(u.x, u.y);
  if (order === "goto") { S.mode = "goto"; $("#map").classList.add("goto"); toast("Click where to go (Esc cancels)"); return; }
  if (order === "bombard") {
    if (!(u.attack_targets || []).length) { S.mode = "bombard"; $("#map").classList.add("goto"); toast("Click what to bombard"); return; }
  }
  if (order === "disband" && !confirm(`Disband ${u.type} ${u.id}?`)) return;
  const res = await act("unit_order", {unit: u.id, order});
  if (res) afterOrder(u.id);
}
// After an order: stay on the unit while it can still act, else go on to the next one.
function afterOrder(id) {
  const u = unit(id);
  if (canAct(u)) { select(id, {scroll: false}); return; }
  selectNext();
}
function waitUnit() { if (S.sel) { S.waited.add(S.sel); selectNext(); } }

async function moveTo(x, y) {
  const u = unit(S.sel); if (!u || ended()) return;
  const target = (u.attack_targets || []).find(t => t.x === x && t.y === y);
  let order = target ? "attack" : "goto";
  if (S.mode === "bombard") order = "bombard";
  if (!target && S.sites && (u.orders || []).includes("settle") && S.sites.some(s => s.x === x && s.y === y)) order = "settle";
  S.mode = null; $("#map").classList.remove("goto");
  const res = await act("unit_order", {unit: u.id, order, x, y});
  if (res) afterOrder(u.id);
}
async function step(dir) {
  const u = unit(S.sel); if (!u || ended()) return;
  const [x, y] = S.world.step(u.x, u.y, dir);
  await moveTo(x, y);
}
// Attacking a civ you're at peace with: the game asks before declaring war.
async function offerWar(e, tool, args) {
  const civ = (e.message.match(/peace with ([A-Z][\w ]+?)[;.,]/) || [])[1] || (e.alternatives || [])[0];
  if (!civ) { toast(e.message, true); return; }
  const ok = await ask(`War with ${civ}?`, `${esc(e.message)}<br><br>Declare war on ${esc(civ)} and attack?`, "Declare war", "danger");
  if (!ok) return;
  const war = await act("diplomacy", {action: "declare_war", civ});
  if (war) { const res = await act(tool, args); if (res && args.unit) afterOrder(args.unit); }
}

async function endTurn(force = false) {
  if (ended() || S.busy) return;
  const pending = myUnits().filter(u => canAct(u)).length;
  if (pending && !force && !S.confirmEnd) {
    S.confirmEnd = true;
    toast(`${pending} unit${pending > 1 ? "s" : ""} still ${pending > 1 ? "have" : "has"} moves. Press Enter again to end the turn.`);
    return;
  }
  S.confirmEnd = false;
  const res = await act("end_turn", {}, {quiet: true});
  if (res && !res.advanced) toast("Turn ended. Waiting for the others…");
}

// ======================================================================== the map: drawing and input

let raf = 0, animating = false;
function draw() { cancelAnimationFrame(raf); raf = requestAnimationFrame(paint); }
function paint() {
  if (!S.world) return;
  const c = $("#map"); if (!c) return;
  const {ctx, w, h} = sizeCanvas(c);
  const u = unit(S.sel);
  const dlgCity = S.dialog?.kind === "city" ? S.dialog.city : null;
  S.painter.draw(ctx, S.world, S.cam, w, h, {
    selected: u, targets: u && !ended() ? u.attack_targets || [] : [], sites: u ? S.sites : null, hover: S.hover,
    mine: new Map(myUnits().map(x => [x.id, x])),
    cityRadius: dlgCity, path: u && S.hover && S.mode ? [[u.x, u.y], S.hover] : null,
  });
  const m = $("#minimap");
  $("#minimapbox").classList.toggle("art", artOn());
  if (m) {
    const mw = artOn() ? 229 : 220, mh = artOn() ? 105 : Math.round(mw * S.world.H / (2 * S.world.W));
    m.style.width = mw + "px"; m.style.height = mh + "px";
    const mm = sizeCanvas(m); S.painter.minimap(mm.ctx, S.world, S.cam, mm.w, mm.h, w, h);
  }
  // the active unit blinks: keep painting while one is selected
  // the active unit blinks, and the art's units animate: keep painting while either is on screen
  if ((u || S.artState === "on") && !document.hidden) { animating = true; raf = requestAnimationFrame(paint); } else animating = false;
}

function mapEvents() {
  const c = $("#map");
  const tileOf = e => { const r = c.getBoundingClientRect(); return S.cam.tileAt(e.clientX - r.left, e.clientY - r.top, r.width, r.height, S.world); };
  c.addEventListener("contextmenu", e => e.preventDefault());
  c.addEventListener("pointerdown", e => {
    if (!S.world) return;
    S.drag = {x: e.clientX, y: e.clientY, cx: S.cam.cx, cy: S.cam.cy, moved: 0, button: e.button};
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener("pointermove", e => {
    if (!S.world) return;
    if (S.drag) {
      const dx = e.clientX - S.drag.x, dy = e.clientY - S.drag.y;
      S.drag.moved = Math.max(S.drag.moved, Math.abs(dx) + Math.abs(dy));
      if (S.drag.moved > 5 && S.drag.button === 0) {
        c.classList.add("drag"); S.cam.cx = S.drag.cx - dx / S.cam.hw; S.cam.cy = clamp(S.drag.cy - dy / (S.cam.hw / 2), 0, S.world.H); hideTip(); draw();
        return;
      }
    }
    const t = tileOf(e);
    if (!t || (S.hover && S.hover[0] === t[0] && S.hover[1] === t[1])) return;
    S.hover = t; draw(); tileTip(e, t);
  });
  c.addEventListener("pointerup", e => {
    c.classList.remove("drag");
    const d = S.drag; S.drag = null;
    if (!d || d.moved > 5 || !S.world) return;
    const t = tileOf(e); if (!t) return;
    if (e.button === 2) { if (S.sel) moveTo(...t); return; }   // right-click: go there (or attack)
    clickTile(...t);
  });
  c.addEventListener("pointerleave", () => { S.hover = null; hideTip(); draw(); });
  c.addEventListener("wheel", e => {
    e.preventDefault();
    S.cam.hw = clamp(S.cam.hw * Math.exp(-e.deltaY * 0.0015), 8, 64); draw();
  }, {passive: false});
  $("#minimap").addEventListener("pointerdown", e => {
    const r = e.target.getBoundingClientRect();
    S.cam.cx = (e.clientX - r.left) / r.width * S.world.W; S.cam.cy = (e.clientY - r.top) / r.height * S.world.H; draw();
  });
}

// A left click: in go-to mode, go there; on your city, open it; on your unit, pick it (or one of a stack).
function clickTile(x, y) {
  if (S.mode) return moveTo(x, y);
  const mine = myUnits().filter(u => u.x === x && u.y === y);
  const city = (state().cities || []).find(c => c.x === x && c.y === y);
  if (city) return openCity(city.id);
  if (mine.length === 1) return select(mine[0].id);
  if (mine.length > 1) return pickFromStack(mine);
}
function pickFromStack(units) {
  dialog(`<header><h2>Units here</h2><span class="grow"></span><button data-x>✕</button></header><div class="body">
    <ul class="opts">${units.map(u => `<li tabindex="0" data-id="${esc(u.id)}"><span>${esc(u.type)} <span class="k">${esc(u.id)}</span></span>
      <span class="k">${esc(statusText(u))}</span><span class="k">${fmtMoves(u.moves_left)}/${u.moves_max}</span></li>`).join("")}</ul></div>`, {kind: "stack"});
  for (const li of $$("#dialog li[data-id]")) li.onclick = li.onkeydown = ev => {
    if (ev.type === "keydown" && ev.key !== "Enter") return; closeDialog(); select(li.dataset.id);
  };
  $("#dialog li")?.focus();
}

// The tooltip: what's known of a tile, then its yield once the env answers.
let tipTimer = 0;
function tileTip(e, [x, y]) {
  const t = S.world.tile(x, y), el = $("#tip");
  if (!t) { hideTip(); return; }
  const c = S.world.city(x, y), us = t.visible ? S.world.unitsAt(x, y) : [];
  const info = S.tileInfo.get(`${x},${y}`);
  let h = c ? `<div><b>${esc(c.name)}</b> <span class="k">size ${c.size}${c.capital ? " · capital" : ""} · ${esc(S.world.civ(c.owner))}</span></div>` : "";
  h += `<div>${esc(t.overlay ? `${t.overlay} on ${t.terrain}` : t.terrain)}${t.river ? " · river" : ""}${t.resource ? ` · <span class="gold">${esc(t.resource)}</span>` : ""}</div>`;
  if (t.improvements.length) h += `<div class="k">${t.improvements.map(esc).join(", ")}</div>`;
  h += t.owner >= 0 ? `<div class="k">${esc(S.world.civ(t.owner))}'s territory</div>` : "";
  for (const u of us) h += `<div><span class="sw" style="background:rgb(${S.world.color(u.owner).join(",")})"></span>${u.count > 1 ? `${u.count} ` : ""}${esc(u.type)}${u.id ? ` <span class="k">${esc(u.id)}</span>` : ` <span class="k">${esc(S.world.civ(u.owner))}</span>`}</div>`;
  if (!t.visible) h += `<div class="k">not in sight now</div>`;
  if (info) {
    const y_ = info.yield || {};
    h += `<div class="k" style="margin-top:3px">yield ${y_.food ?? "?"} food · ${y_.shields ?? "?"} shields · ${y_.commerce ?? "?"} trade</div>`;
    if (info.city_site && !c) h += `<div class="k">${info.city_site.ok ? "a city can be founded here" : esc(info.city_site.reason)}</div>`;
  }
  el.innerHTML = h; el.hidden = false;
  let px = e.clientX + 16, py = e.clientY + 16;
  if (px + el.offsetWidth > innerWidth - 8) px = e.clientX - el.offsetWidth - 16;
  if (py + el.offsetHeight > innerHeight - 8) py = e.clientY - el.offsetHeight - 16;
  el.style.left = px + "px"; el.style.top = py + "px";
  clearTimeout(tipTimer);
  if (!info) tipTimer = setTimeout(async () => {
    try {
      const r = await api(`play/api/tile?x=${x}&y=${y}`);
      S.tileInfo.set(`${x},${y}`, (r.tiles || [])[0] || r);
      if (S.hover && S.hover[0] === x && S.hover[1] === y) tileTip(e, [x, y]);
    } catch (err) { /* off the known map */ }
  }, 450);
}
function hideTip() { $("#tip").hidden = true; clearTimeout(tipTimer); }

// ======================================================================== keys: the game's hotkeys

function keys() {
  document.addEventListener("keydown", e => {
    if (!S.view || e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (S.dialog) {
      if (e.key === "Escape") { e.preventDefault(); closeDialog(); }
      return;
    }
    const k = e.key;
    if (k === "F1") { e.preventDefault(); return advisor("domestic"); }
    if (k === "F4") { e.preventDefault(); return advisor("foreign"); }
    if (k === "F6") { e.preventDefault(); return advisor("science"); }
    if (k === "Escape") { S.mode = null; $("#map").classList.remove("goto"); return; }
    if (k === "Enter") { e.preventDefault(); return endTurn(e.shiftKey); }
    if (k === " " && !S.sel) { e.preventDefault(); return endTurn(); }   // "ENTER or SPACEBAR for next turn"
    if (k === "c" || (k === "C" && !e.shiftKey)) { const u = unit(S.sel); if (u) centerOn(u.x, u.y); return; }
    if (k === "w") { e.preventDefault(); return waitUnit(); }
    if (k === "Tab") { e.preventDefault(); return selectNext(); }
    if (k === "t" && S.art) return useArt(S.artState !== "on");
    const dir = MOVE_KEYS[e.code] || MOVE_KEYS[k];
    if (dir && S.sel) { e.preventDefault(); return step(dir); }
    const order = KEYS[k];
    if (order && S.sel) {
      e.preventDefault();
      const u = unit(S.sel);
      if (order === "goto" || order === "bombard" || (u.orders || []).includes(order)) return command(order);
      toast(`${u.type} can't ${order.replace("_", " ")} now.`, true);
    }
  });
}

// ======================================================================== dialogs

function dialog(html, opts = {}) {
  S.dialog = opts;
  $("#dialog").innerHTML = `<div class="scrim ${opts.side ? "side" : ""}"><div class="dlg" role="dialog">${html}</div></div>`;
  for (const b of $$("#dialog [data-x]")) b.onclick = closeDialog;
  if (!opts.side) $("#dialog .scrim").onclick = e => { if (e.target.classList.contains("scrim")) closeDialog(); };
  hideTip(); draw();
}
function closeDialog() {
  const done = S.dialog?.onClose; S.dialog = null; $("#dialog").innerHTML = ""; draw();
  if (done) done();
}
function ask(title, html, yes = "OK", cls = "primary") {
  return new Promise(resolve => {
    dialog(`<header><h2>${esc(title)}</h2></header><div class="body"><div>${html}</div>
      <div class="actions"><button class="${cls}" data-yes>${esc(yes)}</button><button data-no>Cancel</button></div></div>`,
      {kind: "ask", onClose: () => resolve(false)});
    $("#dialog [data-yes]").onclick = () => { S.dialog.onClose = null; closeDialog(); resolve(true); };
    $("#dialog [data-no]").onclick = () => closeDialog();
    $("#dialog [data-yes]").focus();
  });
}

// A notice as the person reads it: a message from another leader with its sender, as agents read it, and its text as
// written; the env's own notices without their tool hints.
const noticeText = n => n.kind === "message"
  ? `✉ ${n.from}${n.label ? ` (${n.label})` : ""} to ${n.to_all ? "all" : "you"}: "${n.text}"` : plain(n.text);

// The start of a turn: what happened, as the game's advisors report it.
function turnReport() {
  const s = state(), events = s.last_events || [], notices = S.view.notices || [];
  if (S.lastEventsTurn === s.turn) return;
  S.lastEventsTurn = s.turn;
  const noResearch = (s.blockers || []).some(b => b.kind === "no_research");
  if (!events.length && !notices.length) { if (noResearch) advisor("science"); return; }
  const items = [...notices.map(n => ({...n, kind: "notice", text: noticeText(n)})),
                 ...events.map(ev => ({...ev, text: plain(ev.text)}))];
  dialog(`<header><h2>Turn ${s.turn}</h2><span class="muted">${esc(state().civ)}</span><span class="grow"></span><button data-x>✕</button></header>
    <div class="body"><ul class="plain events">${items.map(ev =>
      `<li class="${HOT.has(ev.kind) || ev.kind === "notice" ? "hot" : ""}" ${ev.x != null ? `data-x="${ev.x}" data-y="${ev.y}" tabindex="0" style="cursor:pointer"` : ""}>${esc(ev.text)}</li>`).join("")}</ul>
    <div class="actions"><button class="primary" data-x>Continue</button></div></div>`,
    {kind: "report", onClose: () => { if (noResearch) advisor("science"); }});
  for (const li of $$("#dialog li[data-x]")) li.onclick = () => { closeDialog(); centerOn(+li.dataset.x, +li.dataset.y); };
  $("#dialog .primary").focus();
}

async function openCity(id) {
  let c;
  try { c = await api(`play/api/city?city=${encodeURIComponent(id)}`); } catch (e) { toast(e.message, true); return; }
  centerOn(c.x, c.y);
  const units = myUnits().filter(u => u.x === c.x && u.y === c.y);
  const pct = c.production_cost ? Math.min(100, c.production_stored / c.production_cost * 100) : 0;
  const food = c.food_needed ? Math.min(100, c.food_stored / c.food_needed * 100) : 0;
  dialog(`<header><h2>${esc(c.name)}</h2><span class="muted">size ${c.size}${c.capital ? " · capital" : ""}</span>
      ${c.disorder ? '<span class="bad">· in disorder</span>' : ""}<span class="grow"></span><button data-x>✕</button></header>
    <div class="body">
      <div class="grid2">
        <div class="box"><div class="l">Food</div><div class="v">${c.food_stored}/${c.food_needed} <span class="muted">${c.food_per_turn >= 0 ? "+" : ""}${c.food_per_turn}/turn</span></div>
          <div class="meter" style="margin-top:4px"><i style="width:${food}%;background:#59b35f"></i></div>
          <div class="muted" style="font-size:12px;margin-top:3px">${c.turns_to_grow != null ? `grows in ${c.turns_to_grow} turns` : "not growing"}</div></div>
        <div class="box"><div class="l">Production</div><div class="v">${esc(c.producing || "nothing")} <span class="muted">${c.production_stored}/${c.production_cost ?? "?"}</span></div>
          <div class="meter" style="margin-top:4px"><i style="width:${pct}%;background:#d1a54a"></i></div>
          <div class="muted" style="font-size:12px;margin-top:3px">${c.shields_per_turn} shields/turn${c.turns_to_complete != null ? ` · done in ${c.turns_to_complete} turns` : ""}</div></div>
      </div>
      <div class="actions"><button id="buy" ${c.producing && c.producing !== "Wealth" ? "" : "disabled"}>Buy ${esc(c.producing || "")}</button></div>
      <h3>Build</h3>
      <ul class="opts">${(c.options || []).map(o => `<li tabindex="0" data-item="${esc(o.name)}" class="${o.name === c.producing ? "cur" : ""}">
        <span>${esc(o.name)} <span class="k">${esc(o.kind)}</span></span><span class="k">${o.cost ?? ""} shields</span><span class="k">${o.turns != null ? o.turns + " t" : ""}</span></li>`).join("")}</ul>
      ${(c.buildings || []).length ? `<h3>Buildings</h3><div class="muted">${c.buildings.map(esc).join(" · ")}</div>` : ""}
      ${units.length ? `<h3>Units in the city</h3><ul class="opts">${units.map(u => `<li tabindex="0" data-unit="${esc(u.id)}"><span>${esc(u.type)} <span class="k">${esc(u.id)}</span></span>
        <span class="k">${esc(statusText(u))}</span><span class="k">${fmtMoves(u.moves_left)}/${u.moves_max}</span></li>`).join("")}</ul>` : ""}
    </div>`, {kind: "city", side: true, city: c});
  for (const li of $$("#dialog li[data-item]")) li.onclick = li.onkeydown = async ev => {
    if (ev.type === "keydown" && ev.key !== "Enter") return;
    const res = await act("set_production", {city: c.id, item: li.dataset.item});
    if (res) openCity(c.id);
  };
  for (const li of $$("#dialog li[data-unit]")) li.onclick = () => { closeDialog(); select(li.dataset.unit); };
  $("#buy").onclick = async () => {
    if (!(await ask("Buy", `Complete ${esc(c.producing)} in ${esc(c.name)} next turn? Monarchy and later governments pay gold; Despotism pays with citizens.`, "Buy"))) return openCity(c.id);
    const res = await act("buy", {city: c.id}); openCity(c.id); return res;
  };
}

async function advisor(which) {
  if (which === "science") {
    let t;
    try { t = await api("play/api/techs"); } catch (e) { toast(e.message, true); return; }
    const res = state().research || {};
    dialog(`<header><h2>Science advisor</h2><span class="grow"></span><button data-x>✕</button></header><div class="body">
      <div class="box"><div class="l">Researching</div><div class="v">${res.current ? `${esc(res.current)} <span class="muted">${res.beakers}/${res.cost} · ${res.turns_left ?? "?"} turns</span>` : '<span class="bad">nothing — pick a tech</span>'}</div></div>
      <h3>Choose what to research</h3>
      <ul class="opts">${(t.available || []).map(a => `<li tabindex="0" data-tech="${esc(a.name)}" class="${a.name === t.current ? "cur" : ""}">
        <span>${esc(a.name)} <span class="k">${esc(a.era || "")}</span></span><span class="k">${a.cost} beakers</span><span class="k">${a.turns ?? "?"} t</span>
        ${(a.unlocks || []).length ? `<span class="sub">unlocks ${a.unlocks.map(esc).join(", ")}</span>` : ""}</li>`).join("")}</ul>
      <h3>Known</h3><div class="muted">${(t.known || []).map(esc).join(" · ")}</div></div>`, {kind: "science"});
    for (const li of $$("#dialog li[data-tech]")) li.onclick = li.onkeydown = async ev => {
      if (ev.type === "keydown" && ev.key !== "Enter") return;
      const r = await act("research", {tech: li.dataset.tech}); if (r) closeDialog();
    };
    $("#dialog li")?.focus();
  } else if (which === "domestic") {
    const s = state(), rs = s.rates || {};
    dialog(`<header><h2>Domestic advisor</h2><span class="grow"></span><button data-x>✕</button></header><div class="body">
      <h3>How your cities spend their trade</h3>
      <div class="range"><span>Science</span><input type="range" id="r-sci" min="0" max="10" value="${rs.science ?? 5}"><b id="v-sci"></b></div>
      <div class="range"><span>Luxury</span><input type="range" id="r-lux" min="0" max="10" value="${rs.luxury ?? 0}"><b id="v-lux"></b></div>
      <div class="range"><span>Tax</span><span class="muted">the rest</span><b id="v-tax"></b></div>
      <div class="actions"><button class="primary" id="setrates">Set rates</button></div>
      <h3>Government</h3>
      <div class="box"><div class="v">${esc(s.government)}${s.anarchy_until ? ` <span class="bad">anarchy until T${s.anarchy_until}${s.revolution_target ? `, then ${esc(s.revolution_target)}` : ""}</span>` : ""}</div></div>
      ${(s.governments || []).length ? `<ul class="opts" style="margin-top:6px">${s.governments.map(gv => `<li tabindex="0" data-gov="${esc(gv.name)}">
        <span>${esc(gv.name)}</span><span class="k">corruption ${esc(gv.corruption)}</span><span class="k">hurry: ${esc(gv.hurry)}</span></li>`).join("")}</ul>
        <div class="muted" style="font-size:12px;margin-top:4px">A revolution brings a few turns of anarchy (no taxes, no science) first.</div>` : ""}
    </div>`, {kind: "domestic"});
    const sync = () => {
      let sci = +$("#r-sci").value, lux = +$("#r-lux").value;
      if (sci + lux > 10) { lux = 10 - sci; $("#r-lux").value = lux; }
      $("#v-sci").textContent = sci; $("#v-lux").textContent = lux; $("#v-tax").textContent = 10 - sci - lux;
    };
    $("#r-sci").oninput = $("#r-lux").oninput = sync; sync();
    $("#setrates").onclick = async () => { const r = await act("set_rates", {science: +$("#r-sci").value, luxury: +$("#r-lux").value}); if (r) closeDialog(); };
    for (const li of $$("#dialog li[data-gov]")) li.onclick = async () => {
      if (!(await ask("Revolution", `Change the government to ${esc(li.dataset.gov)}? Anarchy comes first.`, "Revolution", "danger"))) return advisor("domestic");
      await act("revolution", {government: li.dataset.gov});
    };
  } else if (which === "foreign") {
    let d;
    try { d = await api("play/api/diplomacy"); } catch (e) { toast(e.message, true); return; }
    const civs = d.civs || d.rivals || [];
    dialog(`<header><h2>Foreign advisor</h2><span class="grow"></span><button data-x>✕</button></header><div class="body">
      ${civs.length ? `<table class="civs"><tr><th>Civilization</th><th>Relation</th><th>Score</th><th>Government</th><th></th></tr>
      ${civs.map(r => `<tr><td><b>${esc(r.civ)}</b>${r.seat ? ` <span class="muted">${esc(r.seat)}</span>` : ""}</td>
        <td class="${r.at_war ? "bad" : "good"}">${r.at_war ? `at war${r.peace_price != null ? ` · peace: ${r.peace_price} gold` : r.talks_turn ? ` · talks from T${r.talks_turn}` : ""}` : "peace"}</td>
        <td>${r.score?.total ?? r.score ?? ""}</td><td>${esc(r.government || "")}</td>
        <td>${r.at_war ? `<button data-peace="${esc(r.civ)}" data-gold="${r.peace_price ?? 0}">Propose peace</button>` : `<button class="danger" data-war="${esc(r.civ)}">Declare war</button>`}</td></tr>`).join("")}</table>`
        : '<div class="muted">You haven\'t met another civilization yet.</div>'}</div>`, {kind: "foreign"});
    for (const b of $$("#dialog [data-war]")) b.onclick = async () => {
      if (!(await ask(`War with ${b.dataset.war}?`, `Declare war on ${esc(b.dataset.war)}? They will refuse to talk for some turns.`, "Declare war", "danger"))) return advisor("foreign");
      await act("diplomacy", {action: "declare_war", civ: b.dataset.war}); advisor("foreign");
    };
    for (const b of $$("#dialog [data-peace]")) b.onclick = async () => {
      await act("diplomacy", {action: "propose_peace", civ: b.dataset.peace, gold: +b.dataset.gold || 0}); advisor("foreign");
    };
  }
}

function gameOver() {
  if (S.dialog?.kind === "over") return;
  const g = S.view.game, s = state(), v = g.victory;
  dialog(`<header><h2>Game over</h2></header><div class="body">
    <div style="font-size:16px;margin-bottom:8px">${v ? `${esc(v.label || v.civ)} wins by ${esc(v.kind)} on turn ${v.turn}.` : `The game ended at turn ${g.turn}.`}</div>
    <div class="box"><div class="l">Your score</div><div class="v">${s.score.total} <span class="muted">${s.score.cities} cities · ${s.score.pop} pop · ${s.score.techs} techs</span></div></div>
    <div class="actions"><button class="primary" data-x>Look at the map</button></div></div>`, {kind: "over"});
}

// ======================================================================== polling: the others' turns

let polling = false;
async function poll() {
  if (polling || !S.view) return;
  polling = true;
  try {
    const st = await api("play/api/status");
    const g = S.view.game;
    if (st.game !== S.gameId || st.turn !== g.turn || st.game_over !== g.game_over || st.ended !== g.ended) await loadView();
    else if (g.ended) {   // others finishing: refresh who we wait for
      S.view.game.waiting_for = st.waiting_for || g.waiting_for;
      S.view.game.seats = st.seats || g.seats;
      renderWaiting(); renderBar(); renderStatus();
    } else if (st.seats) { S.view.game.seats = st.seats; renderBar(); }
  } catch (e) {
    if (e.status === 401) centerMessage(esc(e.message));
  } finally {
    polling = false;
  }
}

async function boot() {
  if (!TOKEN) { centerMessage("This page plays a seat in an OpenCiv3 game.<br>Open it with your play link: <b>/play#token=…</b><br><span class='muted'>agent-env openciv3 play prints it.</span>"); return; }
  shell();
  try { await loadView(); }
  catch (e) { centerMessage(esc(e.message)); return; }
  setInterval(poll, 1500);
}
boot();
