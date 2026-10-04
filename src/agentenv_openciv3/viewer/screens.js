// The OpenCiv3 client's advisor screens (Domestic F1, Foreign F4, Science F6) and its city screen, in its own art
// (docs/play.md, section 6), when the art is on. Each is the client's 1024x768 frame, scaled to the window, with
// absolutely placed HTML over its background; the numbers are the client's (vendor/OpenCiv3/C7: UIElements/Advisors,
// UIElements/CityScreen, UIElements/Popups, Map/TileAssignmentLayer.cs). Every action goes through play.js's act(),
// as the plain dialogs' do. Needs map.js, art.js and play.js (artScreensOn, artAdvisor and artCity are its hooks).
"use strict";

const SCR = {sciEra: null, foreignTab: 0, images: new Map()};
const SCR_ERAS = ["Ancient Times", "Middle Ages", "Industrial Age", "Modern Era"];
// Only with art that has the screens (an older conversion has just the map's sheets).
const artScreensOn = () => artOn() && !!(S.art.m.screens && S.art.m.techs);
const scrUrl = n => S.art.url((S.art.m.screens || {})[n] || S.art.m.sheets[n]);
const scrScale = () => Math.min(innerWidth / 1024, innerHeight / 768);
// The seat's era (state.era; else its capital's, as known_map has it), by which the client picks heads and pages.
function playerEra() {
  const s = state();
  if (s.era != null) return clamp(s.era | 0, 0, 3);
  const c = (s.cities || []).find(x => x.capital) || (s.cities || [])[0], k = c && S.world.city(c.x, c.y);
  return clamp((k && k.era) | 0, 0, 3);
}

// ---- pieces: a crop of a sheet, a three-state button, a label, the frame ----

function spr(n, [x, y, w, h], left, top, cls = "", attrs = "") {
  return `<i class="cs-sp ${cls}" ${attrs} style="left:${left}px;top:${top}px;width:${w}px;height:${h}px;` +
    `background-image:url('${scrUrl(n)}');background-position:${-x}px ${-y}px"></i>`;
}
// A crop drawn at another size (the city screen's boxes scale their icons): a preloaded map sheet.
function sprAt(n, [x, y, w, h], left, top, dw, dh) {
  const im = S.art.img(n), kx = dw / w, ky = dh / h;
  if (!im) return "";
  return `<i class="cs-sp" style="left:${left}px;top:${top}px;width:${dw}px;height:${dh}px;background-image:url('${scrUrl(n)}');` +
    `background-size:${im.naturalWidth * kx}px ${im.naturalHeight * ky}px;background-position:${-x * kx}px ${-y * ky}px"></i>`;
}
// A TextureButton: its normal crop, the hover one under the mouse, the pressed one while held (CSS swaps them).
function btn(n, [x, y, w, h], hov, prs, left, top, attrs = "", inner = "") {
  return `<button class="cs-btn" ${attrs} style="left:${left}px;top:${top}px;width:${w}px;height:${h}px;` +
    `background-image:url('${scrUrl(n)}');--n:${-x}px ${-y}px;--h:${-hov[0]}px ${-hov[1]}px;--p:${-prs[0]}px ${-prs[1]}px">${inner}</button>`;
}
// A Godot Label: its line box's top-left at (x, y), Noto Sans, black unless styled.
const lab = (x, y, text, size = 14, style = "", cls = "") =>
  `<div class="cs-lb ${cls}" style="left:${x}px;top:${y}px;font-size:${size}px;${style}">${esc(text)}</div>`;
const bg = n => `<img class="cs-bg" alt="" src="${scrUrl(n)}">`;

// The sheets a screen needs, loaded before it shows (the backgrounds are large); the rest of the screens' art is
// fetched behind it the first time, so the next screens open at once.
function scrLoad(names) {
  const one = n => {
    if (!SCR.images.has(n)) SCR.images.set(n, new Promise(ok => { const im = new Image(); im.onload = im.onerror = ok; im.src = scrUrl(n); }));
    return SCR.images.get(n);
  };
  const wait = Promise.race([Promise.all(names.map(one)), new Promise(ok => setTimeout(ok, 4000))]);
  setTimeout(() => Object.keys(S.art.m.screens || {}).forEach(one), 50);
  return wait;
}

// Shows a screen: the frame, centred and scaled to the window, in #dialog, so play.js's Esc and closeDialog() work.
function scrOpen(kind, html, opts = {}) {
  S.dialog = {kind, art: true, ...opts, onKey: e => scrKey(e, opts.onKey)};
  $("#dialog").innerHTML = `<div class="cs-root${kind === "city" ? " cs-city" : ""}"><div class="cs-frame" style="font-family:${S.art.font}">${html}</div></div>`;
  for (const b of $$("#dialog [data-close]")) b.onclick = () => closeDialog();
  scrFit(); hideTip(); draw();
}
function scrFit() {
  const root = $("#dialog .cs-root");
  if (!root) return;
  const s = scrScale();
  root.style.setProperty("--s", s);
  if (S.dialog?.kind === "city" && S.dialog.art) cityCamera(S.dialog.city, s);
}
addEventListener("resize", () => { if (S.dialog?.art) scrFit(); });
// Keys while a screen is open: a popup takes Esc; F1, F4 and F6 switch advisors; the city screen's arrows.
function scrKey(e, more) {
  const pop = S.dialog?.popup;
  if (pop) { if (e.key === "Escape") { e.preventDefault(); pop(null); } return true; }
  const adv = {F1: "domestic", F4: "foreign", F6: "science"}[e.key];
  if (adv) { e.preventDefault(); if (S.dialog?.kind === "city") closeDialog(); advisor(adv); return true; }
  return more ? more(e) : false;
}

// ---- popups (Popups/Popup.cs): the parchment tiled from popupborders.png, an advisor's head over it, a header,
// the message, and the choices as the client's orb buttons; or an informational one with the OK button ----

