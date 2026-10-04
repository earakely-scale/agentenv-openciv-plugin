// End to end for what /play shares with the agents' tools beyond the first turns (docs/play.md): a browser plays a
// human seat in a game well under way, through the page's buttons, hotkeys and dialogs, and checks each against the
// env's view after it. Start a game whose human seat the engine's AI has played for a while, e.g.
//
//   .venv/bin/python playtest/serve_midgame.py /tmp/mid --turns 130 --gold-turns 12 &
//   NODE_PATH=$(npm root -g) node playtest/play_features_e2e.mjs /tmp/mid      # waits for /tmp/mid/play.json
//
// It checks the status bar (the year, the rank by score, land and people, culture once it counts), upgrades a unit
// (U), boards a ship in port (O) and unloads it (L), queues production with Shift+click and clears the queue, quotes,
// balances and proposes a trade in the foreign advisor (F4) and answers an AI's offer, then plays turns
// with Shift+Enter until the game is over (or [turns] turns) and checks the game-over text. Steps the game doesn't offer (no ship in a
// city, no unit that can upgrade) are reported as skipped. Screenshots go to <dir>/features/. Exits non-zero on a
// failed check or a page error.
import fs from "node:fs";
import path from "node:path";
import {createRequire} from "node:module";

const require = createRequire(import.meta.url);
const {chromium} = require("playwright");

const dir = path.resolve(process.argv[2] || ".");
const TURNS = +(process.argv[3] || 6);
const shots = path.join(dir, "features");
fs.mkdirSync(shots, {recursive: true});
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const checks = [];
const check = (ok, what) => { checks.push({ok: !!ok, what}); log(ok ? "PASS" : "FAIL", what); return !!ok; };
const skip = what => log("SKIP", what);
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, ms, what) {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > ms) throw new Error(`timed out: ${what}`);
    await sleep(200);
  }
}

const playFile = path.join(dir, "play.json");
const meta = await waitFor(() => fs.existsSync(playFile) && JSON.parse(fs.readFileSync(playFile, "utf8")), 1_800_000, "play.json");
const [civ, url] = Object.entries(meta.play)[0];
log("playing", civ, "at", url);

const browser = await chromium.launch({executablePath: process.env.CHROMIUM || undefined});
const page = await browser.newPage({viewport: {width: 1440, height: 900}});
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => { if (m.type() === "error" && !/the server responded with a status of 4\d\d/.test(m.text())) errors.push(m.text()); });
page.on("dialog", d => d.accept());           // the browser's own confirm (disband)
await page.goto(url);
await page.waitForSelector("#bar .turn", {timeout: 60_000});

const st = () => page.evaluate(() => S.view.state);
const game = () => page.evaluate(() => S.view.game);
const dialogKind = () => page.evaluate(() => S.dialog && S.dialog.kind);
const shot = name => page.screenshot({path: path.join(shots, `${name}.png`)});
async function closeDialogs() {
  for (let i = 0; i < 6; i++) {
    const k = await dialogKind();
    if (!k || k === "over") return;
    await page.keyboard.press("Escape");
    await sleep(150);
  }
}
const select = id => page.evaluate(id => select(id), id);
const busy = () => page.evaluate(() => S.busy);
async function settle() { await waitFor(async () => !(await busy()), 30_000, "the page's action"); await sleep(150); }
await closeDialogs();
// ART=on or off picks the client's art or the plain UI when the env has the art (OPENCIV_WEB_ART); the art's screens
// (screens.js) are its own city screen and advisors
if (process.env.ART) {
  await waitFor(() => page.evaluate(() => !!S.art || !S.view.game.art), 30_000, "the art");
  await page.evaluate(on => useArt(on), process.env.ART === "on");
}
const ART = await page.evaluate(() => artScreensOn());
log("the client's art:", ART ? "on" : "off");
const statusInfo = () => page.$eval("#status", el => [el.innerText, ...[...el.querySelectorAll("[title]")].map(e => e.title)].join("\n"));

// ---- the status bar: the year, the race ----
{
  const s = await st();
  const bar = await page.$eval("#bar", b => b.innerText);
  check(s.date && bar.includes(s.date), `the bar shows the year (${s.date})`);
  check(s.race?.rank && bar.includes(`of ${s.race.civs_left}`), `the bar shows the rank (${s.race?.rank} of ${s.race?.civs_left})`);
  check(/Land · People/i.test(bar) && /\d+% · \d+%/.test(bar), "the bar shows the shares of land and people");
  const tip = await page.$eval("#bar .stat[title]", el => el.title);
  check(/Domination needs 67%/.test(tip) && /Conquest/.test(tip), "the race tooltip names the victories");
  const culture = /Culture/.test(bar);
  const counts = (s.race.nearest_culture?.culture || 0) >= 10000 || (s.race.best_city?.culture || 0) >= 2000;
  check(culture === counts, `culture is in the bar exactly when it counts (${culture}, top ${s.race.nearest_culture?.culture}, best city ${s.race.best_city?.culture})`);
  await shot("bar");
}

