// A short tour for a first visit: one idea per step, across the map and the
// Delivery page. A large card, and for each thing a step is about a bright
// red ring with a red label pointing at it. The page stays usable underneath
// (nothing is dimmed or blocked).
//
// Steps: {title, body (HTML), marks: [{target: selector | () => Element,
// label, side: "right" | "left" | "top" | "bottom"}], place (the card's
// class: tour-<place>), dirty (the step changes the page: the tour's end
// then starts over), busy (said while enter runs), enter: async (current)
// => void (current() is false once the step is left), next: {label, go}
// (default: the next step)}.
// The tour spans pages: a step on the other page is handed over through
// sessionStorage (pendingStep), and the page there opens it.

const SEEN_KEY = "swathcast-tour"; // not "swathcast.*": Reset keeps it
const STEP_KEY = "swathcast-tour-step";
export const TOUR_STEPS = 6;

export function tourSeen() {
  try { return localStorage.getItem(SEEN_KEY) === "seen"; } catch { return true; }
}
export function markTourSeen() {
  try { localStorage.setItem(SEEN_KEY, "seen"); } catch { /* storage unavailable */ }
}
// The step another page asked this one to open (1-based), once.
export function pendingStep() {
  try {
    const n = Number(sessionStorage.getItem(STEP_KEY));
    sessionStorage.removeItem(STEP_KEY);
    return n || null;
  } catch { return null; }
}
export function handOver(n, url) {
  try { sessionStorage.setItem(STEP_KEY, String(n)); } catch { /* the step is lost: the page just opens */ }
  location.href = url;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// cond() polled until true or the time is up; true when it came true
export async function waitFor(cond, timeout = 15000, every = 200) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (cond()) return true; } catch { /* not yet */ }
    await sleep(every);
  }
  return false;
}

const GAP = 16; // between a target's ring and its label

export class Tour {
  // steps: this page's steps; first: the number of the first (1-based);
  // onEnd(): called when the tour ends (finished or closed) after a step
  // that changed the page
  constructor(steps, { first = 1, onEnd = null, dirty = false } = {}) {
    Object.assign(this, { steps, first, onEnd, dirty, i: -1, marks: [], run: 0 });
    this.card = Object.assign(document.createElement("div"), { className: "tour", hidden: true });
    this.card.setAttribute("role", "dialog");
    this.card.setAttribute("aria-label", "Tour of swathcast");
    document.body.append(this.card);
    this.onKey = (e) => {
      if (this.i < 0) return;
      if (e.key === "Escape") this.end();
      if (e.key === "ArrowRight" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) this.card.querySelector(".tour-next")?.click();
    };
    this.follow = () => { this.place(); if (this.i >= 0) this.raf = requestAnimationFrame(this.follow); };
    // the tour's hand: a red pointer that moves to what the tour clicks, says
    // what it is doing, and clicks it visibly
    this.hand = Object.assign(document.createElement("div"), { className: "tour-hand", hidden: true });
    this.hand.setAttribute("aria-hidden", "true");
    this.hand.innerHTML = '<svg viewBox="0 0 24 24" width="34" height="34"><path d="M4 2l15 9.5-6.6 1.4 3.9 7.3-3 1.6-3.9-7.4L4 19z" fill="var(--tour)" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg><span class="tour-say"></span>';
    document.body.append(this.hand);
  }

  // The hand moved to an element (its centre) or a screen point [x, y],
  // saying what it is about to do; resolves when it has arrived.
  async pointAt(target, say = "") {
    let x, y;
    if (Array.isArray(target)) [x, y] = target;
    else {
      if (!target || !target.getClientRects().length) return false;
      target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      const b = target.getBoundingClientRect();
      [x, y] = [b.left + Math.min(b.width / 2, 60), b.top + b.height / 2];
    }
    const h = this.hand, first = h.hidden;
    h.querySelector(".tour-say").textContent = say;
    h.classList.toggle("says", Boolean(say));
    if (first) { h.style.transition = "none"; h.style.left = `${x + 60}px`; h.style.top = `${y + 60}px`; h.hidden = false; h.getBoundingClientRect(); h.style.transition = ""; }
    h.style.left = `${x}px`;
    h.style.top = `${y}px`;
    await sleep(first ? 750 : 650);
    return true;
  }

  // A visible click: the hand goes there, a ripple, then the click, and the
  // thing clicked flashes. Returns false when the element is not there.
  async click(el, say = "") {
    if (!(await this.pointAt(el, say))) return false;
    this.ripple();
    await sleep(250);
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    flash(el);
    await sleep(500);
    return true;
  }
  // a click on a screen point (the map), by fire(): the hand and the ripple
  async tap(xy, say, fire) {
    await this.pointAt(xy, say);
    this.ripple();
    await sleep(250);
    fire();
    await sleep(500);
  }
  ripple() {
    const r = Object.assign(document.createElement("i"), { className: "tour-ripple" });
    this.hand.append(r);
    setTimeout(() => r.remove(), 700);
  }
  say(text) {
    this.hand.querySelector(".tour-say").textContent = text;
    this.hand.classList.toggle("says", Boolean(text));
  }