const POP_HEADS = {domestic: "head_domestic", foreign: "head_foreign", science: "head_science"};
function scrPopup({advisor: who = "domestic", mood = 3, header, text, choices = null, width = 530}) {
  return new Promise(resolve => {
    const info = !choices, w = info ? 430 : width;
    const n = choices ? choices.length : 0, h = info ? 230 : Math.max(320, 215 + 30 * n + 15);
    const ph = h - 110, left = 1024 - 10 - w, top = Math.round((768 - h) / 2);
    let tiles = "";
    const xs = [0], ys = [0];
    for (let x = 61; x < w - 61; x += 61) xs.push(x);
    xs.push(w - 61);
    for (let y = 44; y < ph - 44; y += 44) ys.push(y);
    ys.push(ph - 44);
    ys.forEach((y, j) => xs.forEach((x, i) => {
      const cx = i === 0 ? 251 : i === xs.length - 1 ? 375 : 313, cy = j === 0 ? 1 : j === ys.length - 1 ? 91 : 46;
      tiles += spr("popup", [cx, cy, 61, 44], x, 110 + y);
    }));
    const head = spr(POP_HEADS[who], [1 + 150 * mood, 40, 149, 110], info ? 275 : 375, 0);
    const body = info
      ? lab(25, 160, text) + btn("xo", [1, 1, 19, 19], [37, 1], [73, 1], w - 40, h - 40, `data-pick="0" title="OK"`)
      : lab(25, 170, text) + choices.map((c, i) => `<button class="cs-orb" data-pick="${i}" style="left:30px;top:${215 + 30 * i}px">` +
          `<i style="background-image:url('${scrUrl("orbs")}')"></i><span>${esc(c)}</span></button>`).join("");
    const el = document.createElement("div");
    el.className = "cs-modal";
    el.innerHTML = `<div class="cs-pop" style="left:${left}px;top:${top}px;width:${w}px;height:${h}px">${tiles}${head}
      <div class="cs-lb cs-pophd" style="top:120px;width:${w}px">${esc(header)}</div>${body}</div>`;
    $("#dialog .cs-frame").appendChild(el);
    const done = v => { if (S.dialog) S.dialog.popup = null; el.remove(); resolve(v); };
    S.dialog.popup = done;
    for (const b of $$("[data-pick]", el)) b.onclick = () => done(info ? true : +b.dataset.pick);
    $("[data-pick]", el)?.focus();
  });
}

// ---- the advisors' common pieces (AdvisorUtils.cs) ----

// Title left edges: 512 - (text width + 14 per glyph + 22 per space, the last glyph's not counted) / 2, Noto Sans 26.
const TITLE_LEFT = {"DOMESTIC ADVISOR": 278.7, "FOREIGN ADVISOR": 295.4, "SCIENCE ADVISOR": 299.0};
function advCommon(head, title, text) {
  const era = playerEra();
  return `<div class="cs-lb cs-title" style="left:${TITLE_LEFT[title]}px;top:15px">${esc(title)}</div>
    ${spr(head, [1, 150 * (era + 1) - 110, 149, 110], 851, 0)}
    ${spr("dialogbox", [0, 0, 207, 132], 806, 110)}${lab(815, 119, text)}
    ${btn("exit", [0, 0, 72, 48], [72, 0], [144, 0], 952, 720, `data-close title="Close (Esc)" aria-label="Close"`)}`;
}

async function artAdvisor(which) {
  if (which === "domestic") return artDomestic();
  if (which === "foreign") return artForeign();
  if (which === "science") return artScience();
}

// ======================================================================== the domestic advisor (DomesticAdvisor.cs)

// The column header icons on CityIcons.png, at their natural size: [crop, position].
const DOM_ICONS = [[[223, 7, 20, 20], 241, 247], [[191, 7, 20, 20], 251, 256], [[160, 5, 22, 22], 294, 245],
  [[129, 5, 22, 22], 298, 253], [[95, 2, 30, 30], 333, 246], [[64, 2, 29, 29], 342, 251], [[591, 2, 29, 29], 458, 251],
  [[373, 2, 29, 29], 443, 251], [[34, 2, 29, 29], 493, 246], [[746, 2, 29, 29], 537, 248]];
// A citizen's head on popHeads.png by mood row (0 content, 1 happy, 3 unhappy) and era.
const popHead = (row, era, x, y) => spr("pop_heads", [1, 200 * era + 50 * row + 1, 48, 48], x, y);
// The heads in mood groups (happy, content, unhappy), a head's gap between groups, overlapping when wider than `max`.
function headRow(city, era, x0, y, max) {
  const groups = [[1, city.happy | 0], [0, city.content | 0], [3, city.unhappy | 0]].filter(([, n]) => n > 0);
  const width = 48 * groups.reduce((s, [, n]) => s + n, 0) + 48 * Math.max(0, groups.length - 1);
  const step = max && width > max ? Math.floor(max / width * 48) : 48;
  let x = x0 == null ? 512 - Math.floor(width / 2) : x0, h = "";
  groups.forEach(([row, n], i) => {
    if (i) x += step;
    for (let k = 0; k < n; k++) { h += popHead(row, era, x, y); x += step; }
  });
  return h;
}

// The client's SpaceAlignedDotFormat: "a.b", a space before a below 10 and after b below 10.
const dotted = (a, b) => `${(a | 0) < 10 ? " " : ""}${a | 0}.${b | 0}${(b | 0) < 10 ? " " : ""}`;

// The city screen's heads from its citizens (CityScreen.RenderPopHeads), in their order: laborers by mood, a head's
// gap between moods and before the specialists, each specialist its own head with what it adds under it (CityIcons:
// a copy per point, half an icon apart). Centred on x 512.
const SPEC_ICONS = [["luxuries", [373, 1, 30, 30]], ["taxes", [435, 1, 30, 30]], ["research", [497, 1, 30, 30]],
  ["corruption", [528, 1, 30, 30]], ["construction", [404, 1, 30, 30]]];
function citizenHeads(c, era, y) {
  const MOOD = {happy: 1, content: 0, unhappy: 3}, spec = new Map((c.specialists || []).map(t => [t.type, t]));
  const items = [];
  let last = null;
  for (const z of c.citizens) {
    const group = z.works === "specialist" ? "specialist" : z.mood;
    if (last != null && group !== last && (group === "specialist" || last !== "specialist")) items.push(null);
    last = group;
    items.push(z);
  }
  const width = 48 * items.length;
  let x = 512 - Math.floor(width / 2), h = "";
  for (const z of items) {
    if (z && z.works === "specialist") {
      const t = spec.get(z.specialist) || {index: 1};
      h += spr("pop_heads", [50 * era + 1, 800 + 50 * ((t.index || 1) - 1) + 1, 48, 48], x, y, "", `title="${esc(z.specialist)}"`);
      let k = 0;
      for (const [key, crop] of SPEC_ICONS) for (let n = 0; n < (t[key] | 0); n++) h += spr("yield_icons", crop, x + 15 * k++, y + 38);
    } else if (z) h += popHead(MOOD[z.mood] ?? 0, era, x, y);
    x += 48;
  }
  return h;
}