// ---- upgrade (U) ----
{
  const s = await st();
  const u = s.units.find(u => u.upgrade?.ok && u.orders.includes("upgrade"));
  const shown = s.units.find(u => u.upgrade);
  if (shown) {
    await select(shown.id);
    const status = await statusInfo();
    check(status.includes(shown.upgrade.to), `the status panel names ${shown.id}'s upgrade to ${shown.upgrade.to}`);
  } else skip("no unit with an upgrade line");
  if (u) {
    await closeDialogs(); await select(u.id);
    const title = await page.$eval('#commands [data-o="upgrade"]', b => b.title);
    check(title.includes(`${u.upgrade.gold} gold`), `the Upgrade button says its price (${title})`);
    await page.keyboard.press("u");
    await waitFor(dialogKind, 5_000, "the upgrade question");
    check(await dialogKind() === "ask", "U asks before spending the gold");
    await page.click("#dialog [data-yes]");
    await settle();
    const after = await st(), w = after.units.find(x => x.id === u.id);
    check(w && w.type === u.upgrade.to && after.gold === s.gold - u.upgrade.gold,
      `U upgrades ${u.id} ${u.type} → ${w?.type} for ${s.gold - after.gold} gold (asked ${u.upgrade.gold})`);
    await shot("upgraded");
  } else skip("no unit can upgrade now");
}

// ---- board (O) and unload (L), in port; with no ship in port, buy one in a coastal city and end the turn ----
async function endTurn() {
  const g = await game();
  await closeDialogs();
  await page.keyboard.press("Shift+Enter");     // ends the turn even with units that still have moves
  await waitFor(async () => { const n = await game(); return n.game_over || n.turn !== g.turn; }, 300_000, "the next turn");
  await sleep(500);
}
function inPort(s) {
  const cityAt = (x, y) => s.cities.some(c => c.x === x && c.y === y);
  const ship = s.units.find(u => u.capacity && (u.cargo || []).length < u.capacity && cityAt(u.x, u.y));
  const rider = ship && s.units.find(u => !u.capacity && !u.aboard && u.x === ship.x && u.y === ship.y
    && u.orders.includes("board") && u.moves_left > 0);
  return [ship, rider];
}
const BOATS = /^(Galley|Caravel|Galleon|Transport)$/;
if (!inPort(await st())[0]) {
  let bought = false;
  for (const c of (await st()).cities) {
    if (ART) {   // setup, not under test: the page's own calls
      const r = await page.evaluate(async id => { const c = await api(`play/api/city?city=${id}`);
        const boat = (c.options || []).map(o => o.name).find(n => /^(Galley|Caravel|Galleon|Transport)$/.test(n));
        return boat && await act("set_production", {city: id, item: boat}) && await act("buy", {city: id}) && boat; }, c.id);
      const now = (await st()).cities.find(x => x.id === c.id);
      if (r && now.production_stored >= now.production_cost) { log(`bought ${r} in ${c.name}`); bought = true; break; }
      continue;
    }
    await closeDialogs();
    await page.evaluate(id => openCity(id), c.id);
    await page.waitForSelector("#dialog li[data-item]");
    const boat = (await page.$$eval("#dialog li[data-item]", ls => ls.map(l => l.dataset.item))).find(i => BOATS.test(i));
    if (!boat) continue;
    await page.click(`#dialog li[data-item="${boat}"]`);
    await settle();
    await page.waitForSelector("#dialog #buy:not([disabled])");
    await page.click("#dialog #buy");
    await waitFor(async () => (await dialogKind()) === "ask", 5_000, "the buy question");
    const gold = (await st()).gold;
    await page.click("#dialog [data-yes]");
    await settle();
    const s = await st(), now = s.cities.find(x => x.id === c.id);
    log(`buy ${boat} in ${c.name}: gold ${gold}→${s.gold}, stored ${now.production_stored}/${now.production_cost}`);
    if (now.production_stored >= now.production_cost) { bought = true; break; }
  }
  await closeDialogs();
  if (bought) { await endTurn(); log("a ship is in port:", !!inPort(await st())[0]); }
}
{
  const s = await st();
  const [ship, rider] = inPort(s);
  if (ship && rider) {
    await closeDialogs(); await select(rider.id);
    await page.keyboard.press("o");
    await settle();
    let after = await st(), r = after.units.find(x => x.id === rider.id);
    check(r?.aboard === ship.id, `O boards ${rider.id} ${rider.type} onto ${ship.type} ${ship.id} (aboard ${r?.aboard})`);
    await select(ship.id);
    const status = await statusInfo();
    check(new RegExp(`Carrying \\d+/${ship.capacity}`).test(status) && status.includes(rider.id), `the ship's panel lists its cargo`);
    await shot("boarded");
    const sh = after.units.find(x => x.id === ship.id);
    if (sh.orders.includes("unload")) {
      await page.keyboard.press("l");
      await settle();
      after = await st(); r = after.units.find(x => x.id === rider.id);
      check(r && !r.aboard, `L unloads ${ship.id} in port (${rider.id} aboard: ${r?.aboard ?? "no"})`);
    } else skip(`${ship.id} has no unload order: ${sh.orders.join(",")}`);
  } else skip(`no ship with room in a city with a unit that can board (ships: ${s.units.filter(u => u.capacity).map(u => `${u.id}@${u.x},${u.y}`).join(" ") || "none"})`);
}

