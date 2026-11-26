'use strict';

/* ============================================================================
 * labelme-web 7.11.0 — 前端（纯原生 JS，无构建步骤）
 * 标注 JSON 与桌面版 labelme 7.11.0 完全一致。
 * ==========================================================================*/

const VERSION = '7.11.0';
const STORE_SESSION = 'labelme.web.session';
const STORE_LABELS = 'labelme.web.labels';
const STORE_LAST = 'labelme.web.lastSession';
const STORE_PROJECT = 'labelme.web.project';   // {owner, project}
const DRAFT_PREFIX = 'labelme.web.draft.';
const LIVE_CHANNEL = 'labelme-web-live';
const LAST_SESSION_MAX_AGE_MS = 1000 * 60 * 60 * 24 * 7; // 7 天内都愿意接上

const COLORMAP = [
  [128, 0, 0], [0, 128, 0], [128, 128, 0], [0, 0, 128], [128, 0, 128],
  [0, 128, 128], [128, 128, 128], [64, 0, 0], [192, 0, 0], [64, 128, 0],
  [192, 128, 0], [64, 0, 128], [192, 0, 128], [64, 128, 128], [192, 128, 128],
  [0, 64, 0], [128, 64, 0], [0, 192, 0], [128, 192, 0], [0, 64, 128],
];

const IMAGE_RE = /\.(jpg|jpeg|png|bmp|gif|tif|tiff|webp)$/i;

/* --------------------------------------------------------------- utilities */
const $ = (id) => document.getElementById(id);

