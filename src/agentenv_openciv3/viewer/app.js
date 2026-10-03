// The match viewer (docs/viewer.md): live at GET /live, and embedded in recordings as one HTML file.
// No libraries. Sections: model, map painter, app shell, map view, agents view, summary view, timeline, broadcast
// (?stream), live.
"use strict";

// ======================================================================== utilities

const TERRAIN_RGB = {
  ocean: [22, 38, 64], sea: [28, 48, 80], coast: [44, 72, 108], grassland: [86, 120, 70], plains: [140, 132, 84],
  desert: [176, 160, 116], tundra: [140, 146, 140], floodplain: [104, 132, 76], hills: [118, 104, 76],
  mountains: [108, 102, 98], forest: [52, 88, 58], jungle: [44, 96, 70], marsh: [80, 102, 92], volcano: [96, 64, 58],
};
const WATER = new Set(["ocean", "sea", "coast"]);
const FOG = [9, 11, 16];
const KIND = {   // label, rank (lower first), hot (red), on the timeline
  civ_destroyed: ["eliminated", 0, true, true], city_captured: ["captured", 1, true, true],
  city_destroyed: ["razed", 1, true, true], war_declared: ["war", 1, true, true], peace_signed: ["peace", 2, false, true],
  lead_change: ["lead", 2, false, true], victory: ["victory", 0, false, true], government_changed: ["government", 3],
  city_founded: ["founded", 4], unit_lost: ["unit lost", 5, true], gold_stolen: ["gold stolen", 5, true],
  disorder: ["disorder", 5, true], disorder_started: ["disorder", 5, true], city_starved: ["starved", 5],
  contact: ["contact", 6], tech_learned: ["tech", 7], unit_promoted: ["promoted", 8], defenseless: ["defenseless", 6],
  attacked: ["attacked", 4, true], bombarded: ["bombarded", 5, true], engine_restarted: ["engine", 8],
};
const kindLabel = k => (KIND[k] || [k])[0];
const kindRank = k => (KIND[k] || [0, 9])[1];
const kindHot = k => !!(KIND[k] || [])[2];

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const hexRgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t].map(Math.round);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const ease = t => 1 - Math.pow(1 - t, 3);
const fmtSecs = s => s == null ? "" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`;
const NS = "http://www.w3.org/2000/svg";

function spread(ys, gap, lo, hi) {
  const order = ys.map((_, i) => i).sort((a, b) => ys[a] - ys[b]), out = ys.slice();
  let prev = lo - gap; for (const i of order) prev = out[i] = Math.max(ys[i], prev + gap);
  let next = hi + gap; for (const i of order.reverse()) next = out[i] = Math.min(out[i], next - gap);
  return out;
}
function niceMax(v) {
  const step = Math.pow(10, Math.floor(Math.log10(Math.max(1, v)))), m = v / step;
  return ([1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(x => m <= x + 1e-9)) * step;
}
function ticks(max) {
  const step = Math.pow(10, Math.floor(Math.log10(max))), m = max / step;
  const s = (m <= 1.5 ? 0.25 : m <= 3 ? 0.5 : m <= 6 ? 1 : 2) * step;
  const out = []; for (let v = 0; v <= max + 1e-9; v += s) out.push(Math.round(v)); return out;
}
function sparkPath(values, w, h, max) {
  const n = values.length; if (n < 2) return "";
  const top = max || Math.max(1, ...values);
  return values.map((v, i) => `${i ? "L" : "M"}${(i / (n - 1) * w).toFixed(1)},${(h - v / top * h).toFixed(1)}`).join("");
}
function sizeCanvas(canvas) {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, w, h, dpr};
}
function makeCanvas(w, h) {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(w, h);
  const c = document.createElement("canvas"); c.width = w; c.height = h; return c;
}

// ======================================================================== model

class Match {
  constructor() { this.reset(); }
  reset() {
    this.game = null; this.ready = false; this.turns = []; this.players = []; this.meta = {}; this.live = null;
    this.version = 0;
  }
  // Fold a data document (docs/viewer.md) in: the first one has `static`; later ones only add turns.
  ingest(doc) {
    if (!doc) return false;
    if (doc.game !== this.game || doc.static) {
      if (!doc.static) return false;            // a new game: the caller fetches it whole
      this.reset();
      this.game = doc.game;
      this._static(doc);
    }
    this.meta = doc.meta || this.meta;
    this._players(doc.players || []);
    let added = false;
    for (const t of doc.turns || []) {
      const last = this.turns.at(-1);
      if (last && t.turn <= last.turn) continue;
      this._addTurn(t); added = true;
    }
    if ("live" in doc) this.live = doc.live;
    this.ready = this.turns.length > 0;
    this.version++;
    return added;
  }
  _static(doc) {
    const m = doc.meta, W = m.map.width;
    this.W = W; this.H = m.map.height;
    this.seam = m.seam - (m.seam % 2);
    this.tiles = doc.static.tiles.map(([x, y, t, o, r]) => ({
      x: (x - this.seam + W) % W, y, base: m.terrain[t], over: o >= 0 ? m.terrain[o] : null, river: r,
    }));
    this.tileAt = new Map(this.tiles.map((t, i) => [t.x * 4096 + t.y, i]));
    this.ownersCk = new Map(); this.knownCk = new Map();
    this.landBox = this.box(this.tiles.filter(t => !WATER.has(t.base)), 3);
    this.series = {}; this.events = []; this.leaders = []; this.unitIndex = []; this.messages = [];
  }
  _players(list) {
    for (const p of list) {
      const have = this.players.find(q => q.index === p.index);
      if (have) { Object.assign(have, p); continue; }
      this.players.push({...p, rgb: hexRgb(p.color)});
    }
    this.byIndex = Object.fromEntries(this.players.map(p => [p.index, p]));
    this.civs = this.players.filter(p => !p.barbarian);
    this.seats = this.players.filter(p => p.seat != null).sort((a, b) => a.seat - b.seat);
    for (const p of this.civs) this.series[p.index] ||= [];
  }
  _addTurn(t) {
    const ti = this.turns.length;
    this.turns.push(t);
    for (const p of this.civs) this.series[p.index][ti] = t.scores[p.index] || this.series[p.index][ti - 1] || [0, 0, 0, 0, 0, 0];
    for (const e of t.events) this.events.push({...e, turn: t.turn, ti, x: e.x == null ? null : this.sx(e.x)});
    for (const m of t.messages || []) this.messages.push({...m, turn: t.turn - 1, ti: Math.max(0, ti - 1)});
    // the score leader, or null while the top score is tied (every civ starts on the same score)
    const top = Math.max(...this.civs.map(p => this.series[p.index][ti][0]));
    const at = this.civs.filter(p => this.series[p.index][ti][0] === top);
    this.leaders[ti] = at.length === 1 ? at[0].index : null;
    const units = new Map();
    for (const u of t.units) if (u[0] !== -1) units.set(u[0], u);
    this.unitIndex[ti] = units;
  }
  get last() { return this.turns.length - 1; }
  // A lead change counts once the new leader has held the lead every turn since, for up to 5 turns: in a close race
  // the lead flips straight back again and again. Uses nothing after turn index `upto`, so it works live too.
  durable(e, upto) {
    if (e.kind !== "lead_change") return true;
    for (let k = e.ti; k <= Math.min(e.ti + 4, upto); k++) if (this.leaders[k] !== e.owner) return false;
    return true;
  }
  get limit() { return this.meta.turn_limit || this.turns.at(-1)?.turn || 1; }
  sx(x) { return (x - this.seam + this.W) % this.W; }
  name(i) { const p = this.byIndex[i]; return p ? (p.label || p.civ) : "?"; }
  full(i) { const p = this.byIndex[i]; return p ? (p.label ? `${p.label} · ${p.civ}` : p.civ) : "?"; }
  tileIndex(x, y) { return this.tileAt.get(((x + this.W) % this.W) * 4096 + y); }
  // Owners / known masks at turn index ti, from the per-turn deltas with a checkpoint every 10 turns.
  _layer(ti, key, ck, fill) {
    if (ck.has(ti)) return ck.get(ti);
    let start = -1, base = null;
    for (const [k, v] of ck) if (k <= ti && k > start) { start = k; base = v; }
    const arr = base ? Int32Array.from(base) : new Int32Array(this.tiles.length).fill(fill);
    for (let t = start + 1; t <= ti; t++) {
      for (const [i, v] of this.turns[t][key]) arr[i] = v;
      if (t % 10 === 0 && !ck.has(t)) ck.set(t, Int32Array.from(arr));
    }
    ck.set(ti, arr);
    if (ck.size > 48) for (const k of ck.keys()) if (k % 10 && k !== ti) { ck.delete(k); break; }
    return arr;
  }
  owners(ti) { return this._layer(ti, "owners", this.ownersCk, -1); }
  known(ti) { return this._layer(ti, "known", this.knownCk, 0); }
  ranks(ti) {
    const order = this.civs.map(p => p.index).sort((a, b) => this.series[b][ti][0] - this.series[a][ti][0] || a - b);
    return Object.fromEntries(order.map((i, r) => [i, r + 1]));
  }
  stats(ti, i) { return (this.turns[ti]?.stats || {})[i] || {}; }
  // What a seat did during turn index ti: entry ti + 1 holds it; the turn in progress live comes from `live`.
  actionsDuring(ti, i) {
    const next = this.turns[ti + 1];
    if (next) return {actions: (next.actions || {})[i] || [], calls: (next.calls || {})[i] || null, done: true};
    const seat = this.liveSeat(i);
    if (seat && ti === this.last) return {actions: seat.actions || [], calls: seat.calls || null, done: false, live: seat};
    return {actions: [], calls: null, done: false};
  }
  liveSeat(i) {
    const p = this.byIndex[i];
    return this.live?.seats?.find(s => s.civ === p?.civ) || null;
  }
  // What a seat wrote for the people watching, as {text, turn}: its newest end_turn note and plan up to the end of turn
  // index ti. Entry ti + 1 holds what was written during turn ti; the turn in progress live comes from `live`.
  lastNote(ti, i) {
    const s = ti === this.last ? this.liveSeat(i) : null;
    return s?.note ? {text: s.note, turn: this.live.turn} : this._newest(ti, "notes", i);
  }
  planAt(ti, i) {
    const s = ti === this.last ? this.liveSeat(i) : null;
    return s?.plan ? {text: s.plan, turn: s.plan_turn ?? null} : this._newest(ti, "plans", i);
  }
  _newest(ti, key, i) {
    for (let k = Math.min(ti + 1, this.last); k >= 0; k--) {
      const text = (this.turns[k][key] || {})[i];
      if (text) return {text, turn: this.turns[k].turn - 1};
    }
    return null;
  }
  // The messages sent up to the end of turn index ti, oldest first: {from, to: [index] | "all", text, turn}.
  messagesUpTo(ti) {
    const out = this.messages.filter(m => m.ti <= ti);
    if (ti !== this.last) return out;
    const civ = n => this.players.find(p => p.civ === n)?.index;
    for (const m of this.live?.messages || []) {
      const from = civ(m.from);
      if (from != null) out.push({from, to: m.to === "all" ? "all" : (m.to || []).map(civ).filter(i => i != null),
        text: m.text, turn: this.live.turn, ti});
    }
    return out;
  }
  box(tiles, margin) {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const t of tiles) { x0 = Math.min(x0, t.x); y0 = Math.min(y0, t.y); x1 = Math.max(x1, t.x); y1 = Math.max(y1, t.y); }
    if (x0 > x1) return {x0: 0, y0: 0, x1: this.W, y1: this.H};
    return {x0: Math.max(-1, x0 - margin), y0: Math.max(-1, y0 - margin * 2), x1: Math.min(this.W, x1 + margin),
      y1: Math.min(this.H, y1 + margin * 2)};
  }
  civBox(i, ti, margin = 3) {
    const own = this.owners(ti), pts = this.tiles.filter((_, k) => own[k] === i);
    if (!pts.length) for (const c of this.turns[ti].cities) if (c[3] === i) pts.push({x: this.sx(c[0]), y: c[1]});
    if (!pts.length) for (const u of this.turns[ti].units) if (u[3] === i) pts.push({x: this.sx(u[1]), y: u[2]});
    return pts.length ? this.box(pts, margin) : null;
  }
  seatBox(seat, ti, margin = 2) {   // what a seat has explored
    const known = this.known(ti), bit = 1 << seat, pts = this.tiles.filter((_, k) => known[k] & bit);
    return pts.length ? this.box(pts, margin) : null;
  }
  turnIndex(turn) { const i = this.turns.findIndex(t => t.turn >= turn); return i < 0 ? this.last : i; }
}

// ======================================================================== map painter

// Fit a tile box into w x h: the half-tile width that fits it, and the box grown to the canvas' aspect.
function fitView(box, w, h, maxHw = 40) {
  const bw = box.x1 - box.x0 + 1, bh = (box.y1 - box.y0 + 1) / 2;
  const hw = Math.min(maxHw, w / bw, h / bh);
  const ex = (w / hw - bw) / 2, ey = (h / hw - bh);
  return {hw, x0: box.x0 - ex, y0: box.y0 - ey};
}

class Painter {
  constructor(match) {
    this.m = match;
    this.cache = new Map();
    this.tColor = match.tiles.map(t => {
      const c = TERRAIN_RGB[t.over || t.base] || [100, 100, 100];
      return mix(c, [0, 0, 0], ((t.x * 7 + t.y * 13) % 5) * 0.025);
    });
    this.rivers = this._rivers();
  }
  // River tiles only carry a flag. Joining every pair of river neighbours draws a lattice of little diamonds, so join
  // them with a spanning forest instead (a branching line), then cut the zigzags' corners at the segments' midpoints.
  _rivers() {
    const m = this.m, par = Int32Array.from(m.tiles, (_, i) => i), segs = [];
    const root = i => { while (par[i] !== i) { par[i] = par[par[i]]; i = par[i]; } return i; };
    m.tiles.forEach((t, i) => {
      if (!t.river) return;
      for (const [dx, dy] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
        const j = m.tileIndex(t.x + dx, t.y + dy);
        if (j == null || !m.tiles[j].river || Math.abs(m.tiles[j].x - t.x) !== 1) continue;
        const a = root(i), b = root(j);
        if (a !== b) { par[a] = b; segs.push([i, j]); }
      }
    });
    const mids = new Map();
    for (const [i, j] of segs) {
      const a = m.tiles[i], b = m.tiles[j], mid = [(a.x + b.x) / 2, (a.y + b.y) / 2];
      for (const k of [i, j]) { if (!mids.has(k)) mids.set(k, []); mids.get(k).push(mid); }
    }
    const lines = [];
    for (const [i, ms] of mids) {
      const t = m.tiles[i];
      if (ms.length === 2) lines.push([i, ...ms[0], ...ms[1]]); else for (const q of ms) lines.push([i, t.x, t.y, ...q]);
    }
    return lines;
  }
  center(x, y, v) { return [(x - v.x0) * v.hw, (y - v.y0) * v.hw / 2]; }
  path(ctx, x, y, v, s = 1) {
    const [cx, cy] = this.center(x, y, v), w = v.hw * s, h = v.hw / 2 * s;
    ctx.moveTo(cx, cy - h); ctx.lineTo(cx + w, cy); ctx.lineTo(cx, cy + h); ctx.lineTo(cx - w, cy); ctx.closePath();
  }
  // The static terrain at a view, cached by view.
  terrain(v, w, h, dpr) {
    const key = [v.x0.toFixed(3), v.y0.toFixed(3), v.hw.toFixed(3), w, h, dpr].join();
    let c = this.cache.get(key);
    if (c) return c;
    c = makeCanvas(Math.round(w * dpr), Math.round(h * dpr));
    const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
    this.ground(ctx, v, w, h);
    if (this.cache.size > 24) this.cache.delete(this.cache.keys().next().value);
    this.cache.set(key, c);
    return c;
  }
  ground(ctx, v, w, h) {
    ctx.fillStyle = "#0b0e14"; ctx.fillRect(0, 0, w, h);
    const m = this.m;
    for (let i = 0; i < m.tiles.length; i++) {
      const t = m.tiles[i], [cx, cy] = this.center(t.x, t.y, v);
      if (cx < -v.hw || cy < -v.hw || cx > w + v.hw || cy > h + v.hw) continue;
      ctx.beginPath(); this.path(ctx, t.x, t.y, v, 1.03); ctx.fillStyle = rgb(this.tColor[i]); ctx.fill();
    }
  }
  /* opts: focus (player index|null), pov (seat number|null: draw only what that seat knows), labels (auto|all|none),
     ownLabels (bool), units, territory, borders (bool), anim ({from, t} to tween from turn index `from`),
     pulses [{x, y, rgb, t}], marks [{x, y, city, age, span}] (recent battles), moving (the camera is flying: a
     cached terrain per frame would only be thrown away), dpr */
  draw(ctx, ti, v, w, h, o = {}) {
    const m = this.m, own = m.owners(ti), focus = o.focus ?? null, dpr = o.dpr || 1;
    const pov = o.pov ?? null, known = pov != null ? m.known(ti) : null, bit = pov != null ? 1 << pov : 0;
    const seen = i => !known || (known[i] & bit) !== 0;
    if (o.moving) this.ground(ctx, v, w, h); else ctx.drawImage(this.terrain(v, w, h, dpr), 0, 0, w, h);
    const vis = t => {
      const cx = (t.x - v.x0) * v.hw, cy = (t.y - v.y0) * v.hw / 2;
      return cx > -v.hw && cy > -v.hw && cx < w + v.hw && cy < h + v.hw;
    };
    const anim = o.anim && o.anim.from >= 0 && o.anim.from !== ti ? o.anim : null;
    const prevOwn = anim ? m.owners(anim.from) : null, at = anim ? ease(anim.t) : 1;
    if (o.territory !== false) {
      for (let i = 0; i < m.tiles.length; i++) {
        const t = m.tiles[i]; if (!vis(t)) continue;
        if (!seen(i)) continue;
        let oNow = own[i], a = 1;
        if (prevOwn && prevOwn[i] !== oNow) {   // tween a tile changing hands
          if (at < 0.5 && prevOwn[i] >= 0) { oNow = prevOwn[i]; a = 1 - at * 2; } else a = oNow >= 0 ? (at - 0.5) * 2 : 0;
          if (oNow < 0) continue;
        }
        if (oNow < 0) continue;
        const p = m.byIndex[oNow]; if (!p) continue;
        const dim = focus != null && oNow !== focus;
        const amt = (dim ? 0.08 : WATER.has(t.base) ? 0.2 : 0.38) * a;
        ctx.beginPath(); this.path(ctx, t.x, t.y, v, 1.03); ctx.fillStyle = rgb(mix(this.tColor[i], p.rgb, amt)); ctx.fill();
      }
    }
    if (this.rivers.length) {   // over the territory tint, under the borders; fog covers what a seat hasn't seen
      ctx.strokeStyle = "rgba(64,98,132,0.95)"; ctx.lineWidth = clamp(v.hw / 10, 0.8, 2.4); ctx.lineCap = "round";
      ctx.lineJoin = "round"; ctx.beginPath();
      for (const [, ax, ay, bx, by] of this.rivers) {
        const [x0, y0] = this.center(ax, ay, v), [x1, y1] = this.center(bx, by, v);
        if (Math.max(x0, x1) < -4 || Math.min(x0, x1) > w + 4 || Math.max(y0, y1) < -4 || Math.min(y0, y1) > h + 4) continue;
        ctx.moveTo(x0, y0); ctx.lineTo(x1, y1);
      }
      ctx.stroke();
    }
    if (o.borders !== false) {
      ctx.lineCap = "round";
      const edges = [[1, -1, 0, 1], [1, 1, 1, 2], [-1, 1, 2, 3], [-1, -1, 3, 0]];
      for (let i = 0; i < m.tiles.length; i++) {
        const oNow = own[i]; if (oNow < 0) continue;
        const t = m.tiles[i]; if (!vis(t) || !seen(i)) continue;
        const p = m.byIndex[oNow]; if (!p) continue;
        const fresh = prevOwn && prevOwn[i] !== oNow;
        const [cx, cy] = this.center(t.x, t.y, v), pw = v.hw * 0.9, ph = v.hw / 2 * 0.9;
        const pts = [[cx, cy - ph], [cx + pw, cy], [cx, cy + ph], [cx - pw, cy]];
        const dim = focus != null && oNow !== focus;
        ctx.strokeStyle = rgb(mix(p.rgb, [255, 255, 255], dim ? 0 : 0.15), (dim ? 0.3 : 1) * (fresh ? at : 1));
        ctx.lineWidth = Math.max(1.1, v.hw / (dim ? 7 : 4.5));
        for (const [dx, dy, a, b] of edges) {
          const j = m.tileIndex(t.x + dx, t.y + dy);
          if (j == null || own[j] !== oNow) { ctx.beginPath(); ctx.moveTo(...pts[a]); ctx.lineTo(...pts[b]); ctx.stroke(); }
        }
      }
    }
    if (known) {   // fog: what this seat has never seen
      ctx.fillStyle = rgb(FOG);
      for (let i = 0; i < m.tiles.length; i++) {
        if (known[i] & bit) continue;
        const t = m.tiles[i]; if (!vis(t)) continue;
        ctx.beginPath(); this.path(ctx, t.x, t.y, v, 1.04); ctx.fill();
      }
    }
    const turn = m.turns[ti];
    const cityAt = new Set(turn.cities.map(c => c[0] * 4096 + c[1]));
    if (o.units !== false && v.hw >= 4) this._units(ctx, ti, v, w, h, {focus, seen, cityAt, anim, at});
    const cities = this._cities(ctx, ti, v, w, h, {focus, seen, anim, at});
    if ((o.labels || "auto") !== "none") this._labels(ctx, cities, v, w, h, o);
    for (const mk of o.marks || []) {
      const i = m.tileIndex(mk.x, mk.y); if (i == null || !seen(i)) continue;
      const [cx, cy] = this.center(mk.x, mk.y, v); if (cx < -20 || cy < -20 || cx > w + 20 || cy > h + 20) continue;
      const a = 1 - mk.age / (mk.span + 1);
      ctx.strokeStyle = `rgba(240,116,90,${a.toFixed(2)})`; ctx.lineCap = "round";
      if (mk.city) {
        ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(cx, cy, Math.max(7, v.hw * 0.95), 0, 7); ctx.stroke();
      }
      const r = clamp(v.hw * 0.32, 3, 7);
      ctx.lineWidth = mk.city ? 2.5 : 2; ctx.beginPath();
      ctx.moveTo(cx - r, cy - r); ctx.lineTo(cx + r, cy + r); ctx.moveTo(cx + r, cy - r); ctx.lineTo(cx - r, cy + r); ctx.stroke();
    }
    for (const pl of o.pulses || []) {
      const [cx, cy] = this.center(pl.x, pl.y, v), r = v.hw * (0.8 + pl.t * 2.4);
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.strokeStyle = rgb(pl.rgb, 1 - pl.t); ctx.lineWidth = 2.5; ctx.stroke();
    }
    return cities;
  }
  _units(ctx, ti, v, w, h, {focus, seen, cityAt, anim, at}) {
    const m = this.m, types = m.meta.unit_types || [], civilian = new Set(m.meta.civilian || []);
    const before = anim ? m.unitIndex[anim.from] : null;
    const stacks = new Map();
    for (const u of m.turns[ti].units) {
      const [id, x0, y, o, type] = u;
      const i = m.tileIndex(m.sx(x0), y); if (i == null || !seen(i)) continue;
      let x = m.sx(x0), yy = y;
      const was = before && id !== -1 ? before.get(id) : null;
      if (was && (was[1] !== x0 || was[2] !== y)) {     // tween a move, the short way round the wrap
        let px = m.sx(was[1]); if (Math.abs(px - x) > m.W / 2) px += px < x ? m.W : -m.W;
        x = px + (x - px) * at; yy = was[2] + (y - was[2]) * at;
      }
      const key = Math.round(x * 8) * 100000 + Math.round(yy * 8) * 16 + 0;
      const mil = !civilian.has(types[type]);
      let s = stacks.get(key);
      if (!s) stacks.set(key, s = {x, y: yy, owners: new Map(), mil: 0, civ: 0, onCity: cityAt.has(x0 * 4096 + y) && !was});
      s.owners.set(o, (s.owners.get(o) || 0) + 1); s[mil ? "mil" : "civ"]++;
    }
    const big = v.hw >= 14, r = clamp(v.hw / 5, 1.6, 4.5);
    for (const s of stacks.values()) {
      if (s.onCity && !big) continue;                       // garrisons are implied by the city
      if (!s.mil && !big) continue;                         // workers and settlers only when zoomed in
      let [cx, cy] = this.center(s.x, s.y, v);
      if (s.onCity) { cx += v.hw * 0.55; cy += v.hw * 0.18; }
      if (cx < -10 || cy < -10 || cx > w + 10 || cy > h + 10) continue;
      const owners = [...s.owners.keys()];
      owners.slice(0, 3).forEach((o, k) => {
        const p = m.byIndex[o]; if (!p) return;
        const dim = focus != null && o !== focus, ox = cx + (k - (Math.min(3, owners.length) - 1) / 2) * r * 1.9;
        ctx.beginPath();
        if (s.mil) ctx.arc(ox, cy, r, 0, 7); else ctx.rect(ox - r * 0.85, cy - r * 0.85, r * 1.7, r * 1.7);
        ctx.fillStyle = rgb(p.rgb, dim ? 0.3 : 1); ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = dim ? "rgba(0,0,0,0.3)" : "rgba(0,0,0,0.85)"; ctx.stroke();
      });
      const n = s.mil + s.civ;
      if (big && n > 1) {
        ctx.font = `600 ${Math.round(clamp(v.hw * 0.42, 9, 12))}px system-ui,sans-serif`;
        ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.lineWidth = 3; ctx.strokeStyle = "rgba(6,8,12,.9)";
        ctx.strokeText(String(n), cx + r + 2, cy - r); ctx.fillStyle = "#e8eaee"; ctx.fillText(String(n), cx + r + 2, cy - r);
      }
    }
  }
  _cities(ctx, ti, v, w, h, {focus, seen, anim, at}) {
    const m = this.m;
    const before = anim ? new Set(m.turns[anim.from].cities.map(c => c[2])) : null;
    const out = [];
    for (const [x0, y, name, o, size, cap, prod] of m.turns[ti].cities) {
      const x = m.sx(x0), i = m.tileIndex(x, y);
      if (i == null || !seen(i)) continue;
      const [cx, cy] = this.center(x, y, v), p = m.byIndex[o];
      if (!p || cx < -30 || cy < -30 || cx > w + 30 || cy > h + 30) continue;
      out.push({x, y, name, o, size, cap, prod, cx, cy, dim: focus != null && o !== focus,
        fresh: before ? !before.has(name) : false});
    }
    out.sort((a, b) => a.size - b.size);
    for (const c of out) {
      const p = m.byIndex[c.o];
      let r = Math.max(2.4, v.hw * (0.3 + Math.min(c.size, 14) * 0.032)) * (c.dim ? 0.7 : 1);
      if (c.fresh) r *= 0.3 + 0.7 * clamp(at * 1.2, 0, 1.1);
      c.r = r;
      if (!c.dim) { ctx.beginPath(); ctx.arc(c.cx, c.cy, r + 1.6, 0, 7); ctx.fillStyle = "rgba(6,8,12,0.92)"; ctx.fill(); }
      ctx.beginPath(); ctx.arc(c.cx, c.cy, r, 0, 7); ctx.fillStyle = rgb(p.rgb, c.dim ? 0.45 : 1); ctx.fill();
      if (c.cap) { ctx.beginPath(); star(ctx, c.cx, c.cy, r * 0.62); ctx.fillStyle = c.dim ? "rgba(255,255,255,.45)" : "#fff"; ctx.fill(); }
      else if (p.seat != null && !c.dim && r > 4) { ctx.beginPath(); ctx.arc(c.cx, c.cy, r * 0.32, 0, 7); ctx.fillStyle = "rgba(255,255,255,.75)"; ctx.fill(); }
    }
    return out;
  }
  _labels(ctx, cities, v, w, h, o) {
    const mode = o.labels || "auto", focus = o.focus ?? null;
    const own = o.ownLabels && focus != null;
    const minSize = mode === "all" ? 0 : v.hw >= 24 ? 0 : v.hw >= 15 ? 4 : v.hw >= 10 ? 7 : 99;
    const ownMin = v.hw >= 10 ? 0 : v.hw >= 7 ? 4 : 7;
    const order = cities.filter(c => own ? !c.dim && (c.cap || c.size >= ownMin)
      : (c.cap || c.size >= minSize) && !(c.dim && !c.cap)).sort((a, b) => (b.cap - a.cap) || (b.size - a.size));
    const placed = [], fs = Math.round(clamp(v.hw * 0.85, 10, 13));
    const zoomedIn = v.hw >= 18;
    for (const c of order) {
      ctx.font = `${c.cap ? 700 : 500} ${fs}px system-ui,-apple-system,Segoe UI,sans-serif`;
      const text = `${c.name} ${c.size}`, tw = ctx.measureText(text).width;
      const sub = zoomedIn && c.prod && !c.dim ? c.prod : null;
      const sw = sub ? (ctx.font = `500 ${fs - 2}px system-ui,sans-serif`, ctx.measureText(sub).width) : 0;
      ctx.font = `${c.cap ? 700 : 500} ${fs}px system-ui,-apple-system,Segoe UI,sans-serif`;
      const bw = Math.max(tw, sw), bh = fs + (sub ? fs : 0);
      const tries = [[0, -c.r - 4, "c", "b"], [0, c.r + 3, "c", "t"], [c.r + 4, 0, "l", "m"], [-c.r - 4, 0, "r", "m"]];
      for (const [dx, dy, al, bl] of tries) {
        const x0 = al === "c" ? c.cx + dx - bw / 2 : al === "l" ? c.cx + dx : c.cx + dx - bw;
        const y0 = bl === "b" ? c.cy + dy - bh : bl === "t" ? c.cy + dy : c.cy - bh / 2;
        const b = [x0 - 2, y0 - 1, x0 + bw + 2, y0 + bh + 1];
        if (b[0] < 0 || b[1] < 0 || b[2] > w || b[3] > h) continue;
        if (placed.some(q => q[0] < b[2] && b[0] < q[2] && q[1] < b[3] && b[1] < q[3])) continue;
        if (v.hw >= 10 && cities.some(q => q !== c && !q.dim && q.cx + q.r > b[0] && q.cx - q.r < b[2] && q.cy + q.r > b[1] && q.cy - q.r < b[3])) continue;
        placed.push(b);
        const tx = al === "c" ? x0 + bw / 2 : al === "l" ? x0 : x0 + bw;
        ctx.textAlign = al === "c" ? "center" : al === "l" ? "left" : "right"; ctx.textBaseline = "top"; ctx.lineJoin = "round";
        ctx.lineWidth = 3; ctx.strokeStyle = "rgba(6,8,12,0.92)";
        ctx.strokeText(text, tx, y0); ctx.fillStyle = c.cap ? "#fff" : "#dadde3"; ctx.fillText(text, tx, y0);
        if (sub) {
          ctx.font = `500 ${fs - 2}px system-ui,sans-serif`; ctx.strokeText(sub, tx, y0 + fs);
          ctx.fillStyle = "#9aa1ab"; ctx.fillText(sub, tx, y0 + fs);
        }
        break;
      }
    }
  }
  pick(px, py, v) {
    const gx = px / v.hw + v.x0, gy = py / (v.hw / 2) + v.y0;
    let best = null, bd = 1.05;
    for (let x = Math.floor(gx) - 1; x <= Math.ceil(gx) + 1; x++)
      for (let y = Math.floor(gy) - 1; y <= Math.ceil(gy) + 1; y++) {
        const i = this.m.tileIndex(x, y); if (i == null) continue;
        const d = Math.abs(gx - x) + Math.abs(gy - y);
        if (d < bd) { bd = d; best = i; }
      }
    return best;
  }
}
function star(ctx, cx, cy, r) {
  for (let i = 0; i < 10; i++) {
    const a = -Math.PI / 2 + i * Math.PI / 5, rr = i % 2 ? r * 0.45 : r;
    ctx[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
  }
  ctx.closePath();
}

// ======================================================================== app state

const M = new Match();
let P = null;                           // the painter, made when the first document arrives
const LIVE = !window.OPENCIV_DATA;
// ?stream: a full-screen layout for broadcasting (agent-env openciv3 stream), with a director instead of controls;
// &client puts the spotlit agent's client view full size instead of in the corner, &cast=URL plays the casters'
// lines from that caster service with captions, &title= names the broadcast (docs/viewer.md, section 5).
const QUERY = new URLSearchParams(location.search);
const STREAM = QUERY.has("stream");
const STREAM_CLIENT = STREAM && QUERY.has("client");
const CAST = STREAM && QUERY.get("cast") ? QUERY.get("cast").replace(/\/+$/, "") : null;
const TITLE = STREAM ? (QUERY.get("title") || "").trim() : "";
const VIDEOS = window.OPENCIV_VIDEOS || {};
const S = {
  ti: 0, follow: true, view: "map", pov: null, focus: null, metric: 0, speed: 5, playing: null,
  layers: {territory: true, borders: true, units: true, labels: "auto"},
  kinds: new Set(["civ_destroyed", "city_captured", "city_destroyed", "war_declared", "peace_signed", "lead_change",
    "victory", "city_founded", "government_changed", "unit_lost"]),
  v: null, userMoved: false, flying: false, anim: null, pulses: [], client: {open: false, seat: null, big: false},
};
const METRICS = [["Score", 0], ["Cities", 1], ["Pop", 2], ["Land", 3], ["Techs", 4]];

function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  const civ = n => M.players.find(p => p.civ === n || p.label === n);
  if (["map", "agents", "summary"].includes(h.get("view"))) S.view = h.get("view");
  if (h.has("t")) { S.ti = M.turnIndex(+h.get("t")); S.follow = false; }
  if (h.get("focus") && civ(h.get("focus"))) S.focus = civ(h.get("focus")).index;
  const pv = h.get("pov") && civ(h.get("pov"));
  if (pv && pv.seat != null) S.pov = pv.seat;
  const cl = h.get("client") && civ(h.get("client"));
  if (cl) S.client = {open: true, seat: cl.index, big: h.get("big") === "1"};
}
let hashTimer = 0;
function writeHash() {
  if (STREAM) return;
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    const h = new URLSearchParams();
    if (S.view !== "map") h.set("view", S.view);
    if (!(LIVE && S.follow)) h.set("t", M.turns[S.ti]?.turn ?? 0);
    if (S.focus != null) h.set("focus", M.byIndex[S.focus]?.civ);
    if (S.pov != null) h.set("pov", M.seats.find(p => p.seat === S.pov)?.civ);
    if (S.client.open && S.client.seat != null) { h.set("client", M.byIndex[S.client.seat]?.civ); if (S.client.big) h.set("big", "1"); }
    const s = h.toString();
    history.replaceState(null, "", s ? "#" + s : location.pathname + location.search);
  }, 150);
}

// ======================================================================== shell

const tip = document.createElement("div");
tip.className = "tip"; tip.hidden = true;
function showTip(e, html) {
  tip.innerHTML = html; tip.hidden = false;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + tip.offsetWidth > innerWidth - 8) x = e.clientX - tip.offsetWidth - 14;
  if (y + tip.offsetHeight > innerHeight - 8) y = e.clientY - tip.offsetHeight - 14;
  tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px";
}
const hideTip = () => { tip.hidden = true; };
const sw = p => `<span class="sw" style="background:${p?.color || "#777"}"></span>`;
const lab = p => esc(p?.label || p?.civ || "?");
// A seat a person plays (docs/play.md), marked where seats are listed.
// Who plays: "3 agents", or "2 agents · 1 person" with people at the table.
function cast() {
  const people = M.seats.filter(p => p.human).length, agents = M.seats.length - people;
  return [agents ? `${agents} agent${agents > 1 ? "s" : ""}` : "", people ? `${people} ${people > 1 ? "people" : "person"}` : ""]
    .filter(Boolean).join(" · ");
}
const person = p => p?.human ? `<span class="person" title="played by a person">person</span>` : "";