// ---- production queue: Shift+click, then Clear ----
{
  const s = await st();
  let c = null, body = "", items = [];
  for (const city of s.cities.filter(c => c.producing)) {   // one whose current item is still an option, to keep it
    await closeDialogs();
    await page.evaluate(id => openCity(id), city.id);
    await page.waitForSelector("#dialog li[data-item]", {state: "attached"});
    if (ART && await page.$("#dialog .cs-pq.closed")) await page.click("#dialog [data-prodbtn]");   // the production list
    body = await page.$eval("#dialog", el => [el.innerText, ...[...el.querySelectorAll("[title]")].map(e => e.title)].join("\n"));
    items = await page.$$eval("#dialog li[data-item]", ls => ls.map(l => l.dataset.item));
    if (items.includes(city.producing)) { c = city; break; }
    check(!/Shift\+click/.test(body), `${city.name}'s ${city.producing} is no longer an option: no queue hint`);
  }
  if (c) {
    const pick = items.find(i => i !== c.producing) || items[0];
    await page.click(`#dialog li[data-item="${pick}"]`, {modifiers: ["Shift"]});
    await settle();
    let city = (await st()).cities.find(x => x.id === c.id);
    check(city.producing === c.producing && (city.queue || []).at(-1) === pick,
      `Shift+click queues ${pick} after ${c.producing} (now ${city.producing}, then ${(city.queue || []).join(",")})`);
    const clear = ART ? "#dialog li[data-clearq]" : "#clearq";
    await page.waitForSelector(clear);
    await shot("queued");
    await page.click(clear);
    await settle();
    city = (await st()).cities.find(x => x.id === c.id);
    check(city.producing === c.producing && !(city.queue || []).length, "Clear empties the queue");
    const anyEffects = await page.$$eval(ART ? "#dialog li[data-item][title*='+']" : "#dialog ul.opts li .sub", l => l.length);
    log("city", c.name, "options with effects:", anyEffects, "| bonus line:", /Buildings: \+/.test(body));
    await closeDialogs();
  } else skip("no city whose current item can be kept");
}