function css(rgb) {
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}
function rgba(rgb, a) {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${a})`;
}
function dist2(a, b) {
  const dx = a[0] - b[0];
  const dy = a[1] - b[1];
  return Math.sqrt(dx * dx + dy * dy);
}
function clamp(v, lo, hi) {
  return v < lo ? lo : v > hi ? hi : v;
}
function round2(v) {
  return Math.round(v * 100) / 100;
}

async function req(method, path, body, isForm) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body && isForm) {
    opts.body = body;
  } else if (body !== undefined && body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // 登录态过期：先落到登录页，标注草稿已在服务端，回来还能接上
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    const err = new Error('登录已过期，请重新登录');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch (e) { /* ignore */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  const type = res.headers.get('content-type') || '';
  return type.includes('json') ? res.json() : res.text();
}

let toastTimer = null;
function toast(msg, isError) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 4200 : 2000);
}

/* ------------------------------------------------------------------- state */
const state = {
  sessionId: null,
  owner: null,          // 当前工程所属账号（普通账号 = 自己）
  project: null,        // 当前工程名
  projects: [],         // 当前账号可见的工程
  user: null,
  images: [],           // [{name,size,annotated}]
  index: -1,
  image: null,          // {name,w,h,img,canvas}
  shapes: [],
  selected: -1,
  selectedSet: [],       // 多选：所有被选中的下标（单选的超集）
  selectedVertex: -1,
  tool: 'select',
  labels: [],
  flagDefs: [],
  flags: {},
  extra: {},             // 桌面版 JSON 的自定义顶层字段（other_data），保存时原样带回
  autoSave: true,
  embedImage: false,     // 是否把图像 base64 写进 JSON（桌面版 "Store image data"）
  crosshair: true,       // 十字准星
  allowOob: false,       // 允许画到图像外（桌面版 allow_out_of_bounds_points）
  undoLimit: 20,
  hideShapes: false,
  fillDrawing: true,
  clipboard: null,
  hover: null,
  keepPrev: false,
  keepZoom: false,
  keepBc: false,
  brightness: 0,
  contrast: 0,
  zoom: 1,
  tx: 0,
  ty: 0,
  drawing: null,        // {points:[[x,y]...], cursor:[x,y]}
  drag: null,           // {mode:'move'|'vertex', index, vertex, before, start}
  panning: null,
  undo: [],
  redo: [],
  saveTimer: null,
  saving: false,
  dirty: false,
  search: '',
};

const view = {
  zoom: 1,
  tx: 0,
  ty: 0,
};

const colorCache = new Map();
function colorForLabel(label) {
  if (!label) return [160, 160, 160];
  if (colorCache.has(label)) return colorCache.get(label);
  const idx = colorCache.size % COLORMAP.length;
  const col = COLORMAP[idx];
  colorCache.set(label, col);
  return col;
}

/* --------------------------------------------------------------- canvas */
const canvas = $('canvas');
const wrap = $('canvasWrap');
const ctx = canvas.getContext('2d');

function clientToImage(clientX, clientY) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (clientX - r.left - view.tx) / view.zoom,
    y: (clientY - r.top - view.ty) / view.zoom,
  };
}
function imgToScreen(p) {
  return [p[0] * view.zoom + view.tx, p[1] * view.zoom + view.ty];
}

function fitToWindow() {
  if (!state.image) return;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const pad = 12;
  const z = Math.min((w - pad * 2) / state.image.w, (h - pad * 2) / state.image.h);
  view.zoom = Math.max(z, 0.02);
  view.tx = (w - state.image.w * view.zoom) / 2;
  view.ty = (h - state.image.h * view.zoom) / 2;
  updateZoomLabel();
  redraw();
}

function zoomAt(factor, cx, cy) {
  if (!state.image) return;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const px = cx === undefined ? w / 2 : cx;
  const py = cy === undefined ? h / 2 : cy;
  const before = { x: (px - view.tx) / view.zoom, y: (py - view.ty) / view.zoom };
  const next = clamp(view.zoom * factor, 0.02, 40);
  view.zoom = next;
  view.tx = px - before.x * next;
  view.ty = py - before.y * next;
  updateZoomLabel();
  redraw();
}

function zoomTo(z) {
  if (!state.image) return;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const center = { x: (w / 2 - view.tx) / view.zoom, y: (h / 2 - view.ty) / view.zoom };
  view.zoom = z;
  view.tx = w / 2 - center.x * z;
  view.ty = h / 2 - center.y * z;
  updateZoomLabel();
  redraw();
}

function updateZoomLabel() {
  const el = $('zoomPct');
  if (el) el.textContent = `${Math.round(view.zoom * 100)}%`;
}

function sizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, wrap.clientWidth);
  const h = Math.max(1, wrap.clientHeight);
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
}

function redraw() {
  sizeCanvas();
  const dpr = window.devicePixelRatio || 1;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#252526';
  ctx.fillRect(0, 0, w, h);

  if (!state.image) return;

  const iw = state.image.w * view.zoom;
  const ih = state.image.h * view.zoom;
  ctx.imageSmoothingEnabled = view.zoom < 1;
  if (state.image.canvas) {
    ctx.drawImage(state.image.canvas, view.tx, view.ty, iw, ih);
  }
  ctx.strokeStyle = '#3f3f46';
    ctx.lineWidth = 1;
  ctx.strokeRect(view.tx - 0.5, view.ty - 0.5, iw + 1, ih + 1);

  if (!state.hideShapes) {
    state.shapes.forEach((shape, i) => drawShape(shape, i, i === state.selected || state.selectedSet.includes(i)));
  }
  drawPending();

  if (state.crosshair !== false && state.hover) {
    const p = imgToScreen(state.hover);
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = 'rgba(220, 240, 255, .55)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(view.tx, p[1]);
    ctx.lineTo(view.tx + iw, p[1]);
    ctx.moveTo(p[0], view.ty);
    ctx.lineTo(p[0], view.ty + ih);
    ctx.stroke();
    ctx.restore();
  }

  if (state.drag && state.drag.mode === 'move') {
    // nothing extra, shape already redrawn at new position
  }
}

function shapePointsScreen(shape) {
  return shape.points.map((p) => imgToScreen(p));
}

function drawShape(shape, index, selected) {
  const pts = shapePointsScreen(shape);
  const col = colorForLabel(shape.label);
  const lw = selected ? 3 : 2;

  ctx.lineJoin = 'round';

  const closed = ['polygon', 'rectangle', 'oriented_rectangle', 'circle', 'mask'].includes(shape.shape_type);

  if (shape.shape_type === 'point' || shape.shape_type === 'points') {
    const r = selected ? 5 : 4;
    for (const p of pts) {
      ctx.beginPath();
      ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
      ctx.fillStyle = rgba(col, selected ? 1 : 0.85);
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.45)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  } else if (shape.shape_type === 'circle') {
    const center = pts[0];
    const edge = pts[1];
    const r = center && edge ? Math.hypot(edge[0] - center[0], edge[1] - center[1]) : 0;
    ctx.beginPath();
    ctx.arc(center[0], center[1], r, 0, Math.PI * 2);
    ctx.fillStyle = rgba(col, 0.22);
    ctx.fill();
    ctx.strokeStyle = css(col);
    ctx.lineWidth = lw;
    ctx.stroke();
  } else if (shape.shape_type === 'rectangle') {
    const a = pts[0];
    const b = pts[1];
    if (a && b) {
      const x = Math.min(a[0], b[0]);
      const y = Math.min(a[1], b[1]);
      const w = Math.abs(b[0] - a[0]);
      const h = Math.abs(b[1] - a[1]);
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.fillStyle = rgba(col, 0.22);
      ctx.fill();
      ctx.strokeStyle = css(col);
      ctx.lineWidth = lw;
      ctx.stroke();
    }
  } else if (shape.shape_type === 'mask') {
    drawMaskShape(shape, selected, lw);
  } else {
    // polygon / oriented_rectangle / line / linestrip
    ctx.beginPath();
    pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1])));
    if (closed) {
      ctx.closePath();
      ctx.fillStyle = rgba(col, 0.22);
      ctx.fill();
    }
    ctx.strokeStyle = css(col);
    ctx.lineWidth = lw;
    ctx.stroke();
  }

  if (selected) {
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = 'rgba(0,0,0,.6)';
    ctx.lineWidth = 1;
    pts.forEach((p, i) => {
      const isVertex = i === state.selectedVertex;
      const r = isVertex ? 6 : 4;
      ctx.beginPath();
      ctx.rect(p[0] - r, p[1] - r, r * 2, r * 2);
      ctx.fillStyle = isVertex ? '#ffd23f' : '#ffffff';
      ctx.fill();
      ctx.stroke();
    });
  }
}

function drawPending() {
  const d = state.drawing;
  if (!d) return;
  const all = d.cursor ? d.points.concat([d.cursor]) : d.points.slice();
  if (!all.length) return;
  const pts = all.map((p) => imgToScreen(p));
  const type = d.type;

  ctx.beginPath();
  ctx.strokeStyle = 'rgba(255, 205, 70, .95)';
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 2;

  if (type === 'rectangle' && pts.length >= 2) {
    const a = pts[0];
    const b = pts[pts.length - 1];
    ctx.rect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
    if (state.fillDrawing) {
      ctx.fillStyle = 'rgba(255, 205, 70, .16)';
      ctx.fill();
    }
    ctx.stroke();
  } else if (type === 'circle') {
    const c = pts[0];
    const e = pts[pts.length - 1];
    const r = Math.hypot(e[0] - c[0], e[1] - c[1]);
    ctx.arc(c[0], c[1], r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(c[0], c[1]);
    ctx.lineTo(e[0], e[1]);
    ctx.stroke();
  } else if (type === 'circle3' && pts.length >= 2) {
    if (pts.length >= 3) {
      const c = circumcircle(d.points[0], d.points[1], d.points[2]);
      if (c) {
        const cs = imgToScreen([c.x, c.y]);
        ctx.arc(cs[0], cs[1], c.r * view.zoom, 0, Math.PI * 2);
        ctx.stroke();
      }
    } else {
      pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1])));
      ctx.stroke();
    }
  } else if (type === 'point') {
    ctx.arc(pts[pts.length - 1][0], pts[pts.length - 1][1], 4, 0, Math.PI * 2);
    ctx.stroke();
  } else {
    pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p[0], p[1]) : ctx.lineTo(p[0], p[1])));
    if (state.fillDrawing && (type === 'polygon' || type === 'oriented_rectangle') && pts.length >= 3) {
      ctx.closePath();
      ctx.fillStyle = 'rgba(255, 205, 70, .16)';
      ctx.fill();
    }
    ctx.stroke();
  }

  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(255, 205, 70, .9)';
  for (const p of d.points.map((q) => imgToScreen(q))) {
    ctx.beginPath();
    ctx.arc(p[0], p[1], 3, 0, Math.PI * 2);
    ctx.fill();
  }
}

function circumcircle(a, b, c) {
  const ax = a[0], ay = a[1], bx = b[0], by = b[1], cx = c[0], cy = c[1];
  const d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by));
  if (Math.abs(d) < 1e-9) return null;
  const ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d;
  const uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d;
  return { x: ux, y: uy, r: Math.hypot(ax - ux, ay - uy) };
}

/* ------------------------------------------------------------- hit testing */
function pointInPolygon(pt, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
    const intersect = (yi > pt[1]) !== (yj > pt[1])
      && pt[0] < ((xj - xi) * (pt[1] - yi)) / (yj - yi || 1e-12) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function distToSegment(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return dist2(p, a);
  let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2;
  t = clamp(t, 0, 1);
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

function shapeHit(shape, pt) {
  const tol = 5 / view.zoom;
  const pts = shape.points;
  if (!pts.length) return false;

  if (shape.shape_type === 'point' || shape.shape_type === 'points') {
    return pts.some((p) => dist2(p, pt) <= 6 / view.zoom);
  }
  if (shape.shape_type === 'circle') {
    const c = pts[0], e = pts[1] || pts[0];
    const r = dist2(c, e);
    return Math.abs(dist2(c, pt) - r) <= tol || dist2(c, pt) <= r;
  }
  if (shape.shape_type === 'rectangle' && pts.length >= 2) {
    const x1 = Math.min(pts[0][0], pts[1][0]);
    const x2 = Math.max(pts[0][0], pts[1][0]);
    const y1 = Math.min(pts[0][1], pts[1][1]);
    const y2 = Math.max(pts[0][1], pts[1][1]);
    return pt[0] >= x1 - tol && pt[0] <= x2 + tol && pt[1] >= y1 - tol && pt[1] <= y2 + tol;
  }
  if (shape.shape_type === 'mask' && pts.length >= 2) {
    const x1 = Math.min(pts[0][0], pts[1][0]);
    const x2 = Math.max(pts[0][0], pts[1][0]);
    const y1 = Math.min(pts[0][1], pts[1][1]);
    const y2 = Math.max(pts[0][1], pts[1][1]);
    return pt[0] >= x1 - tol && pt[0] <= x2 + tol && pt[1] >= y1 - tol && pt[1] <= y2 + tol;
  }
  if (shape.shape_type === 'line' || shape.shape_type === 'linestrip') {
    for (let i = 0; i < pts.length - 1; i++) {
      if (distToSegment(pt, pts[i], pts[i + 1]) <= tol) return true;
    }
    return false;
  }
  // polygon / oriented_rectangle
  if (pts.length >= 3 && pointInPolygon(pt, pts)) return true;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i];
    const b = pts[(i + 1) % pts.length];
    if (distToSegment(pt, a, b) <= tol) return true;
  }
  return false;
}

function hitTestShape(pt) {
  if (state.hideShapes) return -1;
  const p = Array.isArray(pt) ? pt : [pt.x, pt.y];
  for (let i = state.shapes.length - 1; i >= 0; i--) {
    if (shapeHit(state.shapes[i], p)) return i;
  }
  return -1;
}

function hitTestVertex(shape, pt) {
  const tol = 7 / view.zoom;
  const p = Array.isArray(pt) ? pt : [pt.x, pt.y];
  let best = -1;
  let bestD = tol;
  shape.points.forEach((q, i) => {
    const d = dist2(q, p);
    if (d <= bestD) {
      bestD = d;
      best = i;
    }
  });
  return best;
}

/* --------------------------------------------------------------- history */
function snapshot() {
  return JSON.stringify(state.shapes);
}

function recordChange(before) {
  state.undo.push(before);
  while (state.undo.length > (state.undoLimit || 20)) state.undo.shift();
  state.redo.length = 0;
  state.dirty = true;
  scheduleDraft();
  rememberSession();
  refreshDocks();
  updateStatus();
  scheduleSave();
}

function commit(mutator) {
  const before = snapshot();
  mutator();
  recordChange(before);
  redraw();
}

function undo() {
  if (!state.undo.length) {
    toast('没有可撤销的操作');
    return;
  }
  state.redo.push(snapshot());
  state.shapes = JSON.parse(state.undo.pop());
  state.selected = -1;
  state.selectedSet = [];
  state.selectedVertex = -1;
  state.dirty = true;
  scheduleDraft();
  redraw();
  refreshDocks();
  updateStatus();
  scheduleSave();
}

function redo() {
  if (!state.redo.length) {
    toast('没有可重做的操作');
    return;
  }
  state.undo.push(snapshot());
  state.shapes = JSON.parse(state.redo.pop());
  state.selected = -1;
  state.selectedSet = [];
  state.selectedVertex = -1;
  state.dirty = true;
  scheduleDraft();
  redraw();
  refreshDocks();
  updateStatus();
  scheduleSave();
}

/* --------------------------------------------------------------- annotation */
function cleanShape(shape) {
  return {
    label: shape.label,
    points: shape.points.map((p) => [round2(p[0]), round2(p[1])]),
    group_id: shape.group_id === undefined ? null : shape.group_id,
    description: shape.description || '',
    shape_type: shape.shape_type,
    flags: shape.flags || {},
    mask: shape.mask === undefined ? null : shape.mask,
    locked: !!shape.locked,
  };
}

function buildAnnotation() {
  let imageData = null;
  if (state.embedImage && state.image && state.image.canvas) {
    try {
      imageData = state.image.canvas.toDataURL('image/png').split(',')[1];
    } catch (e) {
      imageData = null;
    }
  }
  // 保留桌面版写入的自定义顶层字段（labelme 的 other_data），不静默丢弃
  const base = {
    version: VERSION,
    flags: state.flags || {},
    shapes: state.shapes.map(cleanShape),
    imagePath: state.image ? state.image.name : '',
    imageData,
    imageHeight: state.image ? state.image.h : 0,
    imageWidth: state.image ? state.image.w : 0,
  };
  return Object.assign({}, state.extra || {}, base);
}

function scheduleSave() {
  if (!state.sessionId || !state.image) return;
  if (!state.autoSave) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => saveNow(true), 800);
}

async function saveNow(silent) {
  if (!state.sessionId || !state.image) return;
  clearTimeout(state.saveTimer);
  const target = state.image.name;
  state.saving = true;
  try {
    await req(
      'PUT',
      `/api/sessions/${state.sessionId}/annotations/${encodeURIComponent(target)}`,
      buildAnnotation()
    );
    state.dirty = false;
    clearDraft(state.sessionId, target);
    rememberSession(target);
    const item = state.images.find((x) => x.name === target);
    if (item) item.annotated = true;
    refreshImageList();
    updateStatus();
    if (!silent) toast(`已保存 ${target}.json`);
  } catch (e) {
    if (e.status === 404 && /session|工程/i.test(e.message)) {
      await newSession(`保存时找不到工程目录（${target} 已随目录一起没了）`);
      return;
    }
    toast(`保存失败：${e.message}`, true);
  } finally {
    state.saving = false;
  }
}

/* --------------------------------------------------------------- shape creation */
const MIN_POINTS = {
  polygon: 3,
  rectangle: 2,
  oriented_rectangle: 4,
  circle: 2,
  point: 1,
  line: 2,
  linestrip: 2,
  points: 1,
  mask: 2,
};

const LABEL_TYPES = ['polygon', 'rectangle', 'oriented_rectangle', 'circle', 'point', 'line', 'linestrip'];

function startDrawing(type) {
  if (!state.image) {
    toast('先打开一张图像');
    return;
  }
  state.drawing = { type, points: [], cursor: null };
  setTool(type === 'circle3' ? 'circle3' : type);
}

function dropDuplicateTail(d) {
  if (!d || d.points.length < 2) return;
  const last = d.points[d.points.length - 1];
  const prev = d.points[d.points.length - 2];
  if (dist2(last, prev) <= Math.pow(4 / view.zoom, 2)) d.points.pop();
}

function finishDrawing() {
  const d = state.drawing;
  if (!d) return;
  const storedType = d.type === 'circle3' ? 'circle' : d.type;
  const need = MIN_POINTS[storedType] || 1;
  let pts = d.points.slice();
  // 双击结束可能留下两个重合的末点；只保留一个
  if (pts.length >= 3) {
    const last = pts[pts.length - 1];
    const prev = pts[pts.length - 2];
    if (dist2(last, prev) <= 1 / view.zoom) pts.pop();
  }
  if ((d.type === 'rectangle' || d.type === 'line') && pts.length === 1 && d.cursor) {
    pts.push(d.cursor);
  }

  if (d.type === 'circle3') {
    if (pts.length < 3) {
      toast('三点画圆需要 3 个点');
      state.drawing = null;
      redraw();
      return;
    }
    const c = circumcircle(pts[0], pts[1], pts[2]);
    if (!c) {
      toast('三点共线，无法确定圆心');
      state.drawing = null;
      redraw();
      return;
    }
    pts = [[c.x, c.y], [c.x + c.r, c.y]];
  }

  if (pts.length < need) {
    toast(`至少需要 ${need} 个点`);
    state.drawing = null;
    redraw();
    return;
  }
  if (storedType === 'rectangle' || storedType === 'line') {
    if (dist2(pts[0], pts[pts.length - 1]) < 2 / view.zoom) {
      state.drawing = null;
      redraw();
      return;
    }
  }

  state.drawing = null;
  const finish = (label, meta) => {
    if (label === null) {
      redraw();
      return;
    }
    const shape = {
      label,
      points: pts.map((p) => [p[0], p[1]]),
      group_id: meta && meta.group_id != null ? meta.group_id : null,
      description: (meta && meta.description) || '',
      shape_type: storedType,
      flags: (meta && meta.flags) || {},
      mask: null,
    };
    commit(() => {
      state.shapes.push(shape);
      state.selected = state.shapes.length - 1;
    });
    addLabel(label);
    setTool('select');
  };
  askLabel(lastLabel(), finish);
}

function cancelDrawing() {
  if (!state.drawing) return false;
  state.drawing = null;
  redraw();
  return true;
}

function undoLastPoint() {
  const d = state.drawing;
  if (!d || !d.points.length) {
    toast('当前没有正在绘制的点');
    return;
  }
  d.points.pop();
  d.cursor = d.points.length ? d.points[d.points.length - 1] : null;
  if (!d.points.length) state.drawing = null;
  redraw();
  toast('已撤销上一个点');
}

function lastLabel() {
  for (let i = state.shapes.length - 1; i >= 0; i--) {
    if (state.shapes[i].label) return state.shapes[i].label;
  }
  return state.labels[0] || '';
}

function addLabel(label) {
  if (!label) return;
  if (!state.labels.includes(label)) {
    state.labels.push(label);
    saveLabels();
    refreshLabelList();
  }
}

let labelsTimer = null;
let flagTimer = null;

/** 标签集是工程级的，存服务端 data/<用户名>/<工程>/labels.txt（每行一个）。 */
function saveLabels() {
  try {
    localStorage.setItem(STORE_LABELS, JSON.stringify(state.labels));
  } catch (e) { /* ignore */ }
  clearTimeout(labelsTimer);
  labelsTimer = setTimeout(pushLabels, 500);
}

async function pushLabels() {
  try {
    await req('PUT', '/api/labels' + projectQuery(), { labels: state.labels });
  } catch (e) {
    toast(`标签集保存失败：${e.message}`, true);
  }
}

async function loadLabels() {
  // 先读本地缓存，避免首屏空标签
  try {
    const raw = localStorage.getItem(STORE_LABELS);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) state.labels = arr.filter((x) => typeof x === 'string');
    }
  } catch (e) { /* ignore */ }
  // 服务端为准
  try {
    const res = await req('GET', '/api/labels' + projectQuery());
    if (res && Array.isArray(res.labels)) state.labels = res.labels;
  } catch (e) { /* 后端没起来时继续用缓存 */ }
  await loadFlagDefs();
  refreshLabelList();
}

/** 工程级 flag 定义，存服务端 data/<用户名>/<工程>/flags.txt（每行一个）。 */
async function loadFlagDefs() {
  try {
    const res = await req('GET', '/api/flags' + projectQuery());
    if (res && Array.isArray(res.flags)) state.flagDefs = res.flags;
  } catch (e) { /* 忽略 */ }
  mergeUsedFlags();
}

function mergeUsedFlags() {
  const extra = new Set();
  state.shapes.forEach((s) => {
    Object.keys(s.flags || {}).forEach((f) => extra.add(f));
  });
  Object.keys(state.flags || {}).forEach((f) => extra.add(f));
  extra.forEach((f) => {
    if (!state.flagDefs.includes(f)) state.flagDefs.push(f);
  });
}

function saveFlagDefs() {
  clearTimeout(flagTimer);
  flagTimer = setTimeout(async () => {
    try {
      await req('PUT', '/api/flags' + projectQuery(), { flags: state.flagDefs });
    } catch (e) {
      toast(`flag 定义保存失败：${e.message}`, true);
    }
  }, 400);
}

/** 解析导入的标签文件：.txt 每行一个；.json 支持 ["a","b"] 或 {"labels":[...]} */
function parseLabelFile(text, filename) {
  const raw = text.replace(/^\uFEFF/, '');
  if (/\.json$/i.test(filename)) {
    try {
      const data = JSON.parse(raw);
      const arr = Array.isArray(data) ? data : data && Array.isArray(data.labels) ? data.labels : null;
      if (arr) return arr.filter((x) => typeof x === 'string').map((x) => x.trim()).filter(Boolean);
    } catch (e) {
      throw new Error('JSON 解析失败');
    }
    throw new Error('JSON 里没有标签数组');
  }
  const out = [];
  raw.split(/\r?\n/).forEach((line) => {
    let s = line.trim();
    if (!s || s.startsWith('#') || s.startsWith('//')) return;
    // 兼容带颜色的行：label, 255, 0, 0 / label,255,0,0,255 / label: #ff0000
    s = s.replace(/\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(\s*,\s*\d{1,3})?\s*$/, '');
    s = s.replace(/\s*[,:]\s*#?[0-9a-fA-F]{3,8}\s*$/, '');
    s = s.trim();
    if (s) out.push(s);
  });
  return out;
}

async function importLabelsFile(file) {
  try {
    const text = await file.text();
    const incoming = parseLabelFile(text, file.name);
    const before = state.labels.length;
    mergeLabels(incoming);
    await pushLabels();
    refreshLabelList();
    openLabelManage();
    toast(`导入 ${incoming.length} 个标签，新增 ${state.labels.length - before} 个`);
  } catch (e) {
    toast(`导入失败：${e.message}`, true);
  }
}

function mergeLabels(list) {
  let added = 0;
  (list || []).forEach((label) => {
    const s = String(label).trim();
    if (!s) return;
    if (!state.labels.includes(s)) {
      state.labels.push(s);
      added += 1;
    }
  });
  if (added) saveLabels();
  return added;
}

function exportLabels() {
  if (!state.labels.length) {
    toast('标签集是空的，没有可导出的内容', true);
    return;
  }
  const blob = new Blob([state.labels.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'labels.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* --------------------------------------------------------------- pointer */
function currentTool() {
  return state.tool;
}

function isDrawingTool() {
  return ['polygon', 'rectangle', 'oriented_rectangle', 'circle', 'circle3', 'point', 'line', 'linestrip'].includes(state.tool);
}

canvas.addEventListener('pointerdown', (ev) => {
  if (!state.image) return;
  try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* 合成事件无捕获 */ }

  if (ev.button === 1 || (ev.button === 0 && ev.altKey)) {
    state.panning = { x: ev.clientX, y: ev.clientY, tx: view.tx, ty: view.ty };
    return;
  }
  if (ev.button !== 0) return;

  const pt = clientToImage(ev.clientX, ev.clientY);
  const tool = state.tool;
  if (!state.allowOob && state.image) {
    // 桌面版默认不允许越界点：落到图上之前先裁到图像范围
    pt.x = Math.min(state.image.w, Math.max(0, pt.x));
    pt.y = Math.min(state.image.h, Math.max(0, pt.y));
  }

  if (isDrawingTool()) {
    // 双击结束：第二次 pointerdown 不再补点，直接用已有点位闭合
    if (ev.detail >= 2 && state.drawing && ['polygon', 'linestrip', 'oriented_rectangle', 'circle3'].includes(state.drawing.type)) {
      dropDuplicateTail(state.drawing);
      finishDrawing();
      return;
    }
    handleDrawClick(tool, pt, ev);
    return;
  }

  // select tool
  if (state.selected >= 0) {
    const shape = state.shapes[state.selected];
    if (shape.locked) {
      toast('该形状已锁定，按 Ctrl+Shift+L 解锁');
      return;
    }
    const v = hitTestVertex(shape, pt);
    if (v >= 0) {
      state.selectedVertex = v;
      state.drag = {
        mode: 'vertex',
        index: state.selected,
        vertex: v,
        before: snapshot(),
        shape: JSON.parse(JSON.stringify(shape)),
      };
      redraw();
      return;
    }
  }

  const hit = hitTestShape(pt);
  if (hit >= 0) {
    if (ev.shiftKey) {
      // 多选：Shift + 点选，切换该形状的选中状态
      const set = state.selectedSet.slice();
      const at = set.indexOf(hit);
      if (at >= 0) set.splice(at, 1);
      else set.push(hit);
      state.selectedSet = set;
      state.selected = hit;
      state.selectedVertex = -1;
      refreshDocks();
      updateStatus();
      redraw();
      return;
    }
    state.selected = hit;
    state.selectedSet = [hit];
    state.selectedVertex = -1;
    state.drag = {
      mode: 'move',
      index: hit,
      before: snapshot(),
      start: pt,
      shape: JSON.parse(JSON.stringify(state.shapes[hit])),
    };
    refreshDocks();
    updateStatus();
    redraw();
    return;
  }

  state.selected = -1;
  state.selectedSet = [];
  state.selectedVertex = -1;
  refreshDocks();
  updateStatus();
  redraw();
});

canvas.addEventListener('pointermove', (ev) => {
  const pt = clientToImage(ev.clientX, ev.clientY);
  state.hover = [pt.x, pt.y];
  updateCursorPos(pt);

  if (state.panning) {
    view.tx = state.panning.tx + (ev.clientX - state.panning.x);
    view.ty = state.panning.ty + (ev.clientY - state.panning.y);
    redraw();
    return;
  }

  if (state.drawing) {
    if (state.drawing.type === 'rectangle' || state.drawing.type === 'line') {
      state.drawing.cursor = [pt.x, pt.y];
      if (state.drawing.points.length === 0) {
        // drag mode not started yet
      }
    } else {
      state.drawing.cursor = [pt.x, pt.y];
    }
    redraw();
  }

  if (state.drag && state.drag.index >= 0) {
    const shape = state.shapes[state.drag.index];
    if (!shape) return;
    if (state.drag.mode === 'move') {
      const dx = pt.x - state.drag.start.x;
      const dy = pt.y - state.drag.start.y;
      shape.points = state.drag.shape.points.map((p) => [p[0] + dx, p[1] + dy]);
    } else if (state.drag.mode === 'vertex') {
      const pts = [...state.drag.shape.points.map((p) => [p[0], p[1]])];
      pts[state.drag.vertex] = [pt.x, pt.y];
      shape.points = pts;
    }
    redraw();
  }
});

canvas.addEventListener('pointerup', (ev) => {
  try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
  if (state.panning) {
    state.panning = null;
    return;
  }
  if (state.drag) {
    const d = state.drag;
    state.drag = null;
    if (d.mode === 'move') {
      const moved = dist2(d.shape.points[0], state.shapes[d.index].points[0]) > 0.01;
      if (moved) recordChange(d.before);
      redraw();
    } else {
      recordChange(d.before);
      redraw();
    }
    return;
  }

  // 拖拽创建矩形/直线：松手时如果确实拖动过就完成；没拖动则保留起点等第二下点击
  if (state.drawing && ['rectangle', 'line'].includes(state.drawing.type)) {
    const d = state.drawing;
    const moved = d.cursor && d.points.length === 1
      && dist2(d.points[0], d.cursor) >= 2 / view.zoom;
    if (moved) finishDrawing();
  }
});

canvas.addEventListener('dblclick', (ev) => {
  ev.preventDefault();
  if (state.drawing && ['polygon', 'linestrip', 'oriented_rectangle', 'circle3'].includes(state.drawing.type)) {
    const d = state.drawing;
    // 双击的两次 pointerdown 可能各补了一个点；末尾重复点只保留一个
    dropDuplicateTail(d);
    finishDrawing();
  }
});

canvas.addEventListener('contextmenu', (ev) => {
  ev.preventDefault();
  if (state.drawing && ['polygon', 'linestrip', 'oriented_rectangle', 'circle3'].includes(state.drawing.type)) {
    finishDrawing();
  }
});

canvas.addEventListener('wheel', (ev) => {
  if (!state.image) return;
  ev.preventDefault();
  const r = canvas.getBoundingClientRect();
  const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  zoomAt(factor, ev.clientX - r.left, ev.clientY - r.top);
}, { passive: false });

function handleDrawClick(tool, pt, ev) {
  // 矩形/直线：既支持按住拖拽，也支持“点一下起点、再点一下终点”
  if (tool === 'rectangle' || tool === 'line') {
    if (!state.drawing) {
      state.drawing = { type: tool, points: [[pt.x, pt.y]], cursor: [pt.x, pt.y] };
      redraw();
      return;
    }
    // 第二次点击：补上终点并完成（拖拽路径在 pointerup 里已经 finish 了）
    state.drawing.points.push([pt.x, pt.y]);
    finishDrawing();
    return;
  }

  if (!state.drawing) {
    state.drawing = { type: tool, points: [], cursor: null };
  }
  const d = state.drawing;

  if (tool === 'point') {
    d.points = [[pt.x, pt.y]];
    finishDrawing();
    return;
  }

  if (tool === 'circle') {
    d.points.push([pt.x, pt.y]);
    if (d.points.length >= 2) finishDrawing();
    else redraw();
    return;
  }

  if (tool === 'circle3') {
    d.points.push([pt.x, pt.y]);
    if (d.points.length >= 3) finishDrawing();
    else redraw();
    return;
  }

  if (tool === 'oriented_rectangle') {
    d.points.push([pt.x, pt.y]);
    if (d.points.length >= 4) finishDrawing();
    else redraw();
    return;
  }

  if (tool === 'polygon') {
    // 回到起点即闭合（桌面 labelme 的默认手势），不再重复添加起点
    if (d.points.length >= 3 && d.points[0]
      && dist2(d.points[0], [pt.x, pt.y]) <= Math.pow(8 / view.zoom, 2)) {
      finishDrawing();
      return;
    }
    d.points.push([pt.x, pt.y]);
    redraw();
    return;
  }

  // linestrip
  d.points.push([pt.x, pt.y]);
  redraw();
}

function updateCursorPos(pt) {
  const el = $('statusPos');
  if (!el) return;
  if (!state.image) {
    el.textContent = 'x — , y —';
    return;
  }
  el.textContent = `x ${Math.round(pt.x)} , y ${Math.round(pt.y)}`;
}

/* ------------------------------------------------------------- shape ops */
function deleteSelectedShape() {
  if (state.selected < 0) {
    toast('没有选中形状');
    return;
  }
  const idx = state.selected;
  if (state.shapes[idx].locked) {
    toast('该形状已锁定，按 Ctrl+Shift+L 解锁');
    return;
  }
  const label = state.shapes[idx].label;
  commit(() => {
    state.shapes.splice(idx, 1);
    state.selected = -1;
    state.selectedVertex = -1;
  });
  toast(`已删除形状「${label}」，按 Ctrl+Z 可撤销`);
}

function deleteSelectedPoint() {
  if (state.selected < 0 || state.selectedVertex < 0) {
    toast('先选中形状上的一个顶点');
    return;
  }
  const shape = state.shapes[state.selected];
  if (!['polygon', 'linestrip'].includes(shape.shape_type)) {
    toast('只有多边形和折线可以删除顶点');
    return;
  }
  if (shape.locked) {
    toast('该形状已锁定');
    return;
  }
  const min = shape.shape_type === 'polygon' ? 3 : 2;
  if (shape.points.length <= min) {
    toast(`至少保留 ${min} 个点`);
    return;
  }
  const vi = state.selectedVertex;
  commit(() => {
    shape.points.splice(vi, 1);
    state.selectedVertex = -1;
  });
  toast('已删除顶点');
}

function projectOnSegment(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  if (!len2) return { point: [a[0], a[1]], dist: dist2(p, a) };
  let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const q = [a[0] + t * dx, a[1] + t * dy];
  return { point: q, dist: dist2(p, q), t };
}

function addPointToEdge() {
  if (state.selected < 0) {
    toast('先选中一个多边形或折线');
    return;
  }
  const shape = state.shapes[state.selected];
  if (!['polygon', 'linestrip'].includes(shape.shape_type)) {
    toast('只有多边形和折线可以加点');
    return;
  }
  if (shape.locked) {
    toast('该形状已锁定');
    return;
  }
  if (!state.hover) {
    toast('把鼠标移到要加点的边上');
    return;
  }
  let best = -1;
  let bestD = Infinity;
  let bestPoint = null;
  const n = shape.shape_type === 'polygon' ? shape.points.length : shape.points.length - 1;
  for (let i = 0; i < n; i++) {
    const a = shape.points[i];
    const b = shape.points[(i + 1) % shape.points.length];
    const pr = projectOnSegment(state.hover, a, b);
    if (pr.dist < bestD) {
      bestD = pr.dist;
      best = i;
      bestPoint = pr.point;
    }
  }
  if (best < 0 || !bestPoint) {
    toast('没有可加点的边');
    return;
  }
  if (bestD > Math.pow(18 / view.zoom, 2)) {
    toast('鼠标离边太远，先靠近要加点的边');
    return;
  }
  commit(() => {
    shape.points.splice(best + 1, 0, [bestPoint[0], bestPoint[1]]);
    state.selectedVertex = best + 1;
  });
  toast('已在边上插入顶点');
}

function toggleSelectedLock() {
  if (state.selected < 0) {
    toast('没有选中形状');
    return;
  }
  const shape = state.shapes[state.selected];
  shape.locked = !shape.locked;
  state.dirty = true;
  scheduleDraft();
  scheduleSave();
  refreshDocks();
  redraw();
  toast(shape.locked ? '已锁定形状' : '已解锁形状');
}

function toggleShapeVisibility() {
  state.hideShapes = !state.hideShapes;
  if (state.hideShapes) {
    state.selected = -1;
    state.selectedVertex = -1;
  }
  refreshDocks();
  redraw();
  toast(state.hideShapes ? '已隐藏全部形状' : '已显示全部形状');
}

function setShapeVisibility(show) {
  state.hideShapes = !show;
  if (state.hideShapes) {
    state.selected = -1;
    state.selectedVertex = -1;
  }
  refreshDocks();
  redraw();
}

function shapeCenter(shape) {
  const [x0, y0, x1, y1] = pointBounds(shape.points);
  return [(x0 + x1) / 2, (y0 + y1) / 2];
}

function selectNeighbor(direction) {
  if (!state.shapes.length) return;
  const anchor = state.selected >= 0 ? shapeCenter(state.shapes[state.selected]) : null;
  let best = -1;
  let bestD = Infinity;
  state.shapes.forEach((shape, i) => {
    if (i === state.selected) return;
    const c = shapeCenter(shape);
    const dx = c[0] - (anchor ? anchor[0] : 0);
    const dy = c[1] - (anchor ? anchor[1] : 0);
    const ok = (direction === 'up' && dy < 0)
      || (direction === 'down' && dy > 0)
      || (direction === 'left' && dx < 0)
      || (direction === 'right' && dx > 0);
    if (!ok) return;
    const d = dx * dx + dy * dy;
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  });
  if (best < 0) {
    toast('该方向没有其他形状');
    return;
  }
  state.selected = best;
  state.selectedVertex = -1;
  refreshDocks();
  redraw();
}

function fitToSelection() {
  if (state.selected < 0) {
    toast('先选中一个形状');
    return;
  }
  const shape = state.shapes[state.selected];
  if (!shape.points.length) return;
  const [x0, y0, x1, y1] = pointBounds(shape.points);
  const w = Math.max(8, x1 - x0);
  const h = Math.max(8, y1 - y0);
  const pad = 40;
  const vw = wrap.clientWidth;
  const vh = wrap.clientHeight;
  view.zoom = clamp(Math.min((vw - pad * 2) / w, (vh - pad * 2) / h), 0.02, 40);
  view.tx = vw / 2 - (x0 + w / 2) * view.zoom;
  view.ty = vh / 2 - (y0 + h / 2) * view.zoom;
  updateZoomLabel();
  redraw();
}

function pointBounds(points) {
  // 不用 Math.min(...xs)：掩码转出来的多边形动辄上万个点，展开成实参会爆栈
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const p of points) {
    if (p[0] < x0) x0 = p[0];
    if (p[1] < y0) y0 = p[1];
    if (p[0] > x1) x1 = p[0];
    if (p[1] > y1) y1 = p[1];
  }
  if (!Number.isFinite(x0)) return [0, 0, 0, 0];
  return [x0, y0, x1, y1];
}

function boundingBoxOf(shape) {
  if (shape.shape_type === 'circle') {
    const [c, e] = shape.points;
    const r = Math.hypot(e[0] - c[0], e[1] - c[1]);
    return [[c[0] - r, c[1] - r], [c[0] + r, c[1] + r]];
  }
  const [x0, y0, x1, y1] = pointBounds(shape.points);
  return [[x0, y0], [x1, y1]];
}

function circleToPolygon(center, edge, segments = 32) {
  const r = Math.hypot(edge[0] - center[0], edge[1] - center[1]);
  const out = [];
  for (let i = 0; i < segments; i++) {
    const a = (i / segments) * Math.PI * 2;
    out.push([center[0] + r * Math.cos(a), center[1] + r * Math.sin(a)]);
  }
  return out;
}

function convertSelectedShape(target) {
  if (state.selected < 0) {
    toast('先选中一个形状');
    return;
  }
  const shape = state.shapes[state.selected];
  if (shape.locked) {
    toast('该形状已锁定，按 Ctrl+Shift+L 解锁');
    return;
  }
  const from = shape.shape_type;
  if (from === target) {
    toast('形状已经是该类型');
    return;
  }
  if (target === 'mask') {
    const raster = rasterizeShapeToMask(shape);
    commit(() => {
      shape.shape_type = 'mask';
      shape.points = raster.points;
      shape.mask = raster.mask;
      state.selectedVertex = -1;
    });
    toast('已转换为掩码');
    return;
  }
  if (from === 'mask' && target === 'rectangle') {
    commit(() => {
      shape.shape_type = 'rectangle';
      shape.points = boundingBoxOf(shape);
      shape.mask = null;
    });
    toast('已转换为矩形（掩码外接框）');
    return;
  }
  if (from === 'mask' && target === 'polygon') {
    const pts = maskToPolygonPoints(shape);
    if (!pts) {
      toast('掩码还没解码完成，稍后再试一次', true);
      return;
    }
    commit(() => {
      shape.shape_type = 'polygon';
      shape.points = pts;
      shape.mask = null;
    });
    toast(`已转换为多边形（${pts.length} 个点）`);
    return;
  }
  let points = null;
  if (target === 'polygon') {
    if (from === 'rectangle') {
      const [[x0, y0], [x1, y1]] = boundingBoxOf(shape);
      points = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    } else if (from === 'circle') {
      points = circleToPolygon(shape.points[0], shape.points[1]);
    } else if (from === 'linestrip' || from === 'oriented_rectangle') {
      points = shape.points.map((p) => [p[0], p[1]]);
      if (points.length < 3) {
        toast('至少需要 3 个点才能转成多边形');
        return;
      }
    }
  } else if (target === 'rectangle') {
    if (['polygon', 'circle', 'oriented_rectangle', 'linestrip'].includes(from)) {
      points = boundingBoxOf(shape);
    }
  } else if (target === 'linestrip') {
    if (['polygon', 'line', 'oriented_rectangle'].includes(from)) {
      points = shape.points.map((p) => [p[0], p[1]]);
    }
  } else if (target === 'line') {
    if (from === 'linestrip' && shape.points.length === 2) {
      points = shape.points.map((p) => [p[0], p[1]]);
    } else {
      toast('只有两个端点的折线可以转成直线');
      return;
    }
  }
  if (!points) {
    toast(`暂不支持 ${from} → ${target}`);
    return;
  }
  commit(() => {
    shape.shape_type = target;
    shape.points = points;
    shape.mask = null;
    state.selectedVertex = -1;
  });
  toast(`已转换为 ${target}`);
}

/* ------------------------------------------------------------------ masks
   labelme 的 mask 形状：points 是 [[xmin,ymin],[xmax,ymax]] 外接框，
   mask 是这块区域二值图的 base64 PNG（像素值 0/1）。 */
const maskImageCache = new Map();

function buildMaskCanvas(maskStr, color) {
  const key = `${maskStr}|${color[0]},${color[1]},${color[2]}`;
  const hit = maskImageCache.get(key);
  if (hit) return hit;
  const entry = { ready: false, canvas: null };
  maskImageCache.set(key, entry);
  const img = new Image();
  img.onload = () => {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    const g = c.getContext('2d');
    g.drawImage(img, 0, 0);
    const data = g.getImageData(0, 0, c.width, c.height);
    const px = data.data;
    for (let i = 0; i < px.length; i += 4) {
      const on = px[i] > 0 || px[i + 1] > 0 || px[i + 2] > 0 || px[i + 3] > 0;
      px[i] = color[0];
      px[i + 1] = color[1];
      px[i + 2] = color[2];
      px[i + 3] = on ? 255 : 0;
    }
    g.putImageData(data, 0, 0);
    entry.canvas = c;
    entry.ready = true;
    redraw();
  };
  img.src = `data:image/png;base64,${maskStr}`;
  return entry;
}

function drawMaskShape(shape, selected, lw) {
  const [a, b] = shape.points;
  if (!a || !b) return;
  const x0 = Math.min(a[0], b[0]);
  const y0 = Math.min(a[1], b[1]);
  const w = Math.abs(b[0] - a[0]);
  const h = Math.abs(b[1] - a[1]);
  const col = colorForLabel(shape.label);
  const entry = buildMaskCanvas(shape.mask, col);
  const s = imgToScreen([x0, y0]);
  const sw = w * view.zoom;
  const sh = h * view.zoom;
  if (entry.ready && entry.canvas) {
    ctx.globalAlpha = selected ? 0.75 : 0.55;
    ctx.drawImage(entry.canvas, s[0], s[1], sw, sh);
    ctx.globalAlpha = 1;
  }
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = css(col);
  ctx.lineWidth = lw;
  ctx.strokeRect(s[0], s[1], sw, sh);
  ctx.setLineDash([]);
}

/** 把闭合形状栅格化成 labelme 格式的 mask（value 1 的前景，0 的背景）。 */
function rasterizeShapeToMask(shape) {
  const [x0, y0, x1, y1] = boundingBoxOf(shape).flat();
  const bx = Math.max(0, Math.floor(x0));
  const by = Math.max(0, Math.floor(y0));
  const bw = Math.max(1, Math.ceil(x1) - bx);
  const bh = Math.max(1, Math.ceil(y1) - by);
  const c = document.createElement('canvas');
  c.width = bw;
  c.height = bh;
  const g = c.getContext('2d');
  g.fillStyle = '#000';
  g.fillRect(0, 0, bw, bh);
  g.fillStyle = '#fff';
  const local = (p) => [p[0] - bx, p[1] - by];
  if (shape.shape_type === 'rectangle') {
    const a = local(shape.points[0]);
    const b = local(shape.points[1]);
    g.fillRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
  } else if (shape.shape_type === 'circle') {
    const cen = local(shape.points[0]);
    const r = Math.hypot(shape.points[1][0] - shape.points[0][0], shape.points[1][1] - shape.points[0][1]);
    g.beginPath();
    g.arc(cen[0], cen[1], r, 0, Math.PI * 2);
    g.fill();
  } else {
    const pts = shape.points.map(local);
    g.beginPath();
    pts.forEach((p, i) => (i === 0 ? g.moveTo(p[0], p[1]) : g.lineTo(p[0], p[1])));
    g.closePath();
    g.fill();
  }
  // 转成 0/1 灰度图（与桌面版 mask 的取值一致）
  const data = g.getImageData(0, 0, bw, bh);
  const px = data.data;
  for (let i = 0; i < px.length; i += 4) {
    const v = px[i] > 127 ? 1 : 0;
    px[i] = v;
    px[i + 1] = v;
    px[i + 2] = v;
    px[i + 3] = 255;
  }
  g.putImageData(data, 0, 0);
  const url = c.toDataURL('image/png');
  return { mask: url.split(',')[1], points: [[bx, by], [bx + bw, by + bh]] };
}

/** 把一个形状画到指定 ctx 上（用于合并、栅格化），dx/dy 是画布原点偏移。 */
function paintShapeOntoCtx(shape, g, dx = 0, dy = 0) {
  const shift = (p) => [p[0] - dx, p[1] - dy];
  if (shape.shape_type === 'mask' && shape.mask) {
    const entry = buildMaskCanvas(shape.mask, [255, 255, 255]);
    if (entry.ready && entry.canvas) {
      const [a, b] = shape.points;
      g.drawImage(entry.canvas, Math.min(a[0], b[0]) - dx, Math.min(a[1], b[1]) - dy);
      return;
    }
    // 还没解码：退化成外接框
  }
  if (shape.shape_type === 'rectangle') {
    const a = shift(shape.points[0]);
    const b = shift(shape.points[1]);
    g.fillRect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
    return;
  }
  if (shape.shape_type === 'circle') {
    const c = shift(shape.points[0]);
    const e = shift(shape.points[1]);
    g.beginPath();
    g.arc(c[0], c[1], Math.hypot(e[0] - c[0], e[1] - c[1]), 0, Math.PI * 2);
    g.fill();
    return;
  }
  const pts = shape.points.map(shift);
  if (pts.length < 3) return;
  g.beginPath();
  pts.forEach((p, i) => (i === 0 ? g.moveTo(p[0], p[1]) : g.lineTo(p[0], p[1])));
  g.closePath();
  g.fill();
}

/** 合并多个形状 → 单个 mask 形状（与桌面版「合并形状」语义一致）。 */
function mergeSelectedShapes() {
  const idx = state.selectedSet.length >= 2 ? state.selectedSet.slice() : [];
  if (idx.length < 2) {
    toast('多选用 Shift + 逐个点选，至少两个形状');
    return;
  }
  const shapes = idx.map((i) => state.shapes[i]).filter(Boolean);
  const locked = shapes.find((s) => s.locked);
  if (locked) {
    toast('选中的形状里有锁定的，先解锁再合并');
    return;
  }
  const boxes = shapes.map((s) => boundingBoxOf(s));
  const x0 = Math.max(0, Math.floor(Math.min(...boxes.map((b) => b[0][0]))));
  const y0 = Math.max(0, Math.floor(Math.min(...boxes.map((b) => b[0][1]))));
  const x1 = Math.ceil(Math.max(...boxes.map((b) => b[1][0])));
  const y1 = Math.ceil(Math.max(...boxes.map((b) => b[1][1])));
  const bw = Math.max(1, x1 - x0);
  const bh = Math.max(1, y1 - y0);
  const c = document.createElement('canvas');
  c.width = bw;
  c.height = bh;
  const g = c.getContext('2d');
  g.fillStyle = '#000';
  g.fillRect(0, 0, bw, bh);
  g.fillStyle = '#fff';
  shapes.forEach((s) => paintShapeOntoCtx(s, g, x0, y0));
  const data = g.getImageData(0, 0, bw, bh);
  const px = data.data;
  for (let i = 0; i < px.length; i += 4) {
    const v = px[i] > 127 ? 1 : 0;
    px[i] = v;
    px[i + 1] = v;
    px[i + 2] = v;
    px[i + 3] = 255;
  }
  g.putImageData(data, 0, 0);
  const mask = c.toDataURL('image/png').split(',')[1];
  commit(() => {
    const merged = {
      label: shapes[0].label,
      points: [[x0, y0], [x1, y1]],
      group_id: null,
      description: '',
      shape_type: 'mask',
      flags: {},
      mask,
    };
    state.shapes = state.shapes.filter((_, i) => !idx.includes(i));
    state.shapes.push(merged);
    state.selected = state.shapes.length - 1;
    state.selectedSet = [state.selected];
    state.selectedVertex = -1;
    return undefined;
  });
  refreshDocks();
  redraw();
  toast(`已合并 ${idx.length} 个形状为掩码`);
}

/** 从 mask 里提取最大连通区域的外轮廓，转回多边形（像素级“爬墙”追踪）。 */
function simplifyPolyline(points, epsilon = 1.0) {
  // 掩码轮廓是逐像素走出来的，矩形也能有几千个点；Douglas-Peucker 压到肉眼一致的程度。
  // 用显式栈而不是递归，点太多时递归一样会爆栈。
  const n = points.length;
  if (n < 4) return points;
  const keep = new Uint8Array(n);
  keep[0] = 1;
  keep[n - 1] = 1;
  const stack = [[0, n - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const [ax, ay] = points[a];
    const [bx, by] = points[b];
    const dx = bx - ax;
    const dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    let maxD = -1;
    let idx = -1;
    for (let i = a + 1; i < b; i++) {
      const d = Math.abs(dy * points[i][0] - dx * points[i][1] + bx * ay - by * ax) / len;
      if (d > maxD) {
        maxD = d;
        idx = i;
      }
    }
    if (maxD > epsilon && idx > 0) {
      keep[idx] = 1;
      stack.push([a, idx], [idx, b]);
    }
  }
  return points.filter((_, i) => keep[i]);
}

function maskToPolygonPoints(shape) {
  const [a, b] = shape.points;
  const x0 = Math.min(a[0], b[0]);
  const y0 = Math.min(a[1], b[1]);
  const w = Math.round(Math.abs(b[0] - a[0]));
  const h = Math.round(Math.abs(b[1] - a[1]));
  if (w < 2 || h < 2) return null;
  const entry = buildMaskCanvas(shape.mask, [255, 255, 255]);
  const readPixels = (canvas) => canvas.getContext('2d').getImageData(0, 0, w, h).data;
  let px;
  if (entry.ready && entry.canvas && entry.canvas.width === w && entry.canvas.height === h) {
    px = readPixels(entry.canvas);
  } else {
    return null;
  }
  const on = (x, y) => (x >= 0 && y >= 0 && x < w && y < h ? px[(y * w + x) * 4 + 3] > 0 : false);
  let sx = -1;
  let sy = -1;
  for (let y = 0; y < h && sy < 0; y++) {
    for (let x = 0; x < w; x++) {
      if (on(x, y)) { sx = x; sy = y; break; }
    }
  }
  if (sx < 0) return null;
  const dirs = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
  const contour = [];
  let cx = sx;
  let cy = sy;
  let dir = 0;
  const startKey = `${sx},${sy},0`;
  for (let guard = 0; guard < w * h * 4; guard++) {
    contour.push([cx + x0, cy + y0]);
    let found = false;
    for (let k = 0; k < 8; k++) {
      const nd = (dir + 6 + k) % 8;  // 先右转，再顺时针扫
      const nx = cx + dirs[nd][0];
      const ny = cy + dirs[nd][1];
      if (on(nx, ny)) {
        cx = nx;
        cy = ny;
        dir = nd;
        found = true;
        break;
      }
    }
    if (!found) break;
    if (`${cx},${cy},${dir}` === startKey) break;
  }
  return contour.length >= 3 ? simplifyPolyline(contour, 1.0) : null;
}

function copySelectedShape() {
  if (state.selected < 0) {
    toast('没有选中形状');
    return;
  }
  state.clipboard = [JSON.parse(JSON.stringify(state.shapes[state.selected]))];
  toast('已复制形状，Ctrl+V 粘贴');
}

function pasteShapes() {
  if (!state.clipboard || !state.clipboard.length) {
    toast('剪贴板里没有形状');
    return;
  }
  insertShapes(state.clipboard);
}

function duplicateSelectedShape() {
  if (state.selected < 0) {
    toast('没有选中形状');
    return;
  }
  const src = JSON.parse(JSON.stringify(state.shapes[state.selected]));
  insertShapes([src]);
}

function insertShapes(shapes, offset = 8) {
  if (!state.image) {
    toast('先打开一张图像');
    return;
  }
  const copies = shapes.map((s) => {
    const c = JSON.parse(JSON.stringify(s));
    c.points = c.points.map((p) => [p[0] + offset, p[1] + offset]);
    c.locked = false;
    return c;
  });
  commit(() => {
    state.shapes.push(...copies);
    state.selected = state.shapes.length - 1;
  });
  toast(`已粘贴 ${copies.length} 个形状`);
}

function editSelectedLabel() {
  if (state.selected < 0) {
    toast('没有选中形状');
    return;
  }
  const idx = state.selected;
  const shape = state.shapes[idx];
  askLabel(shape.label, (label, meta) => {
    if (label === null) return;
    commit(() => {
      const s = state.shapes[idx];
      s.label = label;
      s.group_id = meta && meta.group_id != null ? meta.group_id : null;
      s.description = (meta && meta.description) || '';
      s.flags = (meta && meta.flags) || {};
    });
    addLabel(label);
  }, { group_id: shape.group_id, description: shape.description, flags: shape.flags || {} });
}

/* ------------------------------------------------------------- label dialog */
let labelCallback = null;
let labelReturnValue = null;

function askLabel(defaultValue, cb, initial) {
  labelCallback = cb;
  const modal = $('labelModal');
  const input = $('labelInput');
  input.value = defaultValue || '';
  $('labelGroup').value = initial && initial.group_id != null ? String(initial.group_id) : '';
  $('labelDesc').value = (initial && initial.description) || '';
  renderFlagChecks('labelFlags', (initial && initial.flags) || {});

  // 全部标签进 datalist（输入时可下拉选择，等价桌面版的 label 下拉框）
  const datalist = $('labelDatalist');
  datalist.innerHTML = '';
  state.labels.forEach((l) => {
    const opt = document.createElement('option');
    opt.value = l;
    datalist.appendChild(opt);
  });

  const recent = $('recentLabels');
  recent.innerHTML = '';
  state.labels
    .slice()
    .reverse()
    .slice(0, 60)
    .forEach((l) => {
      const b = document.createElement('button');
      b.textContent = l;
      b.addEventListener('click', () => {
        input.value = l;
        input.focus();
      });
      recent.appendChild(b);
    });

  modal.hidden = false;
  setTimeout(() => {
    input.focus();
    input.select();
  }, 30);
}

function renderFlagChecks(boxId, checkedMap) {
  const box = $(boxId);
  if (!box) return;
  mergeUsedFlags();
  box.innerHTML = '';
  if (!state.flagDefs.length) {
    box.innerHTML = '<span class="hint">还没有 flag 定义，点「+ 新建」添加</span>';
    return;
  }
  state.flagDefs.forEach((name) => {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = name;
    cb.checked = !!checkedMap[name];
    const span = document.createElement('span');
    span.textContent = name;
    label.appendChild(cb);
    label.appendChild(span);
    box.appendChild(label);
  });
}

function collectFlagChecks(boxId) {
  const box = $(boxId);
  const out = {};
  if (!box) return out;
  box.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    if (cb.checked) out[cb.value] = true;
  });
  return out;
}

function currentLabelMeta() {
  const g = $('labelGroup').value.trim();
  return {
    group_id: g === '' ? null : Math.max(0, parseInt(g, 10) || 0),
    description: $('labelDesc').value.trim(),
    flags: collectFlagChecks('labelFlags'),
  };
}

function closeLabelDialog(value) {
  $('labelModal').hidden = true;
  const cb = labelCallback;
  labelCallback = null;
  if (cb) cb(value, value === null ? null : currentLabelMeta());
}

async function refreshDocks() {
  refreshLabelList();
  refreshShapeList();
  refreshImageList();
  updateStatus();
}

function refreshLabelList() {
  const box = $('labelList');
  if (!box) return;
  const counts = new Map();
  state.shapes.forEach((s) => counts.set(s.label, (counts.get(s.label) || 0) + 1));
  if (!counts.size) {
    box.innerHTML = '<div class="empty-list">暂无标签</div>';
    return;
  }
  box.innerHTML = '';
  [...counts.entries()].sort((a, b) => b[1] - a[1]).forEach(([label, n]) => {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<span class="dot" style="background:${css(colorForLabel(label))}"></span>
      <span class="name"></span><span class="idxtag">${n}</span>`;
    row.querySelector('.name').textContent = label;
    row.addEventListener('click', () => {
      const idx = state.shapes.findIndex((s) => s.label === label);
      state.selected = idx;
      refreshDocks();
      redraw();
    });
    box.appendChild(row);
  });
}

