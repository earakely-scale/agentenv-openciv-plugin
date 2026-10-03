// The play map in the OpenCiv3 client's own art (docs/play.md, section 6), when the env has it (webart.py). It
// draws what the client draws, the way it draws it: the numbers below are the client's (vendor/OpenCiv3/C7, MapView.cs
// and the Map/ layers), at zoom 1 where a tile is 128x64. Needs map.js.
"use strict";

// ---- loading the art ----

class Art {
  static async load(base) {
    const r = await fetch(base + "manifest.json", {cache: "no-cache"});
    if (!r.ok) throw new Error(`no art (${r.status})`);
    const art = new Art(base, await r.json());
    await Promise.all(Object.keys(art.m.sheets).map(n => art._image(n)));
    await art._fonts();
    return art;
  }
  constructor(base, manifest) {
    this.base = base; this.m = manifest; this.images = {}; this.units = new Map(); this.tinted = new Map();
    this.recolored = new Map(); this.font = "system-ui, sans-serif";
  }
  url(path) { return `${this.base}${path}?v=${this.m.id}`; }
  _image(name) {
    return new Promise((ok, fail) => {
      const im = new Image();
      im.onload = () => { this.images[name] = im; ok(im); };
      im.onerror = () => fail(new Error(`art: ${name} did not load`));
      im.src = this.url(this.m.sheets[name]);
    });
  }
  async _fonts() {
    for (const f of this.m.fonts || []) {
      try {
        const face = new FontFace("C7 Noto Sans", `url(${this.url("fonts/" + f)})`, {weight: f.includes("Bold") ? "700" : "400"});
        document.fonts.add(await face.load());
        this.font = "'C7 Noto Sans', system-ui, sans-serif";
      } catch (e) { /* the system font will do */ }
    }
  }
  img(name) { return this.images[name]; }
  // A unit type's sheets, loaded on first use: null while loading or when the type has no art.
  unit(type, redraw) {
    const spec = this.m.units[type];
    if (!spec) return null;
    let u = this.units.get(type);
    if (!u) {
      u = {spec, anims: {}, ready: false};
      this.units.set(type, u);
      const loads = [];
      for (const [name, a] of Object.entries(spec.animations)) {
        const anim = {...a, sheet: new Image(), mask: a.mask ? new Image() : null};
        u.anims[name] = anim;
        loads.push(new Promise(ok => { anim.sheet.onload = ok; anim.sheet.onerror = ok; anim.sheet.src = this.url(a.sheet); }));
        if (anim.mask) loads.push(new Promise(ok => { anim.mask.onload = ok; anim.mask.onerror = ok; anim.mask.src = this.url(a.mask); }));
      }
      Promise.all(loads).then(() => { u.ready = true; redraw && redraw(); });
    }
    return u.ready ? u : null;
  }
  // An image with the template colours swapped for a civ's, cached (Util.TransformColors: exact RGBA matches).
  recolor(name, swaps, key) {
    const k = `${name}|${key}`;
    let c = this.recolored.get(k);
    if (c) return c;
    const im = this.img(name);
    c = document.createElement("canvas"); c.width = im.width; c.height = im.height;
    const g = c.getContext("2d"); g.drawImage(im, 0, 0);
    const d = g.getImageData(0, 0, c.width, c.height), p = d.data;
    for (let i = 0; i < p.length; i += 4) {
      for (const [from, to] of swaps)
        if (p[i] === from[0] && p[i + 1] === from[1] && p[i + 2] === from[2] && p[i + 3] === from[3]) {
          p[i] = to[0]; p[i + 1] = to[1]; p[i + 2] = to[2]; p[i + 3] = 255; break;
        }
    }
    g.putImageData(d, 0, 0);
    this.recolored.set(k, c);
    return c;
  }
}

// ---- the client's rules ----

const ART_WATER = new Set(["coast", "sea", "ocean"]);
const HILLY = new Set(["hills", "mountains", "volcano"]);
// Base terrain: the sheet for the four tiles around a tile's north corner, and the cell (TerrainTextureFiles.cs).
const BASE_ROLE = {xtgc: ["tundra", "grassland"], xpgc: ["plains", "grassland"], xdgc: ["desert", "grassland"],
  xdpc: ["desert", "plains"], xdgp: ["desert", "grassland"], wCSO: ["coast", "sea"]};