// The client's rule for one step of a rate (MsgChangeSliders): more science takes a tenth from taxes, else from
// luxury; less gives it to taxes. The same with science and luxury swapped.
function rateStep(r, which, up) {
  let sci = r.science, lux = r.luxury, tax = 10 - sci - lux;
  if (which === "science") {
    if (up) { if (sci >= 10) return r; sci++; if (tax > 0) tax--; else lux--; }
    else { if (sci <= 0) return r; sci--; }
  } else if (up) { if (lux >= 10) return r; lux++; if (tax > 0) tax--; else sci--; }
  else { if (lux <= 0) return r; lux--; }
  return {science: sci, luxury: lux};
}
async function setRates(which, steps) {
  const rs = state().rates || {};
  let r = {science: rs.science ?? 5, luxury: rs.luxury ?? 0};
  for (let i = 0; i < Math.abs(steps); i++) r = rateStep(r, which, steps > 0);
  if (r.science !== rs.science || r.luxury !== rs.luxury) await act("set_rates", r);
  if (S.dialog?.kind === "domestic") artDomestic();   // the client re-shows the advisor after every change
}

async function artDomestic() {
  await scrLoad(["domestic", "head_domestic", "dialogbox", "exit", "domestic_button", "plusminus", "pop_heads"]);
  const s = state(), g = S.view.game, rs = s.rates || {}, era = playerEra(), res = s.research || {};
  const left = s.anarchy_until ? Math.max(0, s.anarchy_until - g.turn) : 0;
  let h = bg("domestic") + advCommon("head_domestic", "DOMESTIC ADVISOR",
    s.anarchy_until ? `${left} turns of anarchy left` : "You are running OpenCiv3!");
  // the income (green) and expenses (salmon) boxes, the treasury and the net (state.finance: AggregateFlows)
  const fin = s.finance, net = s.gold_per_turn | 0;
  if (fin) {
    const i = fin.income || {}, x = fin.expenses || {};
    h += `<div class="cs-lb" style="left:84px;top:98px;width:138px;font-size:11px;text-align:right">${esc(
      `From cities: +${i.cities | 0}\nFrom taxmen: +${i.taxmen | 0}\nFrom other civs: +${i.other_civs | 0}\nFrom interest: +${i.interest | 0}`)}</div>`;
    h += lab(254, 102, `Income: ${i.total | 0}`) + lab(254, 152, `Expenses: ${x.total | 0}`);
    h += lab(379, 87, `-${x.science | 0}: Science\n-${x.entertainment | 0}: Entertainment\n-${x.corruption | 0}: Corruption\n` +
      `-${x.maintenance | 0}: Maintenance\n-${x.unit_costs | 0}: Unit costs\n-${x.other_civs | 0}: To other civs`, 11);
  }
  h += lab(83, 193, `Treasury: ${s.gold}`);
  h += lab(254, 193, net > 0 ? `Net gain: +${net}` : net < 0 ? `Net loss: ${net}` : "Neutral: 0");
  h += lab(390, 195, net > 0 ? "Growing!" : net < 0 ? "Shrinking!" : "Balanced");
  h += lab(565, 165, res.current ? `${res.current} (${res.turns_left != null && res.turns_left <= 50 ? res.turns_left : "--"} turns)` : "Not selected (-- turns)");
  // the government: the button names it; it starts a revolution when another government is open
  h += lab(559, 189, "Government", 13);
  h += btn("domestic_button", [1, 1, 145, 24], [1, 26], [1, 52], 650, 186,
    `id="cs-gov" ${(s.governments || []).length || s.anarchy_until ? "" : "disabled"} title="Change the government"`,
    `<span class="cs-lb" style="left:6px;top:2px">${esc(s.government)}</span>`);
  h += lab(122, 253, "Cities") + lab(707, 253, "Population") + lab(899, 252, "Producing");
  for (const [crop, x, y] of DOM_ICONS) h += spr("yield_icons", crop, x, y);
  // the rates: science and luxury sliders (0..10), their grabber the beaker and the smiley; taxes are the rest
  const grab = c => `url('${scrUrl("yield_icons")}') ${-c[0]}px ${-c[1]}px no-repeat`;
  h += `<input type="range" class="cs-range" id="r-sci" min="0" max="10" step="1" value="${rs.science ?? 5}" aria-label="Science rate"
      style="left:566px;top:83px;--g:${grab([34, 2])}">`;
  h += `<input type="range" class="cs-range" id="r-lux" min="0" max="10" step="1" value="${rs.luxury ?? 0}" aria-label="Luxury rate"
      style="left:566px;top:132px;--g:${grab([376, 2])}">`;
  h += lab(760, 89, `${(rs.science ?? 0) * 10}%`, 14, "", "cs-pct-sci") + lab(760, 134, `${(rs.luxury ?? 0) * 10}%`, 14, "", "cs-pct-lux");
  const plus = (x, y, a) => btn("plusminus", [0, 8, 13, 14], [12, 8], [24, 8], x, y, a);
  const minus = (x, y, a) => btn("plusminus", [0, 0, 13, 9], [12, 0], [24, 0], x, y, a);
  h += plus(732, 111, `data-rate="science" data-d="1" title="More science"`) + minus(562, 113, `data-rate="science" data-d="-1" title="Less science"`);
  h += plus(732, 125, `data-rate="luxury" data-d="1" title="More luxury"`) + minus(562, 129, `data-rate="luxury" data-d="-1" title="Less luxury"`);
  // the cities: a row each, 54 px apart: food eaten.surplus, shields corrupt.useful, commerce corrupt.(the rest) at
  // the row's top; maintenance, happy.content, science and taxes centred; the heads; what it builds
  h += `<div class="cs-rows">${(s.cities || []).map((c, i) => {
    const y = 54 * i, col = (x, text, mid) => `<div class="cs-lb cs-col" style="left:${x - 72}px;top:${mid ? 15 : 0}px">${esc(text)}</div>`;
    const turns = c.turns_to_complete != null && c.turns_to_complete < 9999999 ? c.turns_to_complete : "--";
    const cm = c.commerce, sh = c.shields;
    return `<div class="cs-row" style="top:${y}px">
      <button class="cs-tbtn cs-dname" data-city="${esc(c.id)}" style="left:${106 - 72}px;top:0;width:127px;height:50px" title="Open ${esc(c.name)}">${esc(c.name)}</button>
      ${c.food_eaten != null ? col(237, dotted(c.food_eaten, c.food_per_turn | 0)) : col(237, `${c.food_per_turn > 0 ? "+" : ""}${c.food_per_turn ?? ""}`)}
      ${sh ? col(281, dotted(sh.corrupt, sh.useful)) : col(281, c.shields_per_turn ?? "")}
      ${cm ? col(325, dotted(cm.corrupt, (cm.taxes | 0) + (cm.science | 0) + (cm.luxury | 0))) : ""}
      ${c.maintenance != null ? col(398, String(c.maintenance), true) : ""}
      ${col(442, dotted(c.happy, c.content), true)}
      ${cm ? col(486, String(cm.science | 0), true) + col(530, String(cm.taxes | 0), true) : ""}
      ${headRow(c, era, 603 - 72, 0, 220)}
      <div class="cs-lb cs-prod" style="left:${881 - 72}px;top:0">${esc(c.producing || "--")}<br>(${turns} turns)</div></div>`;
  }).join("")}</div>`;
  scrOpen("domestic", h);
  $("#cs-gov").onclick = revolutionPopups;
  for (const b of $$("#dialog [data-rate]")) b.onclick = () => setRates(b.dataset.rate, +b.dataset.d);
  for (const [id, which, pct] of [["#r-sci", "science", ".cs-pct-sci"], ["#r-lux", "luxury", ".cs-pct-lux"]]) {
    const el = $(id), was = +el.value;
    el.oninput = () => { $(`#dialog ${pct}`).textContent = `${el.value * 10}%`; };
    el.onchange = () => { el.blur(); setRates(which, +el.value - was); };
  }
  for (const b of $$("#dialog [data-city]")) b.onclick = () => { closeDialog(); artCity(b.dataset.city); };
}