function refreshShapeList() {
  const box = $('shapeList');
  if (!box) return;
  if (!state.shapes.length) {
    box.innerHTML = '<div class="empty-list">暂无形状</div>';
    return;
  }
  box.innerHTML = '';
  state.shapes.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'row' + (i === state.selected ? ' selected' : '');
    row.innerHTML = `<span class="dot" style="background:${css(colorForLabel(s.label))}"></span>
      <span class="name"></span><span class="idxtag">${s.locked ? '🔒' : ''}${s.shape_type}</span>`;
    row.querySelector('.name').textContent = s.label;
    row.addEventListener('click', () => {
      state.selected = i;
      state.selectedVertex = -1;
      refreshDocks();
      redraw();
    });
    box.appendChild(row);
  });
}

function refreshImageList() {
  const box = $('imageList');
    if (!box) return;
  const q = state.search.trim().toLowerCase();
  const items = state.images.filter((x) => !q || x.name.toLowerCase().includes(q));
  if (!items.length) {
    box.innerHTML = state.images.length
      ? '<div class="empty-list">没有匹配的文件</div>'
      : '<div class="empty-list">工程内暂无图像，先上传</div>';
    return;
  }
  box.innerHTML = '';
  items.forEach((item) => {
    const row = document.createElement('div');
    const active = state.image && state.image.name === item.name;
    row.className = 'row' + (active ? ' selected' : '');
    row.innerHTML = `<input type="checkbox" ${item.annotated ? 'checked' : ''} />
      <span class="name"></span>`;
    row.querySelector('.name').textContent = item.name;
    row.querySelector('input').addEventListener('click', (e) => e.stopPropagation());
    row.addEventListener('click', () => {
      openImage(item.name);
    });
    box.appendChild(row);
  });
}

