// Shared by the viz prototypes: match.json decoding, the diamond-grid geometry and a canvas map painter.
"use strict";

const TERRAIN_RGB = {
  ocean: [22, 38, 64], sea: [28, 48, 80], coast: [44, 72, 108], grassland: [86, 120, 70], plains: [140, 132, 84],
  desert: [176, 160, 116], tundra: [140, 146, 140], floodplain: [104, 132, 76], hills: [118, 104, 76],
  mountains: [108, 102, 98], forest: [52, 88, 58], jungle: [44, 96, 70], marsh: [80, 102, 92], volcano: [96, 64, 58],
};
const WATER = new Set(["ocean", "sea", "coast"]);
const KIND_LABEL = {
  city_founded: "founded", city_captured: "captured", city_destroyed: "razed", civ_destroyed: "eliminated",
  lead_change: "lead", tech_learned: "tech", unit_lost: "unit lost", contact: "contact", gold_stolen: "gold stolen",
  war_declared: "war", peace_signed: "peace",
};
const KIND_RANK = {civ_destroyed: 0, city_captured: 1, city_destroyed: 1, war_declared: 1, lead_change: 2,
  peace_signed: 3, city_founded: 4, unit_lost: 5, gold_stolen: 5, contact: 6, tech_learned: 7};

const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const hexRgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));

class Match {
  constructor(d) {
    Object.assign(this, d);
    this.W = d.meta.map.width; this.H = d.meta.map.height;
    this.limit = d.meta.turn_limit || d.turns[d.turns.length - 1].turn;
    this.seam = d.meta.seam - (d.meta.seam % 2);          // an even shift keeps x+y parity
    this.civs = d.players.filter(p => !p.barbarian);
    this.byIndex = Object.fromEntries(d.players.map(p => [p.index, p]));
    for (const p of d.players) p.rgb = hexRgb(p.color);
    this.tiles = d.static.tiles.map(([x, y, t, o, r]) => ({
      x: (x - this.seam + this.W) % this.W, y, base: d.meta.terrain[t], over: o >= 0 ? d.meta.terrain[o] : null, river: r,
    }));
    this.tileAt = new Map(this.tiles.map((t, i) => [t.x + "," + t.y, i]));
    this.last = d.turns.length - 1;
    this._owners = new Map();   // turn index -> Int16Array, a checkpoint every 10 turns plus the latest
    // Per-civ series of every score key, by turn index.
    this.series = {};
    for (const p of d.players) this.series[p.index] = d.turns.map(t => t.scores[p.index] || [0, 0, 0, 0, 0, 0]);
    this.events = d.turns.flatMap(t => t.events.map(e => ({...e, turn: t.turn,
      x: e.x == null ? null : (e.x - this.seam + this.W) % this.W})));
    this.landBox = this._box(this.tiles.filter(t => !WATER.has(t.base)), 3);
  }
  name(i) { const p = this.byIndex[i]; return p ? (p.label || p.civ) : "?"; }
  full(i) { const p = this.byIndex[i]; return p ? (p.label ? `${p.label} · ${p.civ}` : p.civ) : "?"; }
  sx(x) { return (x - this.seam + this.W) % this.W; }
  owners(ti) {
    if (this._owners.has(ti)) return this._owners.get(ti);
    let start = -1, base = null;
    for (const [k, v] of this._owners) if (k <= ti && k > start) { start = k; base = v; }
    const arr = base ? Int16Array.from(base) : new Int16Array(this.tiles.length).fill(-1);
    for (let t = start + 1; t <= ti; t++) {
      for (const [i, o] of this.turns[t].owners) arr[i] = o;
      if (t % 10 === 0 && !this._owners.has(t)) this._owners.set(t, Int16Array.from(arr));
    }
    this._owners.set(ti, arr);
    if (this._owners.size > 40) for (const k of this._owners.keys()) if (k % 10) { this._owners.delete(k); break; }
    return arr;
  }
  ranks(ti) {   // index -> rank (1-based) by score at turn index ti
    const order = this.civs.map(p => p.index).sort((a, b) =>
      this.series[b][ti][0] - this.series[a][ti][0] || a - b);
    return Object.fromEntries(order.map((i, r) => [i, r + 1]));
  }
  _box(tiles, margin) {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const t of tiles) { x0 = Math.min(x0, t.x); y0 = Math.min(y0, t.y); x1 = Math.max(x1, t.x); y1 = Math.max(y1, t.y); }
    return {x0: Math.max(-1, x0 - margin), y0: Math.max(-1, y0 - margin * 2),
      x1: Math.min(this.W, x1 + margin), y1: Math.min(this.H, y1 + margin * 2)};
  }
  civBox(index, ti, margin = 4) {
    const own = this.owners(ti), tiles = this.tiles.filter((_, i) => own[i] === index);
    const cities = this.turns[ti].cities.filter(c => c[3] === index).map(c => ({x: this.sx(c[0]), y: c[1]}));
    const pts = tiles.length ? tiles : cities;
    return pts.length ? this._box(pts, margin) : null;
  }
}