  show(i) {
    const s = this.steps[i];
    if (!s) return this.end();
    this.i = i;
    if (s.dirty) this.dirty = true;
    const run = ++this.run; // a later step supersedes this one's enter()
    this.hand.hidden = true;
    const n = this.first + i, last = n === TOUR_STEPS;
    const next = s.next || { label: last ? "Finish" : "Next →" };
    this.card.className = `tour${s.place ? ` tour-${s.place}` : ""}`;
    this.card.innerHTML = `<div class="tour-bar" aria-hidden="true"><i style="width:${(100 * n / TOUR_STEPS).toFixed(1)}%"></i></div>`
      + `<div class="tour-top"><span class="tour-n">Step ${n} of ${TOUR_STEPS}</span><button type="button" class="tour-x" aria-label="End the tour" title="End the tour">×</button></div>`
      + `<h3>${s.title}</h3><div class="tour-body">${s.body}</div><div class="tour-status" aria-live="polite"></div>`
      + `<div class="tour-nav"><button type="button" class="tour-skip">${last ? "" : "Skip tour"}</button><span></span>`
      + `${n > 1 ? '<button type="button" class="tour-back">← Back</button>' : ""}<button type="button" class="tour-next">${next.label}</button></div>`;
    this.card.hidden = false;
    this.card.querySelector(".tour-x").onclick = () => this.end();
    const skip = this.card.querySelector(".tour-skip");
    if (last) skip.remove(); else skip.onclick = () => this.end();
    this.card.querySelector(".tour-back")?.addEventListener("click", () => (i > 0 ? this.show(i - 1) : this.back?.()));
    this.card.querySelector(".tour-next").onclick = () => (next.go ? next.go() : i === this.steps.length - 1 ? this.end() : this.show(i + 1));
    this.card.querySelector(".tour-next").focus({ preventScroll: true });
    document.addEventListener("keydown", this.onKey);
    // a ring and a label per mark
    this.clearMarks();
    this.marks = (s.marks || []).map((m) => {
      const ring = Object.assign(document.createElement("div"), { className: "tour-ring", hidden: true });
      const label = Object.assign(document.createElement("div"), { className: `tour-label side-${m.side || "right"}`, hidden: true, textContent: m.label || "" });
      ring.setAttribute("aria-hidden", "true");
      label.setAttribute("aria-hidden", "true");
      document.body.append(ring);
      if (m.label) document.body.append(label);
      return { ...m, ring, label };
    });
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(this.follow);
    if (s.enter) {
      const status = this.card.querySelector(".tour-status");
      status.textContent = s.busy || "";
      Promise.resolve(s.enter(() => run === this.run && this.i === i))
        .catch(() => { /* a demonstration that did not work out: the text still stands */ })
        .finally(() => {
          if (run !== this.run) return;
          status.textContent = "";
          setTimeout(() => { if (run === this.run) this.hand.hidden = true; }, 2500); // its work done, the hand goes
        });
    }
  }

  // rings and labels follow their targets (the panel scrolls, popups move with the map)
  place() {
    const vw = window.innerWidth, vh = window.innerHeight;
    this.marks.forEach((m) => {
      const el = typeof m.target === "function" ? m.target() : document.querySelector(m.target);
      const shown = el && el.getClientRects().length ? el.getBoundingClientRect() : null;
      m.ring.hidden = m.label.hidden = !shown || shown.width < 2;
      if (m.ring.hidden) return;
      // clipped to the screen (a tall list's ring stays visible)
      const b = { left: Math.max(shown.left, 4), top: Math.max(shown.top, 4), right: Math.min(shown.right, vw - 4), bottom: Math.min(shown.bottom, vh - 4) };
      Object.assign(m.ring.style, { left: `${b.left - 5}px`, top: `${b.top - 5}px`, width: `${b.right - b.left + 10}px`, height: `${b.bottom - b.top + 10}px` });
      if (!m.label.isConnected) return;
      const w = m.label.offsetWidth, h = m.label.offsetHeight, side = m.side || "right";
      const cx = (b.left + b.right) / 2, cy = Math.min((b.top + b.bottom) / 2, b.top + 60);
      let x = side === "right" ? b.right + GAP : side === "left" ? b.left - GAP - w : cx - w / 2;
      let y = side === "top" ? b.top - GAP - h : side === "bottom" ? b.bottom + GAP : cy - h / 2;
      x = Math.max(8, Math.min(x, vw - w - 8));
      y = Math.max(8, Math.min(y, vh - h - 8));
      Object.assign(m.label.style, { left: `${x}px`, top: `${y}px` });
    });
  }

  clearMarks() {
    this.marks.forEach((m) => { m.ring.remove(); m.label.remove(); });
    this.marks = [];
  }

  close() {
    this.i = -1;
    this.run++;
    cancelAnimationFrame(this.raf);
    this.card.hidden = true;
    this.hand.hidden = true;
    this.clearMarks();
    document.removeEventListener("keydown", this.onKey);
  }

  // finished or skipped: the page goes back to how it started
  end() {
    this.close();
    if (this.dirty && this.onEnd) this.onEnd();
  }
}

// What the tour just clicked, flashing red for a moment.
export function flash(el) {
  if (!el || !el.classList) return;
  el.classList.remove("tour-flash");
  void el.getBoundingClientRect();
  el.classList.add("tour-flash");
  setTimeout(() => el.classList.remove("tour-flash"), 1600);
}
