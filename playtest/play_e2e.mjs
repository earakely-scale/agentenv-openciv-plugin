// End to end: a browser plays a human seat through the real /play UI, with the game's hotkeys and clicks, while
// playtest/bots.py plays the other seats with MCP tool calls (docs/play.md).
//
//   .venv/bin/python playtest/bots.py --seats 2 --humans Rome=you --turns 8 --fast --size Tiny --out /tmp/hv &
//   NODE_PATH=$(npm root -g) node playtest/play_e2e.mjs /tmp/hv          # waits for /tmp/hv/play.json
//
// It plays every turn until the game is over: closes the turn report, picks research in the science advisor (F6),
// founds a city with B, opens the city by clicking it on the map and picks what to build, moves units with the arrow
// keys, sends one exploring (X) and fortifies the rest (F), sets the rates in the domestic advisor (F1), opens the
// foreign advisor (F4), hovers a tile for its tooltip, and ends the turn with Enter. It then checks what the env saw:
// the seat's actions in the action log, no turn ended for the human by the stall clock, the viewer's human flag,
// and that a wrong token gets 401. Screenshots go to <dir>/e2e/. Exits non-zero on a failed check.
import fs from "node:fs";
import path from "node:path";
import {createRequire} from "node:module";

const require = createRequire(import.meta.url);
const {chromium} = require("playwright");

const dir = path.resolve(process.argv[2] || ".");
const MAX_TURNS = +(process.argv[3] || 50);
const shots = path.join(dir, "e2e");
fs.mkdirSync(shots, {recursive: true});
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const checks = [];
const check = (ok, what) => { checks.push({ok: !!ok, what}); log(ok ? "PASS" : "FAIL", what); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitFor(fn, ms, what) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > ms) throw new Error(`timed out: ${what}`);
    await sleep(250);
  }
}

const playFile = path.join(dir, "play.json");
const meta = await waitFor(() => fs.existsSync(playFile) && JSON.parse(fs.readFileSync(playFile, "utf8")), 600_000, "play.json");
const [civ, url] = Object.entries(meta.play)[0];
log("playing", civ, "at", url);

const browser = await chromium.launch({executablePath: process.env.CHROMIUM || undefined});
const page = await browser.newPage({viewport: {width: 1440, height: 900}});
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
await page.goto(url);
await page.waitForSelector("#bar .turn", {timeout: 60_000});
await page.screenshot({path: path.join(shots, "start.png")});

// ---- what the page knows (its globals), and helpers that act like a person ----
const view = () => page.evaluate(() => S.view && {game: S.view.game, state: {turn: S.view.state.turn,
  research: S.view.state.research, cities: S.view.state.cities, units: S.view.state.units, rates: S.view.state.rates}});
const selected = () => page.evaluate(() => S.sel && S.view.state.units.find(u => u.id === S.sel));
const dialogKind = () => page.evaluate(() => S.dialog && S.dialog.kind);
async function tileCenter(x, y) {
  return page.evaluate(([x, y]) => {
    const r = document.querySelector("#map").getBoundingClientRect();
    const [sx, sy] = S.cam.screen(x, y, r.width, r.height, S.world.wrap ? S.world.W : 0);
    return [r.left + sx, r.top + sy];
  }, [x, y]);
}
async function closeDialogs() {
  for (let i = 0; i < 5; i++) {
    const k = await dialogKind();
    if (!k) return;
    if (k === "science") {
      const v = await view();
      if (!v.state.research?.current) { await pickResearch(); continue; }
    }
    if (k === "over") return;
    await page.keyboard.press("Escape");
    await sleep(150);
  }
}
async function pickResearch() {
  if ((await dialogKind()) !== "science") { await page.keyboard.press("F6"); await page.waitForSelector("#dialog li[data-tech]"); }
  const tech = await page.$eval("#dialog li[data-tech]", li => li.dataset.tech);
  await page.click(`#dialog li[data-tech="${tech}"]`);
  await waitFor(async () => (await view()).state.research?.current, 10_000, "research set");
  log("research:", tech);
  return tech;
}