function shell() {
  $("#app").innerHTML = `
  <header class="top">
    <div class="brand">${TITLE ? esc(TITLE) : "OpenCiv3"}<small id="sub"></small></div>
    <div class="tabs" role="tablist">
      <button data-view="map">Map<span class="k">M</span></button>
      <button data-view="agents">Agents<span class="k">A</span></button>
      <button data-view="summary">Summary<span class="k">S</span></button>
    </div>
    <label class="pov">See as <select id="pov" aria-label="Whose view of the map"></select></label>
    <div class="status"><span id="waiting" class="waiting"></span><span id="livebadge"></span>
      <div class="turnbig" id="turnbig"></div></div>
  </header>
  <main class="view" id="views">
    <div class="mapview" id="v-map">
      <div class="stage" id="stage">
        <canvas class="map" id="map" aria-label="The world map"></canvas>
        <div class="overlay chips" id="layers"></div>
        <div class="overlay zoom"><button id="zin" title="Zoom in (+)">+</button><button id="zout" title="Zoom out (−)">−</button>
          <button id="zfit" title="Fit (0)" style="font-size:12px">⤢</button></div>
        <div class="overlay povnote" id="povnote" hidden></div>
        <div class="overlay legendbox" aria-hidden="true"><span>★ capital</span><span>● army</span><span>■ worker, settler (zoom in)</span>
          <span>numbers: city size</span></div>
        <div class="clientpane" id="clientpane" hidden></div>
      </div>
      <aside class="side" id="side">
        <section><h2>Standings <span class="grow"></span><span class="hint">click to follow an agent</span></h2>
          <table class="standings" id="standings"></table></section>
        <section id="agentcard" class="agentcard" hidden></section>
        <section id="diplo" class="diplo" hidden><h2>Diplomacy<span class="grow"></span><span class="hint" id="diploscope"></span></h2>
          <ul class="msgs" id="msgs"></ul></section>
        <section id="chartsec"><h2><span id="charttitle">Score</span><span class="grow"></span><span class="metrics" id="metrics"></span></h2>
          <svg class="chart" id="chart"></svg></section>
        <div class="feedwrap"><section style="border:0;padding-bottom:4px"><h2>Events<span class="grow"></span>
          <span class="hint" id="feedscope"></span></h2></section>
          <div class="kfilter" id="kfilter"></div><ul class="feed" id="feed"></ul></div>
      </aside>
    </div>
    <div class="agentsview" id="v-agents" hidden></div>
    <div class="summary" id="v-summary" hidden></div>
  </main>
  <footer class="timeline">
    <div class="trow">
      <button class="play" id="play">Play</button>
      <select id="speed" aria-label="Playback speed"><option value="2">2 t/s</option><option value="5" selected>5 t/s</option>
        <option value="10">10 t/s</option><option value="20">20 t/s</option></select>
      <div class="scrub" id="scrub" role="slider" tabindex="0" aria-label="Turn"><svg id="scrubsvg"></svg></div>
      <button class="golive" id="golive" hidden>● Go live</button>
    </div>
    <div class="hints"><kbd>Space</kbd> play · <kbd>←</kbd><kbd>→</kbd> turn (<kbd>Shift</kbd> ×10) · <kbd>1</kbd>–<kbd>9</kbd> follow
      an agent · <kbd>V</kbd> see as it · <kbd>C</kbd> its client view · <kbd>M</kbd><kbd>A</kbd><kbd>S</kbd> views ·
      <kbd>Esc</kbd> clear · drag to pan, wheel to zoom</div>
  </footer>`;
  document.body.appendChild(tip);
  document.body.classList.toggle("stream", STREAM);
  if (STREAM) {
    $("#stage").insertAdjacentHTML("beforeend", `<div class="bubble" id="bubble" hidden></div><div class="chyron" id="chyron" hidden></div>`);
    $("#views").insertAdjacentHTML("beforeend", `<div class="bcl"><div class="caption" id="caption" hidden></div></div>`);
    $(".timeline").insertAdjacentHTML("beforebegin", `<div class="ticker" id="ticker" hidden><div class="tag">Agent notes</div><div class="tk" id="tk"></div></div>`);
    document.body.insertAdjacentHTML("beforeend", `<div class="moment" id="moment" hidden></div>`);
  }
  for (const b of $$(".tabs button")) b.onclick = () => setView(b.dataset.view);
  $("#pov").onchange = () => setPov($("#pov").value === "" ? null : +$("#pov").value);
  $("#play").onclick = togglePlay;
  $("#speed").onchange = () => { S.speed = +$("#speed").value; if (S.playing) { stop(); togglePlay(); } };
  $("#golive").onclick = goLive;
  mapEvents(); scrubEvents(); keys();
  new ResizeObserver(() => { if (!M.ready) return; if (!S.userMoved) fitMap(); draw(); renderChart(); renderScrub();
    if (S.view === "agents") renderAgents(); if (S.view === "summary") renderSummary(); }).observe($("#views"));
}