// Draws the map into a canvas. view = {box: {x0,y0,x1,y1} in tile coords, hw: px per half tile width}.
class MapPainter {
  constructor(match) {
    this.m = match;
    this.terrainCache = new Map();
    this.tColor = match.tiles.map(t => this.terrainColor(t));
  }
  center(x, y, v) { return [(x - v.box.x0) * v.hw, (y - v.box.y0) * v.hw / 2]; }
  diamond(ctx, x, y, v, s = 1) {
    const [cx, cy] = this.center(x, y, v), w = v.hw * s, h = v.hw / 2 * s;
    ctx.moveTo(cx, cy - h); ctx.lineTo(cx + w, cy); ctx.lineTo(cx, cy + h); ctx.lineTo(cx - w, cy); ctx.closePath();
  }
  terrainColor(t) {
    const c = TERRAIN_RGB[t.over || t.base] || [100, 100, 100];
    const n = ((t.x * 7 + t.y * 13) % 5) * 0.025;
    return mix(c, [0, 0, 0], n);
  }
  // The static layer at a given view, cached by its key.
  terrain(v, w, h) {
    const dpr = window.devicePixelRatio || 1;
    const key = [v.box.x0, v.box.y0, v.hw.toFixed(3), w, h, dpr].join();
    if (this.terrainCache.has(key)) return this.terrainCache.get(key);
    const c = new OffscreenCanvas(Math.round(w * dpr), Math.round(h * dpr)), ctx = c.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.fillStyle = "#0e1420"; ctx.fillRect(0, 0, w, h);
    for (const t of this.m.tiles) {
      ctx.beginPath(); this.diamond(ctx, t.x, t.y, v, 1.02); ctx.fillStyle = rgb(this.terrainColor(t)); ctx.fill();
    }
    ctx.strokeStyle = "rgba(120,170,230,0.55)"; ctx.lineWidth = Math.max(1, v.hw / 5); ctx.lineCap = "round";
    for (const t of this.m.tiles) {
      if (!t.river) continue;
      for (const [dx, dy] of [[1, -1], [1, 1]]) {
        const j = this.m.tileAt.get(((t.x + dx + this.m.W) % this.m.W) + "," + (t.y + dy));
        if (j != null && this.m.tiles[j].river && Math.abs(this.m.tiles[j].x - t.x) === 1) {
          ctx.beginPath(); ctx.moveTo(...this.center(t.x, t.y, v)); ctx.lineTo(...this.center(t.x + dx, t.y + dy, v));
          ctx.stroke();
        }
      }
    }
    if (this.terrainCache.size > 6) this.terrainCache.delete(this.terrainCache.keys().next().value);
    this.terrainCache.set(key, c);
    return c;
  }
  // opts: focus (civ index|null), labels ("auto"|"all"|"none"), units (bool), territory (bool), borders (bool),
  //       pulses [{x,y,color,t}] with t in 0..1
  draw(ctx, ti, v, w, h, opts = {}) {
    const m = this.m, own = m.owners(ti), focus = opts.focus ?? null;
    ctx.drawImage(this.terrain(v, w, h), 0, 0, w, h);
    const visible = t => {
      const [cx, cy] = this.center(t.x, t.y, v);
      return cx > -v.hw && cy > -v.hw && cx < w + v.hw && cy < h + v.hw;
    };
    if (opts.territory !== false) {
      for (let i = 0; i < m.tiles.length; i++) {
        const o = own[i]; if (o < 0) continue;
        const t = m.tiles[i]; if (!visible(t)) continue;
        const p = m.byIndex[o], dim = focus != null && o !== focus;
        ctx.beginPath(); this.diamond(ctx, t.x, t.y, v, 1.02);
        ctx.fillStyle = rgb(mix(this.tColor[i], p.rgb, dim ? 0.1 : WATER.has(t.base) ? 0.22 : 0.38)); ctx.fill();
      }
    }
    if (opts.borders !== false) {
      ctx.lineCap = "round";
      const edges = [[1, -1, 0, 1], [1, 1, 1, 2], [-1, 1, 2, 3], [-1, -1, 3, 0]];
      for (let i = 0; i < m.tiles.length; i++) {
        const o = own[i]; if (o < 0) continue;
        const t = m.tiles[i]; if (!visible(t)) continue;
        const [cx, cy] = this.center(t.x, t.y, v), s = 0.9, pw = v.hw * s, ph = v.hw / 2 * s;
        const pts = [[cx, cy - ph], [cx + pw, cy], [cx, cy + ph], [cx - pw, cy]];
        const p = m.byIndex[o], dim = focus != null && o !== focus;
        ctx.strokeStyle = rgb(mix(p.rgb, [255, 255, 255], dim ? 0 : 0.15), dim ? 0.35 : 1);
        ctx.lineWidth = Math.max(1.2, v.hw / (dim ? 7 : 4.5));
        for (const [dx, dy, a, b] of edges) {
          const j = m.tileAt.get(((t.x + dx + m.W) % m.W) + "," + (t.y + dy));
          if (j == null || own[j] !== o) { ctx.beginPath(); ctx.moveTo(...pts[a]); ctx.lineTo(...pts[b]); ctx.stroke(); }
        }
      }
    }
    const turn = m.turns[ti];
    if (opts.units !== false && v.hw >= 5) {
      const r = Math.min(3.5, Math.max(1.5, v.hw / 5));
      const towns = new Set(turn.cities.map(c => c[0] + "," + c[1]));
      for (const [x0, y, o, civil, mil] of turn.units) {
        if (!mil || towns.has(x0 + "," + y)) continue;    // garrisons are implied by the city
        const x = m.sx(x0), [cx, cy] = this.center(x, y, v), p = m.byIndex[o];
        if (!p || cx < 0 || cy < 0 || cx > w || cy > h) continue;
        const dim = focus != null && o !== focus;
        ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7);
        ctx.fillStyle = rgb(p.rgb, dim ? 0.3 : 1); ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.stroke();
      }
    }
    // Cities, biggest last so they sit on top; labels placed greedily, highest priority first.
    const cities = turn.cities.map(([x, y, name, o, size, cap]) => ({x: m.sx(x), y, name, o, size, cap}))
      .sort((a, b) => a.size - b.size);
    const placed = [];
    for (const c of cities) {
      const [cx, cy] = this.center(c.x, c.y, v), p = m.byIndex[c.o];
      if (!p || cx < -20 || cy < -20 || cx > w + 20 || cy > h + 20) continue;
      const dim = focus != null && c.o !== focus;
      const r = Math.max(2.5, v.hw * (0.32 + Math.min(c.size, 12) * 0.035)) * (dim ? 0.7 : 1);
      if (!dim) { ctx.beginPath(); ctx.arc(cx, cy, r + 1.5, 0, 7); ctx.fillStyle = "rgba(8,10,14,0.9)"; ctx.fill(); }
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fillStyle = rgb(p.rgb, dim ? 0.45 : 1); ctx.fill();
      if (c.cap) {
        ctx.beginPath(); star(ctx, cx, cy, r * 0.62); ctx.fillStyle = dim ? "rgba(255,255,255,0.4)" : "#fff"; ctx.fill();
      }
      c.cx = cx; c.cy = cy; c.r = r; c.dim = dim;
    }
    const mode = opts.labels || "auto";
    if (mode !== "none") {
      const own = opts.ownLabels && focus != null;
      const minSize = mode === "all" ? 0 : v.hw >= 22 ? 0 : v.hw >= 14 ? 5 : v.hw >= 10 ? 8 : 99;
      const ownMin = v.hw >= 10 ? 0 : v.hw >= 7 ? 4 : 7;
      const order = cities.filter(c => c.cx != null && (own ? !c.dim && (c.cap || c.size >= ownMin)
          : (c.cap || c.size >= minSize) && !(c.dim && !c.cap)))
        .sort((a, b) => (b.cap - a.cap) || (b.size - a.size));
      const fs = Math.round(Math.max(10, Math.min(13, v.hw * 0.9)));
      for (const c of order) {
        ctx.font = `${c.cap ? 700 : 500} ${fs}px system-ui,-apple-system,Segoe UI,sans-serif`;
        const text = c.name + (c.size ? " " + c.size : ""), tw = ctx.measureText(text).width;
        const tries = [[0, -c.r - 4, "center", "bottom"], [0, c.r + 3, "center", "top"],
          [c.r + 4, 0, "left", "middle"], [-c.r - 4, 0, "right", "middle"]];
        for (const [dx, dy, align, base] of tries) {
          const x0 = align === "center" ? c.cx + dx - tw / 2 : align === "left" ? c.cx + dx : c.cx + dx - tw;
          const y0 = base === "bottom" ? c.cy + dy - fs : base === "top" ? c.cy + dy : c.cy - fs / 2;
          const box = [x0 - 2, y0 - 1, x0 + tw + 2, y0 + fs + 1];
          if (box[0] < 0 || box[1] < 0 || box[2] > w || box[3] > h) continue;
          if (placed.some(b => b[0] < box[2] && box[0] < b[2] && b[1] < box[3] && box[1] < b[3])) continue;
          if (v.hw >= 10 && cities.some(o => o !== c && o.cx != null && !o.dim && o.cx + o.r > box[0] && o.cx - o.r < box[2]
              && o.cy + o.r > box[1] && o.cy - o.r < box[3])) continue;
          placed.push(box);
          ctx.textAlign = align; ctx.textBaseline = base; ctx.lineJoin = "round";
          ctx.lineWidth = 3; ctx.strokeStyle = "rgba(6,8,12,0.92)"; ctx.strokeText(text, c.cx + dx, c.cy + dy);
          ctx.fillStyle = c.cap ? "#ffffff" : "#d9dce2"; ctx.fillText(text, c.cx + dx, c.cy + dy);
          break;
        }
      }
    }
    for (const pl of opts.pulses || []) {
      const [cx, cy] = this.center(pl.x, pl.y, v), r = v.hw * (0.8 + pl.t * 2.2);
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7);
      ctx.strokeStyle = rgb(pl.color, 1 - pl.t); ctx.lineWidth = 2.5; ctx.stroke();
    }
    return cities;
  }
  // The tile under a canvas point, or null.
  pick(px, py, v) {
    const gx = px / v.hw + v.box.x0, gy = py / (v.hw / 2) + v.box.y0;
    let best = null, bd = 9;
    for (let x = Math.floor(gx) - 1; x <= Math.ceil(gx) + 1; x++)
      for (let y = Math.floor(gy) - 1; y <= Math.ceil(gy) + 1; y++) {
        const i = this.m.tileAt.get(((x + this.m.W) % this.m.W) + "," + y);
        if (i == null) continue;
        const d = Math.abs(gx - x) + Math.abs(gy - y) / 1;
        if (d < bd) { bd = d; best = i; }
      }
    return best;
  }
}