function updateStatus() {
  const left = $('statusLeft');
  const mid = $('statusMid');
  const sess = $('statusSession');
  const file = $('statusFile');
  if (sess) sess.textContent = state.sessionId ? '工程空间已连接' : '未连接工程';
  if (file) file.textContent = state.image ? state.image.name : '未打开文件';
  if (mid) {
    const total = state.images.length;
    const done = state.images.filter((x) => x.annotated).length;
    mid.textContent = `${state.image ? state.shapes.length : 0} 个形状 · 已标注 ${done}/${total}`;
  }
  if (left) {
    left.textContent = state.dirty ? '未保存…' : '就绪';
  }
}

/* ------------------------------------------------- 断线/崩溃后的会话恢复 */
/** 把“当前会话 + 当前图像 + 时间”写进 localStorage。
 *  浏览器进程被强杀、蓝屏、断电时 sessionStorage 会没了，但 localStorage 落在磁盘上。 */
function rememberSession(imageName) {
  if (!state.sessionId) return;
  try {
    localStorage.setItem(STORE_LAST, JSON.stringify({
      session: state.sessionId,
      image: imageName || (state.image && state.image.name) || null,
      at: Date.now(),
    }));
  } catch (e) { /* ignore */ }
}

function readLastSession() {
  try {
    const raw = localStorage.getItem(STORE_LAST);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (!obj || !/^(?:\d{14}(_\d+)?|workspace-[0-9a-f]{12})$/.test(String(obj.session || ''))) return null;
    if (Date.now() - Number(obj.at || 0) > LAST_SESSION_MAX_AGE_MS) return null;
    return obj;
  } catch (e) {
    return null;
  }
}