function empty(msg, sub = "") {
  $("#views").innerHTML = `<div class="empty"><div>${esc(msg)}</div><div class="muted">${esc(sub)}</div></div>`;
}

// ======================================================================== state changes

function setTurn(i, {animate = false, user = false} = {}) {
  if (!M.ready) return;
  i = clamp(i, 0, M.last);
  if (user && LIVE) S.follow = i === M.last;
  const from = S.ti;
  S.ti = i;
  S.anim = animate && i === from + 1 ? {from, start: performance.now()} : null;
  if (S.anim) {
    const now = performance.now();
    for (const e of M.turns[i].events)
      if (e.x != null && kindRank(e.kind) <= 4)
        S.pulses.push({x: M.sx(e.x), y: e.y, rgb: M.byIndex[e.owner]?.rgb || [255, 255, 255], start: now});
  }
  renderStatus(); draw(); renderScrub(); writeHash();
  if (S.view === "map") { renderStandings(); renderAgentCard(); renderChart(); renderFeed(); }
  if (S.view === "agents") renderAgents();
  if (S.view === "summary") renderSummary();
  syncClient();
}
function setView(v) {
  S.view = v;
  for (const b of $$(".tabs button")) b.classList.toggle("on", b.dataset.view === v);
  $("#v-map").hidden = v !== "map"; $("#v-agents").hidden = v !== "agents"; $("#v-summary").hidden = v !== "summary";
  writeHash();
  if (v === "map") { draw(); renderStandings(); renderAgentCard(); renderChart(); renderFeed(); }
  if (v === "agents") renderAgents();
  if (v === "summary") renderSummary();
}
function setFocus(i, {fly = false} = {}) {
  S.focus = i;
  if (fly && i != null) flyTo(M.civBox(i, S.ti, 4));
  draw(); renderStandings(); renderAgentCard(); renderChart(); renderFeed(); writeHash();
  if (S.view === "agents") renderAgents();
}
function setPov(seat) {
  S.pov = seat;
  $("#pov").value = seat == null ? "" : String(seat);
  renderPovNote(); draw(); writeHash(); renderAgentCard();
}
function goLive() { S.follow = true; stop(); setTurn(M.last, {}); }

// ======================================================================== top bar

function renderTop() {
  const seats = M.seats.length;
  $("#sub").textContent = `${seats ? cast() + " · " : ""}seed ${M.meta.seed ?? "?"} · ` +
    `${M.W}×${M.H}` + (LIVE ? "" : " · recording");
  $("#pov").innerHTML = `<option value="">Spectator (everything)</option>` +
    M.seats.map(p => `<option value="${p.seat}">${lab(p)} · ${esc(p.civ)}</option>`).join("");
  $("#pov").value = S.pov == null ? "" : String(S.pov);
  for (const b of $$(".tabs button")) b.classList.toggle("on", b.dataset.view === S.view);
}
function renderStatus() {
  const t = M.turns[S.ti];
  $("#turnbig").innerHTML = `T${t.turn} <small>/ ${M.limit}</small>`;
  document.title = `T${t.turn} · OpenCiv3${LIVE ? " live" : ""}`;
  const live = M.live;
  if (LIVE) {
    const over = live?.game_over;
    $("#livebadge").innerHTML = over ? `<span class="livebadge off"><i></i>GAME OVER</span>`
      : S.follow ? `<span class="livebadge"><i></i>LIVE</span>` : `<span class="livebadge off"><i></i>T${M.turns[M.last].turn} live</span>`;
    $("#golive").hidden = S.follow || over;
    const waiting = (live?.seats || []).filter(s => !s.ended);
    $("#waiting").innerHTML = over || !live?.seats?.length ? "" : waiting.length
      ? `T${M.turns[M.last].turn}: waiting for ${waiting.slice(0, 4).map(s => {
          const p = M.players.find(q => q.civ === s.civ);
          return `${sw(p)}${esc(s.label || s.civ)}`;
        }).join(", ")}${waiting.length > 4 ? ` +${waiting.length - 4}` : ""}`
      : `T${M.turns[M.last].turn}: every player has ended the turn`;
  } else {
    $("#livebadge").innerHTML = ""; $("#waiting").innerHTML = ""; $("#golive").hidden = true;
  }
}

// ======================================================================== map view