// ---- trades: the foreign advisor (F4), an offer, the trade builder ----
{
  await closeDialogs();
  await page.keyboard.press("F4");
  await waitFor(async () => (await dialogKind()) === "foreign", 10_000, "the foreign advisor");
  const tradesTab = async () => { if (ART) { await page.click('#dialog [data-tab="1"]'); await waitFor(() => page.$('#dialog [data-tab="1"]'), 5_000, "the tab"); await sleep(300); } };
  await tradesTab();
  await shot("foreign");
  const accept = await page.$("#dialog [data-accept]");
  if (accept) {
    const who = await accept.evaluate(b => b.dataset.accept);
    const before = await page.evaluate(() => ({gold: S.view.state.gold, techs: S.view.state.score.techs}));
    await accept.click();
    await settle();
    const after = await page.evaluate(() => ({gold: S.view.state.gold, techs: S.view.state.score.techs}));
    check(JSON.stringify(after) !== JSON.stringify(before), `Accept takes ${who}'s offer (gold ${before.gold}→${after.gold}, techs ${before.techs}→${after.techs})`);
    await waitFor(async () => (await dialogKind()) === "foreign", 10_000, "the advisor again");
    await sleep(300);
  } else skip("no AI offer stands this turn");
  // the first civ with a tech to trade: each civ's Trade button, and Back when it has none
  const civs = await page.$$eval("#dialog [data-trade]", bs => bs.map(b => b.dataset.trade));
  let who = null, get = null, give = null;
  for (const c of civs) {
    await page.click(`#dialog [data-trade="${c}"]`);
    await waitFor(async () => (await dialogKind()) === "trade", 10_000, "the trade dialog");
    who = c; get = await page.$("#dialog input[data-side=get]"); give = await page.$("#dialog input[data-side=give]");
    if (get || give) break;
    await page.click("#t-back");
    await waitFor(async () => (await dialogKind()) === "foreign", 10_000, "back to the advisor");
    await sleep(300); await tradesTab();
  }
  if (who) {
    if (get) {
      await get.check();
      await waitFor(() => page.$eval("#t-quote", el => /accepts|refuses|decides/.test(el.innerText)), 15_000, "a quote");
      const q = await page.$eval("#t-quote", el => el.innerText);
      log("quote:", q.replace(/\n/g, " | "));
      const tech = await get.evaluate(i => i.value);
      const env = await page.evaluate(([civ, tech]) => api("play/api/act", {tool: "diplomacy", args: {action: "quote_trade", civ,
        get_techs: [tech], give_techs: [], get_gold: 0, give_gold: 0}}).then(r => r.result), [who, tech]);
      check(q.includes(`You get ${tech}: worth ${env.you_value_get} to you, ${env.they_value_get} to ${who}`)
        && q.includes(`You give nothing: worth 0 to you, ${env.they_value_give} to ${who}`), `asking ${who} for ${tech} quotes the env's values`);
      const canPay = /refuses: \d+ gold/.test(q) && !/more than you can give/.test(q);
      check(canPay === !(await page.$eval("#t-balance", b => b.disabled)), `Balance is offered only when you can pay it (${canPay})`);
      if (canPay) {
        await page.click("#t-balance");
        await waitFor(() => page.$eval("#t-quote", el => /accepts|refuses|decides/.test(el.innerText) && !el.innerText.includes("Pick")), 15_000, "a new quote");
        await sleep(500);
        const q2 = await page.$eval("#t-quote", el => el.innerText);
        const gold = await page.$eval("#t-give-gold", i => +i.value);
        log("balanced:", gold, "gold →", q2.replace(/\n/g, " | "));
        check(/accepts/.test(q2) || gold > +(await page.$eval("#t-give-gold", i => i.max)), `Balance adds gold until ${who} accepts (${gold} gold)`);
      }
      await shot("trade");
      if (!(await page.$eval("#t-propose", b => b.disabled))) {
        const before = await page.evaluate(() => ({gold: S.view.state.gold, techs: S.view.state.score.techs}));
        await page.click("#t-propose");
        await settle();
        const after = await page.evaluate(() => ({gold: S.view.state.gold, techs: S.view.state.score.techs}));
        check(after.techs > before.techs, `Propose: ${who} takes the trade (techs ${before.techs}→${after.techs}, gold ${before.gold}→${after.gold})`);
        check(await dialogKind() === "foreign", "the trade goes back to the advisor");
      } else skip(`${who} refuses even balanced`);
    } else if (give) {
      await give.check();
      await waitFor(() => page.$eval("#t-quote", el => /accepts|refuses|decides/.test(el.innerText)), 15_000, "a quote");
      check(true, `offering ${who} a tech gets a quote`);
    } else skip(`no tech to trade with ${who}`);
  } else skip("no civ at peace to trade with");
  await closeDialogs();
}

// ---- turns, to the end or for a while; the turn report's offers button ----
let sawOffers = false;
for (let t = 0; t < TURNS; t++) {
  if ((await game()).game_over) break;
  await endTurn();
  if (!sawOffers && await page.$("#seeoffers")) {
    sawOffers = true;
    await page.click("#seeoffers");
    await waitFor(async () => (await dialogKind()) === "foreign", 10_000, "the offers");
    await sleep(300);
    check(await page.$("#dialog [data-accept]"), "the turn report's offers button opens the offers");
    await shot("offers");
    await page.click("#dialog [data-decline]");
    await settle();
  }
}
const g = await game();
if (g.game_over) {
  await waitFor(async () => (await dialogKind()) === "over", 10_000, "the game-over dialog");
  const text = await page.$eval("#dialog", el => el.innerText);
  log("game over:", text.replace(/\n/g, " | "));
  check(/wins (on score|by \w+)|You win|ended at turn/.test(text), "game over says who won and how");
  await shot("over");
} else skip("the game is not over");

check(errors.length === 0, `no page errors (${errors.slice(0, 5).join(" / ")})`);
await browser.close();
const failed = checks.filter(c => !c.ok);
log(`${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