const BASE_MID = [1, 3, 9, 27], BASE_END = [2, 6, 18, 54];   // by corner: N, NW, NE, the tile itself
const RES_ICON = {Horses: 0, Iron: 1, Saltpeter: 2, Coal: 3, Oil: 4, Rubber: 5, Aluminum: 6, Uranium: 7, Wines: 8,
  Furs: 9, Dyes: 10, Incense: 11, Spices: 12, Ivory: 13, Silks: 14, Gems: 15, Whales: 16, Game: 17, Fish: 18,
  Cattle: 19, Wheat: 20, Gold: 21, "Tropical Fruit": 22, Oasis: 23, Sugar: 24, Tobacco: 25};
const ROAD_BIT = {NE: 1, E: 2, SE: 4, S: 8, SW: 16, W: 32, NW: 64, N: 128};
// River edges in known_map's `river` mask (docs/protocol.md, known_map).
const RIVER = {NE: 1, SE: 2, SW: 4, NW: 8};
const hash = (x, y, salt = 0) => {   // a stable stand-in for the client's per-session random variants
  let h = (x * 374761393 + y * 668265263 + salt * 2246822519) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return (h ^ (h >>> 16)) >>> 0;
};

class ArtPainter {
  constructor(art) {
    this.art = art; this.t0 = performance.now(); this.plain = new PlayPainter();
    this.cache = null; this.cacheKey = "";
  }
  // The same calls as PlayPainter: draw() and minimap().
  minimap(...a) { this.plain.minimap(...a); }

  // ---- the static picture (terrain to fog), cached until the world or the camera changes ----
  // It is drawn as the client draws it, at the art's own scale (tiles 128x64, every sprite on whole pixels, so the
  // semi-transparent fog and borders meet without seams), into an offscreen map, which is then scaled to the screen
  // once. Zoomed far out, the map is drawn at half scale to keep it small.
  draw(ctx, world, cam, w, h, o = {}) {
    const z = cam.hw / 64, dpr = window.devicePixelRatio || 1, scale = z < 0.5 ? 0.5 : 1;
    this.zs = z;
    const key = [world.version, cam.cx.toFixed(4), cam.cy.toFixed(4), cam.hw.toFixed(3), w, h, dpr, o.cityRadius?.id ?? ""].join();
    if (key !== this.cacheKey || !this.cache) {
      if (!this.cache) this.cache = document.createElement("canvas");
      // map pixels per screen pixel: 1/z; the offscreen map covers the screen plus a margin for the fractional origin
      const MW = Math.ceil(w / z) + 2, MH = Math.ceil(h / z) + 2;
      this.cache.width = Math.ceil(MW * scale); this.cache.height = Math.ceil(MH * scale);
      const ox = MW / 2 - 64 * cam.cx, oy = MH / 2 - 32 * cam.cy;   // where map x = 0 falls, in map pixels
      this.origin = {ox: Math.floor(ox), oy: Math.floor(oy), fx: ox - Math.floor(ox), fy: oy - Math.floor(oy), MW, MH};
      const g = this.cache.getContext("2d"); g.setTransform(scale, 0, 0, scale, 0, 0);
      g.imageSmoothingEnabled = scale < 1;
      this._static(g, world, cam, MW, MH, o);
      this.cacheKey = key; this.scale = scale;
    }
    // map pixel (mx, my) is at screen ((mx + fx) - MW/2) * z + w/2 ...: place the map so the camera centre is centred
    const {fx, fy, MW, MH} = this.origin;
    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(this.cache, 0, 0, this.cache.width, this.cache.height,
      w / 2 + (fx - MW / 2) * z, h / 2 + (fy - MH / 2) * z, this.cache.width / this.scale * z, this.cache.height / this.scale * z);
    ctx.restore();
    this.plain.overlays(ctx, world, cam, w, h, o);
    this._units(ctx, world, cam, w, h, o);
  }