const canvas = () => $("#map");
function fitMap() {
  const r = canvas().getBoundingClientRect();
  if (!r.width) return;
  const box = S.pov != null ? (M.seatBox(S.pov, S.ti, 3) || M.landBox) : M.landBox;
  S.v = fitView(box, r.width, r.height);
}
// A new flight replaces the one under way. On the stream a flight is slower, and a far one rises on the way (it zooms
// out and back in), so the viewer keeps their bearings.
let flight = 0;
// A view no wider (taller) than the map stays on it: no void past its edges.
function inMap(v, w, h) {
  const keep = (x0, span, size) => span >= size + 2 ? x0 : clamp(x0, -1, size + 1 - span);
  return {hw: v.hw, x0: keep(v.x0, w / v.hw, M.W), y0: keep(v.y0, 2 * h / v.hw, M.H)};
}
const inOut = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(2 - 2 * t, 3) / 2;
function flyTo(box, {maxHw = 26} = {}) {
  const r = canvas().getBoundingClientRect();
  if (!box || !r.width) return;
  if (!S.v) fitMap();
  const w = r.width, h = r.height, from = {...S.v}, to = inMap(fitView(box, w, h, maxHw), w, h);
  const mid = v => [v.x0 + w / v.hw / 2, v.y0 + h / v.hw], [ax, ay] = mid(from), [bx, by] = mid(to);
  const span = Math.max(w / from.hw, w / to.hw), rise = STREAM ? clamp(Math.hypot(bx - ax, (by - ay) / 2) / span - 0.35, 0, 1.2) : 0;
  const ms = STREAM ? 1700 : 450, start = performance.now(), id = ++flight;
  S.userMoved = true;
  const step = now => {
    if (id !== flight) return;
    const t = clamp((now - start) / ms, 0, 1), e = STREAM ? inOut(t) : ease(t);
    const hw = Math.exp(Math.log(from.hw) + (Math.log(to.hw) - Math.log(from.hw)) * e) / (1 + rise * Math.sin(Math.PI * e));
    const cx = ax + (bx - ax) * e, cy = ay + (by - ay) * e;
    S.v = {hw, x0: cx - w / hw / 2, y0: cy - h / hw};
    S.flying = t < 1; paint();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function zoomAt(f, px, py) {
  const v = S.v, gx = px / v.hw + v.x0, gy = py / (v.hw / 2) + v.y0, hw = clamp(v.hw * f, 2.5, 70);
  S.v = {hw, x0: gx - px / hw, y0: gy - py / (hw / 2)}; S.userMoved = true; draw();
}
let raf = 0;
function draw() { if (S.view !== "map" || !M.ready) return; cancelAnimationFrame(raf); raf = requestAnimationFrame(paint); }
function paint() {
  if (S.view !== "map" || !M.ready) return;
  const {ctx, w, h, dpr} = sizeCanvas(canvas());
  if (!S.v) fitMap();
  const now = performance.now();
  S.pulses = S.pulses.filter(p => now - p.start < 1500);
  let anim = null;
  if (S.anim) {
    const t = (now - S.anim.start) / animMs();
    if (t >= 1) S.anim = null; else anim = {from: S.anim.from, t};
  }
  P.draw(ctx, S.ti, S.v, w, h, {...S.layers, focus: S.focus, pov: S.pov, dpr, anim, marks: battleMarks(S.ti), moving: S.flying,
    pulses: S.pulses.map(p => ({...p, t: (now - p.start) / 1500}))});
  if (S.anim || S.pulses.length) raf = requestAnimationFrame(paint);
}
// Where the fighting is: cities razed or taken in the last 4 turns, units lost in the last 2, fading with age.
const MARK_SPAN = {city_destroyed: 4, city_captured: 4, unit_lost: 2, attacked: 2, bombarded: 2};
function battleMarks(ti) {
  const out = [], turn = M.turns[ti].turn;
  for (let k = M.events.length - 1; k >= 0; k--) {
    const e = M.events[k];
    if (e.ti > ti) continue;
    if (turn - e.turn > 4) break;
    const span = MARK_SPAN[e.kind];
    if (span == null || e.x == null || turn - e.turn > span) continue;
    out.push({x: e.x, y: e.y, city: e.kind.startsWith("city_"), age: turn - e.turn, span});
  }
  return out;
}
const animMs = () => clamp(1000 / (S.playing ? S.speed : 2) * 0.85, 120, 450);

function mapEvents() {
  const c = canvas();
  c.addEventListener("wheel", e => {
    e.preventDefault(); const r = c.getBoundingClientRect();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  }, {passive: false});
  let drag = null;
  c.addEventListener("pointerdown", e => {
    drag = {x: e.clientX, y: e.clientY, v: {...S.v}, moved: 0}; c.setPointerCapture(e.pointerId); c.classList.add("drag");
  });
  c.addEventListener("pointermove", e => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
      if (drag.moved > 3) { S.v = {...drag.v, x0: drag.v.x0 - dx / S.v.hw, y0: drag.v.y0 - dy / (S.v.hw / 2)}; S.userMoved = true; hideTip(); draw(); }
      return;
    }
    mapTip(e);
  });
  c.addEventListener("pointerup", e => { c.classList.remove("drag"); if (drag && drag.moved <= 3) mapClick(e); drag = null; });
  c.addEventListener("pointerleave", hideTip);
  c.addEventListener("dblclick", e => { const r = c.getBoundingClientRect(); zoomAt(1.8, e.clientX - r.left, e.clientY - r.top); });
  $("#zin").onclick = () => { const r = c.getBoundingClientRect(); zoomAt(1.4, r.width / 2, r.height / 2); };
  $("#zout").onclick = () => { const r = c.getBoundingClientRect(); zoomAt(1 / 1.4, r.width / 2, r.height / 2); };
  $("#zfit").onclick = () => { S.userMoved = false; fitMap(); draw(); };
}
function tileUnder(e) { const r = canvas().getBoundingClientRect(); return P.pick(e.clientX - r.left, e.clientY - r.top, S.v); }
function mapTip(e) {
  const i = tileUnder(e);
  if (i == null) return hideTip();
  if (S.pov != null && !(M.known(S.ti)[i] & (1 << S.pov))) return showTip(e, `<span class="k">Unexplored by ${lab(M.seats.find(p => p.seat === S.pov))}</span>`);
  const t = M.tiles[i], own = M.owners(S.ti)[i], turn = M.turns[S.ti];
  const city = turn.cities.find(c => M.sx(c[0]) === t.x && c[1] === t.y);
  const types = M.meta.unit_types || [];
  const units = turn.units.filter(u => M.sx(u[1]) === t.x && u[2] === t.y);
  let h = "";
  if (city) h += `<div class="row"><b>${esc(city[2])}</b><span class="k">size ${city[4]}${city[5] ? " · capital" : ""}</span></div>` +
    (city[6] ? `<div class="k">building ${esc(city[6])}</div>` : "");
  h += `<div class="k">${esc(t.over ? `${t.over} on ${t.base}` : t.base)}${t.river ? " · river" : ""} · (${(t.x + M.seam) % M.W},${t.y})</div>`;
  h += own >= 0 ? `<div class="row">${sw(M.byIndex[own])}${esc(M.full(own))}</div>` : `<div class="k">unclaimed</div>`;
  const byOwner = new Map();
  for (const u of units) { const l = byOwner.get(u[3]) || []; l.push(types[u[4]] || "?"); byOwner.set(u[3], l); }
  for (const [o, l] of byOwner) {
    const counts = {}; for (const n of l) counts[n] = (counts[n] || 0) + 1;
    h += `<div class="row">${sw(M.byIndex[o])}<span>${Object.entries(counts).map(([n, k]) => k > 1 ? `${k} ${esc(n)}` : esc(n)).join(", ")}</span></div>`;
  }
  const now = M.turns[S.ti].turn;
  for (const ev of M.events.filter(ev => ev.x === t.x && ev.y === t.y && ev.ti <= S.ti && now - ev.turn <= 4).slice(-4))
    h += `<div class="row" style="color:#f6b3a4">${sw(M.byIndex[ev.owner])}<span>T${ev.turn} · ${esc(ev.text)}</span></div>`;
  const known = M.known(S.ti)[i];
  if (M.seats.length > 1 && S.pov == null) {
    const who = M.seats.filter(p => known & (1 << p.seat));
    h += `<div class="k" style="margin-top:3px">${who.length === M.seats.length ? "known to every player"
      : who.length ? "known to " + who.map(p => esc(p.label || p.civ)).join(", ") : "no player has seen it"}</div>`;
  }
  showTip(e, h);
}
function mapClick(e) {
  const i = tileUnder(e); if (i == null) return;
  const own = M.owners(S.ti)[i];
  setFocus(own >= 0 && !M.byIndex[own]?.barbarian && S.focus !== own ? own : null);
}
const LAYERS = [["territory", "Territory"], ["borders", "Borders"], ["units", "Units"], ["labels", "Labels"]];
function renderLayers() {
  $("#layers").innerHTML = LAYERS.map(([k, l]) => {
    const on = k === "labels" ? S.layers.labels !== "none" : S.layers[k];
    return `<button class="chip ${on ? "on" : ""}" data-k="${k}">${k === "labels" ? `Labels: ${S.layers.labels}` : l}</button>`;
  }).join("");
  for (const b of $$("#layers .chip")) b.onclick = () => {
    const k = b.dataset.k;
    if (k === "labels") S.layers.labels = {auto: "all", all: "none", none: "auto"}[S.layers.labels]; else S.layers[k] = !S.layers[k];
    renderLayers(); draw();
  };
}
function renderPovNote() {
  const p = M.seats.find(q => q.seat === S.pov), el = $("#povnote");
  el.hidden = !p;
  if (p) {
    el.innerHTML = `${sw(p)}<span>Seeing what <b>${lab(p)}</b> has explored</span><button id="povclear">Spectator</button>`;
    $("#povclear").onclick = () => setPov(null);
  }
}

// ---- side panel ----

function liveMark(p) {
  if (!LIVE || S.ti !== M.last || M.live?.game_over) return "";
  const s = M.liveSeat(p.index); if (!s) return "";
  return s.ended ? `<span class="tick" title="ended the turn">✓</span>` : `<span class="dot-live" title="playing the turn"></span>`;
}
const SWORDS = `<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8M1 8l3 3M8 11l3-3"
  stroke="#f0745a" stroke-width="1.6" stroke-linecap="round" fill="none"/></svg>`;