function draftKey(session, name) {
  return `${DRAFT_PREFIX}${session}.${name}`;
}

/** 服务器侧的“最近一次动过的会话”：localStorage 被硬杀弄丢后的唯一线索。 */
async function fetchRecent() {
  try {
    const q = projectQuery();
    return await req('GET', '/api/recent' + q);
  } catch (e) {
    return null;
  }
}

let draftTimer = null;
let serverDraftTimer = null;

/** 每次改动都存两份草稿：
 *  - localStorage（250ms 合并）：标签页崩溃、刷新、正常关闭时最快；
 *  - 服务器 data/<session>/.drafts/（600ms 合并）：硬杀进程/蓝屏/断电时
 *    只有它能活下来（实测硬杀 Edge 后 localStorage 直接丢）。
 *  真正落盘到标注 JSON 是 800ms 的自动保存，保存成功会清掉这两份草稿。 */
function scheduleDraft() {
  if (!state.sessionId || !state.image) return;
  const session = state.sessionId;
  const target = state.image.name;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    if (state.sessionId === session && state.image && state.image.name === target) writeDraft();
  }, 250);
  clearTimeout(serverDraftTimer);
  serverDraftTimer = setTimeout(() => pushDraft(session, target), 600);
}

function cancelDraftTimers() {
  clearTimeout(draftTimer);
  clearTimeout(serverDraftTimer);
}

function draftPayload() {
  return {
    shapes: state.shapes,
    flags: state.flags,
    imageWidth: state.image.w,
    imageHeight: state.image.h,
    at: Date.now(),
  };
}

function writeDraft() {
  if (!state.sessionId || !state.image) return;
  try {
    localStorage.setItem(
      draftKey(state.sessionId, state.image.name),
      JSON.stringify({
        shapes: state.shapes,
        flags: state.flags,
        w: state.image.w,
        h: state.image.h,
        at: Date.now(),
      })
    );
  } catch (e) { /* 超出配额就算了，服务器那份还在 */ }
}

async function pushDraft(session, target) {
  if (!session || !target) return;
  if (state.sessionId !== session || !state.image || state.image.name !== target) return;
  try {
    await req('PUT', `/api/sessions/${session}/drafts/${encodeURIComponent(target)}`, draftPayload());
  } catch (e) { /* 离线/会话没了就不管，标注本身还有自动保存 */ }
}

function flushDraftNow() {
  if (!state.sessionId || !state.image || !state.dirty) return;
  const session = state.sessionId;
  const target = state.image.name;
  cancelDraftTimers();
  writeDraft();
  pushDraft(session, target);
}