  // The tiles in view, row by row (the client's order), with their centres in the offscreen map, and every map
  // position in view.
  _view(world, cam, MW, MH) {
    const W = world.wrap ? world.W : 0, {ox, oy} = this.origin;
    const gx0 = Math.floor(cam.cx - MW / 128) - 3, gx1 = Math.ceil(cam.cx + MW / 128) + 3;
    const gy0 = Math.max(0, Math.floor(cam.cy - MH / 64) - 4), gy1 = Math.min(world.H - 1, Math.ceil(cam.cy + MH / 64) + 4);
    const known = [], all = [];
    for (let y = gy0; y <= gy1; y++)
      for (let vx = gx0 + (((gx0 + y) % 2) + 2) % 2; vx <= gx1; vx += 2) {
        if (!W && (vx < 0 || vx >= world.W)) continue;
        const sx = ox + 64 * vx, sy = oy + 32 * y;
        const t = world.tile(vx, y), p = {x: world.wx(vx), y, vx, sx, sy, t};
        all.push(p); if (t) known.push(p);
      }
    return {z: 1, known, all};
  }
  // drawImage of a cell, placed relative to a tile centre in zoom-1 pixels.
  _blit(g, im, sx, sy, sw, sh, cx, cy, ox, oy, z, grow = 0) {
    if (!im) return;
    g.drawImage(im, sx, sy, sw, sh, cx + ox * z - grow / 2, cy + oy * z - grow / 2, sw * z + grow, sh * z + grow);
  }

  _static(g, world, cam, w, h, o) {
    const art = this.art, {z, known, all} = this._view(world, cam, w, h);
    g.fillStyle = "#000"; g.fillRect(0, 0, w, h);
    const T = (x, y) => world.tile(x, y);
    const ov = t => t ? (t.overlay || t.terrain) : "";
    // 1. base terrain: a sprite at the north corner of each tile, and of its S, SW and SE neighbours
    for (const p of known)
      for (const [dx, dy] of [[0, 0], [0, 2], [-1, 1], [1, 1]]) {
        const y = p.y + dy; if (y >= world.H) continue;
        const s = this._base(world, p.x + dx, y); if (!s) continue;
        this._blit(g, art.img(s.file), (s.idx % 9) * 128, Math.floor(s.idx / 9) * 64, 128, 64,
          p.sx + dx * 64 * z, p.sy + dy * 32 * z, -64, -64, z);
      }
    // 2. rivers, centred on each tile's east corner, from the river edges of the four tiles round it
    for (const p of known) {
      const i = this._river(world, p.x, p.y); if (!i) continue;
      this._blit(g, art.img("rivers"), (i % 4) * 128, (i >> 2) * 64, 128, 64, p.sx, p.sy, 0, -32, z);
    }
    // 3-5. forest and jungle, marsh, then hills, mountains and volcanoes, each over every tile in turn
    const edgeWater = (x, y) => ["NE", "NW", "SE", "SW"].some(d => { const n = T(x + DIRS[d][0], y + DIRS[d][1]); return n && ART_WATER.has(n.terrain); });
    for (const p of known) {
      const k = ov(p.t); if (k !== "forest" && k !== "jungle") continue;
      const small = edgeWater(p.x, p.y);
      const [y0, cols] = k === "jungle" ? (small ? [176, 6] : [0, 4]) : (small ? [528, 5] : [352, 4]);
      this._blit(g, art.img("forests"), (p.x % cols) * 128, y0 + (p.y % 2) * 88, 128, 88, p.sx, p.sy, -64, -56, z);
    }
    for (const p of known) {
      if (ov(p.t) !== "marsh") continue;
      const [y0, cols] = edgeWater(p.x, p.y) ? [176, 5] : [0, 4];
      this._blit(g, art.img("marsh"), (p.x % cols) * 128, y0 + (p.y % 2) * 88, 128, 88, p.sx, p.sy, -64, -32, z);
    }
    for (const p of known) {
      const k = ov(p.t); if (!HILLY.has(k)) continue;
      let m = 0;
      [["NW", 1], ["NE", 2], ["SW", 4], ["SE", 8]].forEach(([d, b]) => { if (HILLY.has(ov(T(p.x + DIRS[d][0], p.y + DIRS[d][1])))) m |= b; });
      const [sheet, ch, oy] = k === "hills" ? ["hills", 72, -40] : [k === "volcano" ? "volcanos" : "mountains", 88, -56];
      this._blit(g, art.img(sheet), (m & 3) * 128, (m >> 2) * ch, 128, ch, p.sx, p.sy, -64, oy, z);
    }
    // 6. the bonus grassland shield
    for (const p of known)
      if (p.t.bonus && ov(p.t) === "grassland") this._blit(g, art.img("tnt"), 0, 192, 128, 64, p.sx, p.sy, -64, -32, z);
    // 7. improvements, by the client's zIndex: irrigation, roads, mines and ruins, craters, forts, pollution
    for (const p of known) this._improvements(g, world, p, z);
    // 8. resources
    for (const p of known) {
      const i = RES_ICON[p.t.resource]; if (i == null) continue;
      this._blit(g, art.img("resources"), (i % 6) * 50, Math.floor(i / 6) * 50, 50, 50, p.sx, p.sy, -25, -25, z);
    }
    // 9. barbarian camps
    for (const p of known)
      if (p.t.improvements.includes("barbarian_camp")) this._blit(g, art.img("buildings"), 256, 0, 128, 64, p.sx, p.sy, -64, -32, z);
    // 10. borders: the territory edge in the owner's colour wherever the next tile has another owner
    for (const p of known) {
      if (p.t.owner < 0) continue;
      const col = world.color(p.t.owner), dim = col.map(v => Math.round(v * 0.8));
      const sheet = art.recolor("territory", [[[236, 255, 0, 255], col], [[255, 0, 0, 255], dim]], col.join());
      [["NW", 0], ["NE", 1], ["SW", 2], ["SE", 3]].forEach(([d, row]) => {
        const n = T(p.x + DIRS[d][0], p.y + DIRS[d][1]);
        if (!n || n.owner !== p.t.owner) this._blit(g, sheet, 0, row * 72, 128, 72, p.sx, p.sy, -64, -39.96, z);
      });
    }
    // the city's workable tiles, while its screen is open
    if (o.cityRadius) this._cityRadius(g, world, o.cityRadius);
    // cities: the sprite by size and era, the label below
    const labels = [];
    for (const p of known) { const c = world.city(p.x, p.y); if (c) labels.push(this._city(g, world, c, p, z)); }
    // fog of war, at each tile's north corner, over every position in view (unknown land goes black)
    const st = t => !t ? 0 : t.visible ? 2 : 1;
    for (const p of all) {
      const n = st(T(p.x, p.y - 2)), nw = st(T(p.x - 1, p.y - 1)), ne = st(T(p.x + 1, p.y - 1)), me = st(p.t);
      if (n + nw + ne + me === 0 || (n & nw & ne & me) === 2) continue;   // all black already, or all in sight
      const col = n + 3 * nw, row = ne + 3 * me;
      this._blit(g, art.img("fog"), col * 128, row * 64, 128, 64, p.sx, p.sy, -64, -64, z);
    }
    for (const l of labels) l();
  }