function warMark(ti, p) {
  const foes = (M.stats(ti, p.index).at_war || []).map(j => M.byIndex[j]).filter(Boolean);
  return foes.length ? `<span class="warmark" title="at war with ${esc(foes.map(q => q.label || q.civ).join(", "))}">${SWORDS}</span>` : "";
}
function renderStandings() {
  const ti = S.ti, ranks = M.ranks(ti), before = M.ranks(Math.max(0, ti - 10));
  const order = M.civs.slice().sort((a, b) => ranks[a.index] - ranks[b.index]);
  const top = Math.max(1, ...order.map(p => M.series[p.index][ti][0]));
  const live = LIVE && ti === M.last && M.live?.seats?.length;
  $("#standings").innerHTML = `<tr><th></th><th></th><th style="text-align:left">agent</th><th>score</th><th>cities</th>
    <th>pop</th><th>techs</th>${live ? "<th></th>" : ""}</tr>` + order.map(p => {
    const s = M.series[p.index][ti], d = before[p.index] - ranks[p.index];
    const delta = d > 0 ? `<span class="up">▲${d}</span>` : d < 0 ? `<span class="down">▼${-d}</span>` : `<span class="muted">–</span>`;
    return `<tr class="row ${S.focus === p.index ? "focus" : ""} ${s[5] ? "out" : ""}" data-i="${p.index}" tabindex="0">
      <td class="rk">${ranks[p.index]}</td><td class="d">${delta}</td>
      <td><div class="who">${sw(p)}<b>${lab(p)}</b>${person(p)}${p.label ? `<span class="civ">${esc(p.civ)}</span>` : ""}${warMark(ti, p)}</div>
        <div class="bar" style="width:${(s[0] / top * 100).toFixed(1)}%;background:${p.color}"></div></td>
      <td class="s">${s[0]}</td><td class="n">${s[1]}</td><td class="n">${s[2]}</td><td class="n">${s[4]}</td>
      ${live ? `<td class="state">${liveMark(p)}</td>` : ""}</tr>`;
  }).join("");
  for (const tr of $$("#standings tr.row")) {
    tr.onclick = () => { const i = +tr.dataset.i; setFocus(S.focus === i ? null : i, {fly: S.focus !== i}); };
    tr.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); tr.onclick(); } };
  }
}
function renderAgentCard() {
  const el = $("#agentcard"), p = M.byIndex[S.focus];
  if (!p) { el.hidden = true; return; }
  el.hidden = false;
  const ti = S.ti, s = M.series[p.index][ti], st = M.stats(ti, p.index), ranks = M.ranks(ti);
  const during = M.actionsDuring(ti, p.index);
  const wars = (st.at_war || []).map(j => M.byIndex[j]).filter(Boolean);
  const calls = during.calls;
  const live = during.live;
  const state = live ? (live.ended ? `ended the turn after ${fmtSecs(live.seconds)}` : `${p.human ? "playing" : "thinking"} · ${fmtSecs(live.seconds)}`)
    : during.done ? `turn T${M.turns[ti].turn}` : "";
  el.innerHTML = `<div class="head">${sw(p)}<b>${lab(p)}</b>${person(p)}<span class="muted">${esc(p.label ? p.civ : "")}</span>
      <span class="grow" style="flex:1"></span><span class="muted">#${ranks[p.index]}</span><b class="num">${s[0]}</b></div>
    <div class="facts">
      <div class="fact"><div class="l">Gold</div><div class="v num">${st.gold ?? "–"}</div></div>
      <div class="fact"><div class="l">Government</div><div class="v" title="${esc(st.government || "")}">${esc(st.government || "–")}</div></div>
      <div class="fact"><div class="l">Researching</div><div class="v" title="${esc(st.research || "")}">${esc(st.research || "–")}</div></div>
    </div>
    ${wars.length ? `<div class="wars">${wars.map(q => `<span class="war">at war · ${sw(q)}${lab(q)}</span>`).join("")}</div>` : ""}
    ${p.seat != null ? mind(p, ti) : ""}
    ${p.seat != null ? `<h2 style="margin-top:6px">Its turn <span class="grow"></span><span class="hint turnstate">${esc(state)}</span></h2>
      <div class="muted" style="margin-bottom:4px">${calls ? `${calls.ok + calls.failed} tool calls${calls.failed ? `, <span class="down">${calls.failed} failed</span>` : ""}` : "no tool calls recorded"}</div>
      <ul class="lines">${during.actions.length ? during.actions.map(a => `<li class="${a.ok === false ? "bad" : ""}">${esc(a.text)}</li>`).join("")
        : `<li class="muted">No game actions${during.done || live ? "" : " yet"}</li>`}</ul>` : `<div class="muted">The game's own AI plays this civ.</div>`}
    <div class="btnrow">
      ${p.seat != null ? `<button id="ac-pov">${S.pov === p.seat ? "Back to spectator" : "See what it sees"}</button>` : ""}
      ${p.seat != null ? `<button id="ac-client">Client view</button>` : ""}
      <button id="ac-fly">Fly to</button><button id="ac-clear">Unfollow</button></div>`;
  $("#ac-pov") && ($("#ac-pov").onclick = () => { setPov(S.pov === p.seat ? null : p.seat); });
  $("#ac-client") && ($("#ac-client").onclick = () => openClient(p.index));
  $("#ac-fly").onclick = () => flyTo(M.civBox(p.index, S.ti, 4));
  $("#ac-clear").onclick = () => setFocus(null);
}
// A seat's newest note and its plan (docs/viewer.md, sections 2-3): agent-written, so always escaped.
const quote = s => `“${esc(s)}”`;
function mind(p, ti) {
  const note = M.lastNote(ti, p.index), plan = M.planAt(ti, p.index);
  if (!note && !plan) return "";
  return `<div class="mind" style="--a:${p.color}">${note ? `<div class="note">${quote(note.text)}<span class="t">T${note.turn}</span></div>` : ""}
    ${plan ? `<div class="plan"><span class="l">Plan${plan.turn != null ? ` · T${plan.turn}` : ""}</span><span class="tx">${esc(plan.text)}</span></div>` : ""}</div>`;
}
const toWhom = m => m.to === "all" ? `<span class="all">all</span>`
  : m.to.map(j => `<span class="who">${sw(M.byIndex[j])}${lab(M.byIndex[j])}</span>`).join(" ");
function renderMessages() {
  if (S.view !== "map") return;
  const all = M.messagesUpTo(S.ti), f = S.focus, el = $("#diplo");
  el.hidden = !M.messages.length && !all.length;
  if (el.hidden) return;
  const shown = f == null ? all : all.filter(m => m.from === f || m.to === "all" || m.to.includes(f)), turn = M.turns[S.ti].turn;
  $("#diploscope").textContent = (f == null ? "everyone" : M.name(f)) + " · up to T" + turn;
  $("#msgs").innerHTML = shown.slice(-80).reverse().map(m => `<li class="${m.turn === turn ? "now" : ""}">
    <div class="mh"><span class="t">T${m.turn}</span><span class="who">${sw(M.byIndex[m.from])}<b>${lab(M.byIndex[m.from])}</b></span>
      <span class="arrow">→</span>${toWhom(m)}</div><div class="mt">${quote(m.text)}</div></li>`).join("")
    || `<li class="muted">No messages yet.</li>`;
}
function renderMetrics() {
  $("#metrics").innerHTML = METRICS.map(([l, k]) => `<button class="${k === S.metric ? "on" : ""}" data-k="${k}">${l}</button>`).join("");
  for (const b of $$("#metrics button")) b.onclick = () => { S.metric = +b.dataset.k; renderMetrics(); renderChart(); };
  $("#charttitle").textContent = METRICS.find(m => m[1] === S.metric)[0];
}
function renderChart() {
  if (S.view !== "map") return;
  const svg = $("#chart"), W = svg.clientWidth || 350, H = svg.clientHeight || 170, L = 34, R = 58, T = 8, B = 18, k = S.metric;
  const seen = Math.max(1, ...M.civs.flatMap(p => M.series[p.index].slice(0, S.ti + 1).map(s => s[k])));
  const max = niceMax(seen * 1.05);
  // Both scales follow the turns played so far: early on, the game isn't a sliver at the left edge.
  const xMax = Math.max(M.turns[S.ti].turn, Math.min(M.limit, 10));
  const X = i => L + (W - L - R) * M.turns[i].turn / xMax, Y = v => T + (H - T - B) * (1 - v / max);
  let h = ticks(max).map(v => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="#242930"/>` +
    `<text x="${L - 6}" y="${Y(v) + 3.5}" fill="#80868f" font-size="10" text-anchor="end">${v}</text>`).join("");
  for (const t of [0, Math.round(xMax / 2), xMax])
    h += `<text x="${L + (W - L - R) * t / xMax}" y="${H - 4}" fill="#80868f" font-size="10" text-anchor="middle">T${t}</text>`;
  const order = M.civs.slice().sort((a, b) => (a.index === S.focus) - (b.index === S.focus));
  const leader = M.leaders[S.ti], ends = [];
  for (const p of order) {
    const pts = M.series[p.index].slice(0, S.ti + 1).map((s, i) => `${X(i).toFixed(1)},${Y(s[k]).toFixed(1)}`).join(" ");
    const dim = S.focus != null && S.focus !== p.index;
    h += `<polyline points="${pts}" fill="none" stroke="${dim ? "#353a43" : p.color}" stroke-linejoin="round"
      stroke-width="${S.focus === p.index ? 2.6 : p.index === leader && S.focus == null ? 2 : 1.4}"/>`;
    if (!dim) ends.push({y: Y(M.series[p.index][S.ti][k]), x: X(S.ti), p});
  }
  const ys = spread(ends.map(e => e.y), 11, T, H - B);
  ends.forEach((e, i) => { h += `<text x="${e.x + 5}" y="${ys[i] + 3.5}" font-size="10" fill="#b6bac3">${lab(e.p)}</text>`; });
  h += `<line id="xh" y1="${T}" y2="${H - B}" stroke="#80868f" visibility="hidden"/><rect id="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.innerHTML = h;
  const hit = $("#hit", svg), xh = $("#xh", svg);
  const at = e => { const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * W / r.width;
    return Math.min(S.ti, M.turnIndex(Math.round(clamp((x - L) / (W - L - R), 0, 1) * xMax))); };
  hit.onmousemove = e => {
    const i = at(e); xh.setAttribute("x1", X(i)); xh.setAttribute("x2", X(i)); xh.setAttribute("visibility", "visible");
    const rows = M.civs.map(p => [p, M.series[p.index][i][k]]).sort((a, b) => b[1] - a[1]);
    showTip(e, `<div style="margin-bottom:3px"><b>T${M.turns[i].turn}</b> <span class="k">${METRICS[k][0].toLowerCase()}</span></div>` +
      rows.map(([p, v]) => `<div class="row" style="${S.focus != null && S.focus !== p.index ? "opacity:.5" : ""}">${sw(p)}${lab(p)}<span class="v">${v}</span></div>`).join(""));
  };
  hit.onmouseleave = () => { xh.setAttribute("visibility", "hidden"); hideTip(); };
  hit.onclick = e => setTurn(at(e), {user: true});
}
const FEED_KINDS = [["lead_change", "lead"], ["war_declared", "war"], ["peace_signed", "peace"], ["city_captured", "captured"],
  ["city_destroyed", "razed"], ["civ_destroyed", "eliminated"], ["city_founded", "founded"], ["government_changed", "government"],
  ["tech_learned", "techs"], ["unit_lost", "units lost"]];
function renderKinds() {
  const present = new Set(M.events.map(e => e.kind));
  $("#kfilter").innerHTML = FEED_KINDS.filter(([k]) => present.has(k)).map(([k, l]) =>
    `<button class="chip ${S.kinds.has(k) ? "on" : ""}" data-k="${k}">${l}</button>`).join("");
  for (const b of $$("#kfilter .chip")) b.onclick = () => {
    S.kinds.has(b.dataset.k) ? S.kinds.delete(b.dataset.k) : S.kinds.add(b.dataset.k); renderKinds(); renderFeed();
  };
}
function renderFeed() {
  if (S.view !== "map") return;
  const turn = M.turns[S.ti].turn;
  const items = [], groups = new Map();
  for (let k = M.events.length - 1; k >= 0 && items.length < 200; k--) {
    const e = M.events[k];
    if (e.turn > turn || !S.kinds.has(e.kind)) continue;
    if (S.focus != null && e.owner !== S.focus && e.from !== S.focus) continue;
    if (!M.durable(e, S.ti)) continue;
    if (e.kind === "unit_lost") {   // a lost battle is one line: "grok lost 10 units (8 Archer, 2 Worker)"
      const key = `${e.ti}:${e.owner}`, type = (/u\d+\s+(.+?)\s+was lost/.exec(e.text) || [])[1] || "unit";
      const g = groups.get(key);
      if (g) { g.types.push(type); continue; }
      const row = {...e, types: [type]};
      groups.set(key, row); items.push(row); continue;
    }
    items.push(e);
  }
  for (const g of groups.values()) {
    if (g.types.length < 2) continue;
    const counts = {}; for (const t of g.types) counts[t] = (counts[t] || 0) + 1;
    g.text = `${M.name(g.owner)} lost ${g.types.length} units (${Object.entries(counts).sort((a, b) => b[1] - a[1])
      .map(([t, n]) => n > 1 ? `${n} ${t}` : t).join(", ")})`;
  }
  $("#feedscope").textContent = (S.focus == null ? "everyone" : M.name(S.focus)) + " · up to T" + turn;
  $("#feed").innerHTML = items.map((e, k) => `<li class="${e.turn === turn ? "now" : ""} ${kindHot(e.kind) ? "hot" : ""}" data-k="${k}" tabindex="0">
    <span class="t">T${e.turn}</span><span>${sw(M.byIndex[e.owner])}<span class="txt">${esc(e.text)}</span><span class="tag">${kindLabel(e.kind)}</span></span></li>`).join("")
    || `<li><span></span><span class="muted">Nothing of these kinds yet.</span></li>`;
  for (const li of $$("#feed li[data-k]")) li.onkeydown = ev => { if (ev.key === "Enter") li.onclick(); };
  for (const li of $$("#feed li[data-k]")) li.onclick = () => {
    const e = items[+li.dataset.k];
    stop(); setTurn(e.ti, {user: true});
    if (e.x != null) { const r = canvas().getBoundingClientRect(), hw = Math.max(S.v.hw, 16);
      flyTo({x0: e.x - r.width / hw / 2 + 1, x1: e.x + r.width / hw / 2 - 1, y0: e.y - r.height / hw + 2, y1: e.y + r.height / hw - 2}); }
  };
  renderMessages();
}

// ---- the real client's view of a seat ----

let clientTimer = 0, clientUrl = null;
function openClient(i) {
  S.client = {open: true, seat: i ?? S.client.seat ?? S.focus ?? M.seats[0]?.index, big: S.client.big};
  if (S.view !== "map") setView("map");
  renderClient(); writeHash();
}
function closeClient() { S.client.open = false; clearTimeout(clientTimer); renderClient(); writeHash(); }
function renderClient() {
  const el = $("#clientpane");
  el.hidden = !S.client.open;
  el.classList.toggle("big", S.client.big);
  if (!S.client.open) return;
  const p = M.byIndex[S.client.seat];
  el.innerHTML = `<header>${sw(p)}<b>${lab(p)}</b><span class="muted">the OpenCiv3 client, as this agent sees it</span>
    <span class="grow"></span><select id="cl-seat" aria-label="Whose client view">${M.seats.map(q =>
      `<option value="${q.index}" ${q.index === S.client.seat ? "selected" : ""}>${lab(q)}</option>`).join("")}</select>
    <span class="muted" id="cl-turn"></span><button id="cl-big">${S.client.big ? "Smaller" : "Bigger"}</button><button id="cl-x">✕</button></header>
    <div class="body" id="cl-body"></div>`;
  $("#cl-seat").onchange = () => { S.client.seat = +$("#cl-seat").value; renderClient(); writeHash(); };
  $("#cl-big").onclick = () => { S.client.big = !S.client.big; renderClient(); writeHash(); };
  $("#cl-x").onclick = closeClient;
  const body = $("#cl-body"), video = VIDEOS[p?.civ];
  if (!LIVE && video) {
    body.innerHTML = `<video muted playsinline preload="auto"></video><div class="msg" id="cl-msg" hidden></div>`;
    const v = $("video", body);
    v.src = typeof video === "string" ? video : video.file;
    v.onerror = () => { $("#cl-msg").hidden = false; $("#cl-msg").innerHTML = `<span>Couldn't play <b>${esc(decodeURIComponent(v.src.split("/").pop()))}</b>.
      Keep it in the same folder as this page (<b>agent-env openciv3 recordings --out</b> does), and open the page in a browser that plays MP4 video.</span>`; };
    v.onloadedmetadata = syncClient;
  } else if (LIVE && M.live?.client) {
    body.innerHTML = `<img alt="the client's view" hidden><div class="msg" id="cl-msg">Asking the client for ${lab(p)}'s view…</div>`;
  } else {
    body.innerHTML = `<div class="msg"><span>${LIVE ? "This env has no OpenCiv3 client. Set it up with <b>agent-env openciv3 setup --client</b>."
      : "This recording has no client view. Record in an image with the client (<b>agent-env openciv3 setup --client</b>)."}</span></div>`;
  }
  syncClient();
}
function syncClient() {
  if (!S.client.open || !M.ready) return;
  const p = M.byIndex[S.client.seat], body = $("#cl-body"); if (!p || !body) return;
  const video = VIDEOS[p.civ], turn = M.turns[S.ti].turn;
  if (!LIVE && video) {
    const v = $("video", body); if (!v || !v.duration) return;
    const fps = video.fps || 4, turns = video.turns || M.turns.map(t => t.turn);
    let k = 0; for (let j = 0; j < turns.length; j++) if (turns[j] <= turn) k = j;
    v.currentTime = Math.min(v.duration - 0.01, (k + 0.5) / fps);
    $("#cl-turn").textContent = `T${turns[k]}`;
    return;
  }
  if (LIVE && M.live?.client) {
    clearTimeout(clientTimer);
    const want = `live/client.png?seat=${encodeURIComponent(p.civ)}&turn=${turn}`;
    (async () => {
      try {
        const r = await fetch(want, {cache: "no-store"});
        const msg = $("#cl-msg"), img = $("img", body); if (!img) return;
        if (!r.ok) {
          msg.hidden = false; msg.textContent = r.status === 503 ? `The client is drawing ${p.label || p.civ}'s view…` : await r.text();
          clientTimer = setTimeout(syncClient, 2000); return;
        }
        const shown = r.headers.get("X-OpenCiv3-Turn"), blob = await r.blob(), old = clientUrl;
        clientUrl = URL.createObjectURL(blob); img.src = clientUrl; img.hidden = false; msg.hidden = true;
        if (old) URL.revokeObjectURL(old);
        $("#cl-turn").textContent = shown ? `T${shown}${+shown < turn ? " · drawing T" + turn : ""}` : "";
        if (shown && +shown < turn) clientTimer = setTimeout(syncClient, 2000);
      } catch (e) { clientTimer = setTimeout(syncClient, 3000); }
    })();
  }
}

// ======================================================================== agents view

let gridGame = null;
function renderAgents() {
  const el = $("#v-agents");
  const seats = M.seats.length ? M.seats : M.civs;
  const n = seats.length, cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4, rows = Math.ceil(n / cols);
  el.style.gridTemplateColumns = `repeat(${cols}, 1fr)`; el.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  el.classList.toggle("dense", rows > 2);
  if (gridGame !== M.game + ":" + n) {
    gridGame = M.game + ":" + n;
    el.innerHTML = seats.map(p => `<div class="acard" data-i="${p.index}" tabindex="0" title="Open ${lab(p)} on the map">
      <div class="hd">${sw(p)}<span class="nm"><b>${lab(p)}</b>${person(p)}<span class="civ">${esc(p.label ? p.civ : "")}</span></span>
        <span class="rank"><span class="lead-tag"></span><span class="dd"></span><span class="r"></span><span class="s num"></span></span></div>
      <div class="mm"><canvas></canvas><span class="badge">${p.seat != null ? "its view" : "territory"}</span><span class="livestate" hidden></span>
        <span class="warchip" hidden></span></div>
      <div class="ft"><div class="stats"></div><svg class="spark" width="110" height="20"></svg><div class="think"></div><div class="act"></div></div></div>`).join("");
    for (const c of $$(".acard", el)) {
      const open = () => { const i = +c.dataset.i, p = M.byIndex[i];
        S.focus = i; if (p.seat != null) S.pov = p.seat; $("#pov").value = S.pov == null ? "" : String(S.pov);
        renderPovNote(); setView("map"); flyTo(p.seat != null ? M.seatBox(p.seat, S.ti, 2) : M.civBox(i, S.ti, 4)); };
      c.onclick = open; c.onkeydown = e => { if (e.key === "Enter") open(); };
    }
  }
  const ti = S.ti, t = M.turns[ti], ranks = M.ranks(ti), before = M.ranks(Math.max(0, ti - 10)), back = Math.max(0, ti - 10);
  const leader = M.leaders[ti], peak = Math.max(1, ...M.civs.map(p => Math.max(...M.series[p.index].slice(0, ti + 1).map(s => s[0]))));
  for (const c of $$(".acard", el)) {
    const i = +c.dataset.i, p = M.byIndex[i], s = M.series[i][ti], b = M.series[i][back];
    c.classList.toggle("lead", i === leader); c.classList.toggle("out", !!s[5]);
    $(".lead-tag", c).textContent = i === leader ? "LEADS" : "";
    const d = before[i] - ranks[i];
    $(".dd", c).innerHTML = d > 0 ? `<span class="up">▲${d}</span>` : d < 0 ? `<span class="down">▼${-d}</span>` : "";
    $(".r", c).textContent = `#${ranks[i]}`; $(".s", c).textContent = s[0];
    const inc = (k, one, many) => `<span><b>${s[k]}</b> ${s[k] === 1 ? one : many}${s[k] > b[k] ? `<i>+${s[k] - b[k]}</i>` : ""}</span>`;
    const st = M.stats(ti, i);
    // most important first: when a card is narrow, gold is what gets cut, behind an ellipsis
    $(".stats", c).innerHTML = inc(1, "city", "cities") + inc(2, "pop", "pop") + inc(4, "tech", "techs") +
      (st.gold != null ? `<span><b>${st.gold}</b> gold</span>` : "");
    const foes = (st.at_war || []).map(j => M.byIndex[j]).filter(Boolean), wc = $(".warchip", c);
    wc.hidden = !foes.length;
    if (foes.length) wc.innerHTML = `${SWORDS}<span>at war · ${foes.map(q => `${sw(q)}${lab(q)}`).join(" ")}</span>`;
    // the sparkline spans the turns played so far, like the chart
    $(".spark", c).innerHTML = M.civs.filter(q => q !== p).map(q => `<path d="${sparkPath(M.series[q.index].slice(0, ti + 1).map(x => x[0]), 110, 18, peak)}" fill="none" stroke="#323740" stroke-width="1"/>`).join("") +
      `<path d="${sparkPath(M.series[i].slice(0, ti + 1).map(x => x[0]), 110, 18, peak)}" fill="none" stroke="${p.color}" stroke-width="1.8"/>`;
    const during = M.actionsDuring(ti, i), ls = $(".livestate", c);
    if (during.live) {
      ls.hidden = false;
      ls.innerHTML = during.live.ended ? `<span class="tick">✓</span> ended · ${fmtSecs(during.live.seconds)}`
        : `<span class="dot-live"></span> ${p.human ? "playing" : "thinking"} · ${fmtSecs(during.live.seconds)} · ${(during.live.calls?.ok || 0) + (during.live.calls?.failed || 0)} calls`;
    } else ls.hidden = true;
    $(".think", c).innerHTML = p.seat != null ? mind(p, ti) : "";
    const acts = during.actions.slice(-2).reverse(), evs = M.events.filter(e => e.ti <= ti && (e.owner === i || e.from === i) && kindRank(e.kind) <= 4).slice(-2).reverse();
    $(".act", c).innerHTML = acts.length ? acts.map(a => `<div class="${a.ok === false ? "down" : ""}"><span class="t">T${t.turn}</span>${esc(a.text)}</div>`).join("")
      : evs.length ? evs.map(e => `<div><span class="t">T${e.turn}</span>${esc(e.text)}</div>`).join("") : `<div class="muted">Nothing yet</div>`;
    const cv = $("canvas", c), {ctx, w, h, dpr} = sizeCanvas(cv);
    // the camera frames the civ as it is at this turn: early on its first city, not the empire it ends with
    const box = M.civBox(i, ti, 4) || (p.seat != null ? M.seatBox(p.seat, ti, 1) : null) || M.landBox;
    P.draw(ctx, ti, fitView(box, w, h, 30), w, h, {focus: i, pov: p.seat ?? null, labels: "auto", ownLabels: true, dpr});
  }
}