// The revolution, as the client asks it (DomesticAdvisor.cs); the bridge takes the new government up front, so the
// government is picked next (the client asks that when the anarchy ends: GovernmentSelection.cs).
async function revolutionPopups() {
  const s = state(), govs = s.governments || [];
  if (s.anarchy_until) {
    await scrPopup({advisor: "domestic", mood: 0, header: "Domestic Advisor", text: "You already started a revolution. Remember?"});
    return;
  }
  if (!govs.length) return;
  const yes = await scrPopup({header: "Domestic Advisor", text: "You say you want a revolution?",
    choices: ["Yes, you know it's gonna be alright.", "No. You can count me out."]});
  if (yes !== 0) return;
  let pick = 0;
  if (govs.length > 1) {
    pick = await scrPopup({mood: 0, header: "Select a new government type", text: "Which government, once the anarchy ends?",
      choices: govs.map(gv => `${gv.name} (corruption ${gv.corruption}, hurry: ${gv.hurry})`)});
    if (pick == null) return;
  }
  await act("revolution", {government: govs[pick].name});
  if (S.dialog?.kind === "domestic") artDomestic();
}

// ======================================================================== the foreign advisor (ForeignAdvisor.cs)

// The client shows only its tab panel (Treaties, Trades, Details) and no data; the bridge's diplomacy goes on the
// empty main sheet (a row per civ met: relations, score, government, war or peace) and in the panel's body.
async function artForeign() {
  let d;
  const [, got] = await Promise.all([scrLoad(["foreign", "head_foreign", "foreign_tab", "dialogbox", "exit", "orbs"]),
    api("play/api/diplomacy").then(x => (d = x), e => { toast(e.message, true); return null; })]);
  if (!got) return;
  const civs = d.civs || d.rivals || [], tab = SCR.foreignTab, me = state();
  let h = bg("foreign") + advCommon("head_foreign", "FOREIGN ADVISOR", "You are running OpenCiv3!");
  h += spr("foreign_tab", [1 + 224 * tab, 1, 223, 236], 740, 300);
  h += [0, 1, 2].map(i => `<button class="cs-hit" data-tab="${i}" style="left:${740 + 77 * i}px;top:300px;width:74px;height:18px"
    title="${["Treaties", "Trades", "Details"][i]}"></button>`).join("");
  // the main sheet: the civs met
  h += lab(100, 90, "Civilization") + lab(290, 90, "Relations") + lab(490, 90, "Score") + lab(550, 90, "Government");
  h += `<div class="cs-rule" style="left:96px;top:112px;width:710px"></div>`;
  if (!civs.length) h += lab(100, 124, "You haven't met another civilization yet.");
  civs.forEach((r, i) => {
    const y = 124 + 34 * i, seat = S.view.game.seats.find(x => x.civ === r.civ);
    const who = seat?.label && seat.label !== r.civ ? `${r.civ} (${seat.label}${seat.human ? ", a person" : ""})` : r.civ;
    const rel = r.at_war ? `At war${r.peace_price != null ? `: peace for ${r.peace_price} gold` : r.talks_turn ? `: talks from turn ${r.talks_turn}` : ""}` : "At peace";
    h += `<div class="cs-lb cs-clip" style="left:100px;top:${y}px;width:185px" title="${esc(who)}">${esc(who)}</div>`;
    h += `<div class="cs-lb cs-clip" style="left:290px;top:${y}px;width:195px;color:${r.at_war ? "#8b0000" : "#1e5a1e"}" title="${esc(rel)}">${esc(rel)}</div>`;
    h += lab(490, y, String(r.score?.total ?? r.score ?? "")) + `<div class="cs-lb cs-clip" style="left:550px;top:${y}px;width:115px">${esc(r.government || "")}</div>`;
    h += r.at_war
      ? `<button class="cs-orb" data-peace="${esc(r.civ)}" data-gold="${r.peace_price ?? 0}" style="left:670px;top:${y - 1}px"><i style="background-image:url('${scrUrl("orbs")}')"></i><span>Propose peace</span></button>`
      : `<button class="cs-orb" data-war="${esc(r.civ)}" style="left:670px;top:${y - 1}px"><i style="background-image:url('${scrUrl("orbs")}')"></i><span>Declare war</span></button>`;
  });
  // the panel's body: by tab
  const war = civs.filter(r => r.at_war), peace = civs.filter(r => !r.at_war);
  if (tab === 1) {   // trades: the offers standing for you, with Accept and Decline; then a Trade button per civ at peace
    const offers = civs.filter(r => r.trade_offered), lines = [];
    for (const r of offers) lines.push(`<div style="margin-bottom:4px">${esc(offerText(r.civ, r.trade_offered))}<br>
      <button class="cs-tbtn" style="position:static" data-accept="${esc(r.civ)}">Accept</button> · <button class="cs-tbtn" style="position:static" data-decline="${esc(r.civ)}">Decline</button></div>`);
    for (const r of war.filter(r => r.peace_price != null)) lines.push(`<div>${esc(`${r.civ} asks ${r.peace_price} gold for peace`)}</div>`);
    for (const r of peace.filter(r => r.talks !== false)) lines.push(`<div><button class="cs-tbtn" style="position:static" data-trade="${esc(r.civ)}">Trade with ${esc(r.civ)}…</button></div>`);
    h += `<div class="cs-lb cs-panel" style="left:750px;top:326px;max-height:200px;overflow:auto;pointer-events:auto">${lines.join("") || "No offers."}</div>`;
  } else {
    const body = tab === 0 ? [...peace.map(r => `Peace with ${r.civ}`), ...war.map(r => `War with ${r.civ}`)].join("\n") || "No treaties."
      : `${civs.length} civilization${civs.length === 1 ? "" : "s"} met${d.unmet ? `, ${d.unmet} not yet` : ""}\nYour government: ${me.government}`;
    h += `<div class="cs-lb cs-panel" style="left:750px;top:326px">${esc(body)}</div>`;
  }
  scrOpen("foreign", h);
  for (const b of $$("#dialog [data-tab]")) b.onclick = () => { SCR.foreignTab = +b.dataset.tab; artForeign(); };
  for (const b of $$("#dialog [data-war]")) b.onclick = async () => {
    const ok = await scrPopup({advisor: "foreign", header: "Foreign Advisor", text: `This will cause war with ${b.dataset.war}.\nAre you sure?`,
      choices: ["I said DO IT!", "No. You're right, perhaps we should reconsider."]});
    if (ok !== 0) return;
    await act("diplomacy", {action: "declare_war", civ: b.dataset.war});
    if (S.dialog?.kind === "foreign") artForeign();
  };
  for (const b of $$("#dialog [data-peace]")) b.onclick = async () => {
    const gold = +b.dataset.gold || 0;
    const ok = await scrPopup({advisor: "foreign", mood: 0, header: "Foreign Advisor",
      text: `Propose peace to ${b.dataset.peace}${gold ? `, paying the ${gold} gold they ask` : ""}?`,
      choices: ["Yes, let us have peace.", "No, not now."]});
    if (ok !== 0) return;
    await act("diplomacy", {action: "propose_peace", civ: b.dataset.peace, gold});
    if (S.dialog?.kind === "foreign") artForeign();
  };
  tradeButtons(() => artForeign());
}