  // The base sprite at the north corner of (x, y): the client's rule (TerrainTextureFiles.cs). A corner the seat
  // doesn't know borrows a known one of the same corner; fog covers it anyway.
  _base(world, x, y) {
    const at = (dx, dy) => {
      const yy = y + dy;
      if (yy < 0 || yy >= world.H || (!world.wrap && (x + dx < 0 || x + dx >= world.W))) return "coast";   // off the map
      return world.tile(x + dx, yy)?.terrain ?? null;
    };
    const c = [at(0, -2), at(-1, -1), at(1, -1), at(0, 0)];
    const fill = c.find(k => k != null);
    if (fill == null) return null;
    for (let i = 0; i < 4; i++) if (c[i] == null) c[i] = c[3] ?? fill;
    for (let i = 0; i < 4; i++) if (c[i] === "floodplain" || c[i] === "flood plain") c[i] = "desert";
    const n = {}; for (const k of c) n[k] = (n[k] || 0) + 1;
    const water = (n.ocean || 0) + (n.sea || 0) + (n.coast || 0), uniq = Object.keys(n).length;
    let f;
    if (water === 4) {
      if (n.ocean === 4) return {file: "wOOO", idx: hash(world.wx(x), y, 1) % 81};
      if (n.sea === 4) return {file: "wSSS", idx: hash(world.wx(x), y, 2) % 81};
      f = "wCSO";
    } else if (n.tundra) f = "xtgc";
    else if (uniq >= 3) f = !n.coast ? "xdgp" : !n.grassland ? "xdpc" : !n.desert ? "xpgc" : "xdgc";
    else if (uniq === 2) {
      const D = !!n.desert, P = !!n.plains, G = !!n.grassland, C = !!(n.coast || n.sea || n.ocean);
      f = (G && C) || (P && C) ? "xpgc" : (D && C) ? "xdpc" : (G && P) ? "xpgc" : (G && D) ? "xdgc" : (P && D) ? "xdpc" : "xpgc";
    } else f = n.grassland ? "xdgc" : n.plains ? "xpgc" : n.desert ? "xdgp" : "xpgc";
    const [start, mid] = BASE_ROLE[f];
    let idx = 0;
    c.forEach((k, i) => { if (k !== start) idx += k === mid ? BASE_MID[i] : BASE_END[i]; });
    return {file: f, idx};
  }
  // The river sprite at the east corner of (x, y): which of the four edges meeting there carry a river.
  _river(world, x, y) {
    const r = (dx, dy) => world.tile(x + dx, y + dy)?.river | 0;
    const me = r(0, 0), n = r(1, -1), e = r(2, 0), s = r(1, 1);
    let i = 0;
    if ((n & RIVER.SW) || (me & RIVER.NE)) i |= 1;
    if ((e & RIVER.NW) || (n & RIVER.SE)) i |= 2;
    if ((me & RIVER.SE) || (s & RIVER.NW)) i |= 4;
    if ((s & RIVER.NE) || (e & RIVER.SW)) i |= 8;
    return i;
  }
  _improvements(g, world, p, z) {
    const art = this.art, imp = p.t.improvements, has = (t, k) => !!t && t.improvements.includes(k);
    const around = (bits, test) => {
      let m = 0;
      for (const [d, b] of Object.entries(bits)) if (test(world.tile(p.x + DIRS[d][0], p.y + DIRS[d][1]))) m |= b;
      return m;
    };
    const cell = (sheet, sx, sy) => this._blit(g, art.img(sheet), sx, sy, 128, 64, p.sx, p.sy, -64, -32, z);
    if (imp.includes("irrigation")) {
      const m = around({NW: 1, NE: 2, SW: 4, SE: 8}, t => has(t, "irrigation"));
      cell("irrigation", (m % 4) * 128, (m >> 2) * 64);
    }
    const road = t => has(t, "road") || has(t, "railroad");
    if (imp.includes("railroad")) {
      const q = around(ROAD_BIT, t => has(t, "road") && !has(t, "railroad")), r = around(ROAD_BIT, t => has(t, "railroad"));
      if (q) cell("roads", (q & 15) * 128, (q >> 4) * 64);
      cell("railroads", (r & 15) * 128, (r >> 4) * 64);
    } else if (imp.includes("road")) {
      const m = around(ROAD_BIT, road);
      cell("roads", (m & 15) * 128, (m >> 4) * 64);
    }
    if (imp.includes("mine")) cell("buildings", 256, 64);
    if (imp.includes("ruins")) {
      const v = hash(p.x, p.y, 3) % 3;
      this._blit(g, art.img("ruins"), v * 167, 0, 167, 95, p.sx, p.sy, -83.5, -47.5, z);
    }
    for (const key of ["craters", "pollution"]) {
      if (!imp.includes(key)) continue;
      const m = around({NW: 1, NE: 2, SE: 4, SW: 8}, t => has(t, key));
      if (m) cell(key, ((m - 1) % 5) * 128, (Math.floor((m - 1) / 5) + 2) * 64);
      else { const v = hash(p.x, p.y, key.length) % 10; cell(key, (v % 5) * 128, Math.floor(v / 5) * 64); }
    }
    if (imp.includes("fortress") || imp.includes("barricade")) cell("buildings", 0, 0);
  }