// ======================================================================== summary view

function renderSummary() {
  const el = $("#v-summary"), ti = S.ti, T = M.turns[ti].turn, civs = M.civs;
  const fin = i => M.series[i][ti], ranksAt = k => M.ranks(k);
  const rk = ranksAt(ti), order = civs.slice().sort((a, b) => rk[a.index] - rk[b.index]);
  if (!order.length) return;
  const win = order[0], second = order[1] || order[0];
  const leaders = M.leaders.slice(0, ti + 1);
  const led = Object.fromEntries(civs.map(p => [p.index, leaders.filter(l => l === p.index).length]));
  let since = ti; while (since > 0 && leaders[since - 1] === win.index) since--;
  const totalLand = Math.max(1, civs.reduce((s, p) => s + fin(p.index)[3], 0));
  const reach = (p, n) => { const k = M.series[p.index].slice(0, ti + 1).findIndex(s => s[1] >= n); return k < 0 ? null : M.turns[k].turn; };
  const over = ti === M.last && (M.meta.victory || T >= M.limit || M.live?.game_over);
  const v = M.meta.victory;
  const totals = {};
  for (const p of civs) totals[p.index] = {ok: 0, failed: 0, actions: 0};
  for (let k = 1; k <= ti; k++) for (const [i, c] of Object.entries(M.turns[k].calls || {})) if (totals[i]) {
    totals[i].ok += c.ok; totals[i].failed += c.failed; totals[i].actions += ((M.turns[k].actions || {})[i] || []).length; }
  const anyCalls = Object.values(totals).some(t => t.ok + t.failed);
  const lc = M.events.filter(e => e.ti <= ti && e.kind === "lead_change" && M.durable(e, ti)).length;
  const margin = fin(win.index)[0] - fin(second.index)[0];
  const tied = order.filter(p => fin(p.index)[0] === fin(win.index)[0]);
  const headline = v ? `${sw(M.players.find(p => p.civ === v.civ))}${esc(v.label || v.civ)} wins by ${esc(v.kind)} on turn ${v.turn}`
    : margin === 0 ? (tied.length === civs.length ? `Every player ${over ? "ties" : "is tied"}` : `${tied.map(p => sw(p) + lab(p)).join(", ")} ${over ? "tie" : "are tied"}`) +
      ` on ${fin(win.index)[0]} points${over ? "" : ` at turn ${T}`}`
    : over ? `${sw(win)}${lab(win)} wins on score, by ${margin} point${margin === 1 ? "" : "s"}`
    : `${sw(win)}${lab(win)} leads at turn ${T}, by ${margin} point${margin === 1 ? "" : "s"}`;
  const most = k => order.reduce((a, b) => fin(b.index)[k] > fin(a.index)[k] ? b : a);
  const fastest = order.filter(p => reach(p, 10) != null).sort((a, b) => reach(a, 10) - reach(b, 10))[0];
  const longest = order.reduce((a, b) => led[b.index] > led[a.index] ? b : a);
  el.innerHTML = `<div class="wrap">
    <div class="eyebrow">OpenCiv3 · ${cast()} · seed ${M.meta.seed ?? "?"} · ${over ? "final" : `after ${T} of ${M.limit} turns`}</div>
    <h1>${headline}</h1>
    <p class="lede">${lab(win)} has ${fin(win.index)[0]} points to ${lab(second)}'s ${fin(second.index)[0]}, with ${fin(win.index)[1]} cities and
      ${fin(win.index)[2]} population. It has led for ${led[win.index]} of ${ti + 1} turns${since < ti ? `, without a break since T${M.turns[since].turn}` : ""}.
      The lead has changed hands ${lc} time${lc === 1 ? "" : "s"}.</p>
    <div class="tiles">${[["Most cities", most(1), fin(most(1).index)[1], "cities"], ["Most techs", most(4), fin(most(4).index)[4], "techs"],
      ["Fastest to 10 cities", fastest, fastest ? "T" + reach(fastest, 10) : "–", "turn reached"],
      ["Longest in the lead", longest, led[longest.index], "turns on top"]].map(([h, p, val, s]) =>
      `<div class="tile"><div class="eyebrow">${h}</div><div class="v">${val}</div><div class="s">${p ? sw(p) + lab(p) + " · " : ""}${s}</div></div>`).join("")}</div>
    <div class="card"><h2>Standings</h2><p class="note">Score = 10·cities + 3·pop + 1·tiles + 4·techs.${anyCalls ? " Calls are the agent's MCP tool calls; failed ones in red." : ""}</p>
      <table class="final"><tr><th class="l"></th><th class="l">agent</th><th class="l">over the game</th><th>score</th><th>cities</th><th>pop</th>
        <th>techs</th><th>land share</th><th>led</th><th>10 cities</th>${anyCalls ? "<th>calls</th><th>failed</th><th>actions</th>" : ""}</tr>
      ${order.map((p, r) => { const s = fin(p.index), t2 = totals[p.index];
        return `<tr><td class="l muted">${r + 1}</td><td class="l">${sw(p)} <b>${lab(p)}</b> <span class="muted">${esc(p.label ? p.civ : "")}</span></td>
          <td class="l"><svg width="90" height="18"><path d="${sparkPath(M.series[p.index].slice(0, ti + 1).map(x => x[0]), 90, 16, Math.max(...order.map(q => fin(q.index)[0])))}" fill="none" stroke="${p.color}" stroke-width="1.5"/></svg></td>
          <td class="b">${s[0]}</td><td>${s[1]}</td><td>${s[2]}</td><td>${s[4]}</td><td>${(s[3] / totalLand * 100).toFixed(1)}%</td>
          <td>${led[p.index] || "–"}</td><td>${reach(p, 10) != null ? "T" + reach(p, 10) : "–"}</td>
          ${anyCalls ? `<td>${t2.ok + t2.failed}</td><td class="${t2.failed ? "down" : ""}">${t2.failed ? `${t2.failed} (${(t2.failed / Math.max(1, t2.ok + t2.failed) * 100).toFixed(1)}%)` : "–"}</td><td>${t2.actions}</td>` : ""}</tr>`; }).join("")}</table></div>
    <div class="card"><h2>Who led when</h2><p class="note">The score leader at each turn. Hover a band.</p><svg id="sm-ribbon" width="100%" height="58"></svg></div>
    <div class="card"><h2>Rank by turn</h2><p class="note">Place in the score table on a 9-turn average, every 10 turns. Hover a line.</p><svg id="sm-bump" width="100%" height="300"></svg></div>
    <div class="grid2"><div class="card"><h2>Score, one agent at a time</h2><p class="note">Each panel against the others in grey, on one scale. Hover to compare.</p><div class="sm" id="sm-mult"></div></div>
      <div class="card"><h2>The expansion race</h2><p class="note">The turn each reached 5, 10, 15 and 20 cities.</p><svg id="sm-race" width="100%" height="320"></svg></div></div>
    <div class="card"><h2>Key moments</h2><p class="note">Wars and peace, cities taken or razed, eliminations and lead changes. Click to jump there.</p><ul class="moments" id="sm-moments"></ul></div>
  </div>`;
  summaryRibbon(ti); summaryBump(ti); summaryMultiples(ti, order); summaryRace(ti, reach);
  const key = M.events.filter(e => e.ti <= ti && kindRank(e.kind) <= 2 && M.durable(e, ti));
  $("#sm-moments").innerHTML = key.map((e, k) => `<li data-k="${k}"><span class="t">T${e.turn}</span><span>${sw(M.byIndex[e.owner])} ${esc(e.text)}</span></li>`).join("")
    || `<li><span></span><span class="muted">None yet.</span></li>`;
  for (const li of $$("#sm-moments li[data-k]")) li.onclick = () => { const e = key[+li.dataset.k]; setView("map"); setTurn(e.ti, {user: true}); };
}
function hookTips(root) { for (const g of $$("[data-tip]", root)) { g.onmousemove = e => showTip(e, g.dataset.tip); g.onmouseleave = hideTip; } }
function summaryRibbon(ti) {
  const svg = $("#sm-ribbon"), W = svg.clientWidth, X = t => W * t / M.limit;
  let h = "", start = 0;
  for (let i = 1; i <= ti + 1; i++) {
    if (i <= ti && M.leaders[i] === M.leaders[start]) continue;
    const p = M.byIndex[M.leaders[start]], a = X(M.turns[start].turn), b = X(i <= ti ? M.turns[i].turn : M.turns[ti].turn + 1);
    const span = `<div class='k'>T${M.turns[start].turn}–T${M.turns[i - 1].turn}, ${i - start} turns</div>`;
    h += `<g data-tip="${esc(p ? `${sw(p)}<b>${lab(p)}</b> leads${span}` : `<b>Tied at the top</b>${span}`)}">
      <rect x="${a + 1}" y="4" width="${Math.max(1, b - a - 2)}" height="26" rx="4" fill="${p ? p.color : "#3a3f48"}"/>
      ${b - a > 56 ? `<text x="${(a + b) / 2}" y="21.5" fill="${p ? "#0f1115" : "#b6bac3"}" font-size="12" font-weight="650" text-anchor="middle">${p ? lab(p) : "tied"}</text>` : ""}</g>`;
    start = i;
  }
  for (const t of [0, .25, .5, .75, 1].map(f => Math.round(M.limit * f)))
    h += `<text x="${clamp(X(t), 12, W - 14)}" y="50" fill="#80868f" font-size="11" text-anchor="middle">T${t}</text>`;
  svg.innerHTML = h; hookTips(svg);
}
function summaryBump(ti) {
  const svg = $("#sm-bump"), W = svg.clientWidth, H = 300, L = 110, R = 110, T = 12, B = 22, n = M.civs.length;
  const mean = (i, t) => { let s = 0, c = 0; for (let k = Math.max(0, t - 8); k <= t; k++) { s += M.series[i][k][0]; c++; } return s / c; };
  const samples = []; for (let k = 0; k <= ti; k++) if ((M.turns[k].turn % 10 === 0 && M.turns[k].turn >= 10) || k === ti) samples.push(k);
  if (samples.length < 2) { svg.innerHTML = `<text x="10" y="20" fill="#80868f" font-size="12">Too early for ranks.</text>`; return; }
  const rank = {}; for (const k of samples) {
    const ord = M.civs.map(p => p.index).sort((a, b) => mean(b, k) - mean(a, k) || a - b); rank[k] = Object.fromEntries(ord.map((i, r) => [i, r + 1])); }
  const t0 = M.turns[samples[0]].turn, t1 = M.turns[ti].turn;
  const X = k => L + (W - L - R) * (M.turns[k].turn - t0) / Math.max(1, t1 - t0), Y = r => T + (H - T - B) * (r - 1) / Math.max(1, n - 1);
  let h = ""; for (let r = 1; r <= n; r++) h += `<line x1="${L}" x2="${W - R}" y1="${Y(r)}" y2="${Y(r)}" stroke="#20242b"/>`;
  for (const k of [samples[0], ...samples.filter(k => M.turns[k].turn % 50 === 0), ti])
    h += `<text x="${X(k)}" y="${H - 5}" fill="#80868f" font-size="11" text-anchor="middle">T${M.turns[k].turn}</text>`;
  for (const p of M.civs) {
    const pts = samples.map(k => [X(k), Y(rank[k][p.index])]);
    const d = pts.map(([x, y], j) => j ? `C${(pts[j - 1][0] + x) / 2},${pts[j - 1][1]} ${(pts[j - 1][0] + x) / 2},${y} ${x},${y}` : `M${x},${y}`).join("");
    h += `<g class="ln" data-i="${p.index}"><path d="${d}" fill="none" stroke="${p.color}" stroke-width="2.5" stroke-linecap="round"/>
      <path d="${d}" fill="none" stroke="transparent" stroke-width="12"/>
      <circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="4" fill="${p.color}" stroke="#16191f" stroke-width="2"/>
      <circle cx="${pts.at(-1)[0]}" cy="${pts.at(-1)[1]}" r="4" fill="${p.color}" stroke="#16191f" stroke-width="2"/>
      <text x="${L - 10}" y="${pts[0][1] + 4}" text-anchor="end" font-size="12" fill="#b6bac3">${lab(p)} ${rank[samples[0]][p.index]}</text>
      <text x="${W - R + 10}" y="${pts.at(-1)[1] + 4}" font-size="12" fill="#eceef2" font-weight="600">${rank[ti][p.index]} ${lab(p)}</text></g>`;
  }
  svg.innerHTML = h;
  const lines = $$(".ln", svg);
  for (const g of lines) {
    g.onmousemove = e => { for (const o of lines) o.style.opacity = o === g ? 1 : 0.15;
      const p = M.byIndex[+g.dataset.i]; showTip(e, `${sw(p)}<b>${lab(p)}</b> <span class="k">${esc(p.civ)}</span>`); };
    g.onmouseleave = () => { for (const o of lines) o.style.opacity = 1; hideTip(); };
    g.onclick = () => { setView("map"); setFocus(+g.dataset.i, {fly: true}); };
  }
}
function summaryMultiples(ti, order) {
  const top = Math.max(1, ...M.civs.map(p => M.series[p.index][ti][0]));
  const box = $("#sm-mult");
  box.innerHTML = order.map(p => `<div class="cell" data-i="${p.index}"><div class="h"><b>${sw(p)} ${lab(p)}</b><span>#${M.ranks(ti)[p.index]} · ${M.series[p.index][ti][0]}</span></div>
    <svg width="100%" height="64"></svg></div>`).join("");
  for (const cell of $$(".cell", box)) {
    const p = M.byIndex[+cell.dataset.i], svg = $("svg", cell), W = svg.clientWidth, H = 64;
    const path = q => sparkPath(M.series[q.index].slice(0, ti + 1).map(s => s[0]), W, H - 4, top);
    svg.innerHTML = M.civs.filter(q => q !== p).map(q => `<path d="${path(q)}" fill="none" stroke="#353a43" stroke-width="1"/>`).join("") +
      `<path d="${path(p)}" fill="none" stroke="${p.color}" stroke-width="2"/><line class="xh" y1="0" y2="${H}" stroke="#80868f" visibility="hidden"/>`;
    svg.onmousemove = e => {
      const r = svg.getBoundingClientRect(), k = Math.round(clamp((e.clientX - r.left) / r.width, 0, 1) * ti);
      for (const c of $$(".cell", box)) { const s2 = $("svg", c), x = k / Math.max(1, ti) * s2.clientWidth, l = $(".xh", s2);
        l.setAttribute("x1", x); l.setAttribute("x2", x); l.setAttribute("visibility", "visible"); }
      showTip(e, `<b>T${M.turns[k].turn}</b>` + order.map(q => `<div class="row" style="${q === p ? "font-weight:650" : "color:#b6bac3"}">${sw(q)}${lab(q)}<span class="v">${M.series[q.index][k][0]}</span></div>`).join(""));
    };
    svg.onmouseleave = () => { hideTip(); for (const l of $$(".xh", box)) l.setAttribute("visibility", "hidden"); };
  }
}
function summaryRace(ti, reach) {
  const svg = $("#sm-race"), W = svg.clientWidth, H = 320, L = 90, R = 16, T = 20, B = 24, marks = [5, 10, 15, 20];
  const rows = M.civs.slice().sort((a, b) => (reach(a, 10) ?? 1e9) - (reach(b, 10) ?? 1e9) || (reach(a, 5) ?? 1e9) - (reach(b, 5) ?? 1e9));
  const first = Math.max(0, Math.floor((Math.min(...M.civs.map(p => reach(p, 5) ?? M.limit)) - 10) / 10) * 10);
  const X = t => L + (W - L - R) * (t - first) / Math.max(1, M.limit - first), rh = (H - T - B) / Math.max(1, rows.length);
  let h = `<text x="${L}" y="11" font-size="11" fill="#80868f">the turn of the 5th, 10th, 15th and 20th city</text>`;
  for (const t of [first, ...[50, 100, 150, 200, 250, 300].filter(t => t <= M.limit && X(t) - X(first) >= 40)])
    h += `<line x1="${X(t)}" x2="${X(t)}" y1="${T - 4}" y2="${H - B}" stroke="#22262d"/><text x="${X(t)}" y="${H - 7}" fill="#80868f" font-size="11" text-anchor="middle">T${t}</text>`;
  rows.forEach((p, r) => {
    const y = T + rh * (r + 0.5), ts = marks.map(n => reach(p, n)), known = ts.filter(t => t != null);
    h += `<text x="${L - 10}" y="${y + 4}" text-anchor="end" font-size="12" fill="#b6bac3">${lab(p)}</text>`;
    if (known.length > 1) h += `<line x1="${X(known[0])}" x2="${X(known.at(-1))}" y1="${y}" y2="${y}" stroke="${p.color}" stroke-width="2" opacity=".45"/>`;
    ts.forEach((t, k) => { if (t == null) return;
      h += `<g data-tip="${esc(`${sw(p)}<b>${lab(p)}</b>: ${marks[k]} cities at T${t}`)}"><circle cx="${X(t)}" cy="${y}" r="${6 + k * 1.5}" fill="${p.color}" stroke="#16191f" stroke-width="2"/>
        <text x="${X(t)}" y="${y + 3.5}" text-anchor="middle" font-size="9" font-weight="700" fill="#0f1115">${marks[k]}</text></g>`; });
  });
  svg.innerHTML = h; hookTips(svg);
}

