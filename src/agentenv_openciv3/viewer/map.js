// The play UI's map (docs/play.md): what one seat knows of the world, drawn on a canvas. No libraries.
"use strict";

const TERRAIN = {
  ocean: [24, 44, 74], sea: [30, 54, 88], coast: [48, 84, 122], grassland: [92, 128, 72], plains: [148, 138, 86],
  desert: [184, 166, 118], tundra: [146, 152, 146], floodplain: [110, 140, 80], hills: [124, 110, 80],
  mountains: [116, 110, 106], forest: [54, 92, 60], jungle: [46, 100, 72], marsh: [84, 106, 96], volcano: [102, 68, 60],
};
const WATER = new Set(["ocean", "sea", "coast"]);
// Directions on the diamond grid (x + y is always even): north is (x, y - 2), north-east (x + 1, y - 1), …
const DIRS = {N: [0, -2], NE: [1, -1], E: [2, 0], SE: [1, 1], S: [0, 2], SW: [-1, 1], W: [-2, 0], NW: [-1, -1]};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const hexRgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const mix = (a, b, t) => [0, 1, 2].map(i => Math.round(a[i] + (b[i] - a[i]) * t));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function sizeCanvas(canvas) {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, w, h, dpr};
}

// A unit type as two letters on its marker: "Warrior" -> "Wa", "Medieval Infantry" -> "MI".
function abbrev(type) {
  const words = String(type || "?").split(/\s+/);
  return words.length > 1 ? (words[0][0] + words[1][0]).toUpperCase() : words[0].slice(0, 2);
}
const CIVILIAN = new Set(["Settler", "Worker", "Explorer", "Galley", "Caravel", "Curragh", "Galleon", "Transport", "Leader"]);

// The seat's world: tiles it knows, by position, plus cities and units, from the bridge's known_map.
class World {
  constructor(map, colors) { this.update(map, colors); }
  update(map, colors) {
    this.W = map.width; this.H = map.height; this.wrap = !!map.wrap_x; this.turn = map.turn;
    this.version = (this.version || 0) + 1;   // a new picture to draw
    this.players = Object.fromEntries(map.players.map(p => [p.index, p]));
    this.me = map.players.find(p => p.me)?.index;
    this.colors = Object.fromEntries(Object.entries(colors || {}).map(([k, v]) => [k, hexRgb(v)]));
    this.tiles = new Map();
    for (const [x, y, terrain, overlay, river, owner, visible, resource, improvements, bonus] of map.tiles)
      this.tiles.set(x * 4096 + y, {x, y, terrain, overlay, river, owner, visible, resource, improvements: improvements || [], bonus: !!bonus});
    this.cities = new Map(map.cities.map(c => [c.x * 4096 + c.y, c]));
    this.units = new Map();
    for (const u of map.units) {
      const k = u.x * 4096 + u.y;
      if (!this.units.has(k)) this.units.set(k, []);
      this.units.get(k).push(u);
    }
    this.rivers = this._rivers();
  }
  // River tiles only carry a flag, and joining every pair of river neighbours draws a lattice of little diamonds, so
  // join them with a spanning forest (a branching line) and cut its corners at the segments' midpoints, as the
  // viewer does. Each line is [tile key, x0, y0, x1, y1] in tile coordinates, drawn relative to that tile.
  _rivers() {
    const keys = [...this.tiles.keys()].filter(k => this.tiles.get(k).river), par = new Map(keys.map(k => [k, k]));
    const root = k => { while (par.get(k) !== k) { par.set(k, par.get(par.get(k))); k = par.get(k); } return k; };
    const mids = new Map(), add = (k, m) => { if (!mids.has(k)) mids.set(k, []); mids.get(k).push(m); };
    for (const k of keys) {
      const t = this.tiles.get(k);
      for (const [dx, dy] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
        const j = this.key(t.x + dx, t.y + dy), n = this.tiles.get(j);
        if (!n || !n.river) continue;
        const a = root(k), b = root(j);
        if (a === b) continue;
        par.set(a, b);
        add(k, [t.x + dx / 2, t.y + dy / 2]); add(j, [n.x - dx / 2, n.y - dy / 2]);
      }
    }
    const lines = [];
    for (const [k, ms] of mids) {
      const t = this.tiles.get(k);
      if (ms.length === 2) lines.push([k, ...ms[0], ...ms[1]]); else for (const m of ms) lines.push([k, t.x, t.y, ...m]);
    }
    return lines;
  }
  key(x, y) { return this.wx(x) * 4096 + y; }
  wx(x) { return this.wrap ? ((x % this.W) + this.W) % this.W : x; }
  tile(x, y) { return this.tiles.get(this.key(x, y)); }
  city(x, y) { return this.cities.get(this.key(x, y)); }
  unitsAt(x, y) { return this.units.get(this.key(x, y)) || []; }
  color(i) { return this.colors[i] || (this.players[i]?.barbarian ? [107, 111, 120] : [150, 150, 150]); }
  civ(i) { return this.players[i]?.civ ?? "?"; }
  step(x, y, dir) { const [dx, dy] = DIRS[dir]; return [this.wx(x + dx), y + dy]; }
  // The bounding box of the tiles the seat knows, for the first view.
  bounds() {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const t of this.tiles.values()) { x0 = Math.min(x0, t.x); y0 = Math.min(y0, t.y); x1 = Math.max(x1, t.x); y1 = Math.max(y1, t.y); }
    return x0 > x1 ? null : {x0, y0, x1, y1};
  }
}

