// The match viewer's map in the OpenCiv3 client's own art (docs/viewer.md, section 6), when the env has the art: the
// play page's ArtPainter (art.js) drawing every civ's world for spectators, or what one seat has explored. The static
// picture (terrain to cities) is drawn in chunks at three scales and kept from turn to turn wherever nothing in it
// changed; units walk the paths they took (patches/0012) and battles play as the client plays them (patches/0010).
// The viewer builds it in with map.js and art.js (viewer/__init__.py) and reads the match through app.js's Match.
"use strict";

const VA_LEVELS = [0.25, 0.5, 1];   // the chunks' scales: map pixels (a tile is 128x64) to canvas pixels
const VA_CHUNK = 512;               // a chunk's side, in canvas pixels
const VA_KEEP = 80;                 // chunks kept, about 1 MB each
const VA_MARGIN = 192;              // map pixels round a tile that a change to it can reach (borders, corners, sprites)
const VA_STEP_MS = 260;             // a step's walk, at most, when a turn plays out in full
const VA_WINNER = {a: "attacker", d: "defender", r: "retreat"};

// A turn of the match as ArtPainter reads a seat's world (map.js World): tiles and cities by position, in the
// viewer's coordinates (x from the seam, no wrap). Units are drawn by ViewArt itself.
class MatchWorld {
  constructor(m, ti, pov, version) {
    this.W = m.W; this.H = m.H; this.wrap = false; this.version = version; this.me = null; this.players = m.byIndex;
    this.m = m; this.noFog = pov == null;   // the spectator sees everything: no fog, not even at the map's edges
    const own = m.owners(ti), looks = m.looks(ti), known = pov != null ? m.known(ti) : null, bit = pov != null ? 1 << pov : 0;
    const res = m.meta.resources || [], imps = m.meta.improvements || [], terrain = m.meta.terrain || [];
    this.tiles = new Map();
    m.tiles.forEach((t, i) => {
      if (known && !(known[i] & bit)) return;
      const [ov, r, imp, bonus] = m.constructor.unlook(looks[i]);
      const improvements = [];
      for (let b = 0; b < imps.length; b++) if (imp & (1 << b)) improvements.push(imps[b]);
      this.tiles.set(t.x * 4096 + t.y, {x: t.x, y: t.y, terrain: t.base, overlay: ov >= 0 ? terrain[ov] : null,
        river: t.river, owner: own[i], visible: true, resource: r >= 0 ? res[r] : null, improvements, bonus: !!bonus});
    });
    this.cities = new Map();
    for (const [x0, y, name, owner, size, cap, prod, , era, walls] of m.turns[ti].cities) {
      const x = m.sx(x0);
      if (!this.tiles.has(x * 4096 + y)) continue;
      this.cities.set(x * 4096 + y, {x, y, name, owner, size, capital: !!cap, producing: prod, era: era | 0, walls: !!walls});
    }
  }
  key(x, y) { return x * 4096 + y; }
  wx(x) { return x; }
  tile(x, y) { return this.tiles.get(x * 4096 + y); }
  city(x, y) { return this.cities.get(x * 4096 + y); }
  unitsAt() { return []; }
  color(i) { return this.players[i]?.rgb || [150, 150, 150]; }
  civ(i) { return this.players[i]?.civ ?? "?"; }
}

// The direction of a step, as the unit sheets name them.
const vaDir = (dx, dy) => !dy ? (dx > 0 ? "E" : "W") : !dx ? (dy > 0 ? "S" : "N") : (dy < 0 ? "N" : "S") + (dx > 0 ? "E" : "W");

class ViewArt {
  constructor(art, match) {
    this.art = art; this.m = match; this.p = new ArtPainter(art);
    this.chunks = new Map();     // `${level}:${i}:${j}` -> {c: canvas, ti, pov, used}
    this.worlds = new Map();     // `${ti}:${pov}` -> MatchWorld, the last few
    this.dirty = new Map();      // turn index -> {all: [[mx, my]], known: [[mx, my]]}: map pixels of the tiles it changed
    this.facing = new Map();     // unit id -> the way it last went
    this.version = 0; this.replay = null; this.pending = false; this.game = match.game;
    this.drawn = {units: 0, walking: 0, fights: 0, battles: 0};   // what the last frame showed (the tests read it)
  }