// ======================================================================== the science advisor (ScienceAdvisor.cs, TechBox.cs)

const TB_SIZE = {small: [106, 82], medium: [163, 82], long: [188, 82], large: [163, 106]};
const TB_COL = {small: 0, medium: 1, long: 2, large: 3}, TB_ROW = {known: 0, current: 1, queued: 1, possible: 2, blocked: 3};
const TB_COLOR = {known: "#0000CD", current: "#4169E1", queued: "#4169E1", possible: "#556B2F"};
// A box's size by what the tech brings: buildings it allows or obsoletes, units the seat's civ can build, terraforms.
function techSize(t, civ) {
  const units = t.units.filter(u => { const i = S.art.m.unit_info?.[u]; return !i || !i.civs || i.civs.includes(civ); });
  const cost = t.buildings + units.length + t.terraforms.length;
  return {cost, units, key: cost > 4 && cost <= 6 ? "large" : cost > 3 ? "long" : cost > 1 ? "medium" : "small"};
}

async function artScience(page) {
  let t;
  const [, got] = await Promise.all([scrLoad(["head_science", "dialogbox", "exit", "science_nav", "techboxes_fit", "non_required",
    `science_${SCR.sciEra ?? playerEra()}`]), api("play/api/techs").then(x => (t = x), e => { toast(e.message, true); return null; })]);
  if (!got) return;
  const s = state(), era = playerEra();
  if (page != null) SCR.sciEra = page;
  if (SCR.sciEra == null) SCR.sciEra = era;
  const view = SCR.sciEra, known = new Set(t.known || []), avail = new Map((t.available || []).map(a => [a.name, a]));
  const queue = [t.current, ...((s.research || {}).queue || [])].filter((n, i, a) => n && a.indexOf(n) === i);
  const turns = n => n != null && n <= 50 ? n : "--";
  let h = bg(`science_${view}`) + advCommon("head_science", "SCIENCE ADVISOR", "You are running OpenCiv3!");
  if (view > 0) h += btn("science_nav", [0, 1, 129, 33], [0, 35], [0, 69], 284, 720, `data-era="${view - 1}" title="${SCR_ERAS[view - 1]}"`,
      `<span class="cs-lb cs-navlb">Previous Era</span>`) + spr("science_nav", [0, 103, 44, 9], 240, 733);
  if (view < 3) h += btn("science_nav", [0, 1, 129, 33], [0, 35], [0, 69], 612, 720, `data-era="${view + 1}" title="${SCR_ERAS[view + 1]}"`,
      `<span class="cs-lb cs-navlb">Next Era</span>`) + spr("science_nav", [46, 103, 44, 9], 741, 733);
  for (const tech of S.art.m.techs.filter(x => x.era === view)) {
    const {cost, units, key} = techSize(tech, s.civ), [w, hh] = TB_SIZE[key], q = queue.indexOf(tech.name) + 1;
    const st = known.has(tech.name) ? "known" : q === 1 ? "current" : q > 1 ? "queued" : avail.has(tech.name) ? "possible" : "blocked";
    const text = st === "current" ? `${q}. ${tech.name} (${turns(t.turns_left)} turns)` : st === "queued" ? `${q}. ${tech.name}`
      : st === "possible" ? `${tech.name} (${turns(avail.get(tech.name).turns)} turns)` : tech.name;
    const limit = Math.floor((w - 16) / 6), shown = text.length > limit ? text.trim().slice(0, limit - 1) + "..." : text;
    const color = TB_COLOR[st] || (tech.era > era ? "#696969" : "#000");
    const font = st === "current" ? "font-weight:700" : !tech.required ? "font-style:italic" : "";
    // the tech's icon, then what it brings: its units (the standalone client's placeholder) and terraforms
    let icons = spr(tech.icon, [0, 0, 32, 32], 12, 32);
    const fx = [...units.map(() => ["unit_small", [0, 0, 32, 32]]), ...tech.terraforms.map(([c, r]) => ["buttons", [32 * c, 32 * r, 32, 32]])];
    let ox = 32 + 7, oy = 0;
    fx.forEach(([n, crop], i) => {
      icons += spr(n, crop, 12 + ox, 32 + oy);
      if (cost > 4 && i === 2) { ox = 7; oy += 33; } else ox += crop[2] + 1;
    });
    const later = tech.era > era, idle = st === "known" || later;
    const tip = `${tech.name}: ${st === "known" ? "known" : st === "current" ? "being researched" : st === "queued" ? `queued (${q})`
      : st === "possible" ? `can be researched (${turns(avail.get(tech.name).turns)} turns)` : later ? "a later era's" : "needs its prerequisites: research them first"}`
      + (tech.required ? "" : " · not required for the next era");
    h += `<div class="cs-tb ${st}${idle ? " idle" : ""}" data-tech="${esc(tech.name)}" role="button" tabindex="${idle ? -1 : 0}" title="${esc(tip)}"
      style="left:${tech.x}px;top:${tech.y}px;width:${w}px;height:${hh}px;background-image:url('${scrUrl("techboxes_fit")}');
      background-position:${-190 * TB_COL[key]}px ${-108 * TB_ROW[st]}px">${icons}
      <span class="cs-tn" style="color:${color};${font}">${esc(shown)}</span>
      ${tech.required ? "" : spr("non_required", [0, 0, 45, 45], w - 20, 0)}</div>`;
  }
  scrOpen("science", h);
  for (const b of $$("#dialog [data-era]")) b.onclick = () => artScience(+b.dataset.era);
  for (const b of $$("#dialog .cs-tb")) b.onclick = b.onkeydown = async ev => {
    if (ev.type === "keydown" && ev.key !== "Enter") return;
    if (b.classList.contains("idle") || (b.classList.contains("current") && queue.length <= 1)) return;
    const r = await act("research", {tech: b.dataset.tech});
    if (r && S.dialog?.kind === "science") artScience();
  };
}