// ======================================================================== timeline

function renderScrub() {
  if (!M.ready) return;
  const svg = $("#scrubsvg"), W = svg.clientWidth || 800, H = 40, y = 25, X = t => 6 + (W - 12) * t / M.limit;
  const lastT = M.turns[M.last].turn, cur = M.turns[S.ti].turn;
  let h = `<rect x="6" y="${y - 2}" width="${W - 12}" height="4" rx="2" fill="#262b33"/>`;
  h += `<rect x="6" y="${y - 2}" width="${Math.max(0, X(lastT) - 6)}" height="4" rx="2" fill="#3a404b"/>`;
  let start = 0;
  for (let i = 1; i <= M.last + 1; i++) {
    if (i <= M.last && M.leaders[i] === M.leaders[start]) continue;
    const p = M.byIndex[M.leaders[start]];
    if (p) h += `<rect x="${X(M.turns[start].turn)}" y="${y + 5}" width="${Math.max(1.5, X(i <= M.last ? M.turns[i].turn : lastT + 1) - X(M.turns[start].turn))}" height="3" fill="${p.color}"><title>${esc(M.name(p.index))} leads from T${M.turns[start].turn}</title></rect>`;
    start = i;
  }
  for (const e of M.events) {
    if (!(KIND[e.kind] || [])[3] || !M.durable(e, M.last)) continue;
    const c = e.kind === "lead_change" ? "#e9c46a" : kindHot(e.kind) ? "#f0745a" : "#9fb4d8";
    h += `<path d="M${X(e.turn)},${y - 15} l4,5 l-4,5 l-4,-5z" fill="${c}"><title>T${e.turn} · ${esc(e.text)}</title></path>`;
  }
  h += `<rect x="6" y="${y - 2}" width="${Math.max(0, X(cur) - 6)}" height="4" rx="2" fill="#8b919b"/>`;
  h += `<circle cx="${X(cur)}" cy="${y}" r="7" fill="#eceef2" stroke="#0f1115" stroke-width="2"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.innerHTML = h;
}
function scrubEvents() {
  const el = $("#scrub");
  const go = e => {
    const r = el.getBoundingClientRect(), t = Math.round(clamp((e.clientX - r.left - 6) / (r.width - 12), 0, 1) * M.limit);
    setTurn(Math.min(M.last, M.turnIndex(t)), {user: true});
  };
  let down = false;
  el.addEventListener("pointerdown", e => { if (!M.ready) return; down = true; el.setPointerCapture(e.pointerId); stop(); go(e); });
  el.addEventListener("pointermove", e => { if (down) { hideTip(); go(e); } else if (M.ready) scrubTip(e); });
  el.addEventListener("pointerup", () => { down = false; });
  el.addEventListener("pointerleave", hideTip);
  const scrubTip = e => {
    const r = el.getBoundingClientRect(), t = Math.round(clamp((e.clientX - r.left - 6) / (r.width - 12), 0, 1) * M.limit);
    if (t > M.turns[M.last].turn) return showTip(e, `<b>T${t}</b> <span class="k">not played yet</span>`);
    const i = M.turnIndex(t), lead = M.byIndex[M.leaders[i]];
    const hot = M.events.filter(ev => ev.ti === i && (KIND[ev.kind] || [])[3] && M.durable(ev, M.last));
    showTip(e, `<b>T${M.turns[i].turn}</b> <span class="k">·</span> ${lead ? `${sw(lead)}${lab(lead)} <span class="k">leads</span>`
      : `<span class="k">tied at the top</span>`}` +
      hot.slice(0, 4).map(ev => `<div class="row">${sw(M.byIndex[ev.owner])}<span>${esc(ev.text)}</span></div>`).join("") +
      (hot.length > 4 ? `<div class="k">+${hot.length - 4} more</div>` : ""));
  };
  el.addEventListener("keydown", e => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); stop();
      setTurn(S.ti + (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 10 : 1), {user: true, animate: e.key === "ArrowRight"}); }
  });
}
function stop() { clearInterval(S.playing); S.playing = null; $("#play").textContent = "Play"; }
function togglePlay() {
  if (!M.ready) return;
  if (S.playing) return stop();
  if (S.ti >= M.last) setTurn(0, {user: true});
  $("#play").textContent = "Pause";
  S.playing = setInterval(() => {
    if (S.ti >= M.last) { stop(); if (LIVE) { S.follow = true; renderStatus(); } return; }
    setTurn(S.ti + 1, {animate: true, user: true});
  }, 1000 / S.speed);
}
function keys() {
  document.addEventListener("keydown", e => {
    if (!M.ready || e.target.tagName === "SELECT" || e.target.tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === " ") { e.preventDefault(); togglePlay(); }
    else if (k === "ArrowRight") { stop(); setTurn(S.ti + (e.shiftKey ? 10 : 1), {user: true, animate: !e.shiftKey}); }
    else if (k === "ArrowLeft") { stop(); setTurn(S.ti - (e.shiftKey ? 10 : 1), {user: true}); }
    else if (k === "Home") { stop(); setTurn(0, {user: true}); }
    else if (k === "End") { stop(); LIVE ? goLive() : setTurn(M.last, {user: true}); }
    else if (k === "Escape") { if (S.client.open) closeClient(); else if (S.pov != null) setPov(null); else setFocus(null); }
    else if (/^[1-9]$/.test(k)) { const rk = M.ranks(S.ti), p = M.civs.find(c => rk[c.index] === +k); if (p) setFocus(S.focus === p.index ? null : p.index, {fly: S.focus !== p.index}); }
    else if (k === "m" || k === "M") setView("map");
    else if (k === "a" || k === "A") setView("agents");
    else if (k === "s" || k === "S") setView("summary");
    else if ((k === "v" || k === "V") && S.focus != null && M.byIndex[S.focus].seat != null) setPov(S.pov === M.byIndex[S.focus].seat ? null : M.byIndex[S.focus].seat);
    else if (k === "c" || k === "C") S.client.open ? closeClient() : openClient(S.focus != null && M.byIndex[S.focus].seat != null ? S.focus : null);
    else if (k === "l" || k === "L") { if (LIVE) goLive(); }
    else if (k === "+" || k === "=") $("#zin").click();
    else if (k === "-") $("#zout").click();
    else if (k === "0") $("#zfit").click();
    else return;
  });
}

// ======================================================================== broadcast (?stream)

// The stream's director cuts between shots. The match's events, the agents' messages and the casters' lines queue
// shots by priority; a shot holds the screen for at least DWELL before a more important one cuts in, a queued shot goes
// stale after MAX_AGE, and full-screen cards are at least CARD_GAP apart. With nothing queued it runs the loop: the
// whole map (20 s), three agents in the spotlight (15 s each: the map flies to the civ, its card shows its plan and
// turn, and with the client its client view sits in the corner), then every agent's panel (25 s); with ?stream&client
// and the client, the whole map, then every agent's client view full size in turn. A title card opens the broadcast;
// once the game is over a winner card leads to the summary, which stays up.
const DWELL = 6000, MAX_AGE = 45000, CARD_GAP = 8000, LEAD_GAP = 45000;
class Director {
  constructor(loop) { this.loop = loop; this.queue = []; this.keys = new Set(); this.on = null; this.lastCard = -Infinity; this.step = 0; }
  // Queue a shot {key, prio (lower first), ms, card, focus, expires, run}; a key queued before is ignored.
  add(shot, now) {
    if (this.keys.has(shot.key)) return false;
    this.keys.add(shot.key);
    this.queue.push({expires: now + MAX_AGE, ...shot, born: now});
    return true;
  }
  // The shot to cut to at `now`, or null to stay on the one on screen.
  next(now) {
    this.queue = this.queue.filter(s => s.expires > now);
    const on = this.on, held = on ? now - on.start : Infinity, done = !on || held >= on.ms;
    const best = this.queue.filter(s => !s.card || now - this.lastCard >= CARD_GAP)
      .sort((a, b) => a.prio - b.prio || a.born - b.born)[0];
    if (best && (done || (held >= DWELL && best.prio < on.prio))) {
      this.queue.splice(this.queue.indexOf(best), 1);
      return this.cut(best, now);
    }
    return done && !this.queue.length ? this.cut(this.loop(this.step++), now) : null;   // a card waits for its gap
  }
  cut(shot, now) {
    if (shot.card) this.lastCard = now;
    return (this.on = {...shot, start: now});
  }
}

const bc = {director: null, timers: [], leader: null, leadAt: -Infinity, notes: new Set(), tick: 0};
function later(ms, fn) { bc.timers.push(setTimeout(fn, ms)); }
function tick() {
  if (!M.ready || !bc.director) return;
  const shot = bc.director.next(performance.now());
  if (!shot) return;
  for (const t of bc.timers) clearTimeout(t);
  bc.timers = [];
  for (const el of [$("#bubble"), $("#chyron")]) if (!el.hidden) leave(el);   // they belong to the shot that ends
  shot.run();
}
function broadcastStart() {
  for (const t of bc.timers) clearTimeout(t);
  Object.assign(bc, {director: new Director(loopShot), timers: [], leader: M.leaders[M.last], leadAt: -Infinity, notes: new Set()});
  for (const m of M.messagesUpTo(M.last)) bc.director.keys.add(msgKey(m));   // only what happens from now on
  bc.director.add({key: "title", prio: 0, card: true, ms: 9000, run: () => { overview(); card("title", titleCard(), 8000); }},
    performance.now());
  broadcastNews(M.last);
}
// What the newest data adds: events of turns after turn index `wasLast`, new messages, a new leader, the game's end.
function broadcastNews(wasLast) {
  const d = bc.director, now = performance.now();
  if (!d) return;
  let k = M.events.length; while (k > 0 && M.events[k - 1].ti > wasLast) k--;
  for (const e of M.events.slice(k)) { const s = eventShot(e); if (s) d.add(s, now); }
  for (const m of M.messagesUpTo(M.last)) d.add(messageShot(m), now);
  const lead = M.leaders[M.last];
  if (lead != null && lead !== bc.leader && now - bc.leadAt >= LEAD_GAP && M.turns[M.last].turn > 5) {
    bc.leader = lead; bc.leadAt = now; d.add(leadShot(M.byIndex[lead]), now);
  }
  if ($("#ticker").hidden) renderTicker();
  if (M.live?.game_over) d.add({key: "over", prio: 0, card: true, ms: Infinity,
    run: () => { overview(); card("win", winCard(), 8000); later(7600, () => setView("summary")); }}, now);
}

// ---- shots ----

// `zoom`: how far in a shot may go, as a multiple of the whole map's zoom (on a small map a fixed one is no zoom at all).
function onMap({focus = null, box = null, zoom = 1.8} = {}) {
  closeClient();
  if (S.view !== "map") setView("map");
  setFocus(focus);
  const r = canvas().getBoundingClientRect();
  if (box && r.width) flyTo(box, {maxHw: Math.min(56, fitView(M.landBox, r.width, r.height).hw * zoom)});
}
function overview() { onMap({box: M.landBox, zoom: 1}); }
function spotlight(p) {
  onMap({focus: p.index, box: M.civBox(p.index, S.ti, 4)});
  S.client.big = STREAM_CLIENT;
  if (M.live?.client && p.seat != null) openClient(p.index);
}
function loopShot(step) {
  const seats = (M.seats.length ? M.seats : M.civs).filter(p => !M.series[p.index][M.last][5]);
  const shot = (ms, run, p = null) => ({key: "loop", prio: 9, ms, run, focus: p?.index ?? null});
  if (!seats.length) return shot(20000, overview);
  if (STREAM_CLIENT && M.live?.client) {
    const p = seats[step % (seats.length + 1) - 1];
    return p ? shot(15000, () => spotlight(p), p) : shot(20000, overview);
  }
  const k = step % 5, p = seats[(Math.floor(step / 5) * 3 + k - 1) % seats.length];
  return k === 0 ? shot(20000, overview) : k === 4 ? shot(25000, () => { closeClient(); setFocus(null); setView("agents"); })
    : shot(15000, () => spotlight(p), p);
}
const near = (x, y, r) => ({x0: x - r, x1: x + r, y0: y - r * 2, y1: y + r * 2});
function eventShot(e) {
  const p = M.byIndex[e.owner], q = M.byIndex[e.from];
  if (!p || p.barbarian) return null;
  if (e.kind === "civ_destroyed")
    return {key: "out:" + e.owner, prio: 1, card: true, ms: 9000, run: () => { overview(); card("out", outCard(p, e), 4500); }};
  if ((e.kind === "war_declared" || e.kind === "peace_signed") && q && !q.barbarian) {
    const war = e.kind === "war_declared";
    return {key: `${e.kind}:${e.turn}:${e.owner}:${e.from}`, prio: war ? 2 : 4, card: true, ms: 10000, run: () => {
      onMap(); card(war ? "war" : "peace", pairCard(p, q, war, e), 4000);
      later(3600, () => onMap({box: borderBox(p.index, q.index), zoom: 2.2}));
    }};
  }
  if ((e.kind === "city_captured" || e.kind === "city_destroyed") && e.x != null) {
    const taker = e.kind === "city_captured" ? e.owner : null, cutaway = taker != null && M.live?.client && p.seat != null;
    return {key: `${e.kind}:${e.turn}:${e.x},${e.y}`, prio: 2, ms: cutaway ? 12000 : 8000, focus: taker, run: () => {
      onMap({focus: taker, box: near(e.x, e.y, 5), zoom: 3}); chyron(e, cutaway ? 6000 : 7600);
      if (cutaway) later(6000, () => { S.client.big = true; openClient(taker); });   // the taker's own view of it
    }};
  }
  // a civ's second to fourth city: the opening's expansion race, a short look that soon goes stale
  const cities = M.turns[e.ti].cities.filter(c => c[3] === e.owner).length;
  if (e.kind === "city_founded" && e.x != null && cities >= 2 && cities <= 4)
    return {key: `founded:${e.turn}:${e.x},${e.y}`, prio: 7, ms: 6000, expires: performance.now() + 20000, focus: e.owner,
      run: () => { onMap({focus: e.owner, box: near(e.x, e.y, 6), zoom: 2.5}); chyron(e, 5600); }};
  return null;
}
const leadShot = p => ({key: `lead:${p.index}:${M.last}`, prio: 3, card: true, ms: 10000, focus: p.index,
  run: () => { onMap(); card("lead", leadCard(p), 4000); later(3600, () => spotlight(p)); }});
const msgKey = m => `msg:${m.turn}:${m.from}:${m.text}`;
const messageShot = m => ({key: msgKey(m), prio: 5, ms: 7000, focus: m.from,
  run: () => { onMap({focus: m.from, box: M.civBox(m.from, S.ti, 4)}); bubble(m, 6600); }});
// Where two civs meet: their shared border, or else the two closest cities of theirs.
function borderBox(a, b) {
  const own = M.owners(S.ti), pts = [];
  M.tiles.forEach((t, i) => {
    if (own[i] !== a) return;
    for (const [dx, dy] of [[1, 1], [1, -1], [-1, 1], [-1, -1], [2, 0], [-2, 0], [0, 2], [0, -2]]) {
      const j = M.tileIndex(t.x + dx, t.y + dy);
      if (j != null && own[j] === b) { pts.push(t, M.tiles[j]); return; }
    }
  });
  if (pts.length) return M.box(pts, 3);
  const at = i => M.turns[S.ti].cities.filter(c => c[3] === i).map(c => ({x: M.sx(c[0]), y: c[1]}));
  let best = null;
  for (const c of at(a)) for (const d of at(b)) {
    const dist = Math.hypot(c.x - d.x, (c.y - d.y) / 2);
    if (!best || dist < best[0]) best = [dist, c, d];
  }
  return best ? M.box(best.slice(1), 3) : M.landBox;
}

// ---- what the shots show: full-screen cards, the speech bubble, the event chyron ----

// An overlay enters (.in), stays `ms`, then leaves (.out); ms Infinity stays until leave().
const overlayTimers = {};
function flash(el, ms) {
  for (const t of overlayTimers[el.id] || []) clearTimeout(t);
  el.hidden = false; el.classList.remove("in", "out"); void el.offsetWidth; el.classList.add("in");
  overlayTimers[el.id] = ms === Infinity ? [] : [setTimeout(() => leave(el), ms - 350)];
}
function leave(el) {
  for (const t of overlayTimers[el.id] || []) clearTimeout(t);
  el.classList.add("out");
  overlayTimers[el.id] = [setTimeout(() => { el.hidden = true; }, 350)];
}
function card(kind, html, ms) { const el = $("#moment"); el.className = "moment card-" + kind; el.innerHTML = html; flash(el, ms); }
const shout = p => `<div class="big${lab(p).length > 10 ? " long" : ""}">${lab(p)}</div>`;
const NUMBERS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve"];
function titleCard() {
  const seats = M.seats.length ? M.seats : M.civs, people = seats.filter(p => p.human).length, agents = seats.length - people;
  const n = k => NUMBERS[k] || String(k);
  const who = `${n(agents)} AI model${agents === 1 ? "" : "s"}${people ? ` and ${n(people).toLowerCase()} ${people === 1 ? "person" : "people"}` : ""}`;
  return `<div class="titlecard"><h1>${esc(TITLE || "OpenCiv3")}</h1>
    <p>${who} share${seats.length === 1 ? "s" : ""} one world for ${M.limit} turns.${seats.length > 1 ? " They can message each other." : ""}</p>
    <div class="lineup" style="--n:${seats.length}">${seats.map((p, k) => `<div class="seat" style="--a:${p.color};--k:${k}"><b>${lab(p)}</b>
      <span>${esc(p.civ)}${p.human ? ", played by a person" : ""}</span></div>`).join("")}</div></div>`;
}
function leadCard(p) {
  const ti = M.last, rk = M.ranks(ti), second = M.civs.find(q => rk[q.index] === 2), s = M.series[p.index][ti][0];
  return `<div class="slab" style="--a:${p.color}"><div class="words">${shout(p)}<div class="what">takes the lead</div></div></div>
    <div class="meta">${esc(p.civ)}, ${s} points${second ? `, ${s - M.series[second.index][ti][0]} ahead of ${lab(second)}` : ""}, turn ${M.turns[ti].turn}</div>`;
}
function pairCard(p, q, war, e) {
  const side = r => `<div class="party">${shout(r)}<div class="civ">${esc(r.civ)}</div></div>`;
  return `<div class="slab pair" style="--a:${p.color};--b:${q.color}">${side(p)}<div class="vs"><span>${war ? "War" : "Peace"}</span></div>${side(q)}</div>
    <div class="meta">${esc(e.text)}, turn ${e.turn}</div>`;
}
function outCard(p, e) {
  return `<div class="slab gone" style="--a:${p.color}"><div class="words">${shout(p)}<div class="what">eliminated</div></div></div>
    <div class="meta">${esc(p.civ)} is out of the game, turn ${e.turn}</div>`;
}
function winCard() {
  const ti = M.last, rk = M.ranks(ti), order = M.civs.slice().sort((a, b) => rk[a.index] - rk[b.index]);
  const score = p => M.series[p.index][ti][0], v = M.meta.victory, w = (v && M.players.find(p => p.civ === v.civ)) || order[0];
  const tied = !v && order.length > 1 && score(order[1]) === score(w);
  const how = v ? `by ${esc(v.kind)} victory, turn ${v.turn}` : tied ? `tied on ${score(w)} points, turn ${M.turns[ti].turn}`
    : `on score, ${score(w)} points to ${lab(order[1] || w)}'s ${score(order[1] || w)}, turn ${M.turns[ti].turn}`;
  return `<div class="slab" style="--a:${tied ? "#3a3f48" : w.color}"><div class="words"><div class="what">game over</div>
      ${tied ? `<div class="big">a tie</div>` : `${shout(w)}<div class="what">wins</div>`}</div></div>
    <div class="meta">${how}</div>
    <div class="final">${order.map(p => `<span>${sw(p)}<b>${lab(p)}</b>${score(p)}</span>`).join("")}</div>`;
}
function bubble(m, ms) {
  const el = $("#bubble"), p = M.byIndex[m.from];
  el.style.setProperty("--a", p?.color || "#777");
  el.innerHTML = `<div class="bh"><span class="who">${sw(p)}<b>${lab(p)}</b><span class="civ">${esc(p?.civ)}</span></span>
    <span class="arrow">→</span>${toWhom(m)}</div><p class="${m.text.length > 140 ? "long" : ""}">${quote(m.text)}</p>`;
  flash(el, ms);
}
function chyron(e, ms) {
  const el = $("#chyron");
  el.style.setProperty("--k", kindHot(e.kind) ? "var(--bad)" : M.byIndex[e.owner]?.color || "#777");
  el.classList.toggle("hot", kindHot(e.kind));
  el.innerHTML = `<span class="k">${esc(kindLabel(e.kind))}</span>${sw(M.byIndex[e.owner])}<span>${esc(e.text)}</span><span class="t">T${e.turn}</span>`;
  flash(el, ms);
}