function readDraft(session, name) {
  try {
    const raw = localStorage.getItem(draftKey(session, name));
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function clearDraft(session, name) {
  try {
    localStorage.removeItem(draftKey(session, name));
  } catch (e) { /* ignore */ }
  req('DELETE', `/api/sessions/${session}/drafts/${encodeURIComponent(name)}`)
    .catch(() => { /* 没有就算了 */ });
}

/** 服务器上的草稿：硬杀进程 / 蓝屏 / 断电后 localStorage 已经没了，
 *  只有 data/<session>/.drafts/ 里这一份还在。stale 表示标注 JSON 比草稿新，直接丢掉。 */
async function readServerDraft(session, name) {
  try {
    const res = await req('GET', `/api/sessions/${session}/drafts/${encodeURIComponent(name)}`);
    return res && res.draft ? res : null;
  } catch (e) {
    return null;
  }
}

/** 同一个浏览器里可能开着别的标签页。开新标签页时先问一句“有人活着吗”，
 *  有人用着同一个会话就新建一个，避免两个页面同时写同一份 JSON。 */
function probeLiveTab(session, timeoutMs) {
  return new Promise((resolve) => {
    if (typeof BroadcastChannel === 'undefined') {
      resolve(false);
      return;
    }
    let ch;
    try {
      ch = new BroadcastChannel(LIVE_CHANNEL);
    } catch (e) {
      resolve(false);
      return;
    }
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      try { ch.close(); } catch (e) { /* ignore */ }
      resolve(v);
    };
    ch.onmessage = (ev) => {
      const d = ev.data || {};
      if (d.type === 'pong' && d.session === session) finish(true);
    };
    try {
      ch.postMessage({ type: 'ping' });
    } catch (e) { /* ignore */ }
    setTimeout(() => finish(false), timeoutMs || 450);
  });
}

function initLiveChannel() {
  if (typeof BroadcastChannel === 'undefined') return;
  try {
    const ch = new BroadcastChannel(LIVE_CHANNEL);
    ch.onmessage = (ev) => {
      const d = ev.data || {};
      if (d.type === 'ping' && state.sessionId) {
        ch.postMessage({ type: 'pong', session: state.sessionId });
      }
    };
  } catch (e) { /* ignore */ }
}

/* --------------------------------------------------------------- projects */
function readProjectPref() {
  try {
    const obj = JSON.parse(localStorage.getItem(STORE_PROJECT) || 'null');
    return obj && obj.project ? obj : null;
  } catch (e) {
    return null;
  }
}

function rememberProject() {
  try {
    localStorage.setItem(STORE_PROJECT, JSON.stringify({ owner: state.owner, project: state.project }));
  } catch (e) { /* ignore */ }
}

function projectQuery() {
  const p = new URLSearchParams();
  if (state.owner) p.set('owner', state.owner);
  if (state.project) p.set('project', state.project);
  const s = p.toString();
  return s ? `?${s}` : '';
}

function projectLabel() {
  return `${state.owner || '—'} / ${state.project || '—'}`;
}

/** 登录后先定工程：服务器给的列表里，优先沿用上次那个工程。 */
async function initProjects() {
  const pref = readProjectPref();
  const res = await req('GET', '/api/projects');
  state.owner = res.owner;
  state.projects = res.projects || [];
  const match = pref && state.projects.find((p) => p.owner === state.owner && p.name === pref.project);
  state.project = match ? match.project : (state.projects[0] && state.projects[0].name) || '默认工程';
  if (!state.projects.some((p) => p.name === state.project)) {
    await req('POST', '/api/projects', { name: state.project });
    state.projects = (await req('GET', '/api/projects')).projects || [];
  }
  rememberProject();
  const sp = $('statusProject');
  if (sp) sp.textContent = projectLabel();
}

/** 切换工程：先保存当前图，再换标签集与工程空间。 */
async function switchProject(name) {
  const target = String(name || '').trim();
  if (!target || target === state.project) return;
  try {
    if (state.dirty && state.image) await saveNow(true);
  } catch (e) { /* 保存失败也照常切，草稿在服务器上 */ }
  state.project = target;
  state.projects = (await req('GET', '/api/projects')).projects || state.projects;
  rememberProject();
  const sp = $('statusProject');
  if (sp) sp.textContent = projectLabel();
  state.sessionId = null;
  sessionStorage.removeItem(STORE_SESSION);
  resetSessionState();
  await loadLabels();
  const sessions = await listSessions();
  // 一个工程只有一个固定工作空间；有图就接回，没图就等上传
  const withImages = sessions.filter((s) => s.images > 0);
  const next = withImages[0] || sessions[0];
  if (next) {
    await loadSession(next.session, null);
  } else {
    await newSession();
  }
  updateStatus();
  toast(`已切换到工程「${target}」`);
}

async function createProject(name) {
  const clean = String(name || '').trim();
  if (!clean) throw new Error('工程名不能为空');
  const res = await req('POST', '/api/projects', { name: clean });
  state.projects = (await req('GET', '/api/projects')).projects || [];
  await switchProject(res.project ? res.project.name : clean);
}

function openProjectModal() {
  const modal = $('projectModal');
  if (!modal) return;
  modal.hidden = false;
  const box = $('projectList');
  box.innerHTML = '<div class="empty-list">加载中…</div>';
  req('GET', '/api/projects').then((res) => {
    state.projects = res.projects || [];
    if (!state.projects.length) {
      box.innerHTML = '<div class="empty-list">还没有工程，下面新建一个</div>';
      return;
    }
    box.innerHTML = '';
    state.projects.forEach((p) => {
      const row = document.createElement('div');
      row.className = 'row' + (p.name === state.project ? ' selected' : '');
      row.innerHTML = `<span class="name"></span><span class="meta">${p.created_at}</span>`;
      row.querySelector('.name').textContent = p.name;
      row.addEventListener('click', async () => {
        modal.hidden = true;
        try {
          await switchProject(p.name);
        } catch (e) {
          toast(e.message, true);
        }
      });
      box.appendChild(row);
    });
  }).catch((e) => {
    box.innerHTML = `<div class="empty-list">${e.message}</div>`;
  });
  const input = $('newProjectName');
  if (input) input.value = '';
}

async function submitProjectModal() {
  const input = $('newProjectName');
  if (!input || !input.value.trim()) return;
  try {
    $('projectModal').hidden = true;
    await createProject(input.value.trim());
  } catch (e) {
    toast(e.message, true);
  }
}

/* --------------------------------------------------------------- sessions */
function resetSessionState() {
  cancelDraftTimers();
  state.image = null;
  state.shapes = [];
  state.flags = {};
  state.selected = -1;
  state.selectedSet = [];
  state.images = [];
  state.undo = [];
  state.redo = [];
  state.drawing = null;
  state.drag = null;
  state.dirty = false;
}

async function newSession(reason) {
  const res = await req('POST', '/api/sessions' + projectQuery());
  state.sessionId = res.session;
  sessionStorage.setItem(STORE_SESSION, res.session);
  resetSessionState();
  state.images = (await req('GET', `/api/sessions/${res.session}/images`)).images || [];
  rememberSession(null);
  showEmpty(state.images.length === 0);
  refreshDocks();
  refreshImageList();
  updateStatus();
  redraw();
  if (reason) toast(`${reason}，已重新连接工程空间`, true);
  return res.session;
}

/** 会话目录可能在服务器上被删掉（清空 data/、换机器、他人清理），
 *  浏览器 sessionStorage 里的 id 就成了悬空引用。这里先探活，失效就地重建。 */
async function ensureSessionAlive(reason) {
  if (!state.sessionId) return await newSession();
  try {
    await req('GET', `/api/sessions/${state.sessionId}/images`);
    return state.sessionId;
  } catch (e) {
    if (e.status === 404) return await newSession(reason || '原工程目录已不存在');
    throw e;
  }
}

async function ensureSession() {
  const id = sessionStorage.getItem(STORE_SESSION);
  if (id) {
    try {
      await req('GET', `/api/sessions/${id}/images`);
      state.sessionId = id;
      return id;
    } catch (e) {
      if (e.status !== 404) throw e;
    }
  }
  return await newSession();
}

async function loadSession(id, preferredImage) {
  cancelDraftTimers();
  state.sessionId = id;
  sessionStorage.setItem(STORE_SESSION, id);
  state.shapes = [];
  state.flags = {};
  state.selected = -1;
  state.undo = [];
  state.redo = [];
  state.image = null;
  const res = await req('GET', `/api/sessions/${id}/images`);
  state.images = res.images || [];
  if (res.owner && res.project && (res.owner !== state.owner || res.project !== state.project)) {
    // 从 URL / 别的入口进到另一个工程的会话时，把当前工程同步过来
    state.owner = res.owner;
    state.project = res.project;
    rememberProject();
    const sp = $('statusProject');
    if (sp) sp.textContent = projectLabel();
    await loadLabels();
  }
  rememberSession(preferredImage);
  refreshImageList();
  updateStatus();
  redraw();
  showEmpty(state.images.length === 0);
  if (state.images.length) {
    const wanted = preferredImage && state.images.some((x) => x.name === preferredImage)
      ? preferredImage
      : state.images[0].name;
    await openImage(wanted);
  }
}

async function listSessions() {
  const res = await req('GET', '/api/sessions' + projectQuery());
  // 后端契约是数组；这里兼容一下旧版 {sessions:[...]}，避免任一版本升级错位把列表弄空
  if (Array.isArray(res)) return res;
  return (res && Array.isArray(res.sessions)) ? res.sessions : [];
}

async function uploadFiles(files) {
  const picked = [...files].filter((f) => IMAGE_RE.test(f.name));
  if (!picked.length) {
    toast('没有可用的图像文件（jpg/png/bmp/webp…）', true);
    return;
  }
  const form = new FormData();
  picked.forEach((f) => form.append('files', f, f.name));
  const doUpload = () => req('POST', `/api/sessions/${state.sessionId}/images`, form, true);

  try {
    await ensureSessionAlive('上传前的工程空间已失效');
    let res;
    try {
      res = await doUpload();
    } catch (e) {
      if (e.status === 404 && /session|工程/i.test(e.message)) {
        await newSession('上传时工程目录已消失');
        res = await doUpload();
      } else {
        throw e;
      }
    }
    toast(`已上传 ${res.saved.length} 张图像`);
    await reloadImages();
    const last = res.saved[res.saved.length - 1];
    if (last) await openImage(last);
  } catch (e) {
    toast(`上传失败：${e.message}`, true);
  }
}

async function reloadImages() {
  if (!state.sessionId) return;
  const res = await req('GET', `/api/sessions/${state.sessionId}/images`);
  state.images = res.images || [];
  refreshImageList();
  updateStatus();
}

/* ------------------------------------------------------------ image loading */
function showEmpty(show) {
  $('emptyState').hidden = !show;
}

async function openImage(name, carryShapes = null, keepView = null) {
  if (!state.sessionId) return;
  if (state.image && state.image.name === name) return;
  if (state.dirty && state.image) await saveNow(true);
  cancelDraftTimers();

  const url = `/api/sessions/${state.sessionId}/raw/${encodeURIComponent(name)}`;
  const im = new Image();
  im.decoding = 'async';
  im.src = url;

  await new Promise((resolve, reject) => {
    im.onload = resolve;
    im.onerror = () => reject(new Error('图像加载失败'));
  }).catch((e) => {
    toast(e.message, true);
  });
  if (!im.naturalWidth) return;

  if (state.image && !state.keepBc) {
    state.brightness = 0;
    state.contrast = 0;
  }

  const canvasImg = document.createElement('canvas');
  canvasImg.width = im.naturalWidth;
  canvasImg.height = im.naturalHeight;
  const c2 = canvasImg.getContext('2d');
  c2.filter = `brightness(${1 + state.brightness / 100}) contrast(${1 + state.contrast / 100})`;
  c2.drawImage(im, 0, 0);

  state.image = { name, w: im.naturalWidth, h: im.naturalHeight, canvas: canvasImg };
  state.index = state.images.findIndex((x) => x.name === name);
  state.selected = -1;
  state.selectedVertex = -1;
  state.hover = null;
  state.drawing = null;
  state.undo = [];
  state.redo = [];
  state.dirty = false;
  showEmpty(false);

  try {
    const ann = await req('GET', `/api/sessions/${state.sessionId}/annotations/${encodeURIComponent(name)}`);
    state.shapes = Array.isArray(ann.shapes) ? ann.shapes : [];
    state.flags = ann.flags && typeof ann.flags === 'object' ? ann.flags : {};
    const known = new Set(['version', 'flags', 'shapes', 'imagePath', 'imageData', 'imageHeight', 'imageWidth']);
    state.extra = {};
    Object.keys(ann || {}).forEach((k) => {
      if (!known.has(k)) state.extra[k] = ann[k];
    });
    state.shapes.forEach((s) => addLabel(s.label));
  } catch (e) {
    if (e.status === 404) {
      state.shapes = [];
      state.flags = {};
      state.extra = {};
    } else {
      toast(`读取标注失败：${e.message}`, true);
      state.shapes = [];
      state.extra = {};
    }
  }

  if (carryShapes && carryShapes.length && state.shapes.length === 0) {
    state.shapes = carryShapes.map((s) => JSON.parse(JSON.stringify(s)));
    state.dirty = true;
    scheduleDraft();
    toast(`已沿用上一张的 ${state.shapes.length} 个形状`);
  }

  // 先看 localStorage 里的草稿（同一次浏览器进程内最靠谱），
  // 再看服务器 .drafts/ 里的（进程被硬杀、蓝屏、断电后只剩它）
  let draft = readDraft(state.sessionId, name);
  let draftFrom = '浏览器';
  if (!draft) {
    const remote = await readServerDraft(state.sessionId, name);
    if (remote && !remote.stale) {
      draft = remote.draft;
      draftFrom = '服务器';
    } else if (remote && remote.stale) {
      clearDraft(state.sessionId, name);
    }
  }
  if (draft && Array.isArray(draft.shapes)) {
    const dw = Number(draft.w || draft.imageWidth);
    const dh = Number(draft.h || draft.imageHeight);
    const sameSize = dw === state.image.w && dh === state.image.h;
    if (sameSize && JSON.stringify(draft.shapes) !== JSON.stringify(state.shapes)) {
      state.shapes = draft.shapes;
      state.flags = draft.flags && typeof draft.flags === 'object' ? draft.flags : state.flags;
      toast(`已恢复 ${name} 未保存的草稿（${state.shapes.length} 个形状，来自${draftFrom}）`);
      scheduleSave();
    } else if (sameSize) {
      clearDraft(state.sessionId, name);
    }
  }

  rememberSession(name);
  if (keepView) {
    view.zoom = keepView.zoom;
    view.tx = keepView.tx;
    view.ty = keepView.ty;
    updateZoomLabel();
  } else {
    fitToWindow();
  }
  refreshDocks();
  refreshImageList();
  updateStatus();
}

async function navigate(step) {
  if (!state.images.length) return;
  const cur = state.image ? state.images.findIndex((x) => x.name === state.image.name) : -1;
  const next = cur + step;
  if (next < 0 || next >= state.images.length) {
    toast(step > 0 ? '已经是最后一张' : '已经是第一张');
    return;
  }
  const carry = state.keepPrev && state.image
    ? JSON.parse(JSON.stringify(state.shapes))
    : null;
  const keepView = state.keepZoom && state.image ? { ...view } : null;
  await openImage(state.images[next].name, carry, keepView);
}

function closeCurrentImage() {
  if (!state.image) return;
  if (state.dirty) saveNow(true);
  state.image = null;
  state.shapes = [];
  state.selected = -1;
  state.selectedVertex = -1;
  state.hover = null;
  state.drawing = null;
  state.undo = [];
  state.redo = [];
  showEmpty(state.images.length === 0);
  refreshDocks();
  redraw();
  toast('已关闭当前图像');
}

async function deleteAnnotationFile() {
  if (!state.image) return;
  const name = state.image.name;
  if (!confirm(`删除 ${name}.json 标注文件？（图像保留）`)) return;
  try {
    await req('DELETE', `/api/sessions/${state.sessionId}/annotations/${encodeURIComponent(name)}`);
    clearDraft(state.sessionId, name);
    state.shapes = [];
    state.flags = {};
    state.selected = -1;
    state.selectedVertex = -1;
    state.dirty = false;
    const item = state.images.find((x) => x.name === name);
    if (item) item.annotated = false;
    refreshDocks();
    redraw();
    toast(`已删除 ${name}.json`);
  } catch (e) {
    toast(`删除标注失败：${e.message}`, true);
  }
}

async function deleteCurrentImage() {
  if (!state.image) return;
  const name = state.image.name;
  if (!confirm(`删除图像 ${name} 及其标注？`)) return;
  try {
    await req('DELETE', `/api/sessions/${state.sessionId}/images/${encodeURIComponent(name)}`);
    clearDraft(state.sessionId, name);
    state.image = null;
    state.shapes = [];
    state.selected = -1;
    await reloadImages();
    redraw();
    showEmpty(state.images.length === 0);
    toast(`已删除 ${name}`);
    if (state.images.length) await openImage(state.images[0].name);
  } catch (e) {
    toast(`删除失败：${e.message}`, true);
  }
}

function downloadAnnotation() {
  if (!state.image) {
    toast('没有打开图像');
    return;
  }
  const data = buildAnnotation();
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${state.image.name}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function exportLabelsFile() {
  const labels = (state.labels || []).slice();
  if (!labels.length) {
    toast('当前工程还没有标签');
    return;
  }
  const blob = new Blob([labels.join('\n') + '\n'], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'labels.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast(`已导出 ${labels.length} 个标签`);
}

function importLabelsFile() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.txt,text/plain';
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const names = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      if (!names.length) {
        toast('文件里没有标签', true);
        return;
      }
      const res = await fetch('/api/labels' + projectQuery(), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ labels: names }),
      });
      if (!res.ok) throw new Error(`导入失败（${res.status}）`);
      await loadLabels();
      refreshDocks();
      toast(`已导入 ${names.length} 个标签`);
    } catch (err) {
      toast(err.message || '导入失败', true);
    }
  });
  input.click();
}

/* ------------------------------------------------------------------ icons */
const ICONS = {
  open: '<svg viewBox="0 0 24 24"><path d="M6 3h7l5 5v13H6z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M13 3v5h5" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M4 7a1 1 0 0 1 1-1h4l2 2h7a1 1 0 0 1 1 1v10H4z" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  prev: '<svg viewBox="0 0 24 24"><path d="M15 5l-8 7 8 7" fill="none" stroke="currentColor" stroke-width="1.9"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M9 5l8 7-8 7" fill="none" stroke="currentColor" stroke-width="1.9"/></svg>',
  save: '<svg viewBox="0 0 24 24"><path d="M5 4h11l3 3v13H5z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M8 4v5h7V4M8 20v-6h8v6" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  undo: '<svg viewBox="0 0 24 24"><path d="M9 7L4 12l5 5" fill="none" stroke="currentColor" stroke-width="1.9"/><path d="M4 12h9a6 6 0 1 1-6 6" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="8" y="4" width="12" height="14" fill="none" stroke="currentColor" stroke-width="1.7"/><rect x="4" y="8" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  del: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2"/></svg>',
  sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M6 6l2 2M16 16l2 2M18 6l-2 2M8 16l-2 2" stroke="currentColor" stroke-width="1.5"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.7"/></svg>',
  select: '<svg viewBox="0 0 24 24"><path d="M6 4l12 8-5 1.6L11 19z" fill="currentColor"/></svg>',
  polygon: '<svg viewBox="0 0 24 24"><path d="M4 9l6-5 9 3-2 9-9 2z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="4" cy="9" r="1.6" fill="currentColor"/><circle cx="19" cy="16" r="1.6" fill="currentColor"/></svg>',
  rectangle: '<svg viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="4" cy="6" r="2" fill="currentColor"/></svg>',
  oriented: '<svg viewBox="0 0 24 24"><path d="M8 4l12 3-2 12-12-3z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  circle: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/></svg>',
  circle3: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="6" cy="10" r="1.8" fill="currentColor"/><circle cx="14" cy="6" r="1.8" fill="currentColor"/><circle cx="17" cy="15" r="1.8" fill="currentColor"/></svg>',
  point: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" fill="currentColor"/></svg>',
  line: '<svg viewBox="0 0 24 24"><path d="M5 19L19 5" stroke="currentColor" stroke-width="2"/><circle cx="5" cy="19" r="2" fill="currentColor"/><circle cx="19" cy="5" r="2" fill="currentColor"/></svg>',
  linestrip: '<svg viewBox="0 0 24 24"><path d="M4 18l5-6 4 3 7-9" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="4" cy="18" r="1.8" fill="currentColor"/><circle cx="16" cy="6" r="1.8" fill="currentColor"/></svg>',
  ai: '<svg viewBox="0 0 24 24"><path d="M12 4l2 5 5 2-5 2-2 5-2-5-5-2 5-2z" fill="currentColor"/></svg>',
  fit: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
};