// The camera: hw is half a tile's width in pixels; (cx, cy) the tile at the canvas centre.
class Camera {
  constructor() { this.hw = 44; this.cx = 0; this.cy = 0; }   // tiles 88 px wide, near the game's 128
  screen(x, y, w, h, W) {   // the copy of x nearest the camera, round the wrap
    let dx = x - this.cx;
    if (W) dx = ((dx % W) + W + W / 2) % W - W / 2;
    return [w / 2 + dx * this.hw, h / 2 + (y - this.cy) * this.hw / 2];
  }
  tileAt(px, py, w, h, world) {   // the tile under a canvas point, or null
    const gx = this.cx + (px - w / 2) / this.hw, gy = this.cy + (py - h / 2) / (this.hw / 2);
    let best = null, bd = 1.0001;
    for (let x = Math.floor(gx) - 1; x <= Math.ceil(gx) + 1; x++)
      for (let y = Math.floor(gy) - 1; y <= Math.ceil(gy) + 1; y++) {
        if (((x + y) % 2 + 2) % 2) continue;
        const d = Math.abs(gx - x) + Math.abs(gy - y);
        if (d < bd && y >= 0 && y < world.H) { bd = d; best = [world.wx(x), y]; }
      }
    return best;
  }
}

class PlayPainter {
  constructor() { this.t0 = performance.now(); }
  diamond(ctx, cx, cy, hw, s = 1) {
    const w = hw * s, h = hw / 2 * s;
    ctx.moveTo(cx, cy - h); ctx.lineTo(cx + w, cy); ctx.lineTo(cx, cy + h); ctx.lineTo(cx - w, cy); ctx.closePath();
  }
  terrainColor(t) {
    const c = TERRAIN[t.overlay || t.terrain] || TERRAIN[t.terrain] || [100, 100, 100];
    return mix(c, [0, 0, 0], ((t.x * 7 + t.y * 13) % 5) * 0.02);
  }
  /* o: {selected: unit, targets: [{x,y,win_chance}], path: [[x,y]...], sites: [{x,y,score}], hover: [x,y],
         cityRadius: city, mode} */
  draw(ctx, world, cam, w, h, o = {}) {
    const hw = cam.hw, W = world.wrap ? world.W : 0;
    ctx.fillStyle = "#06080b"; ctx.fillRect(0, 0, w, h);
    const tiles = [];
    for (const t of world.tiles.values()) {
      const [sx, sy] = cam.screen(t.x, t.y, w, h, W);
      if (sx < -hw * 1.2 || sy < -hw || sx > w + hw * 1.2 || sy > h + hw) continue;
      tiles.push([t, sx, sy]);
    }
    // terrain, tinted by the owner, darker where the seat can't see now (fog of war)
    for (const [t, sx, sy] of tiles) {
      let c = this.terrainColor(t);
      if (t.owner >= 0) c = mix(c, world.color(t.owner), WATER.has(t.terrain) ? 0.18 : 0.3);
      if (!t.visible) c = mix(c, [8, 10, 14], 0.45);
      ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 1.02); ctx.fillStyle = rgb(c); ctx.fill();
    }
    if (hw >= 14) for (const [t, sx, sy] of tiles) this._detail(ctx, t, sx, sy, hw);
    // rivers: the world's branching lines, each drawn from its tile's place on screen
    const onScreen = new Map(tiles.map(([t, sx, sy]) => [world.key(t.x, t.y), [t, sx, sy]]));
    ctx.strokeStyle = "rgba(64,98,132,.95)"; ctx.lineWidth = clamp(hw / 10, 1, 3); ctx.lineCap = ctx.lineJoin = "round";
    ctx.beginPath();
    for (const [k, ax, ay, bx, by] of world.rivers) {
      const o = onScreen.get(k); if (!o) continue;
      const [t, sx, sy] = o;
      ctx.moveTo(sx + (ax - t.x) * hw, sy + (ay - t.y) * hw / 2); ctx.lineTo(sx + (bx - t.x) * hw, sy + (by - t.y) * hw / 2);
    }
    ctx.stroke();
    // borders between owners
    ctx.lineCap = "round";
    const edges = [[1, -1, 0, 1], [1, 1, 1, 2], [-1, 1, 2, 3], [-1, -1, 3, 0]];
    for (const [t, sx, sy] of tiles) {
      if (t.owner < 0) continue;
      const pw = hw * 0.92, ph = hw / 2 * 0.92, pts = [[sx, sy - ph], [sx + pw, sy], [sx, sy + ph], [sx - pw, sy]];
      ctx.strokeStyle = rgb(mix(world.color(t.owner), [255, 255, 255], 0.2), t.visible ? 1 : 0.6);
      ctx.lineWidth = clamp(hw / 7, 1.2, 3.5);
      for (const [dx, dy, a, b] of edges) {
        const n = world.tile(t.x + dx, t.y + dy);
        if (!n || n.owner !== t.owner) { ctx.beginPath(); ctx.moveTo(...pts[a]); ctx.lineTo(...pts[b]); ctx.stroke(); }
      }
    }
    this.overlays(ctx, world, cam, w, h, o, tiles);
    this.unitsOnly(ctx, world, cam, w, h, o, tiles);
  }
  // What a seat's orders need on the map, under the units: good city sites, the city's radius, the path, attack
  // targets, the tile under the mouse. ArtPainter draws these too.
  overlays(ctx, world, cam, w, h, o, tiles = null) {
    const hw = cam.hw, W = world.wrap ? world.W : 0;
    for (const s of o.sites || []) {
      const [sx, sy] = cam.screen(s.x, s.y, w, h, W);
      ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 0.8); ctx.strokeStyle = "rgba(255,226,140,.85)"; ctx.lineWidth = 2;
      ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);
      if (hw >= 16) this._tag(ctx, sx, sy + hw * 0.18, String(s.score), "#ffe28c");
    }
    if (o.cityRadius && tiles) {   // ArtPainter draws the client's outline instead
      const c = o.cityRadius;
      // the 21 tiles a city works: the 5x5 square around it in the game's own grid, without its corners
      for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
        if ((dx + dy) & 1) continue;
        const a = Math.abs((dx + dy) / 2), b = Math.abs((dy - dx) / 2);
        if (Math.max(a, b) > 2 || (a === 2 && b === 2)) continue;
        const [sx, sy] = cam.screen(c.x + dx, c.y + dy, w, h, W);
        ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 0.96); ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.lineWidth = 1; ctx.stroke();
      }
    }
    if (o.path && o.path.length > 1) {
      ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.lineWidth = 2; ctx.setLineDash([6, 5]); ctx.beginPath();
      o.path.forEach(([x, y], k) => { const [sx, sy] = cam.screen(x, y, w, h, W); k ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
      ctx.stroke(); ctx.setLineDash([]);
    }
    for (const tg of o.targets || []) {
      const [sx, sy] = cam.screen(tg.x, tg.y, w, h, W);
      ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 0.92); ctx.fillStyle = "rgba(240,80,60,.22)"; ctx.fill();
      ctx.strokeStyle = "rgba(255,110,90,.95)"; ctx.lineWidth = 2; ctx.stroke();
      if (tg.win_chance != null) this._tag(ctx, sx, sy - hw * 0.62, `${Math.round(tg.win_chance * 100)}%`, "#ffb4a6");
    }
    if (o.hover) {
      const [sx, sy] = cam.screen(o.hover[0], o.hover[1], w, h, W);
      ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 0.98); ctx.strokeStyle = "rgba(255,255,255,.7)"; ctx.lineWidth = 1.5; ctx.stroke();
    }
  }
  // Cities (when `tiles` are given: the plain map), units, the active unit's ring, then city labels on top.
  unitsOnly(ctx, world, cam, w, h, o, tiles = null) {
    const hw = cam.hw, W = world.wrap ? world.W : 0, labels = [];
    if (tiles) for (const [t, sx, sy] of tiles) {
      const c = world.city(t.x, t.y);
      if (c) labels.push(this._city(ctx, world, c, sx, sy, hw));
    }
    tiles = tiles || this._onScreen(world, cam, w, h);
    const sel = o.selected;
    for (const [t, sx, sy] of tiles) {
      if (!t.visible) continue;
      const us = world.unitsAt(t.x, t.y);
      if (us.length) this._units(ctx, world, us, sx, sy, hw, !!world.city(t.x, t.y), sel);
    }
    if (sel) {   // the active unit: a pulsing ring, as the game blinks it
      const [sx, sy] = cam.screen(sel.x, sel.y, w, h, W);
      const p = 0.5 + 0.5 * Math.sin((performance.now() - this.t0) / 180);
      ctx.beginPath(); this.diamond(ctx, sx, sy, hw, 1.0); ctx.strokeStyle = `rgba(255,255,255,${0.45 + 0.55 * p})`;
      ctx.lineWidth = 2.5; ctx.stroke();
    }
    for (const l of labels) l();
  }
  _onScreen(world, cam, w, h) {
    const hw = cam.hw, W = world.wrap ? world.W : 0, out = [];
    for (const t of world.tiles.values()) {
      const [sx, sy] = cam.screen(t.x, t.y, w, h, W);
      if (sx < -hw * 1.2 || sy < -hw || sx > w + hw * 1.2 || sy > h + hw) continue;
      out.push([t, sx, sy]);
    }
    return out;
  }
  _detail(ctx, t, sx, sy, hw) {   // improvements and resources, once tiles are big enough to read
    const imp = t.improvements;
    if (imp.includes("road")) {
      ctx.strokeStyle = "rgba(120,90,60,.9)"; ctx.lineWidth = Math.max(1.5, hw / 12);
      ctx.beginPath(); ctx.moveTo(sx - hw * 0.35, sy); ctx.lineTo(sx + hw * 0.35, sy); ctx.stroke();
    }
    if (imp.includes("mine")) { ctx.fillStyle = "rgba(40,30,20,.75)"; ctx.fillRect(sx - hw * 0.12, sy + hw * 0.08, hw * 0.24, hw * 0.14); }
    if (imp.includes("irrigation")) {
      ctx.strokeStyle = "rgba(110,190,240,.8)"; ctx.lineWidth = 1;
      for (const o of [-0.15, 0.15]) { ctx.beginPath(); ctx.moveTo(sx - hw * 0.3, sy + hw * o); ctx.lineTo(sx + hw * 0.3, sy + hw * o); ctx.stroke(); }
    }
    if (imp.includes("barbarian_camp")) this._tag(ctx, sx, sy, "camp", "#e0a0a0");
    if (t.resource) {
      ctx.beginPath(); ctx.arc(sx - hw * 0.42, sy - hw * 0.05, Math.max(2, hw / 9), 0, 7);
      ctx.fillStyle = "#f1d36b"; ctx.fill(); ctx.strokeStyle = "rgba(0,0,0,.6)"; ctx.lineWidth = 1; ctx.stroke();
    }
  }
  _tag(ctx, x, y, text, color) {
    ctx.font = "600 11px system-ui,sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.strokeText(text, x, y); ctx.fillStyle = color; ctx.fillText(text, x, y);
  }
  _city(ctx, world, c, sx, sy, hw) {
    const col = world.color(c.owner), mine = c.owner === world.me, r = Math.max(5, hw * 0.42);
    ctx.beginPath(); ctx.rect(sx - r, sy - r * 0.62, r * 2, r * 1.24);
    ctx.fillStyle = rgb(mix(col, [0, 0, 0], 0.15)); ctx.fill();
    ctx.strokeStyle = mine ? "#fff" : "rgba(0,0,0,.8)"; ctx.lineWidth = 2; ctx.stroke();
    // a few roofs, so a city reads as a town rather than a unit
    ctx.fillStyle = "rgba(255,255,255,.55)";
    for (const dx of [-0.5, 0, 0.5]) { ctx.beginPath(); ctx.moveTo(sx + dx * r - r * 0.22, sy); ctx.lineTo(sx + dx * r, sy - r * 0.36); ctx.lineTo(sx + dx * r + r * 0.22, sy); ctx.fill(); }
    return () => {   // the banner: size, name and, for your own, what it builds and when
      const fs = Math.round(clamp(hw * 0.5, 10, 14));
      ctx.font = `700 ${fs}px system-ui,sans-serif`;
      const name = c.name, size = String(c.size), nw = ctx.measureText(name).width, bw = nw + fs * 1.9 + 8;
      const bx = sx - bw / 2, by = sy + r * 0.62 + 3, bh = fs + 6;
      ctx.fillStyle = "rgba(10,12,16,.82)"; ctx.fillRect(bx, by, bw, bh);
      ctx.fillStyle = rgb(col); ctx.fillRect(bx, by, fs * 1.6, bh);
      ctx.textBaseline = "middle"; ctx.textAlign = "center"; ctx.fillStyle = "#fff";
      ctx.fillText(size, bx + fs * 0.8, by + bh / 2);
      ctx.textAlign = "left"; ctx.fillText(name, bx + fs * 1.6 + 4, by + bh / 2);
      if (c.capital) { ctx.fillStyle = "#ffd75e"; ctx.fillText("★", bx + bw + 2, by + bh / 2); }
      if (mine && c.producing && hw >= 16) {
        ctx.font = `500 ${fs - 2}px system-ui,sans-serif`; ctx.textAlign = "center"; ctx.lineWidth = 3;
        const sub = `${c.producing}${c.turns_to_complete != null ? ` · ${c.turns_to_complete}` : ""}`;
        ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.strokeText(sub, sx, by + bh + fs / 2 + 2);
        ctx.fillStyle = "#cfd3da"; ctx.fillText(sub, sx, by + bh + fs / 2 + 2);
      }
    };
  }
  _units(ctx, world, us, sx, sy, hw, inCity, sel) {
    // the top unit of the stack: the selected one if it's here, else the seat's own, else the first
    const mineHere = us.filter(u => u.owner === world.me);
    const top = (sel && mineHere.find(u => u.id === sel.id)) || mineHere[0] || us[0];
    const count = us.reduce((n, u) => n + (u.count || 1), 0);
    const r = clamp(hw * 0.3, 4, 13), x = inCity ? sx + hw * 0.5 : sx, y = inCity ? sy - hw * 0.25 : sy - hw * 0.06;
    const col = world.color(top.owner), civilian = CIVILIAN.has(top.type);
    ctx.beginPath();
    if (civilian) ctx.rect(x - r, y - r, r * 2, r * 2); else ctx.arc(x, y, r, 0, 7);
    ctx.fillStyle = rgb(col); ctx.fill();
    ctx.lineWidth = top.owner === world.me ? 2 : 1.5; ctx.strokeStyle = top.owner === world.me ? "#fff" : "rgba(0,0,0,.9)"; ctx.stroke();
    if (hw >= 12) {
      ctx.font = `700 ${Math.round(r * 0.95)}px system-ui,sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillStyle = mix(col, [0, 0, 0], 0).reduce((a, v) => a + v, 0) > 420 ? "#111" : "#fff";
      ctx.fillText(abbrev(top.type), x, y + 0.5);
    }
    if (count > 1) this._tag(ctx, x + r + 4, y - r + 1, String(count), "#fff");
  }
  // The minimap: every known tile as a dot, and the camera's frame.
  minimap(ctx, world, cam, w, h, viewW, viewH) {
    ctx.fillStyle = "#06080b"; ctx.fillRect(0, 0, w, h);
    const sx = w / world.W, sy = h / world.H;
    for (const t of world.tiles.values()) {
      let c = WATER.has(t.terrain) ? [36, 60, 96] : [96, 120, 80];
      if (t.owner >= 0) c = world.color(t.owner);
      if (!t.visible) c = mix(c, [0, 0, 0], 0.4);
      ctx.fillStyle = rgb(c); ctx.fillRect(Math.floor(t.x * sx), Math.floor(t.y * sy), Math.ceil(sx * 2), Math.ceil(sy * 2));
    }
    const fw = viewW / cam.hw * sx, fh = viewH / (cam.hw / 2) * sy;
    const fx = (((cam.cx * sx - fw / 2) % w) + w) % w, fy = cam.cy * sy - fh / 2;
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5;
    if (fw >= w) { ctx.strokeRect(0.75, fy, w - 1.5, fh); return; }   // the view is wider than the world
    ctx.strokeRect(fx, fy, fw, fh);
    if (fx + fw > w) ctx.strokeRect(fx - w, fy, fw, fh);
  }
}