// ======================================================================== the city screen (CityScreen.cs, ProductionMenu.cs)

// The camera as the client puts it (CityScreen.cs): on the tile south of the city, at the frame's centre (the
// window's), at the client's zoom 1 in the scaled frame: a tile is 128 frame pixels wide.
function cityCamera(c, s = scrScale()) {
  S.cam.cx = c.x; S.cam.cy = c.y + 2; S.cam.hw = 64 * s; draw();
}
const CI = {good: [129, 5, 22, 22], empty_shield: [253, 5, 22, 22], wasted: [160, 5, 22, 22], full: [191, 7, 20, 20],
  empty_food: [284, 7, 20, 20], no_food: [315, 7, 20, 20], eaten: [223, 7, 20, 20]};
// A grid of icons, row-major from the top-left, each scaled to `size`.
const grid = (cells, cols, size, x0, y0) => cells.map((k, i) => sprAt("yield_icons", CI[k], x0 + (i % cols) * size, y0 + Math.floor(i / cols) * size, size, size)).join("");
const repeat = (k, n) => Array(Math.max(0, n | 0)).fill(k);

// A unit's own sprite (its idle pose, facing south-east, in the seat's colour) as a small image, cached; null while
// its art loads (the list redraws when it has).
const thumbs = new Map();
function unitThumb(type) {
  if (thumbs.has(type)) return thumbs.get(type);
  const ua = S.art.unit(type, () => {   // loaded: swap the placeholders shown meanwhile
    const url = unitThumb(type);
    if (url) for (const el of $$(`#dialog [data-thumb="${CSS.escape(type)}"]`)) el.style.cssText += `;background:url('${url}') center/contain no-repeat`;
  });
  if (!ua) return null;
  const cell = S.art.unitCell(ua, "default", "SE", 0, rgb(S.world.color(S.world.me)));
  const c = document.createElement("canvas"); c.width = c.height = 48;
  const k = Math.min(48 / cell.width, 48 / cell.height, 1);
  c.getContext("2d").drawImage(cell, (48 - cell.width * k) / 2, (48 - cell.height * k) / 2, cell.width * k, cell.height * k);
  const url = c.toDataURL();
  thumbs.set(type, url);
  return url;
}

// The production list's icon and text for an option: a unit's from units_32.png and "Warrior 1.1.1"; a building's
// from buildings-small.png.
function optionArt(o, era) {
  const m = S.art.m, u = m.unit_info?.[o.name], b = m.building_icons?.[o.name];
  if (o.kind === "unit" && u) {
    const text = `${o.name} ${u.a}${u.b > 0 ? `(${u.b})` : ""}.${u.d}.${u.m}`, thumb = unitThumb(o.name);
    if (thumb) return {icon: `<i class="cs-sp" style="left:0;top:0;width:32px;height:32px;background:url('${thumb}') center/contain no-repeat"></i>`, text};
    const i = (u.icon_era || {})[era] ?? u.icon;   // the art pack's icon sheet is a placeholder: only while the unit loads
    return {icon: spr("units_32", [1 + 33 * (i % 14), 1 + 33 * Math.floor(i / 14), 32, 32], 0, 0, "", `data-thumb="${esc(o.name)}"`), text};
  }
  return {icon: b != null ? spr("buildings_small", [33, 33 + 33 * b, 32, 32], 0, 0) : "", text: o.name};
}