  world(ti, pov) {
    if (this.m.game !== this.game) { this.chunks.clear(); this.worlds.clear(); this.dirty.clear(); this.facing.clear(); this.game = this.m.game; }
    const key = `${ti}:${pov}`;
    let w = this.worlds.get(key);
    if (!w) {
      w = new MatchWorld(this.m, ti, pov, ++this.version);
      this.worlds.set(key, w);
      if (this.worlds.size > 4) this.worlds.delete(this.worlds.keys().next().value);
    }
    return w;
  }

  // ---- the static picture, in chunks ----

  // The tiles turn index `ti` changed (owner, look, city), and those whose `known` changed, as map pixels.
  _dirty(ti) {
    let d = this.dirty.get(ti);
    if (d) return d;
    const m = this.m, t = m.turns[ti], px = i => [64 * m.tiles[i].x, 32 * m.tiles[i].y];
    const all = [...(t.owners || []), ...(t.looks || [])].map(r => px(r[0]));
    const was = ti > 0 ? new Map(m.turns[ti - 1].cities.map(c => [c[0] * 4096 + c[1], c])) : new Map();
    const look = c => [c[3], c[5], c[4] > 12 ? 2 : c[4] > 6 ? 1 : 0, c[8] | 0, c[9] | 0].join();
    for (const c of t.cities) {
      const k = c[0] * 4096 + c[1], before = was.get(k);
      if (!before || look(before) !== look(c)) all.push([64 * m.sx(c[0]), 32 * c[1]]);
      was.delete(k);
    }
    for (const c of was.values()) all.push([64 * m.sx(c[0]), 32 * c[1]]);
    d = {all, known: (t.known || []).map(r => px(r[0]))};
    this.dirty.set(ti, d);
    return d;
  }
  // Whether chunk `e` (drawn for another turn) still holds for turn `ti`: nothing in it changed in between.
  _holds(e, ti, pov, x0, y0, S) {
    if (e.pov !== pov || Math.abs(ti - e.ti) > 24) return false;
    const lo = Math.min(ti, e.ti) + 1, hi = Math.max(ti, e.ti);
    const inside = ([mx, my]) => mx > x0 - VA_MARGIN && mx < x0 + S + VA_MARGIN && my > y0 - VA_MARGIN && my < y0 + S + VA_MARGIN;
    for (let k = lo; k <= hi; k++) {
      const d = this._dirty(k);
      if (d.all.some(inside) || (pov != null && d.known.some(inside))) return false;
    }
    return true;
  }
  _render(e, level, i, j, world, ti, pov) {
    const S = VA_CHUNK / level;
    if (!e.c) { e.c = document.createElement("canvas"); e.c.width = e.c.height = VA_CHUNK; }
    const g = e.c.getContext("2d");
    g.setTransform(level, 0, 0, level, 0, 0);
    g.imageSmoothingEnabled = level < 1;
    this.p.origin = {ox: -i * S, oy: -j * S, fx: 0, fy: 0, MW: S, MH: S};
    this.p._static(g, world, {cx: (i * S + S / 2) / 64, cy: (j * S + S / 2) / 32}, S, S, {labels: false});
    e.ti = ti; e.pov = pov;
  }
  _chunk(key) {
    const e = this.chunks.get(key);
    if (e) { this.chunks.delete(key); this.chunks.set(key, e); }   // most recently used last
    return e;
  }
  _keep(key, e) {
    this.chunks.set(key, e);
    while (this.chunks.size > VA_KEEP) this.chunks.delete(this.chunks.keys().next().value);
  }