let founded = false, setBuild = false, rated = false, foreign = false, hovered = false, explored = false, moved = 0;
let lastTurn = -1, turnsPlayed = 0, keyEnds = 0;
const ARROWS = ["ArrowUp", "ArrowRight", "ArrowDown", "ArrowLeft"];
let tried = new Set();   // unit:arrow refused this turn
const t0 = Date.now();
while (turnsPlayed < MAX_TURNS) {
  // wait for our turn: the turn moved on (or the game ended) and the seat hasn't ended it
  const v = await waitFor(async () => {
    const v = await view();
    return v && (v.game.game_over || (!v.game.ended && v.game.turn !== lastTurn)) ? v : null;
  }, 900_000, "our turn");
  if (v.game.game_over) break;
  lastTurn = v.game.turn;
  turnsPlayed++;
  tried = new Set();
  await sleep(300);
  await closeDialogs();
  if (!(await view()).state.research?.current) await pickResearch();

  // every unit that needs orders, the way the game hands them over: the selected one
  for (let n = 0; n < 30; n++) {
    await closeDialogs();
    const u = await selected();
    if (!u || !u.needs_orders) break;
    const before = JSON.stringify([u.x, u.y, u.moves_left, u.status]);
    if (u.orders.includes("found_city") && u.can_found_city?.ok && (!founded || u.type === "Settler")) {
      await page.keyboard.press("b");
      const ok = await waitFor(async () => ((await view()).state.cities || []).length > (v.state.cities || []).length, 15_000, "city founded").catch(() => false);
      check(ok, `T${lastTurn}: B founds a city with ${u.id}`);
      founded = founded || !!ok;
      await page.screenshot({path: path.join(shots, `T${lastTurn}-founded.png`)});
      continue;
    }
    if (u.orders.includes("auto_work") && (u.type === "Worker" || u.type === "Settler")) {
      await page.keyboard.press("a");                      // a worker automates
    } else if (u.type === "Settler" && ARROWS.some(k => !tried.has(`${u.id}:${k}`))) {
      // a settler without a site here walks on, a direction it hasn't been refused this turn
      const key = ARROWS.find(k => !tried.has(`${u.id}:${k}`));
      tried.add(`${u.id}:${key}`);
      await page.keyboard.press(key);
      await waitFor(async () => { const w = (await view()).state.units.find(x => x.id === u.id);
        return !w || JSON.stringify([w.x, w.y, w.moves_left, w.status]) !== before; }, 4_000, "settler moved").catch(() => null);
      continue;
    } else if (u.type === "Settler") {
      await page.keyboard.press("Space");                  // nowhere to go: skip its turn
    } else if (moved < 3 && ARROWS.some(k => !tried.has(`${u.id}:${k}`))) {
      // a tile the unit can't enter (water, a foreign border at peace) is refused: try another direction
      const key = ARROWS.find(k => !tried.has(`${u.id}:${k}`) && k === ARROWS[moved % 4]) || ARROWS.find(k => !tried.has(`${u.id}:${k}`));
      tried.add(`${u.id}:${key}`);
      await page.keyboard.press(key);
      const after = await waitFor(async () => {
        const w = (await view()).state.units.find(x => x.id === u.id);
        return w && JSON.stringify([w.x, w.y, w.moves_left, w.status]) !== before ? w : null;
      }, 4_000, "unit moved").catch(() => null);
      if (after && (after.x !== u.x || after.y !== u.y)) { moved++; check(true, `T${lastTurn}: arrow key moves ${u.id} (${u.x},${u.y}) → (${after.x},${after.y})`); }
      continue;
    } else if (!explored && u.orders.includes("explore")) {
      await page.keyboard.press("x"); explored = true;
      log("explore", u.id);
    } else if (u.orders.includes("fortify")) {
      await page.keyboard.press("f");
    } else {
      await page.keyboard.press("Space");
    }
    const changed = await waitFor(async () => {
      const s = await selected();
      return !s || s.id !== u.id || JSON.stringify([s.x, s.y, s.moves_left, s.status]) !== before;
    }, 10_000, "order taken").catch(() => false);
    if (!changed) { await page.keyboard.press("w"); }   // stuck: wait, as a player would
  }

  // the city screen: click the city on the map, pick what to build
  const cities = (await view()).state.cities || [];
  if (cities.length && !setBuild) {
    const c = cities[0];
    await page.evaluate(([x, y]) => { S.cam.cx = x; S.cam.cy = y; draw(); }, [c.x, c.y]);
    await sleep(100);
    const [px, py] = await tileCenter(c.x, c.y);
    await page.mouse.click(px, py);
    await page.waitForSelector("#dialog li[data-item]", {timeout: 10_000});
    await page.screenshot({path: path.join(shots, `T${lastTurn}-city.png`)});
    const items = await page.$$eval("#dialog li[data-item]", ls => ls.map(l => l.dataset.item));
    const want = ["Settler", "Worker", "Warrior"].find(i => items.includes(i) && i !== c.producing) || items.find(i => i !== c.producing);
    await page.click(`#dialog li[data-item="${want}"]`);
    const ok = await waitFor(async () => ((await view()).state.cities || []).find(x => x.id === c.id)?.producing === want, 10_000, "production").catch(() => false);
    check(ok, `T${lastTurn}: the city screen sets ${c.name} to build ${want} (from ${c.producing})`);
    setBuild = true;
    await page.keyboard.press("Escape");
  }
  if (!rated && turnsPlayed >= 2) {
    await page.keyboard.press("F1");
    await page.waitForSelector("#r-sci");
    await page.$eval("#r-sci", el => { el.value = 7; el.dispatchEvent(new Event("input")); });
    await page.click("#setrates");
    const ok = await waitFor(async () => (await view()).state.rates?.science === 7, 10_000, "rates").catch(() => false);
    check(ok, `T${lastTurn}: the domestic advisor (F1) sets science to 70%`);
    rated = true;
  }
  if (!foreign && turnsPlayed >= 3) {
    await page.keyboard.press("F4");
    await page.waitForSelector("#dialog .dlg h2");
    await page.screenshot({path: path.join(shots, `T${lastTurn}-foreign.png`)});
    check((await dialogKind()) === "foreign", `T${lastTurn}: F4 opens the foreign advisor`);
    await page.keyboard.press("Escape");
    foreign = true;
  }
  if (!hovered && founded) {
    const c = ((await view()).state.cities || [])[0];
    if (c) {
      const [px, py] = await tileCenter(c.x + 1, c.y + 1);
      await page.mouse.move(px, py);
      const tip = await waitFor(() => page.$eval("#tip", el => !el.hidden && el.textContent.includes("yield") && el.textContent), 5_000, "tooltip").catch(() => null);
      check(tip, `T${lastTurn}: hovering a tile shows its tooltip with the yield (${(tip || "").replace(/\s+/g, " ").slice(0, 80)})`);
      await page.screenshot({path: path.join(shots, `T${lastTurn}-tooltip.png`)});
      await page.mouse.move(5, 300);
      hovered = true;
    }
  }

  // end the turn: Enter, and Enter again if the game asks because units still have moves
  await closeDialogs();
  await page.screenshot({path: path.join(shots, `T${lastTurn}.png`)});
  await page.keyboard.press("Enter");
  let ended = await waitFor(async () => { const w = await view(); return w.game.ended || w.game.turn !== lastTurn || w.game.game_over; }, 3_000, "ended").catch(() => false);
  if (!ended) {
    await page.keyboard.press("Enter");
    ended = await waitFor(async () => { const w = await view(); return w.game.ended || w.game.turn !== lastTurn || w.game.game_over; }, 10_000, "ended").catch(() => false);
  }
  check(ended, `T${lastTurn}: Enter ends the turn`);
  if (ended) keyEnds++;
  const w = await view();
  if (w.game.ended && !w.game.game_over) {
    await page.waitForSelector("#waiting:not([hidden])", {timeout: 5_000}).catch(() => {});
    await page.screenshot({path: path.join(shots, `T${lastTurn}-waiting.png`)});
  }
}
const elapsed = (Date.now() - t0) / 1000;
const final = await view();
await sleep(500);
await page.screenshot({path: path.join(shots, "end.png")});
log(`played ${turnsPlayed} turns in ${elapsed.toFixed(0)}s; game over: ${final.game.game_over}`);