async function artCity(id, {keepProd = false} = {}) {
  let c;
  const [, got] = await Promise.all([scrLoad(["city_bg", "city_buttons", "prod_button", "prod_queue", "pop_heads", "units_32", "buildings_small", "buildings_large", "luxury_icons"]),
    api(`play/api/city?city=${encodeURIComponent(id)}`).then(x => (c = x), e => { toast(e.message, true); return null; })]);
  if (!got) return;
  const was = S.dialog?.kind === "city" && S.dialog.art ? S.dialog : null;
  const saved = was ? was.saved : {cx: S.cam.cx, cy: S.cam.cy, hw: S.cam.hw}, prodOpen = keepProd && !!was?.prodOpen;
  const era = playerEra(), cities = state().cities || [];
  // the yields: the city's own (shields, food_eaten, commerce), else what its worked tiles add up to
  const w = c.worked || null, sum = i => w.reduce((s, t) => s + (t[i] | 0), 0);
  const eaten = c.food_eaten ?? (w ? Math.max(0, sum(2) - (c.food_per_turn | 0)) : null);
  const food = eaten != null ? eaten + (c.food_per_turn | 0) : null;
  const useful = c.shields ? c.shields.useful | 0 : c.shields_per_turn | 0;
  const shields = c.shields ? c.shields.total | 0 : w ? sum(3) : null;
  const wasted = c.shields ? c.shields.corrupt | 0 : w ? Math.max(0, shields - useful) : 0;
  const commerce = c.commerce ? null : w ? sum(4) : null;
  let h = bg("city_bg");
  h += `<div class="cs-hole" title=""></div>`;   // the map shows through; a click there closes the production list
  h += btn("city_buttons", [1, 1, 48, 48], [1, 50], [1, 99], 359, 31, `data-step="-1" title="Previous city (←)"`);
  h += btn("city_buttons", [42, 1, 48, 48], [42, 50], [42, 99], 625, 31, `data-step="1" title="Next city (→)"`);
  h += btn("city_buttons", [155, 1, 38, 48], [155, 50], [155, 99], 950, 20, `data-close title="Close (Esc)" aria-label="Close"`);
  h += `<div class="cs-lb cs-cityname" style="left:400px;top:18px;width:230px">${esc(c.name)}</div>`;
  if (c.disorder) h += `<div class="cs-lb cs-center" style="left:400px;top:62px;width:230px;color:#b00000">Civil disorder!</div>`;
  h += lab(5, 4, "STRATEGIC RESOURCES") + lab(714, 4, "CULTURE") + lab(7, 514, "IMPROVEMENTS") + lab(162, 514, "LUXURIES");
  // culture: per turn, and the total against the next border growth
  if (c.culture) h += lab(790, 4, `${c.culture.per_turn | 0}/turn`) + lab(714, 60, `Total: ${c.culture.total | 0}/${c.culture.next_border | 0}`);
  if (bonusText(c)) h += `<div class="cs-lb cs-clip" style="left:714px;top:78px;width:300px;font-size:11px" title="${esc(bonusText(c))}">${esc(bonusText(c))}</div>`;
  // strategic resources: resources.png's icon at 45x45, the count centred under it
  if (c.strategic) h += `<div class="cs-list cs-strat" style="left:4px;top:24px;width:290px;height:65px">${c.strategic.map(r =>
    `<div class="cs-res" title="${esc(r.name)}">${sprAt("resources", [50 * (r.icon % 6), 50 * Math.floor(r.icon / 6), 50, 50], 0, 0, 45, 45)}` +
    `<span>${r.count | 0}</span></div>`).join("")}</div>`;
  // luxuries: "(count)", then the small icon
  if (c.luxuries) h += `<div class="cs-list" style="left:158px;top:537px;width:123px;height:167px">${c.luxuries.map(r =>
    `<div class="cs-lux" title="${esc(r.name)}"><span>(${r.count | 0})</span>${r.icon >= 8
      ? `<i style="background-image:url('${scrUrl("luxury_icons")}');background-position:${-22 * (r.icon - 8)}px 0"></i>` : ""}</div>`).join("")}</div>`;
  h += `<div class="cs-list" style="left:7px;top:537px;width:146px;height:222px">${(c.buildings || []).map(b => `<div>${esc(b)}</div>`).join("")}</div>`;
  // production: the line, the shield row (wasted from the left, useful from the right), the box, the button
  h += lab(284, 509, `PRODUCTION: ${shields ?? useful} per turn`, 12);
  {
    const n = useful + wasted, step = n ? Math.min(22, Math.floor((547 - (wasted > 0 ? 100 : 0)) / n)) : 22;
    for (let i = 0; i < wasted; i++) h += spr("yield_icons", CI.wasted, 288 + i * step, 522);
    for (let i = 0; i < useful; i++) h += spr("yield_icons", CI.good, 288 + 547 - 22 - i * step, 522);
  }
  const cost = c.production_cost | 0, stored = c.production_stored | 0;
  if (cost > 0) {
    const cols = Math.ceil(Math.sqrt(cost)), rows = Math.ceil(cost / cols), size = Math.min(Math.floor(138 / rows), Math.floor(116 / cols));
    h += grid([...repeat("good", Math.min(cost, stored)), ...repeat("empty_shield", cost - Math.min(cost, stored))], cols, size,
      904 + Math.floor((116 - cols * size) / 2), 626 + Math.floor((138 - rows * size) / 2));
  }
  h += `<div class="cs-lb cs-center" style="left:904px;top:608px;width:117px;font-size:10px">${c.turns_to_complete != null ? `Complete in ${c.turns_to_complete} turns` : "--"}</div>`;
  const prodItem = c.producing || "";
  h += btn("prod_button", [1, 0, 114, 95], [116, 0], [231, 0], 906, 515, `data-prodbtn title="What to build${(c.queue || []).length ? esc(`; then ${c.queue.join(", ")}`) : ""}"`,
    `<canvas class="cs-produnit" width="240" height="240"></canvas>${(c.options || []).find(o => o.name === prodItem)?.kind !== "unit" && S.art.m.building_icons?.[prodItem] != null
      ? spr("buildings_large", [33, 33 + 41 * S.art.m.building_icons[prodItem], 50, 40], 32, 15) : ""}
      <span class="cs-lb cs-prodlb">${esc(prodItem)}</span>`);
  // food: the line (the worked tiles' food), the row (eaten from the left, the surplus from the right), the box
  if (food != null) h += lab(285, 557, `${food} per turn`, 12);
  {
    const surplus = Math.max(0, c.food_per_turn | 0), e = eaten || 0, n = e + surplus;
    const step = n ? Math.min(30, Math.floor((480 - (surplus > 0 ? 100 : 0)) / n)) : 30;
    for (let i = 0; i < e; i++) h += spr("yield_icons", CI.eaten, 287 + i * step, 571);
    for (let i = 0; i < surplus; i++) h += spr("yield_icons", CI.full, 287 + 480 - 20 - i * step, 571);
  }
  h += `<div class="cs-lb cs-center" style="left:776px;top:563px;width:117px;font-size:10px">${
    (c.food_per_turn | 0) < 0 ? "Starving!" : c.turns_to_grow != null && (c.food_per_turn | 0) > 0 ? `Growth in ${c.turns_to_grow} turns.` : "Not growing."}</div>`;
  h += foodBox(c);
  if (c.commerce) {   // where the commerce goes
    const cm = c.commerce;
    h += lab(290, 620, `${cm.taxes | 0} gold/turn to taxes`, 20);
    h += lab(290, 653, `${cm.science | 0} gold/turn to science  (${cm.corrupt | 0} corrupt)`, 20);
    h += lab(290, 685, `${cm.luxury | 0} gold/turn to happiness`, 20);
  } else if (commerce != null) h += lab(290, 620, `${commerce} commerce per turn from its tiles`, 20);
  // buying: the bridge's buy (the client has no button for it): in the empty box under the commerce
  const canBuy = prodItem && prodItem !== "Wealth";
  h += `<button class="cs-tbtn" id="buy" style="left:296px;top:728px" ${canBuy ? "" : "disabled"} title="Complete it next turn">Hurry: buy ${esc(prodItem || "nothing")}</button>`;
  // the citizens, at y 433 over the map
  h += c.citizens ? citizenHeads(c, era, 433) : headRow(c, era, null, 433);
  // the production list
  // the queue (set_production's then) heads the list, a click clears it
  const queued = (c.queue || []).length ? `<li tabindex="0" data-clearq title="${keepsCurrent(c) ? "Click to clear the queue" : esc(`${prodItem} is no longer an option, so the queue can't be changed`)}">
    <i class="cs-pqi"></i><span>Then: ${esc(c.queue.join(", "))}</span><span>${keepsCurrent(c) ? "clear" : ""}</span></li>` : "";
  h += `<div class="cs-pq${prodOpen ? "" : " closed"}" style="background-image:url('${scrUrl("prod_queue")}')"><ul>${queued}${(c.options || []).map(o => {
    const a = optionArt(o, era);
    return `<li tabindex="0" data-item="${esc(o.name)}" class="${o.name === prodItem ? "cur" : ""}" title="${esc(o.name)}${o.cost ? `: ${o.cost} shields` : ""}${(o.effects || []).length ? `\n${o.effects.join("\n")}` : ""}${prodItem && keepsCurrent(c) ? `\nShift+click: queue it after ${esc(prodItem)}` : ""}">
      <i class="cs-pqi">${a.icon}</i><span>${esc(a.text)}</span><span>${o.turns != null ? `${o.turns} turns` : ""}</span></li>`;
  }).join("")}</ul></div>`;
  scrOpen("city", h, {city: c, saved, prodOpen, onClose: () => { Object.assign(S.cam, saved); draw(); }, onKey: e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); cityStep(c.id, e.key === "ArrowLeft" ? -1 : 1); return true; }
    return false;
  }});
  drawProdUnit(c);
  const pq = $("#dialog .cs-pq");
  $("#dialog [data-prodbtn]").onclick = () => { pq.classList.toggle("closed"); S.dialog.prodOpen = !pq.classList.contains("closed"); };
  $("#dialog .cs-hole").onclick = () => { pq.classList.add("closed"); S.dialog.prodOpen = false; };
  for (const b of $$("#dialog [data-step]")) b.onclick = () => cityStep(c.id, +b.dataset.step);
  for (const li of $$("#dialog li[data-item]")) li.onclick = li.onkeydown = async ev => {
    if (ev.type === "keydown" && ev.key !== "Enter") return;
    const r = await chooseProduction(c, li.dataset.item, ev.shiftKey);
    if (r && S.dialog?.kind === "city") artCity(c.id, {keepProd: true});
  };
  const clearq = $("#dialog li[data-clearq]");
  if (clearq && keepsCurrent(c)) clearq.onclick = clearq.onkeydown = async ev => {
    if (ev.type === "keydown" && ev.key !== "Enter") return;
    const r = await act("set_production", {city: c.id, item: prodItem, then: []});
    if (r && S.dialog?.kind === "city") artCity(c.id, {keepProd: true});
  };
  $("#buy").onclick = async () => {
    const ok = await scrPopup({header: "Domestic Advisor", text: `Complete ${prodItem} in ${c.name} next turn?\nMonarchy and later governments pay gold; Despotism pays with citizens.`,
      choices: ["Yes, hurry it.", "No, let them work."]});
    if (ok !== 0) return;
    await act("buy", {city: c.id});
    if (S.dialog?.kind === "city") artCity(c.id, {keepProd: true});
  };
  if (cities.length < 2) for (const b of $$("#dialog [data-step]")) b.disabled = true;
}
function cityStep(id, d) {
  const cities = state().cities || [], i = cities.findIndex(x => x.id === id);
  if (cities.length < 2 || i < 0) return;
  artCity(cities[(i + d + cities.length) % cities.length].id, {keepProd: S.dialog?.prodOpen});
}
// The food box (CityScreen.cs): the stored food in a grid that fills from the top; with a granary and half the box
// full, the granary's half below its label.
function foodBox(c) {
  const need = c.food_needed | 0, stored = Math.max(0, c.food_stored | 0);
  if (need <= 0) return "";
  const cols = {20: 2, 40: 4, 60: 6}[need] || Math.ceil(Math.sqrt(need)), half = Math.floor(need / 2);
  const lost = (c.food_per_turn | 0) < 0 ? Math.min(stored, -c.food_per_turn) : 0;
  if (!(c.buildings || []).includes("Granary") || stored < half) {
    const rows = Math.ceil(need / cols), size = Math.min(Math.floor(180 / rows), Math.floor(120 / cols)), have = Math.min(need, stored);
    return grid([...repeat("full", have - lost), ...repeat("no_food", lost), ...repeat("empty_food", need - have)], cols, size,
      774 + Math.floor((122 - cols * size) / 2), 584 + Math.floor((177 - rows * size) / 2));
  }
  const rows = Math.max(1, Math.floor(Math.ceil(need / cols) / 2)), size = Math.min(Math.floor(80 / rows), Math.floor(120 / cols));
  const top = Math.min(half, stored - half), lostTop = Math.min(lost, top), lostGran = lost - lostTop;
  const height = 2 * rows * size + 8 + 14, y0 = 584 + Math.floor((177 - height) / 2), x0 = 774 + Math.floor((122 - cols * size) / 2);
  return grid([...repeat("full", top - lostTop), ...repeat("no_food", lostTop), ...repeat("empty_food", half - top)], cols, size, x0, y0)
    + `<div class="cs-lb cs-center" style="left:774px;top:${y0 + rows * size + 4}px;width:122px;font-size:10px">GRANARY</div>`
    + grid([...repeat("full", half - lostGran), ...repeat("no_food", lostGran)], cols, size, x0, y0 + rows * size + 22);
}
// The production button's unit: its idle frame facing south-east in the seat's colour, the tile centre at (57, 35).
function drawProdUnit(c, tries = 0) {
  const cv = $("#dialog .cs-produnit"), o = (c.options || []).find(x => x.name === c.producing);
  if (!cv || !c.producing || (o && o.kind !== "unit")) return;
  const ua = S.art.unit(c.producing, () => { if (S.dialog?.city === c) drawProdUnit(c); });
  if (!ua) {   // loading (the art calls back only its first asker, maybe the production list): look again shortly
    if (tries < 50 && S.art.m.units[c.producing]) setTimeout(() => { if (S.dialog?.city === c) drawProdUnit(c, tries + 1); }, 200);
    return;
  }
  const cell = S.art.unitCell(ua, "default", "SE", 0, rgb(S.world.color(S.world.me))), [ax, ay] = ua.spec.anchor, g = cv.getContext("2d");
  g.clearRect(0, 0, 240, 240);
  g.drawImage(cell, 120 - ax, 120 - ay);
}
