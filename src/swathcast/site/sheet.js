// sheet.js: the bottom sheet shared by the maps on egagli.github.io (phones, tablets, landscape phones).
// The same file lives in every map's repository; keep the copies identical (sheet.css goes with it).
//
// MapSheet.MSheet({sheet, header, peek: () => px, label, onChange(state, geom), dockFull, onDragTo, onEscape})
//   -> {set(state), cycle(), attach(header, region), state, geom(), heightOf(state), refresh()}
// Layout applies under MapSheet.MS_MQ. Modes: 'bottom' (peek / half / full, moved by a transform) and
// 'dock' (landscape phones: a card on the left, peek / full). It writes body[data-sheet],
// body[data-sheet-mode] and the custom properties --ms-full, --ms-sheet (the visible height, for things
// that ride on the sheet), --ms-y (the translation) and --ms-hidden (scroll padding at half).
(function () {
const MS_MQ = '(max-width: 1023px), (pointer: coarse) and (max-height: 520px)';
const MS_DOCK = '(pointer: coarse) and (max-height: 520px)';
function MSheet(opt) {
  const root = document.documentElement, body = document.body;
  const mq = matchMedia(MS_MQ), dockMq = matchMedia(MS_DOCK);
  const sheet = opt.sheet, header = opt.header;
  let state = 'peek', drag = null;
  const mode = () => (dockMq.matches ? 'dock' : 'bottom');
  const H = () => window.innerHeight;
  const order = () => (mode() === 'dock' ? ['peek', 'full'] : ['peek', 'half', 'full']);
  const fullH = () => (mode() === 'dock' ? Math.round(opt.dockFull ? opt.dockFull() : sheet.parentElement.clientHeight - 24) : Math.round(H() * 0.88));
  const heightOf = (st) => {
    if (st === 'peek') return Math.min(140, Math.round(opt.peek()));
    if (st === 'half') return mode() === 'dock' ? fullH() : Math.round(H() * 0.5);
    return fullH();
  };
  const geom = () => ({ mode: mode(), state, visible: heightOf(state), width: sheet.offsetWidth, full: fullH() });

  const headers = [];
  sheet.setAttribute('role', 'region');
  sheet.setAttribute('aria-label', opt.label || 'Map panel');
  const nextOf = (st) => { const o = order(); return o[(o.indexOf(st) + 1) % o.length]; };

  function paint(px, animate) {
    const full = fullH();
    if (!animate) { if (!body.classList.contains('ms-dragging')) body.classList.add('ms-dragging'); if (!drag) requestAnimationFrame(() => requestAnimationFrame(() => { if (!drag && body.classList.contains('ms-dragging')) body.classList.remove('ms-dragging'); })); }
    root.style.setProperty('--ms-full', full + 'px');
    root.style.setProperty('--ms-sheet', px + 'px');
    root.style.setProperty('--ms-y', Math.max(0, full - px) + 'px');
  }
  function set(st, { animate = true, silent = false } = {}) {
    if (!order().includes(st)) st = st === 'half' ? 'full' : 'peek';
    state = st;
    body.dataset.sheet = st;
    body.dataset.sheetMode = mode();
    paint(heightOf(st), animate);
    root.style.setProperty('--ms-hidden', Math.max(0, fullH() - heightOf(st)) + 'px');
    if (st === 'peek') sheet.scrollTop = 0;
    const expanded = st !== 'peek', nx = nextOf(st);
    headers.forEach(({ titleBtn, chev }) => {
      titleBtn && titleBtn.setAttribute('aria-expanded', String(expanded));
      if (chev) { chev.setAttribute('aria-label', nx === 'peek' ? 'Collapse panel' : 'Expand panel'); chev.dataset.dir = nx === 'peek' ? 'down' : 'up'; }
    });
    if (!silent && opt.onChange) opt.onChange(st, geom());
  }
  const cycle = () => set(nextOf(state));

  // drag anywhere on a header (not on its buttons); a tap cycles. attach() binds another header
  // (e.g. a feature's details shown in the sheet's place) to the same state.
  function attach(header, region) {
    const titleBtn = header.querySelector('.ms-titlebtn'), chev = header.querySelector('.ms-chev');
    headers.push({ titleBtn, chev });
    header.addEventListener('pointerdown', (e) => {
      if (!mq.matches || e.button > 0 || e.target.closest('button, a, input, select, label')) return;
      drag = { id: e.pointerId, y: e.clientY, t: performance.now(), h: heightOf(state), cur: heightOf(state), moved: false };
      header.setPointerCapture(e.pointerId);
    });
    header.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dy) < 6) return;
      drag.moved = true;
      drag.cur = Math.max(heightOf('peek') - 24, Math.min(H() * 0.92, drag.h - dy));
      paint(drag.cur, false);
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag; drag = null; if (body.classList.contains('ms-dragging')) body.classList.remove('ms-dragging');
      if (!d.moved) { cycle(); return; }
      const v = (d.y - e.clientY) / Math.max(1, performance.now() - d.t); // px/ms, + = up
      const target = d.cur + v * 180;
      const best = order().map((s) => [s, Math.abs(heightOf(s) - target)]).sort((a, b) => a[1] - b[1])[0][0];
      if (opt.onDragTo && opt.onDragTo(best, header) === false) { set(state); return; }
      set(best);
    };
    header.addEventListener('pointerup', end);
    header.addEventListener('pointercancel', end);
    titleBtn && titleBtn.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cycle(); } });
    chev && chev.addEventListener('click', cycle);
    (region || header).addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && mq.matches && (opt.onEscape ? opt.onEscape(header) !== false : true) && state !== 'peek') { set('peek'); (titleBtn || chev).focus(); }
    });
    set(state, { animate: false, silent: true });
  }
  attach(header, sheet);

  let lastSize = '';
  const onResize = () => {
    if (!mq.matches) return;
    const size = innerWidth + 'x' + innerHeight + mode();
    if (size === lastSize) return;
    lastSize = size; set(state, { animate: false });
  };
  window.addEventListener('resize', onResize);
  dockMq.addEventListener('change', onResize);
  return { set, cycle, attach, get state() { return state; }, geom, heightOf, mq, dockMq, refresh: () => set(state, { animate: false }) };
}
function msHeader({ title, sub }) {
  const h = document.createElement('div');
  h.className = 'ms-header';
  h.innerHTML = '<span class="ms-grip" aria-hidden="true"></span>' +
    '<div class="ms-titlebtn" role="button" tabindex="0" aria-expanded="false">' +
    '<div class="ms-title"></div><div class="ms-sub"></div></div>' +
    '<button type="button" class="ms-chev" aria-label="Expand panel"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M6 15l6-6 6 6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>';
  h.querySelector('.ms-title').textContent = title;
  if (sub) h.querySelector('.ms-sub').textContent = sub; else h.querySelector('.ms-sub').remove();
  return h;
}
// Start MapLibre's attribution folded to its (i) in the sheet layout (call it on the map's 'load').
function foldAttribution(container) {
  const a = (container || document).querySelector('.maplibregl-ctrl-attrib.maplibregl-compact-show');
  if (a && matchMedia(MS_MQ).matches) { a.classList.remove('maplibregl-compact-show'); a.removeAttribute('open'); }
}
window.MapSheet = { MS_MQ, MS_DOCK, MSheet, msHeader, foldAttribution };
})();