function star(ctx, cx, cy, r) {
  for (let i = 0; i < 10; i++) {
    const a = -Math.PI / 2 + i * Math.PI / 5, rr = i % 2 ? r * 0.45 : r;
    i ? ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr) : ctx.moveTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
  }
  ctx.closePath();
}

// Fit a tile box into a w x h canvas: the half-tile width that fits, and the box grown to the canvas aspect.
function fitView(box, w, h) {
  const bw = box.x1 - box.x0 + 1, bh = (box.y1 - box.y0 + 1) / 2;
  const hw = Math.min(w / bw, h / bh);
  const extraX = (w / hw - bw) / 2, extraY = (h / hw - bh);
  return {hw, box: {x0: box.x0 - extraX, y0: box.y0 - extraY, x1: box.x1 + extraX, y1: box.y1 + extraY}};
}

// Pixel-ratio aware canvas sizing; returns the CSS size.
function sizeCanvas(canvas) {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, w, h, dpr};
}

// A small SVG sparkline path.
function sparkPath(values, w, h, max) {
  const n = values.length; if (n < 2) return "";
  const top = max || Math.max(1, ...values);
  return values.map((v, i) => `${i ? "L" : "M"}${(i / (n - 1) * w).toFixed(1)},${(h - v / top * h).toFixed(1)}`).join("");
}