/* ------------------------------------------------------------------ menus */
const MENUS = [
  {
    name: '文件',
    items: [
      { id: 'openFile', label: '打开文件(O)…', sc: 'Ctrl+O' },
      { id: 'upload', label: '打开文件夹…', sc: 'Ctrl+Shift+O' },
      { id: 'projects', label: '切换工程…' },
      { id: 'newProject', label: '新建工程…' },
      { sep: true },
      { id: 'save', label: '保存(S)', sc: 'Ctrl+S' },
      { id: 'saveAs', label: '另存为 JSON…', sc: 'Ctrl+Shift+S' },
      { id: 'autoSave', label: '自动保存（停手 0.8s 落盘）' },
      { id: 'embedImage', label: '把图像数据内嵌到 JSON（imageData）' },
      { id: 'exportLabels', label: '导出标签集（labels.txt）…' },
      { id: 'importLabels', label: '导入标签集（labels.txt）…' },
      { sep: true },
      { id: 'imgFlags', label: '编辑图像标记（image flags）…' },
      { sep: true },
      { id: 'closeImage', label: '关闭当前图像', sc: 'Ctrl+W' },
      { id: 'deleteAnnotation', label: '删除标注文件…' },
      { id: 'deleteImage', label: '删除当前图像…' },
      { sep: true },
      { id: 'logout', label: '退出登录' },
    ],
  },
  {
    name: '编辑',
    items: [
      { id: 'undo', label: '撤销', sc: 'Ctrl+Z' },
      { id: 'redo', label: '重做', sc: 'Ctrl+Y' },
      { id: 'undoLastPoint', label: '撤销上一个点', sc: '绘图时 Ctrl+Z' },
      { sep: true },
      { id: 'copyShape', label: '复制到剪贴板', sc: 'Ctrl+C' },
      { id: 'pasteShape', label: '从剪贴板粘贴', sc: 'Ctrl+V' },
      { id: 'duplicateShape', label: '复制形状（副本）', sc: 'Ctrl+D' },
      { id: 'deleteShape', label: '删除形状', sc: 'Delete' },
      { sep: true },
      { id: 'deletePoint', label: '删除选中顶点', sc: 'Backspace' },
      { id: 'addPoint', label: '在边上加点' },
      { id: 'lockShape', label: '锁定 / 解锁形状', sc: 'Ctrl+Shift+L' },
      { id: 'undoDepth', label: '撤销步数…' },
      { sep: true },
      { id: 'hideShapes', label: '隐藏全部形状' },
      { id: 'showShapes', label: '显示全部形状' },
      { id: 'toggleShapes', label: '切换形状显示', sc: 'T' },
      { sep: true },
      { id: 'selectUp', label: '选中上方形状', sc: 'Ctrl+↑' },
      { id: 'selectDown', label: '选中下方形状', sc: 'Ctrl+↓' },
      { id: 'selectLeft', label: '选中左侧形状', sc: 'Ctrl+←' },
      { id: 'selectRight', label: '选中右侧形状', sc: 'Ctrl+→' },
      { sep: true },
      { id: 'editShapeLabel', label: '修改选中形状标签…' },
      { id: 'editLabels', label: '编辑标签集…' },
    ],
  },
  {
    name: '转换',
    items: [
      { id: 'convertPolygon', label: '转换为多边形' },
      { id: 'convertRectangle', label: '转换为矩形（外接框）' },
      { id: 'convertLinestrip', label: '转换为折线' },
      { id: 'convertLine', label: '转换为直线' },
      { id: 'mergeShapes', label: '合并所选形状（多选后）' },
      { id: 'convertMask', label: '转换为掩码（二值图）' },
    ],
  },
  {
    name: '视图',
    items: [
      { id: 'fit', label: '适合窗口', sc: 'Ctrl+F' },
      { id: 'fitSelection', label: '适合选中形状', sc: 'Ctrl+Shift+F' },
      { id: 'zoom100', label: '原始大小', sc: 'Ctrl+0' },
      { id: 'zoomIn', label: '放大', sc: 'Ctrl++' },
      { id: 'zoomOut', label: '缩小', sc: 'Ctrl+-' },
      { sep: true },
      { id: 'bc', label: '亮度 / 对比度…' },
      { id: 'keepBc', label: '保留上一张亮度/对比度（切换图像时）' },
      { id: 'fillDrawing', label: '绘制时填充多边形' },
      { id: 'crosshair', label: '显示十字准星' },
      { id: 'allowOob', label: '允许点在图像外（不裁剪）' },
      { id: 'keepPrev', label: '保留上一张标注（切换图像时）' },
      { id: 'keepZoom', label: '保留上一张缩放（切换图像时）' },
      { id: 'reload', label: '刷新图像列表' },
      { sep: true },
      { id: 'toggleDocks', label: '显示 / 隐藏侧边面板' },
      { id: 'resetLayout', label: '重置面板布局' },
    ],
  },
  {
    name: '帮助',
    items: [
      { id: 'help', label: '快捷键与关于' },
    ],
  },
];

function buildMenubar() {
  const bar = $('menubar');
  MENUS.forEach((menu) => {
    const btn = document.createElement('button');
    btn.className = 'menu-btn';
    btn.textContent = menu.name;

    const list = document.createElement('div');
    list.className = 'menu-list';

    menu.items.forEach((item) => {
      if (item.sep) {
        const s = document.createElement('div');
        s.className = 'menu-sep';
        list.appendChild(s);
        return;
      }
      const row = document.createElement('div');
      row.className = 'menu-item';
      const label = document.createElement('span');
      label.textContent = item.label;
      const sc = document.createElement('span');
      sc.className = 'sc';
      sc.textContent = item.sc || '';
      row.appendChild(label);
      row.appendChild(sc);
      row.addEventListener('click', () => {
        hideMenus();
        runAction(item.id);
      });
      list.appendChild(row);
    });

    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const open = list.classList.contains('show');
      hideMenus();
      if (!open) {
        const rect = btn.getBoundingClientRect();
        list.style.left = `${rect.left}px`;
        list.classList.add('show');
        btn.classList.add('open');
      }
    });

    bar.appendChild(btn);
    bar.appendChild(list);
  });

  document.addEventListener('click', hideMenus);
}

function hideMenus() {
  document.querySelectorAll('.menu-list').forEach((el) => el.classList.remove('show'));
  document.querySelectorAll('.menu-btn').forEach((el) => el.classList.remove('open'));
}

/* ---------------------------------------------------------------- toolbar */
const TOOLBAR = [
  { id: 'openFile', icon: 'open', text: '打开文件' },
  { id: 'upload', icon: 'folder', text: '打开文件夹' },
  { sep: true },
  { id: 'prev', icon: 'prev', text: '上一张' },
  { id: 'next', icon: 'next', text: '下一张' },
  { sep: true },
  { id: 'save', icon: 'save', text: '保存' },
  { id: 'deleteImage', icon: 'trash', text: '删除文件' },
  { sep: true },
  { id: 'undo', icon: 'undo', text: '撤销' },
  { id: 'duplicateShape', icon: 'copy', text: '复制' },
  { id: 'deleteShape', icon: 'del', text: '删除' },
  { sep: true },
  { id: 'bc', icon: 'sun', text: '亮度对比度' },
];

function buildToolbar() {
  const bar = $('toolbar');
  bar.innerHTML = '';

  TOOLBAR.forEach((item) => {
    if (item.sep) {
      const s = document.createElement('div');
      s.className = 'tb-sep';
      bar.appendChild(s);
      return;
    }
    const btn = document.createElement('button');
    btn.className = 'tb-btn';
    btn.innerHTML = `${ICONS[item.icon] || ''}<span>${item.text}</span>`;
    btn.title = item.text;
    btn.addEventListener('click', () => runAction(item.id));
    bar.appendChild(btn);
  });

  // zoom box
  const zoom = document.createElement('div');
  zoom.className = 'zoom-box';
  zoom.innerHTML = `<button data-z="out">−</button><span class="pct" id="zoomPct">100%</span>
    <button data-z="in">+</button><button class="fit" data-z="fit" title="适合窗口">${ICONS.fit}</button>`;
  zoom.querySelector('[data-z="out"]').addEventListener('click', () => zoomAt(1 / 1.2));
  zoom.querySelector('[data-z="in"]').addEventListener('click', () => zoomAt(1.2));
  zoom.querySelector('[data-z="fit"]').addEventListener('click', fitToWindow);
  bar.appendChild(zoom);

  // search
  const search = document.createElement('div');
  search.className = 'search-box';
  search.innerHTML = `${ICONS.search}<input id="searchInput" type="search" placeholder="搜索图像…" autocomplete="off" />`;
  search.querySelector('input').addEventListener('input', (e) => {
    state.search = e.target.value;
    refreshImageList();
  });
  bar.appendChild(search);

  // AI (disabled in web build)
  const ai = document.createElement('div');
  ai.className = 'ai-box';
  ai.innerHTML = `<select disabled title="Web 版未部署 SAM 模型"><option>SAM（Web 版未启用）</option></select>
    <button class="icon-btn" disabled title="AI-Segment 需要服务端 SAM 模型">${ICONS.ai}</button>`;
  bar.appendChild(ai);
}

/* -------------------------------------------------------------- toolstrip */
const TOOLS = [
  { id: 'select', label: '选择', icon: 'select' },
  { id: 'polygon', label: '多边形', icon: 'polygon' },
  { id: 'rectangle', label: '矩形', icon: 'rectangle' },
  { id: 'oriented_rectangle', label: '有向矩形', icon: 'oriented' },
  { id: 'circle', label: '圆形', icon: 'circle' },
  { id: 'circle3', label: '三点画圆', icon: 'circle3' },
  { id: 'point', label: '点', icon: 'point' },
  { id: 'line', label: '直线', icon: 'line' },
  { id: 'linestrip', label: '折线', icon: 'linestrip' },
  { id: 'ai-segment', label: 'AI-Segment', icon: 'ai', disabled: true, title: 'Web 版未部署 SAM 模型' },
  { id: 'ai-findall', label: 'AI-Find All', icon: 'ai', disabled: true, title: 'Web 版未部署 SAM 模型' },
];

function buildToolstrip() {
  const strip = $('toolstrip');
  strip.innerHTML = '';
  TOOLS.forEach((tool) => {
    const btn = document.createElement('button');
    btn.className = 'tool-btn';
    btn.dataset.tool = tool.id;
    btn.innerHTML = `${ICONS[tool.icon] || ''}<span>${tool.label}</span>`;
    if (tool.disabled) {
      btn.disabled = true;
      btn.title = tool.title || '';
    }
    btn.addEventListener('click', () => setTool(tool.id));
    strip.appendChild(btn);
  });
}

function setTool(id) {
  if (id === 'ai-segment' || id === 'ai-findall') {
    toast('AI 辅助标注需要服务端 SAM 模型，Web 版未启用', true);
    return;
  }
  if (id !== 'select' && !state.image) {
    toast('先打开一张图像');
    return;
  }
  if (state.tool !== id && state.drawing && state.drawing.type !== id) cancelDrawing();
  state.tool = id;
  document.querySelectorAll('.tool-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tool === id);
  });
  canvas.style.cursor = id === 'select' ? 'default' : 'crosshair';
  if (id !== 'select' && !state.drawing) state.drawing = null;
  redraw();
}

/* ------------------------------------------------------------------ actions */
function runAction(id) {
  switch (id) {
    case 'openFile':
      pickFiles(false);
      break;
    case 'upload':
      pickFiles(true);
      break;
    case 'projects':
      openProjectModal();
      break;
    case 'newProject':
      openProjectModal();
      setTimeout(() => {
        const input = $('newProjectName');
        if (input) input.focus();
      }, 30);
      break;
    case 'save':
      saveNow(false);
      break;
    case 'saveAs':
      downloadAnnotation();
      break;
    case 'closeImage':
      closeCurrentImage();
      break;
    case 'deleteAnnotation':
      deleteAnnotationFile();
      break;
    case 'deleteImage':
      deleteCurrentImage();
      break;
    case 'logout':
      logoutUser();
      break;
    case 'undo':
      undo();
      break;
    case 'redo':
      redo();
      break;
    case 'undoLastPoint':
      undoLastPoint();
      break;
    case 'copyShape':
      copySelectedShape();
      break;
    case 'pasteShape':
      pasteShapes();
      break;
    case 'duplicateShape':
      duplicateSelectedShape();
      break;
    case 'deleteShape':
      deleteSelectedShape();
      break;
    case 'deletePoint':
      deleteSelectedPoint();
      break;
    case 'addPoint':
      addPointToEdge();
      break;
    case 'lockShape':
      toggleSelectedLock();
      break;
    case 'hideShapes':
      setShapeVisibility(false);
      toast('已隐藏全部形状');
      break;
    case 'showShapes':
      setShapeVisibility(true);
      toast('已显示全部形状');
      break;
    case 'toggleShapes':
      toggleShapeVisibility();
      break;
    case 'selectUp':
      selectNeighbor('up');
      break;
    case 'selectDown':
      selectNeighbor('down');
      break;
    case 'selectLeft':
      selectNeighbor('left');
      break;
    case 'selectRight':
      selectNeighbor('right');
      break;
    case 'convertPolygon':
      convertSelectedShape('polygon');
      break;
    case 'convertRectangle':
      convertSelectedShape('rectangle');
      break;
    case 'convertLinestrip':
      convertSelectedShape('linestrip');
      break;
    case 'convertLine':
      convertSelectedShape('line');
      break;
    case 'mergeShapes':
      mergeSelectedShapes();
      break;
    case 'convertMask':
      convertSelectedShape('mask');
      break;
    case 'fitSelection':
      fitToSelection();
      break;
    case 'fillDrawing':
      state.fillDrawing = !state.fillDrawing;
      redraw();
      toast(state.fillDrawing ? '绘制时填充已开启' : '绘制时填充已关闭');
      break;
    case 'crosshair':
      state.crosshair = !state.crosshair;
      redraw();
      toast(state.crosshair ? '十字准星已显示' : '十字准星已隐藏');
      break;
    case 'allowOob':
      state.allowOob = !state.allowOob;
      toast(state.allowOob ? '现在可以把点画到图像外' : '点会被裁到图像边界内');
      break;
    case 'keepPrev':
      state.keepPrev = !state.keepPrev;
      toast(state.keepPrev ? '切换图像时保留上一张标注' : '已关闭保留上一张标注');
      break;
    case 'keepZoom':
      state.keepZoom = !state.keepZoom;
      toast(state.keepZoom ? '切换图像时保留缩放' : '已关闭保留缩放');
      break;
    case 'editShapeLabel':
      editSelectedLabel();
      break;
    case 'editLabels':
      openLabelManage();
      break;
    case 'prev':
      navigate(-1);
      break;
    case 'next':
      navigate(1);
      break;
    case 'fit':
      fitToWindow();
      break;
    case 'zoom100':
      zoomTo(1);
      break;
    case 'zoomIn':
      zoomAt(1.2);
      break;
    case 'zoomOut':
      zoomAt(1 / 1.2);
      break;
    case 'bc':
      openBrightnessModal();
      break;
    case 'keepBc':
      state.keepBc = !state.keepBc;
      toast(state.keepBc ? '切换图像时保留亮度/对比度' : '已关闭保留亮度/对比度');
      break;
    case 'autoSave':
      state.autoSave = !state.autoSave;
      toast(state.autoSave ? '自动保存已开启' : '自动保存已关闭，请用 Ctrl+S 手动保存');
      break;
    case 'embedImage':
      state.embedImage = !state.embedImage;
      toast(state.embedImage ? '保存时会把图像数据写进 JSON（文件会明显变大）' : '已关闭图像内嵌，JSON 只记路径');
      break;
    case 'exportLabels':
      exportLabelsFile();
      break;
    case 'importLabels':
      importLabelsFile();
      break;
    case 'toggleDocks':
      document.body.classList.toggle('docks-hidden');
      redraw();
      break;
    case 'resetLayout':
      document.body.classList.remove('docks-hidden');
      redraw();
      toast('已恢复默认面板布局');
      break;
    case 'imgFlags':
      openImageFlagsModal();
      break;
    case 'undoDepth':
      editUndoDepth();
      break;
    case 'reload':
      reloadImages();
      break;
    case 'help':
      $('helpModal').hidden = false;
      break;
    default:
      break;
  }
}