// ---- the notes ticker: each seat's newest end_turn note, the ones not shown yet first ----

function renderTicker() {
  if (!M.ready || !$("#ticker")) return;
  const notes = M.seats.map(p => ({p, n: M.lastNote(M.last, p.index)})).filter(x => x.n);
  $("#ticker").hidden = !notes.length;
  if (!notes.length) return;
  const key = x => `${x.n.turn}:${x.p.index}`, fresh = notes.filter(x => !bc.notes.has(key(x)));
  const x = fresh.length ? fresh.sort((a, b) => a.n.turn - b.n.turn || a.p.seat - b.p.seat)[0] : notes[bc.tick++ % notes.length];
  bc.notes.add(key(x));
  $("#tk").innerHTML = `<div class="item">${sw(x.p)}<b>${lab(x.p)}:</b><span class="q">${quote(x.n.text)}</span><span class="t">T${x.n.turn}</span></div>`;
}

// ---- the casters (&cast=URL): their lines in order, voiced, with a caption while each plays ----

const CASTERS = {pbp: ["play-by-play", "#e9c46a"], color: ["analyst", "#8fb4ff"]};
const voice = {last: 0, queue: [], on: null, first: true};
async function pollCast() {
  try {
    const r = await fetch(`${CAST}/cast.json?since=${voice.last}`, {cache: "no-store"}), doc = await r.json();
    const lines = (doc.lines || []).filter(l => l.id > voice.last).sort((a, b) => a.id - b.id);
    if (lines.length) voice.last = lines.at(-1).id;
    voice.queue.push(...(voice.first ? stillOn(lines, doc.speaking_until) : lines));
    voice.first = false;
    speak();
  } catch (e) { /* the caster isn't up yet: keep asking */ }
  setTimeout(pollCast, 1000);
}
// A page opened mid-broadcast starts with the lines still under way by the caster's clock, not its whole backlog.
function stillOn(lines, until) {
  let left = (until || 0) - Date.now() / 1000, k = lines.length;
  while (k > 0 && left > 0) left -= (lines[--k].seconds || 0) + 0.5;
  return lines.slice(k);
}
function speak() {
  if (voice.on || !voice.queue.length) return;
  const line = voice.queue.shift(), el = $("#caption"), [role, colour] = CASTERS[line.speaker] || ["", "#b6bac3"];
  const secs = line.seconds || String(line.text).split(/\s+/).length / 2.6, start = performance.now();
  voice.on = line;
  el.style.setProperty("--c", colour);
  el.innerHTML = `<div class="who"><b>${esc(line.name)}</b><span>${role}</span></div><p>${esc(line.text)}</p>`;
  flash(el, Infinity);
  castFocus(line);
  let timer = setTimeout(done, (secs + 3) * 1000);   // audio that never ends
  function done() { clearTimeout(timer); if (voice.on !== line) return; voice.on = null; leave(el); setTimeout(speak, 400); }
  function silent() { clearTimeout(timer); timer = setTimeout(done, Math.max(0, secs * 1000 - (performance.now() - start))); }
  if (!line.audio) return silent();
  const audio = new Audio(`${CAST}/${line.audio}`);
  audio.onended = done; audio.onerror = silent;
  audio.play().catch(silent);   // autoplay refused: the caption still shows for the line's length
}
function castFocus(line) {
  if (!line.focus || !bc.director || !M.ready) return;
  const f = String(line.focus).toLowerCase(), p = M.civs.find(q => q.civ.toLowerCase() === f || (q.label || "").toLowerCase() === f);
  const now = performance.now(), ms = Math.max(DWELL, (line.seconds || 4) * 1000);
  if (p && bc.director.on?.focus !== p.index)
    bc.director.add({key: "cast:" + line.id, prio: 6, ms, expires: now + ms, focus: p.index, run: () => spotlight(p)}, now);
}

// ======================================================================== startup and live polling

// A link pasted into the open page (only its #hash changed) takes it to the same view.
addEventListener("hashchange", () => {
  if (!M.ready || STREAM) return;
  const before = {view: S.view, ti: S.ti, focus: S.focus, pov: S.pov, client: S.client.open};
  S.view = "map"; S.focus = null; S.pov = null; S.client = {open: false, seat: null, big: false};
  S.ti = M.last; S.follow = true;   // no #t= means the newest turn
  readHash();
  $("#pov").value = S.pov == null ? "" : String(S.pov);
  renderPovNote();
  if (S.view !== before.view) setView(S.view);
  setTurn(S.ti);
  setFocus(S.focus, {fly: S.focus != null && S.focus !== before.focus});
  renderClient();
});

function start() {
  P = new Painter(M);
  S.ti = M.last;
  if (STREAM) S.kinds.delete("unit_lost"); else readHash();
  $("#emptymsg").hidden = true;
  for (const id of ["#v-map", "#v-agents", "#v-summary"]) $(id).hidden = true;
  renderTop(); renderLayers(); renderMetrics(); renderKinds(); renderPovNote();
  setView(S.view);
  setTurn(S.ti);
  if (S.client.open) renderClient();
  if (STREAM) broadcastStart();
}
function waiting(msg) {
  $("#emptymsg").hidden = false; $("#emptymsg").innerHTML = `<div>${esc(msg)}</div><div class="muted">${LIVE ? "This page follows the game as it plays." : ""}</div>`;
  for (const id of ["#v-map", "#v-agents", "#v-summary"]) $(id).hidden = true;
}
let polling = false, failures = 0;
async function poll() {
  if (polling) return; polling = true;
  try {
    const since = M.ready ? M.turns[M.last].turn : -1;
    let r = await fetch(`live/data.json?since=${since}`, {cache: "no-store"});
    let doc = await r.json();
    if (M.ready && doc.game !== M.game) { r = await fetch("live/data.json?since=-1", {cache: "no-store"}); doc = await r.json(); }
    failures = 0;
    const wasReady = M.ready, wasLast = M.last, game = M.game;
    const added = M.ingest(doc);
    if (!M.ready) { waiting(doc.live?.recording === false ? "Recording is off (OPENCIV_RECORD=0), so there is nothing to show."
      : "Waiting for the game to start…"); return; }
    if (!wasReady || game !== M.game) { gridGame = null; P = null; start(); renderKinds(); return; }
    if (added) {
      renderKinds();
      if (S.follow && !S.playing) setTurn(M.last, {animate: M.last === wasLast + 1});
      else { renderScrub(); renderStatus(); }
    } else {
      renderStatus();
      if (S.ti === M.last) { if (S.view === "map") { renderStandings(); renderAgentCard(); renderMessages(); } if (S.view === "agents") renderAgents(); }
    }
    if (STREAM) broadcastNews(wasLast);
  } catch (e) {
    failures++;
    if ($("#waiting")) $("#waiting").textContent = failures > 1 ? "Lost the env; retrying…" : "";
  } finally {
    polling = false;
    setTimeout(poll, document.hidden ? 5000 : 1500);
  }
}

shell();
$("#views").insertAdjacentHTML("beforeend", `<div class="empty" id="emptymsg" hidden></div>`);
if (STREAM) { setInterval(tick, 250); setInterval(renderTicker, 5000); if (CAST) pollCast(); }
if (LIVE) { waiting("Connecting to the game…"); poll(); }
else if (M.ingest(window.OPENCIV_DATA) && M.ready) start();
else waiting("This recording has no turns.");