  // The map at view v ({hw, x0, y0}, as app.js's Painter takes it) for turn index ti, then its units. o: {pov, dpr,
  // units, battles}. Returns the cities in view, for the labels.
  draw(ctx, ti, v, w, h, o = {}) {
    const z = v.hw / 64, dpr = o.dpr || 1, pov = o.pov ?? null, now = performance.now();
    const level = VA_LEVELS.find(l => l >= z * dpr * 0.9) ?? 1, S = VA_CHUNK / level;
    const world = this.world(ti, pov);
    const mx0 = 64 * v.x0, my0 = 32 * v.y0, span = [w / z, h / z];
    const i0 = Math.max(Math.floor(mx0 / S), Math.floor(-128 / S)), i1 = Math.min(Math.floor((mx0 + span[0]) / S), Math.floor((64 * this.m.W + 128) / S));
    const j0 = Math.max(Math.floor(my0 / S), Math.floor(-128 / S)), j1 = Math.min(Math.floor((my0 + span[1]) / S), Math.floor((32 * this.m.H + 128) / S));
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h);
    const stale = [];
    ctx.save(); ctx.imageSmoothingEnabled = true;
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
      const key = `${level}:${i}:${j}`;
      let e = this._chunk(key);
      const x = Math.round((i * S - mx0) * z), y = Math.round((j * S - my0) * z);
      const x1 = Math.round(((i + 1) * S - mx0) * z), y1 = Math.round(((j + 1) * S - my0) * z);
      if (e && e.c) {
        // drawn for this turn, or for another with nothing changed in it since; else redrawn soon, the old picture
        // standing in meanwhile
        if (!(e.ti === ti && e.pov === pov)) {
          if (this._holds(e, ti, pov, i * S, j * S, S)) { e.ti = ti; e.pov = pov; } else stale.push([e, level, i, j]);
        }
      } else {
        // never drawn: a coarser chunk stands in until its turn comes; with none, it is drawn now
        const coarse = this._coarse(level, i, j);
        if (!e) { e = {c: null, ti: -1, pov}; this._keep(key, e); }
        if (coarse) {
          const [ce, cl, ci, cj] = coarse, CS = VA_CHUNK / cl;
          ctx.drawImage(ce.c, (i * S - ci * CS) * cl, (j * S - cj * CS) * cl, S * cl, S * cl, x, y, x1 - x, y1 - y);
          stale.push([e, level, i, j]);
          continue;
        }
        this._render(e, level, i, j, world, ti, pov);
      }
      ctx.drawImage(e.c, 0, 0, VA_CHUNK, VA_CHUNK, x, y, x1 - x, y1 - y);
    }
    ctx.restore();
    // a few of them a frame (at least one): about 10 ms
    const tr = performance.now();
    let n = 0;
    for (const [e, l, i, j] of stale) {
      if (n && performance.now() - tr > 10) break;
      this._render(e, l, i, j, world, ti, pov);
      n++;
    }
    this.pending = n < stale.length;
    const cam = this._cam(v, w, h);
    if (o.units !== false && v.hw >= 8) this._units(ctx, ti, world, cam, w, h, z, pov, now);
    return this._cities(world, v, w, h, z);
  }
  // The coarsest-but-finer-first chunk already drawn that covers chunk (i, j) of `level`: [entry, level, ci, cj].
  _coarse(level, i, j) {
    const S = VA_CHUNK / level;
    for (const l of [...VA_LEVELS].reverse()) {
      if (l >= level) continue;
      const CS = VA_CHUNK / l, ci = Math.floor(i * S / CS), cj = Math.floor(j * S / CS), e = this.chunks.get(`${l}:${ci}:${cj}`);
      if (e && e.c && e.ti >= 0) return [e, l, ci, cj];
    }
    return null;
  }
  _cam(v, w, h) {
    return {hw: v.hw, cx: v.x0 + w / v.hw / 2, cy: v.y0 + h / v.hw, screen: (x, y) => [(x - v.x0) * v.hw, (y - v.y0) * v.hw / 2]};
  }
  _cities(world, v, w, h, z) {
    const out = [];
    for (const c of world.cities.values()) {
      const cx = (c.x - v.x0) * v.hw, cy = (c.y - v.y0) * v.hw / 2;
      if (cx < -60 || cy < -60 || cx > w + 60 || cy > h + 60) continue;
      out.push({x: c.x, y: c.y, name: c.name, o: c.owner, size: c.size, cap: c.capital, prod: c.producing, cx, cy,
        dim: false, r: Math.max(4, 40 * z), city: c});
    }
    return out;
  }
  // The client's own city labels (CityLabelScene), for views zoomed in far enough to fit them.
  labels(ctx, cities, v) {
    const z = v.hw / 64;
    this.p.zs = z;
    const world = {color: i => this.m.byIndex[i]?.rgb || [150, 150, 150], me: null};
    for (const c of cities) {
      ctx.save(); ctx.translate(c.cx, c.cy); ctx.scale(z, z);
      this.p._cityLabel(ctx, world, c.city, {sx: 0, sy: 0}, 1);
      ctx.restore();
    }
  }

  // ---- a turn played out: units walking their paths, battles ----

  // Turn index `ti` arrives from `from` (its predecessor): its units walk where they went. `full` plays the turn out in
  // order (steps and battles by their seq, up to a few seconds); otherwise the units only walk, within `ms`.
  play(from, ti, {full = false, ms = 400, pov = null} = {}) {
    const m = this.m, t = m.turns[ti], types = m.meta.unit_types || [], bit = pov != null ? 1 << pov : 0;
    const seen = mask => pov == null || (mask & bit) !== 0;
    const events = [];
    for (const r of t.moves || []) {
      if (!seen(r[4])) continue;
      const path = [];
      for (let k = 5; k + 1 < r.length; k += 2) path.push([m.sx(r[k]), r[k + 1]]);
      if (path.length > 1) events.push({seq: r[0], kind: "move", id: r[1], owner: r[2], type: types[r[3]], path});
    }
    if (full)
      for (const r of t.battles || []) {
        if (!seen(r[5])) continue;
        const side = k => ({owner: r[k], type: types[r[k + 1]], x: m.sx(r[k + 2]), y: r[k + 3], hp_before: r[k + 4], hp_after: r[k + 5], hp_max: r[k + 6]});
        events.push({seq: r[0], kind: "battle", city: r[4], b: {kind: r[1] ? "bombard" : "attack", winner: VA_WINNER[r[2]] || "retreat",
          rounds: [...String(r[3] || "")], attacker: side(6), defender: side(13)}});
      }
    events.sort((a, b) => a.seq - b.seq);
    // when each starts and how long it takes, in ms from now
    const walk = full ? clamp(500 + 40 * events.length, 900, 3500) : ms;
    const steps = new Map();   // unit id -> its runs of steps, in order
    events.forEach((e, r) => {
      if (e.kind === "move") {
        const n = e.path.length - 1;
        if (full) { e.at = walk * 0.7 * r / Math.max(1, events.length - 1); e.dur = Math.min(VA_STEP_MS * n, walk * 0.5); }
        if (!steps.has(e.id)) steps.set(e.id, []);
        steps.get(e.id).push(e);
      } else e.at = walk * 0.7 * r / Math.max(1, events.length - 1);
    });
    if (!full)   // only walking: each unit's runs one after the other, within ms
      for (const runs of steps.values()) {
        const n = runs.reduce((s, e) => s + e.path.length - 1, 0);
        let at = 0;
        for (const e of runs) { e.at = at; e.dur = ms * (e.path.length - 1) / n; at += e.dur; }
      }
    const prev = from >= 0 && from < m.turns.length ? new Map(m.turns[from].units.filter(u => u[0] !== -1).map(u => [u[0], u])) : new Map();
    this.replay = {ti, from, start: performance.now(), walk, steps, battles: events.filter(e => e.kind === "battle"), prev,
      fights: [], end: walk};
    for (const e of events) if (e.kind === "move") this.replay.end = Math.max(this.replay.end, e.at + e.dur);
  }
  // Whether a turn is still playing out (the page keeps drawing).
  busy(now = performance.now()) {
    const r = this.replay;
    if (!r) return this.pending;
    if (now - r.start <= r.end || r.battles.some(e => !e.done) || r.fights.length) return true;
    this._settle(r);
    this.replay = null;
    return this.pending;
  }
  stop() { if (this.replay) this._settle(this.replay); this.replay = null; }
  // The units that walked keep facing the way they went.
  _settle(r) {
    for (const [id, runs] of r.steps) {
      const p = runs.at(-1).path, [a, b] = [p.at(-2), p.at(-1)];
      this.facing.set(id, vaDir(b[0] - a[0], b[1] - a[1]));
    }
  }

  // Where unit `id` is `at` ms into the replay: [x, y, facing, walking (a frame of its run) or null]; null when it isn't
  // on the map (it walked, then died).
  _where(r, id, final, at) {
    const runs = r.steps.get(id);
    if (runs) {
      let last = null;
      for (const e of runs) {
        if (at < e.at) {
          const p = last ? last.path.at(-1) : e.path[0];
          return [p[0], p[1], this.facing.get(id), null];
        }
        const n = e.path.length - 1, k = Math.min(n - 1, Math.floor((at - e.at) / (e.dur / n))), f = (at - e.at) / (e.dur / n) - k;
        if (at < e.at + e.dur) {
          const [a, b] = [e.path[k], e.path[k + 1]], dir = vaDir(b[0] - a[0], b[1] - a[1]);
          this.facing.set(id, dir);
          return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, dir, f];
        }
        last = e;
      }
      const end = last.path.at(-1);
      if (!final) return null;
      return at < r.end ? [end[0], end[1], this.facing.get(id), null] : [this.m.sx(final[1]), final[2], this.facing.get(id), null];
    }
    if (!final) return null;
    const was = r.prev.get(id), x = this.m.sx(final[1]), y = final[2];
    if (was && (was[1] !== final[1] || was[2] !== y)) {   // it moved, but no path says how: straight there
      let px = this.m.sx(was[1]); if (Math.abs(px - x) > this.m.W / 2) px += px < x ? this.m.W : -this.m.W;
      const f = clamp(at / r.walk, 0, 1);
      if (f < 1) return [px + (x - px) * f, was[2] + (y - was[2]) * f, vaDir(x - px, y - was[2]), (at % VA_STEP_MS) / VA_STEP_MS];
    }
    return [x, y, this.facing.get(id), null];
  }

  _units(ctx, ti, world, cam, w, h, z, pov, now) {
    const m = this.m, types = m.meta.unit_types || [], civilian = new Set(m.meta.civilian || []);
    const r = this.replay && this.replay.ti === ti ? this.replay : null, at = r ? now - r.start : 0;
    const hz = 1 / clamp(z, 0.5, 1);
    // battles start at their turn in the replay, and hold their tiles while they play
    const busy = new Set();
    if (r) {
      for (const e of r.battles)
        if (!e.done && at >= e.at) {
          const f = this.p.fightOf(e.b, now);
          if (f) { r.fights.push(f); e.done = true; }
          else if (at > e.at + 3000) e.done = true;   // its art never came
        }
      r.fights = r.fights.filter(f => now - f.t0 <= f.total);
      for (const f of r.fights) for (const s of [f.a, f.d]) busy.add(s.x * 4096 + s.y);
    }
    const shown = pov != null ? (x, y) => world.tile(Math.round(x), Math.round(y)) : () => true;
    const still = new Map(), walking = [];
    const place = (row, id) => {
      const where = r ? this._where(r, id, row, at) : [m.sx(row[1]), row[2], this.facing.get(id), null];
      if (!where) return;
      const [x, y, facing, f] = where;
      if (!shown(x, y)) return;
      const run = row ? null : r.steps.get(id)[0];   // a unit that walked, then died: what its steps say
      const u = {id, owner: row ? row[3] : run.owner, type: row ? types[row[4]] : run.type,
        hp: row ? row[5] ?? 3 : 3, hp_max: row ? row[6] ?? 3 : 3, fortified: !!(row && row[7]), x, y, facing, f};
      if (f != null) { walking.push(u); return; }
      const k = Math.round(x) * 4096 + Math.round(y);
      if (busy.has(k)) return;
      if (!still.has(k)) still.set(k, []);
      still.get(k).push(u);
    };
    const finals = new Set();
    for (const row of m.turns[ti].units) { place(row, row[0]); if (row[0] !== -1) finals.add(row[0]); }
    if (r) for (const id of r.steps.keys()) if (!finals.has(id)) place(null, id);   // walked, then died
    const draws = [];
    for (const us of still.values()) {
      const top = us.find(u => !civilian.has(u.type)) || us[0];
      draws.push({u: top, count: us.length});
    }
    for (const u of walking) draws.push({u, count: 1});
    draws.sort((a, b) => a.u.y - b.u.y || a.u.x - b.u.x);
    ctx.save();
    ctx.imageSmoothingEnabled = z < 0.999;
    for (const {u, count} of draws) {
      const [sx, sy] = cam.screen(u.x, u.y);
      if (sx < -80 || sy < -80 || sx > w + 80 || sy > h + 80) continue;
      const art = this.art.unit(u.type, () => draw());
      let action = u.fortified ? "fortify" : "default", frame = 0;
      if (u.f != null && art) { const run = Art.act(art, "run"); action = "run"; frame = Math.floor(u.f * run.frames) % run.frames; }
      this.p._drawUnit(ctx, world, u, art, sx, sy, {z, hz, cam, action, frame, facing: u.facing || "SE", count,
        hp: u.hp, hpMax: u.hp_max});
    }
    for (const f of r ? r.fights : []) this.p._drawBattle(ctx, world, cam, w, h, f, z, hz);
    ctx.restore();
    this.drawn = {units: draws.length, walking: walking.length, fights: r ? r.fights.length : 0,
      battles: r ? r.battles.filter(e => e.done).length : this.drawn.battles};
  }
}