// ---- what the env saw ----
check(founded, "a city was founded from the UI");
check(keyEnds === turnsPlayed || final.game.game_over, `every turn was ended with Enter (${keyEnds}/${turnsPlayed})`);
const base = new URL(url).origin;
const bad = await fetch(`${base}/play/api/view`, {headers: {"X-OpenCiv3-Token": "0".repeat(32)}});
check(bad.status === 401, `a wrong token gets 401 (got ${bad.status})`);
const none = await fetch(`${base}/play/api/status`);
check(none.status === 401, `no token gets 401 (got ${none.status})`);
const live = await (await fetch(`${base}/live/data.json?since=1000000`)).json();
const seat = (live.live?.seats || []).find(s => s.civ === civ);
check(seat && seat.human === true, `the viewer's live seats mark ${civ} as human`);
const logPath = path.join(dir, "actions.jsonl");
const lines = fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8").trim().split("\n").filter(Boolean).map(l => JSON.parse(l)) : [];
// a one-seat game logs no seat: every line is that seat's
const mine = lines.filter(l => (l.seat ?? civ) === civ);
const tools = new Set(mine.map(l => l.tool));
check(mine.length > 0, `the action log has ${mine.length} actions by ${civ}: ${[...tools].join(", ")}`);
for (const t of ["unit_order", "set_production", "research", "set_rates", "end_turn"]) check(tools.has(t), `the action log has ${civ}'s ${t}`);
// bots.py writes data/get once the game is over: the env's count of turns it ended for a seat
const summaryPath = path.join(dir, "summary.json");
const summary = final.game.game_over
  ? await waitFor(() => fs.existsSync(summaryPath) && JSON.parse(fs.readFileSync(summaryPath, "utf8")), 120_000, "summary.json").catch(() => null)
  : null;
const sumSeat = summary && ((summary.seats || []).find(x => x.civ === civ) || (summary.human ? summary : null));
if (summary) {
  check(sumSeat && sumSeat.human === true, `data/get marks ${civ} as human`);
  check(sumSeat && !sumSeat.auto_ended_turns, `no turn was ended for ${civ} by the stall clock (${sumSeat?.auto_ended_turns ?? "?"})`);
}
check(errors.length === 0, `no page errors${errors.length ? ": " + errors.slice(0, 3).join(" | ") : ""}`);
fs.writeFileSync(path.join(shots, "result.json"), JSON.stringify({civ, turnsPlayed, elapsed, checks, errors}, null, 2));
await browser.close();
const failed = checks.filter(c => !c.ok);
log(failed.length ? `${failed.length} FAILED of ${checks.length}` : `all ${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