  // A city: the town, city or metropolis of its owner's era (walls on a walled town), and its label.
  _city(g, world, c, p, z) {
    const art = this.art, era = clamp(c.era | 0, 0, 3);
    const rank = c.size > 12 ? 2 : c.size > 6 ? 1 : 0;
    if (c.walls && rank === 0) this._blit(g, art.img("walls"), 0, era * 95, 166, 95, p.sx, p.sy, -83, -47.5, z);
    else this._blit(g, art.img("cities"), rank * 166, era * 95, 166, 95, p.sx, p.sy, -83, -47.5, z);
    return () => this._cityLabel(g, world, c, p, z);
  }
  // The label (CityLabelScene.cs): the size in the civ's colour, the name and growth, a rule in the civ's colour,
  // what it builds; a star box on a capital. Drawn at its real size, scaled down only when zoomed far out.
  _cityLabel(g, world, c, p, z) {
    const s = Math.max(1, 0.75 / this.zs), col = rgb(world.color(c.owner)), mine = c.owner === world.me;   // readable when zoomed out
    const font = this.art.font;
    g.save(); g.translate(p.sx, p.sy + 24 * z); g.scale(s, s);
    g.font = `11px ${font}`;
    const grow = c.turns_to_grow != null && c.turns_to_grow >= 0 ? c.turns_to_grow : "- -";
    const nameLine = mine ? `${c.name} : ${grow}` : c.name;
    const prodLine = mine ? (c.producing ? `${c.producing} : ${c.turns_to_complete ?? "--"}` : "-- : --") : null;
    const W = Math.max(60, Math.ceil(g.measureText(nameLine).width) + 6, prodLine ? Math.ceil(g.measureText(prodLine).width) + 6 : 0);
    const inner = prodLine ? 35 : 18, bw = 1 + 24 + 4 + W + 4 + (c.capital ? 24 : 0) + 1, bh = inner + 2, x0 = -bw / 2;
    g.fillStyle = "rgba(64,64,64,.75)"; g.fillRect(x0, 0, bw, bh);
    g.strokeStyle = "rgba(80,80,80,.75)"; g.lineWidth = 1; g.strokeRect(x0 + 0.5, 0.5, bw - 1, bh - 1);
    g.fillStyle = col; g.fillRect(x0 + 1, 1, 24, inner);
    g.font = `18px ${font}`; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillStyle = mine && c.starving ? "#f00" : "#fff"; g.fillText(String(c.size), x0 + 13, 1 + inner / 2 + 1);
    const cx0 = x0 + 1 + 28, cxm = cx0 + W / 2;
    g.fillStyle = "rgba(32,32,32,.75)"; g.fillRect(cx0 - 4, 1, W + 8, 1);
    g.font = `11px ${font}`; g.fillStyle = "#fff"; g.fillText(nameLine, cxm, 2 + 8);
    if (prodLine) {
      g.fillStyle = col; g.fillRect(cx0 - 4, 18, W + 8, 1);
      g.fillStyle = "#fff"; g.fillText(prodLine, cxm, 19 + 8);
    }
    g.fillStyle = "rgba(48,48,48,.75)"; g.fillRect(cx0 - 4, inner, W + 8, 1);
    if (c.capital) {
      const bx = cx0 + W + 4;
      g.fillStyle = col; g.fillRect(bx, 1, 24, inner);
      const star = this.art.img("city_icons");
      if (star) g.drawImage(star, 20, 1, 18, 18, bx + 3, 1 + (inner - 18) / 2, 18, 18);
    }
    g.restore();
  }
  // The city's workable tiles while its screen is open: a white outline round them (TileAssignmentLayer.cs).
  _cityRadius(g, world, c) {
    const inR = (dx, dy) => {
      if ((dx + dy) & 1) return false;
      const a = Math.abs((dx + dy) / 2), b = Math.abs((dy - dx) / 2);
      return Math.max(a, b) <= 2 && !(a === 2 && b === 2);
    };
    const {ox, oy} = this.origin, z = 1;
    let vx = c.x;   // the copy of the city nearest the camera's centre, round the wrap
    if (world.wrap) { const mid = (this.origin.MW / 2 - ox) / 64; vx += Math.round((mid - vx) / world.W) * world.W; }
    g.strokeStyle = "#fff"; g.lineWidth = 2; g.beginPath();
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      if (!inR(dx, dy)) continue;
      const sx = ox + 64 * (vx + dx), sy = oy + 32 * (c.y + dy), hx = 64 * z, hy = 32 * z;
      const edges = [[-1, -1, [sx - hx, sy], [sx, sy - hy]], [1, -1, [sx + hx, sy], [sx, sy - hy]],
        [-1, 1, [sx - hx, sy], [sx, sy + hy]], [1, 1, [sx + hx, sy], [sx, sy + hy]]];
      for (const [ex, ey, a, b] of edges) if (!inR(dx + ex, dy + ey)) { g.moveTo(...a); g.lineTo(...b); }
    }
    g.stroke();
  }

  // ---- units: drawn every frame over the cached picture (they animate) ----
  _units(ctx, world, cam, w, h, o) {
    this.plain.unitsOnly(ctx, world, cam, w, h, o);   // replaced once the unit art is wired in
  }
}