function pickFiles(directory) {
  const input = $('fileInput');
  input.multiple = true;
  if (directory) input.setAttribute('webkitdirectory', '');
  else input.removeAttribute('webkitdirectory');
  input.value = '';
  input.click();
}

/* ---------------------------------------------------------------- modals */
async function logoutUser() {
  try {
    if (state.dirty && state.image) await saveNow(true);
  } catch (e) { /* 保存失败也照常退出，草稿还在服务器 */ }
  try {
    await req('POST', '/api/auth/logout');
  } catch (e) { /* ignore */ }
  location.replace('/login');
}

function openLabelManage() {
  const modal = $('labelsEditModal');
  modal.hidden = false;
  const box = $('labelManage');
  box.innerHTML = '';
  if (!state.labels.length) {
    box.innerHTML = '<div class="empty-list">还没有标签，先新增一个</div>';
  }
  state.labels.forEach((label, i) => {
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<span class="dot" style="background:${css(colorForLabel(label))}"></span>
      <span class="name"></span><button class="del" title="删除">✕</button>`;
    row.querySelector('.name').textContent = label;
    row.querySelector('.del').addEventListener('click', () => {
      state.labels.splice(i, 1);
      saveLabels();
      openLabelManage();
      refreshLabelList();
    });
    box.appendChild(row);
  });
}

function openBrightnessModal() {
  const modal = $('bcModal');
  modal.hidden = false;
  $('brightness').value = state.brightness;
  $('contrast').value = state.contrast;
  $('brightnessVal').value = state.brightness;
  $('contrastVal').value = state.contrast;
}

/* ----------------------------------------------------------- image flags */
let imgFlagsDraft = {};

function openImageFlagsModal() {
  if (!state.image) {
    toast('先打开一张图像');
    return;
  }
  imgFlagsDraft = Object.assign({}, state.flags || {});
  renderFlagChecks('imgFlagsList', imgFlagsDraft);
  $('imgFlagsModal').hidden = false;
}

function closeImageFlags(save) {
  $('imgFlagsModal').hidden = true;
  if (!save) return;
  const next = collectFlagChecks('imgFlagsList');
  state.dirty = true;
  state.flags = next;
  scheduleDraft();
  scheduleSave();
  redraw();
  toast('已更新图像标记');
}

function editUndoDepth() {
  const raw = prompt('撤销步数（1–200）', String(state.undoLimit));
  if (raw === null) return;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 1 || n > 200) {
    toast('请输入 1–200 之间的整数', true);
    return;
  }
  state.undoLimit = n;
  if (state.undo.length > n) state.undo = state.undo.slice(-n);
  toast(`撤销步数已设为 ${n}`);
}

/* --------------------------------------------------------------- shortcuts */
document.addEventListener('keydown', (ev) => {
  const tag = (ev.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select';

  if (typing && ev.key !== 'Escape') return;

  const ctrl = ev.ctrlKey || ev.metaKey;
  const key = ev.key;

  if (ctrl) {
    switch (key.toLowerCase()) {
      case 'o':
        ev.preventDefault();
        pickFiles(false);
        return;
      case 'u':
        ev.preventDefault();
        pickFiles(true);
        return;
      case 'delete':
        ev.preventDefault();
        deleteAnnotationFile();
        return;
      case 's':
        ev.preventDefault();
        if (ev.shiftKey) downloadAnnotation();
        else saveNow(false);
        return;
      case 'w':
        ev.preventDefault();
        closeCurrentImage();
        return;
      case 'z':
        ev.preventDefault();
        if (ev.shiftKey) redo();
        else if (state.drawing) undoLastPoint();
        else undo();
        return;
      case 'y':
        ev.preventDefault();
        redo();
        return;
      case 'c':
        ev.preventDefault();
        copySelectedShape();
        return;
      case 'v':
        ev.preventDefault();
        pasteShapes();
        return;
      case 'd':
        ev.preventDefault();
        if (ev.shiftKey) navigate(1);
        else duplicateSelectedShape();
        return;
      case 'a':
        if (ev.shiftKey) {
          ev.preventDefault();
          navigate(-1);
          return;
        }
        break;
      case 'e':
        ev.preventDefault();
        editSelectedLabel();
        return;
      case 'l':
        if (ev.shiftKey) {
          ev.preventDefault();
          toggleSelectedLock();
          return;
        }
        break;
      case 'n':
        ev.preventDefault();
        setTool('polygon');
        return;
      case 'r':
        ev.preventDefault();
        setTool('rectangle');
        return;
      case 'f':
        ev.preventDefault();
        if (ev.shiftKey) fitToSelection();
        else fitToWindow();
        return;
      case '0':
        ev.preventDefault();
        zoomTo(1);
        return;
      case '+':
      case '=':
        ev.preventDefault();
        zoomAt(1.2);
        return;
      case '-':
        ev.preventDefault();
        zoomAt(1 / 1.2);
        return;
      case 'arrowup':
        ev.preventDefault();
        selectNeighbor('up');
        return;
      case 'arrowdown':
        ev.preventDefault();
        selectNeighbor('down');
        return;
      case 'arrowleft':
        ev.preventDefault();
        selectNeighbor('left');
        return;
      case 'arrowright':
        ev.preventDefault();
        selectNeighbor('right');
        return;
      default:
        break;
    }
    return;
  }

  switch (key) {
    case 'Escape':
      if (!$('labelModal').hidden) closeLabelDialog(null);
      else if (state.drawing) cancelDrawing();
      else if (state.selected >= 0) {
        state.selected = -1;
        state.selectedVertex = -1;
        refreshDocks();
        redraw();
      }
      break;
    case 'Enter':
    case ' ':
      if (state.drawing && ['polygon', 'linestrip', 'oriented_rectangle', 'circle3'].includes(state.drawing.type)) {
        ev.preventDefault();
        finishDrawing();
      }
      break;
    case 'Delete':
      if (!typing) {
        ev.preventDefault();
        deleteSelectedShape();
      }
      break;
    case 'Backspace':
      if (!typing) {
        ev.preventDefault();
        if (state.drawing) undoLastPoint();
        else deleteSelectedPoint();
      }
      break;
    case 'a':
    case 'A':
      navigate(-1);
      break;
    case 'd':
    case 'D':
      if (!ctrl) navigate(1);
      break;
    case 't':
    case 'T':
      toggleShapeVisibility();
      break;
    default:
      break;
  }
});

/* --------------------------------------------------------------------- boot */
function bindUi() {
  $('fileInput').addEventListener('change', (ev) => {
    const files = ev.target.files;
    if (files && files.length) uploadFiles(files);
  });

  $('btnOpenFile2').addEventListener('click', () => pickFiles(false));
  $('btnUpload2').addEventListener('click', () => pickFiles(true));

  $('btnLabelOk').addEventListener('click', () => closeLabelDialog($('labelInput').value.trim() || null));
  $('btnLabelCancel').addEventListener('click', () => closeLabelDialog(null));
  $('btnNewFlag').addEventListener('click', () => {
    const name = (prompt('新建 flag 名称（会保存到本工程 flags.txt）') || '').trim();
    if (!name) return;
    if (!state.flagDefs.includes(name)) {
      state.flagDefs.push(name);
      saveFlagDefs();
    }
    renderFlagChecks('labelFlags', collectFlagChecks('labelFlags'));
  });
  $('btnImgFlagsOk').addEventListener('click', () => closeImageFlags(true));
  $('btnAddImgFlag').addEventListener('click', () => {
    const input = $('newImgFlagInput');
    const name = (input.value || '').trim();
    if (!name) return;
    if (!state.flagDefs.includes(name)) {
      state.flagDefs.push(name);
      saveFlagDefs();
    }
    const checked = collectFlagChecks('imgFlagsList');
    checked[name] = true;
    input.value = '';
    renderFlagChecks('imgFlagsList', checked);
  });
  $('labelInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      closeLabelDialog($('labelInput').value.trim() || null);
    }
  });
  $('labelGroup').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      closeLabelDialog($('labelInput').value.trim() || null);
    }
  });
  $('labelDesc').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      closeLabelDialog($('labelInput').value.trim() || null);
    }
  });

  const btnCreateProject = $('btnCreateProject');
  if (btnCreateProject) btnCreateProject.addEventListener('click', submitProjectModal);
  const newProjectName = $('newProjectName');
  if (newProjectName) {
    newProjectName.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        submitProjectModal();
      }
    });
  }

  $('btnBcOk').addEventListener('click', () => {
    state.brightness = Number($('brightness').value);
    state.contrast = Number($('contrast').value);
    $('bcModal').hidden = true;
    if (state.image) reloadImageCanvas();
  });
  $('btnBcReset').addEventListener('click', () => {
    $('brightness').value = 0;
    $('contrast').value = 0;
    $('brightnessVal').value = 0;
    $('contrastVal').value = 0;
  });
  ['brightness', 'contrast'].forEach((k) => {
    $(k).addEventListener('input', (e) => {
      $(`${k}Val`).value = e.target.value;
      const v = Number(e.target.value);
      if (k === 'brightness') state.brightness = v;
      else state.contrast = v;
      if (state.image) reloadImageCanvas();
    });
  });

  $('btnAddLabel').addEventListener('click', () => {
    const v = $('newLabelInput').value.trim();
    if (!v) return;
    addLabel(v);
    $('newLabelInput').value = '';
    openLabelManage();
  });

  $('btnEditLabels').addEventListener('click', openLabelManage);
  $('newLabelInput').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') $('btnAddLabel').click();
  });

  $('btnLabelsImport').addEventListener('click', () => {
    const input = $('labelsFileInput');
    input.value = '';
    input.click();
  });
  $('labelsFileInput').addEventListener('change', (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (file) importLabelsFile(file);
  });
  $('btnLabelsExport').addEventListener('click', exportLabels);

  $('imageSearch').addEventListener('input', (e) => {
    state.search = e.target.value;
    refreshImageList();
  });

  document.querySelectorAll('[data-close]').forEach((el) => {
    el.addEventListener('click', () => {
      el.closest('.modal-backdrop').hidden = true;
    });
  });

  document.querySelectorAll('.modal-backdrop').forEach((backdrop) => {
    backdrop.addEventListener('mousedown', (ev) => {
      if (ev.target !== backdrop) return;
      if (backdrop.id === 'labelModal') closeLabelDialog(null);
      else if (backdrop.id === 'imgFlagsModal') closeImageFlags(false);
      else backdrop.hidden = true;
    });
  });

  wrap.addEventListener('dragover', (ev) => {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
  });
  wrap.addEventListener('drop', (ev) => {
    ev.preventDefault();
    const files = ev.dataTransfer.files;
    if (files && files.length) uploadFiles(files);
  });

  new ResizeObserver(() => redraw()).observe(wrap);

  window.addEventListener('beforeunload', () => {
    if (state.dirty && state.image) {
      // 关标签页/关浏览器时自动保存可能还没到 800ms，这里补一枪：
      // 1) 直接写标注；2) 顺手把草稿也 keepalive 发出去，双保险
      try {
        fetch(`/api/sessions/${state.sessionId}/annotations/${encodeURIComponent(state.image.name)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildAnnotation()),
          keepalive: true,
        });
      } catch (e) { /* ignore */ }
      try {
        writeDraft();
        fetch(`/api/sessions/${state.sessionId}/drafts/${encodeURIComponent(state.image.name)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draftPayload()),
          keepalive: true,
        });
      } catch (e) { /* ignore */ }
    }
  });

  // 切到后台/合盖前先落一次草稿：笔记本睡眠直接断电，回来就当崩溃处理
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) flushDraftNow();
  });
  window.addEventListener('pagehide', flushDraftNow);
}

async function reloadImageCanvas() {
  if (!state.image) return;
  const url = `/api/sessions/${state.sessionId}/raw/${encodeURIComponent(state.image.name)}`;
  const im = new Image();
  im.src = url;
  await new Promise((res, rej) => {
    im.onload = res;
    im.onerror = rej;
  }).catch(() => {});
  if (!im.naturalWidth) return;
  const c = document.createElement('canvas');
  c.width = im.naturalWidth;
  c.height = im.naturalHeight;
  const g = c.getContext('2d');
  g.filter = `brightness(${1 + state.brightness / 100}) contrast(${1 + state.contrast / 100})`;
  g.drawImage(im, 0, 0);
  state.image.canvas = c;
  redraw();
}

async function boot() {
  try {
    const meRes = await req('GET', '/api/auth/me');
    if (meRes && meRes.user) {
      state.user = meRes.user;
      const su = $('statusUser');
      if (su) su.textContent = meRes.user.username;
    }
  } catch (e) {
    if (e.status === 401) return; // 正在跳登录页
  }
  try {
    await initProjects();
  } catch (e) {
    toast(`工程初始化失败：${e.message}`, true);
  }
  await loadLabels();
  buildMenubar();
  buildToolbar();
  buildToolstrip();
  bindUi();
  setTool('select');
  showEmpty(true);
  initLiveChannel();

  try {
    const forced = new URLSearchParams(location.search).get('session');
    if (forced && /^\d{14}(_\d+)?$/.test(forced)) {
      sessionStorage.setItem(STORE_SESSION, forced);
      try {
        await loadSession(forced);
      } catch (e) {
        if (e.status === 404) await newSession(`链接里的工程空间 ${forced} 不存在`);
        else throw e;
      }
      return;
    }

    // 1) 同一个标签页刷新：sessionStorage 还在，直接用
    const active = sessionStorage.getItem(STORE_SESSION);
    if (active) {
      try {
        await loadSession(active);
        return;
      } catch (e) {
        if (e.status !== 404) throw e;
      }
    }

    // 2) 新标签页/浏览器崩溃后重开：sessionStorage 没了，退回 localStorage 的上次工程
    const last = readLastSession();
    if (last) {
      const busy = await probeLiveTab(last.session);
      if (busy) {
        toast('另一个标签页也打开了同一个工程，注意不要同时改同一张图', true);
      }
      try {
        await loadSession(last.session, last.image);
        toast(`已恢复上次工程${last.image ? ' · ' + last.image : ''}`);
        return;
      } catch (e) {
        if (e.status !== 404) throw e;
      }
    }

    // 3) localStorage 也被硬杀/蓝屏弄丢了：问服务器“最近动过哪个工程”
    const recent = await fetchRecent();
    if (recent && recent.session) {
      try {
        await loadSession(recent.session, recent.image);
        toast(`已从服务器记录恢复上次工程${recent.at ? '（最后修改 ' + recent.at + '）' : ''}`);
        return;
      } catch (e) {
        if (e.status !== 404) throw e;
      }
    }

    // 4) 真的什么都没有：进入当前工程的固定工作空间
    await ensureSession();
    await loadSession(state.sessionId);
  } catch (e) {
    toast(`无法连接后端：${e.message}`, true);
    const left = $('statusLeft');
    if (left) left.textContent = '后端未连接';
  }
}

boot();
