// Talent Search app window. All page scripts live here so the Content-Security-Policy
// can forbid inline scripts. Pages opt in with data- attributes:
//   data-confirm="question"   on a form: ask before submitting ("{stage}" in it names the stage picked in the form);
//                             on a submit button: ask only when that button sent the form
//   data-autosubmit           on a select: submit its form when it changes by mouse or touch. Picked with the keyboard
//                             (arrow keys), it submits on Enter or when the select loses focus, so each arrow press
//                             doesn't reload the page. On a checkbox it submits at once.
//   data-recipe="run|copy"    on a button with data-prompt (and optional data-fill)
//   data-copy-text="..." or data-copy-from="element id"   on a button: copy that text
//   data-copy-note="next"     show the "Copied" message in the next element instead of the button
//   data-move-url="/jobs/1/move"   on the pipeline: drag a card to another column to move it
//   data-add-step             on a button in the follow-up steps form: show the next empty step row
//   data-check-all            on a checkbox: tick or untick every person (name candidate_ids) in its form
//   data-check-name="pair"    on a data-check-all box: tick the boxes with that name instead (they may sit outside the
//                             form element and point at it with the form attribute)
//   (Candidates table)        Shift-click a second row box to tick or untick every row between the two
//   data-print                on a button: open the browser's print dialog (print or save as PDF)
//   data-busy="message"       on a form: once sent, show the message in its [data-busy-note] and send it only once
//                             (a submit button's own data-busy wins when that button sent it)
//   data-decide="Out stage"   on the Decide form: before sending, confirm naming who gets the offer and who is
//                             passed on (radio offer and ticked pass boxes, each with data-name; data-ahead counts
//                             their interviews still to come, which get cancelled; data-open their sessions
//                             with no outcome yet, named as still open)
//   data-tz-prefill           on a time zone box: fill it with this browser's own zone when it's empty
//   data-tz-use               on a submit button beside it: shown as "Use <this browser's zone>" (one click saves it)
//   data-stage-pick           on a stage menu: a mouse pick moves at once (or asks for a reason first for an out stage);
//                             with the keyboard it waits for Enter or blur. After a move, focus returns to that person.
//   data-theme-pick           on a theme radio on Settings: preview the theme at once (Save keeps it)
//   data-color-bubble         on a colour bubble button (data-cid, data-color, data-next, data-prev): a click saves the
//                             next colour at once and updates the bubble in place; Right-click or Shift-click goes to
//                             the colour before. Without script the bubble's own form posts and the page reloads.
//   data-color-count="red"    on a number in the colour chips: refreshed after a bubble changes
//   data-table-stage          on a stage menu in the job table view: fills the one shared move form (#tbl-move)
//                             (reason options with data-like="a|b" hide when the stage is named a or b)
//   <div id="tour-data" data-tour="{json}">   the first-run tour's steps (see tour.py): drawn as a spotlight and a popover.
//                             While it shows, the page behind it is inert (except the lit part); a step may also light
//                             a second element ("reach") so the recruiter can click it, and the finish step offers
//                             check boxes (delete_people) that are posted with Finish or Skip.
(function () {
  "use strict";

  document.addEventListener("change", e => {
    const t = e.target;
    if (t && t.matches && t.matches("input[data-theme-pick]") && t.checked) document.documentElement.dataset.theme = t.value;
  });

  function fallbackCopy(text) {
    const t = document.createElement("textarea");
    t.value = text; document.body.appendChild(t);
    t.select(); try { document.execCommand("copy"); } catch (e) { /* nothing else to try */ }
    t.remove();
  }

  function copyText(text, note, msg) {
    const done = () => { if (note) note.textContent = msg; };
    if (navigator.clipboard && window.isSecureContext !== false) {
      navigator.clipboard.writeText(text).then(done, () => { fallbackCopy(text); done(); });
    } else { fallbackCopy(text); done(); }
  }

  function promptFor(btn) {
    let p = btn.dataset.prompt || "";
    const src = btn.dataset.fill && document.getElementById(btn.dataset.fill);
    if (src && src.value.trim()) p = p.replace("[paste the resume or profile here]", src.value.trim());
    return p;
  }

  function recipeNote(btn) {
    const box = btn.closest(".recipe-actions");
    return box && box.querySelector(".recipe-note");
  }

  // AI recipes: copy the prompt, then try to open Claude with it filled in.
  function runRecipe(btn) {
    const p = promptFor(btn);
    copyText(p, recipeNote(btn), "Prompt copied. If Claude didn't open, open it and paste (Ctrl+V or Cmd+V).");
    window.location.href = "claude://claude.ai/new?q=" + encodeURIComponent(p);
  }

  function copyRecipe(btn) {
    copyText(promptFor(btn), recipeNote(btn), "Copied. Paste it into Claude, Codex, ChatGPT or LM Studio.");
  }

  document.addEventListener("click", e => {
    const btn = e.target.closest && e.target.closest("button[data-recipe], button[data-copy-text], button[data-copy-from]");
    if (!btn) return;
    if (btn.dataset.recipe === "run") return runRecipe(btn);
    if (btn.dataset.recipe === "copy") return copyRecipe(btn);
    const from = btn.dataset.copyFrom && document.getElementById(btn.dataset.copyFrom);
    const text = from ? from.innerText : (btn.dataset.copyText || "");
    const note = btn.dataset.copyNote === "next" ? btn.nextElementSibling : btn;
    copyText(text, note, btn.dataset.copyMsg || "Copied");
  });

  document.addEventListener("click", e => {
    if (e.target.closest && e.target.closest("button[data-print]")) window.print();
  });

  // Follow-up steps form: "Add a step" shows the next empty row; a box with data-check-all ticks every person in its form.
  document.addEventListener("click", e => {
    const add = e.target.closest && e.target.closest("button[data-add-step]");
    if (!add) return;
    const next = add.form.querySelector(".seq-step[hidden]");
    if (next) { next.hidden = false; const f = next.querySelector("input"); if (f) f.focus(); }
    if (!add.form.querySelector(".seq-step[hidden]")) add.hidden = true;
  });
  document.addEventListener("change", e => {
    const all = e.target.closest && e.target.closest("input[data-check-all]");
    if (!all) return;
    const name = all.dataset.checkName || "candidate_ids";
    Array.from(all.form.elements).filter(b => b.name === name && b.type === "checkbox")
      .forEach(b => { b.checked = all.checked; });
  });

  // Candidates table: click one row's box, then Shift-click another to tick (or untick) everyone between them.
  let lastPick = null;
  document.addEventListener("click", e => {
    const box = e.target.closest && e.target.closest("input[type=checkbox][name='candidate_ids']");
    if (!box) return;
    if (e.shiftKey && lastPick && lastPick !== box && document.contains(lastPick)) {
      const boxes = Array.from(document.querySelectorAll("input[type=checkbox][name='candidate_ids']"));
      const a = boxes.indexOf(lastPick), b = boxes.indexOf(box);
      if (a >= 0 && b >= 0) {
        boxes.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(x => { x.checked = box.checked; });
      }
    }
    lastPick = box;
  });

  // The time zone banner: suggest the zone this browser is in, so saving it is one click.
  function prefillZones() {
    let zone = "";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* an old browser: leave it empty */ }
    if (!zone) return;
    document.querySelectorAll("input[data-tz-prefill]").forEach(b => { if (!b.value) b.value = zone; });
    // One click: "Use America/Chicago" saves this browser's zone as it is.
    document.querySelectorAll("button[data-tz-use]").forEach(b => { b.textContent = "Use " + zone; b.hidden = false; });
    // The banner says what it found, so the one click is the obvious thing to do.
    document.querySelectorAll("form[data-tz-banner]:not([data-tz-saved]), form[data-tz-banner][data-tz-saved='']").forEach(f => {
      const t = f.querySelector("[data-tz-text]");
      const m = t && t.textContent.match(/^Times show in ([^.]+)\./);
      if (t && m && m[1].trim() !== zone) t.textContent = "Times show in " + m[1].trim() + ", but this browser is in " + zone + ". Is that yours?";
    });
  }
  // The banner only shows when it matters: the server shows it when this computer says UTC with no zone saved; for a
  // saved zone it stays hidden unless this browser's zone has a different offset now.
  function offsetOf(zone) {
    try {
      const p = new Intl.DateTimeFormat("en-US", {timeZone: zone, timeZoneName: "longOffset"}).formatToParts(new Date())
        .find(x => x.type === "timeZoneName");
      return p ? p.value : null;
    } catch (e) { return null; }
  }
  function checkZoneBanner() {
    let zone = "";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* leave it hidden */ }
    document.querySelectorAll("form[data-tz-banner][hidden]").forEach(f => {
      const here = offsetOf(zone), saved = offsetOf(f.dataset.tzSaved);
      if (zone && here && saved && here !== saved) f.hidden = false;
    });
  }
  function zoneStart() { checkZoneBanner(); prefillZones(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", zoneStart);
  else zoneStart();

  // The Decide form: say exactly who gets the offer and who is passed on before anything moves.
  function decideQuestion(form) {
    const offer = form.querySelector("input[name=offer]:checked");
    const passed = Array.from(form.querySelectorAll("input[name=pass]:checked"))
      .filter(b => !offer || b.value !== offer.value);
    let q = "Record this decision?\n\nOffer to " + (offer ? offer.dataset.name : "nobody yet") + ".";
    if (!passed.length) q += "\nNobody is passed on.";
    else {
      q += "\nPass on " + passed.length + ", moving to " + form.dataset.decide + ":";
      passed.forEach(b => {
        const n = parseInt(b.dataset.ahead || "0", 10);
        q += "\n  " + b.dataset.name + (n ? " (cancels " + n + " interview" + (n === 1 ? "" : "s") + " still to come)" : "");
      });
    }
    const open = [offer, ...passed].filter(b => b && b.dataset.open);
    if (open.length) {
      q += "\n\nStill open:";
      open.forEach(b => {
        const n = parseInt(b.dataset.open, 10);
        q += "\n  " + b.dataset.name + " has " + n + " session" + (n === 1 ? "" : "s") + " with no outcome";
      });
    }
    return q + "\n\nYou can undo it all under Changes.";
  }

  // A confirm can name the stage picked in the form's stage menu: "{stage}" reads as the chosen stage.
  function confirmText(form) {
    const pick = form.querySelector("select[name=stage]");
    return form.dataset.confirm.split("{stage}").join(pick && pick.value ? pick.value : "the first stage");
  }

  document.addEventListener("submit", e => {
    const form = e.target;
    if (form.dataset && form.dataset.decide !== undefined && !window.confirm(decideQuestion(form))) { e.preventDefault(); return; }
    if (form.dataset && form.dataset.confirm && !window.confirm(confirmText(form))) { e.preventDefault(); return; }
    if (e.submitter && e.submitter.dataset && e.submitter.dataset.confirm && !window.confirm(e.submitter.dataset.confirm)) { e.preventDefault(); return; }
    if (form.dataset && form.dataset.busy) {
      // A big import takes a while: say so, and don't let a second click send it again.
      if (form.dataset.sent) { e.preventDefault(); return; }
      form.dataset.sent = "1";
      form.setAttribute("aria-busy", "true");
      const note = form.querySelector("[data-busy-note]");
      // The button that sent it can say what it does instead ("Check again" saves nothing).
      if (note) note.textContent = (e.submitter && e.submitter.dataset.busy) || form.dataset.busy;
    }
  });
  // Coming back to a page from the browser's history: let its forms be sent again.
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-sent]").forEach(f => {
      delete f.dataset.sent; f.removeAttribute("aria-busy");
      const note = f.querySelector("[data-busy-note]");
      if (note) note.textContent = "";
    });
  });

  // Menus that wait for the keyboard: filters (data-autosubmit) and the stage menus on the board and the job table.
  // A mouse or touch pick goes at once. With the keyboard, arrow keys change the value one option at a time, so wait
  // for Enter or for the menu to lose focus.
  const DEFER = "select[data-autosubmit], select[data-stage-pick], select[data-table-stage], form.stage-form .reason select";
  const isAuto = n => n && n.matches && n.matches(DEFER);
  const sendForm = form => { form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})); };
  // Make the optional reason box show, and put the keyboard in it.
  function askReason(reason, o, who, quiet) {
    // Hide the reason that only repeats the stage ("Withdrew" when moving to Withdrawn).
    const key = (o.textContent || "").toLowerCase().replace(/[\W_]+/g, " ").trim();
    reason.querySelectorAll("option[data-like]").forEach(r => { r.hidden = r.dataset.like.split("|").includes(key); });
    const w = reason.querySelector(".tbl-who");
    if (w) w.textContent = "Why " + (who || "this person") + " is going to " + o.textContent + ":";
    const wasHidden = reason.hidden;
    reason.hidden = false;
    // Nothing has been sent yet. Say so on the page and out loud, so the stage menu doesn't look like a finished move.
    let note = reason.querySelector(".reason-note");
    if (!note) { note = document.createElement("span"); note.className = "reason-note"; reason.prepend(note); }
    note.textContent = "Not moved yet. ";
    const holder = reason.closest("form") || reason;
    holder.classList.add("unsent");
    const name = who || (reason.closest("[data-who]") || {dataset: {}}).dataset.who || "this person";
    const pick = reason.querySelector("select");
    if (pick) {
      if (!quiet) pick.focus();
      else if (wasHidden) {
        announceColor("Not moved yet. Pick a reason for " + name + ", or press Move without a reason.");
        // The keyboard is already leaving the stage menu; put it in the reason box once it has landed.
        window.setTimeout(() => { pick.focus(); }, 0);
      }
    }
  }
  // A stage menu or reason menu has settled: an out stage shows the optional reason first; any other pick moves.
  function stageSettled(el, quiet) {
    if (el.matches("select[data-table-stage]")) {  // job table view: one shared move form
      const shared = document.getElementById("tbl-move");
      if (!shared) return;
      shared.elements.candidate_id.value = el.dataset.cid;
      shared.elements.stage.value = el.value;
      const o = el.selectedOptions && el.selectedOptions[0];
      const reason = shared.querySelector(".reason");
      if (o && o.dataset.out && reason) { askReason(reason, o, el.dataset.who, quiet); return; }
      if (reason) { reason.hidden = true; shared.classList.remove("unsent"); }
      sendForm(shared);
      return;
    }
    const form = el.closest && el.closest("form.stage-form");
    if (!form) return;
    if (el.matches("select[data-stage-pick]")) {
      const o = el.selectedOptions && el.selectedOptions[0];
      const reason = form.querySelector(".reason");
      if (o && o.dataset.out && reason) { askReason(reason, o, "", quiet); return; }
      if (reason) { reason.hidden = true; form.classList.remove("unsent"); }
    }
    sendForm(form);
  }
  function settle(el, quiet) {
    if (el.matches("select[data-autosubmit]")) { if (el.form) el.form.submit(); return; }
    stageSettled(el, quiet);
  }
  document.addEventListener("keydown", e => {
    const el = e.target;
    if (!isAuto(el)) return;
    if (e.key === "Enter") {
      if (el.dataset.pending) { delete el.dataset.pending; delete el.dataset.kb; e.preventDefault(); settle(el, false); }
    } else if (/^(Arrow|Home$|End$|Page)/.test(e.key) || (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey)) {
      el.dataset.kb = "1";
    }
  }, true);
  document.addEventListener("pointerdown", e => { const el = e.target.closest && e.target.closest(DEFER); if (el) delete el.dataset.kb; }, true);
  document.addEventListener("focusout", e => {
    const el = e.target;
    if (!isAuto(el) || !el.dataset.pending) return;
    const to = e.relatedTarget;
    delete el.dataset.pending; delete el.dataset.kb;
    if (to && el.form && to.form === el.form && to.type === "submit") return;  // the Go or Move button sends it
    settle(el, true);
  });
  document.addEventListener("change", e => {
    const el = e.target;
    if (isAuto(el)) {
      if (el.dataset.kb) { el.dataset.pending = "1"; return; }
      settle(el, false); return;
    }
    if (el.matches && el.matches("input[type=checkbox][data-autosubmit]") && el.form) { el.form.submit(); return; }
  });

  // After a stage move the board or table is drawn again. Say what moved, and put the keyboard back on that
  // person's stage menu (or leave it where the person tabbed to, when they are already somewhere on the page).
  let moved = null;
  const cssId = v => (window.CSS && CSS.escape) ? CSS.escape(String(v)) : String(v);
  document.addEventListener("htmx:beforeRequest", e => {
    const cfg = e.detail && e.detail.requestConfig;
    if (!cfg || !/\/jobs\/\d+\/move$/.test(cfg.path || "")) return;
    const p = cfg.parameters || {};
    const cid = p.candidate_id, stage = p.stage;
    if (!cid) { moved = null; return; }
    const at = document.querySelector("#pipeline [data-cid=\"" + cssId(cid) + "\"]");
    const strong = at && at.querySelector && at.querySelector("strong");
    moved = {cid, stage, who: (at && at.dataset.who) || (strong && strong.textContent.trim()) || "This person"};
  });
  document.addEventListener("htmx:afterSettle", e => {
    const m = moved;
    const t = e.detail && e.detail.target;
    if (!m || !t || t.id !== "pipeline") return;
    moved = null;
    const lost = t.querySelector(".error, .ask-cancel");
    announceColor(lost ? (lost.textContent || "").trim().replace(/\s+/g, " ") : m.who + " moved to " + (m.stage || "the new stage") + ".");
    if (lost) return;
    const a = document.activeElement;
    if (a && a !== document.body && document.body.contains(a)) return;
    const pick = t.querySelector("[data-cid=\"" + cssId(m.cid) + "\"] select[data-stage-pick]") ||
      t.querySelector("select[data-table-stage][data-cid=\"" + cssId(m.cid) + "\"]");
    if (pick) pick.focus();
  });

  // "Show more" swaps its own row away. Put the keyboard on the first row or card that was added.
  let more = null;
  document.addEventListener("htmx:beforeRequest", e => {
    const elt = e.detail && e.detail.elt;
    const holder = elt && elt.closest && elt.closest("tr.more, li.more");
    if (!holder) return;
    more = {prev: holder.previousElementSibling, parent: holder.parentElement};
  });
  document.addEventListener("htmx:afterSettle", () => {
    if (!more) return;
    const m = more; more = null;
    const a = document.activeElement;
    if (a && a !== document.body && document.body.contains(a)) return;
    const first = m.prev ? m.prev.nextElementSibling : (m.parent && m.parent.firstElementChild);
    const target = first && first.querySelector && first.querySelector("a[href], button");
    if (target) target.focus();
  });
  // The Today search: say the match count once, not the whole list of people.
  document.addEventListener("htmx:afterSettle", e => {
    const t = e.detail && e.detail.target;
    const live = document.getElementById("results-status");
    if (!live || !t || t.id !== "results") return;
    const meta = t.querySelector(".meta");
    live.textContent = meta ? meta.textContent.replace(/\s+/g, " ").trim() : "";
  });

  // After a save that reloads the page, put the keyboard on the notice (or on the result a page marks with
  // data-focus-here), so a screen reader reads it. Not while the tour owns focus, and not over an autofocus.
  function focusResult() {
    if (document.getElementById("tour-data") || document.querySelector("[autofocus]")) return;
    const a = document.activeElement;
    if (a && a !== document.body && a.id !== "main") return;
    let ended = false;  // the tour was just ended: keyboard and screen reader start at the page's heading
    try { ended = window.sessionStorage.getItem("ts-tour-ended") === "1"; window.sessionStorage.removeItem("ts-tour-ended"); } catch (e) { /* no storage: skip */ }
    if (ended) {
      const h = document.querySelector("main h1") || document.querySelector("h1");
      if (h) { h.setAttribute("tabindex", "-1"); try { h.focus({preventScroll: true}); } catch (e) { h.focus(); } return; }
    }
    const t = document.querySelector("[data-focus-here]") || document.querySelector("main > .flash:not(.tz-banner), main > p.error[role=alert]");  // never the time zone box
    if (!t) return;
    if (!t.hasAttribute("tabindex")) t.setAttribute("tabindex", "-1");
    try { t.focus({preventScroll: !!window.location.hash}); } catch (e) { t.focus(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", focusResult);
  else focusResult();

  // "All day" on the log form: the date box asks for a day only, with no time.
  document.addEventListener("change", e => {
    const box = e.target.closest && e.target.closest("input[data-all-day]");
    if (!box || !box.form) return;
    const at = box.form.querySelector("input[data-all-day-input]");
    if (!at) return;
    const old = at.value;
    at.type = box.checked ? "date" : "datetime-local";
    at.value = box.checked ? old.slice(0, 10) : (old ? old + "T09:00" : "");
    const tz = box.form.elements.tz;
    if (tz) tz.disabled = box.checked;
  });
  // A visible count next to a box with data-count-limit, from 80% of the limit on.
  document.addEventListener("input", e => {
    const box = e.target.closest && e.target.closest("input[data-count-limit]");
    if (!box || !box.form) return;
    const note = box.form.querySelector("[data-count-note]");
    if (!note) return;
    const n = box.value.length, max = parseInt(box.dataset.countLimit, 10);
    const kind = box.form.elements.kind;
    note.textContent = (!kind || kind.value === "interview") && n >= max * 0.8 ? n + " of " + max + " used." : "";
  });

  // Drag a card to another column to move it. Every card also has a stage menu, so dragging is optional.
  const card = e => e.target.closest && e.target.closest("[data-cid]");
  const column = e => e.target.closest && e.target.closest("[data-move-url] [data-stage]");
  document.addEventListener("dragstart", e => {
    const c = card(e);
    if (!c) return;
    e.dataTransfer.setData("text/plain", c.dataset.cid);
    e.dataTransfer.effectAllowed = "move";
    c.classList.add("dragging");
  });
  document.addEventListener("dragend", e => { const c = card(e); if (c) c.classList.remove("dragging"); });
  document.addEventListener("dragover", e => { const col = column(e); if (col) { e.preventDefault(); col.classList.add("over"); } });
  document.addEventListener("dragleave", e => {
    const col = column(e);
    if (col && !col.contains(e.relatedTarget)) col.classList.remove("over");
  });
  document.addEventListener("drop", e => {
    const col = column(e);
    if (!col) return;
    e.preventDefault(); col.classList.remove("over");
    const cid = e.dataTransfer.getData("text/plain");
    const board = col.closest("[data-move-url]");
    if (!cid || !board || !window.htmx) return;
    htmx.ajax("POST", board.dataset.moveUrl, {target: "#pipeline", swap: "innerHTML",
      values: {candidate_id: cid, stage: col.dataset.stage, view: "board"}});
  });

  // Colour bubbles on the Candidates table and a person's page.
  let colorLive = null;
  function announceColor(text) {
    if (!colorLive) {
      colorLive = document.createElement("div");
      colorLive.className = "sr-only"; colorLive.setAttribute("role", "status");
      document.body.appendChild(colorLive);
    }
    colorLive.textContent = text;
  }
  function setBubble(btn, d) {
    const was = btn.dataset.color || "none", now = d.color || "none";
    btn.dataset.color = d.color; btn.dataset.next = d.next; btn.dataset.prev = d.prev;
    btn.setAttribute("aria-label", d.aria); btn.title = d.title;
    const letter = btn.querySelector("span"); if (letter) letter.textContent = d.letter;
    const hidden = btn.form && btn.form.elements.color; if (hidden) hidden.value = d.next;
    // The numbers follow the search and filters on the page, so move one person from the old colour to the new
    // one here instead of swapping in whole-database totals.
    if (was !== now) document.querySelectorAll("[data-color-count]").forEach(n => {
      const k = n.dataset.colorCount, v = parseInt(n.textContent, 10);
      if (!isNaN(v) && k === was) n.textContent = Math.max(0, v - 1);
      else if (!isNaN(v) && k === now) n.textContent = v + 1;
    });
  }
  function pickColor(btn, back) {
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    fetch("/candidates/" + encodeURIComponent(btn.dataset.cid) + "/color", {
      method: "POST", credentials: "same-origin",
      headers: {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
      body: new URLSearchParams({color: back ? btn.dataset.prev : btn.dataset.next}).toString(),
    }).then(r => r.json().then(d => ({ok: r.ok, d})))
      .then(({ok, d}) => {
        const who = btn.dataset.who ? btn.dataset.who + ": " : "";
        if (!ok) { announceColor(who + (d.error || "Couldn't save that color.")); return; }
        setBubble(btn, d); announceColor(who + "color is now " + d.name);
      })
      .catch(() => { announceColor((btn.dataset.who ? btn.dataset.who + ": " : "") + "Couldn't save that color."); })
      .finally(() => { delete btn.dataset.busy; });
  }
  document.addEventListener("click", e => {
    const btn = e.target.closest && e.target.closest("button[data-color-bubble]");
    if (!btn || !window.fetch) return;  // no fetch: the form posts and the page reloads
    e.preventDefault(); pickColor(btn, e.shiftKey);
  });
  document.addEventListener("contextmenu", e => {
    const btn = e.target.closest && e.target.closest("button[data-color-bubble]");
    if (!btn || !window.fetch) return;
    e.preventDefault(); pickColor(btn, true);
  });
  // ---- Guided tour (see tour.py). The server renders the steps as escaped JSON into the data-tour attribute of
  // <div id="tour-data"> (no inline script, so the CSP is fine) and keeps the step in settings, so a page load
  // picks up where the last one left off. All text goes in with textContent, never as HTML.
  function startTour() {
    const dataEl = document.getElementById("tour-data");
    if (!dataEl) return;
    let data;
    try { data = JSON.parse(dataEl.dataset.tour); } catch (e) { return; }
    if (!data || !Array.isArray(data.steps) || !data.steps.length) return;
    // ?tour=short only starts the replay; once it's running, take it off the address so a reload doesn't restart it.
    try {
      const u = new URL(window.location.href);
      if (u.searchParams.has("tour")) { u.searchParams.delete("tour"); window.history.replaceState(null, "", u.pathname + u.search + u.hash); }
    } catch (e) { /* leave the address alone */ }

    const steps = data.steps;
    let idx = Math.max(0, Math.min(data.step || 0, steps.length - 1));
    let target = null, reach = null, pulsed = null, confirming = false, shown = -1;
    const picks = [];  // the finish step's "also delete" boxes: {input, field, id}
    const calm = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; };

    const root = el("div", "tour");
    const dims = [0, 1, 2, 3].map(() => el("div", "tour-dim"));
    const ring = el("div", "tour-ring");
    const pop = el("div", "tour-pop");
    pop.setAttribute("role", "dialog"); pop.setAttribute("aria-modal", "true"); pop.tabIndex = -1;
    const count = el("p", "tour-count"), title = el("h2", "tour-title"), body = el("p", "tour-body"), note = el("p", "tour-note");
    const extras = el("div", "tour-extras");
    title.id = "tour-title"; body.id = "tour-body";
    pop.setAttribute("aria-labelledby", "tour-title"); pop.setAttribute("aria-describedby", "tour-body");
    const acts = el("div", "tour-acts");
    pop.append(count, title, body, extras, note, acts);
    root.append(...dims, ring, pop);
    document.body.appendChild(root);

    function postForm(action, fields) {  // a real form post: the server redirects to the page to show next
      if (action === "/tour/skip" || action === "/tour/finish") {  // ending: nothing stays dimmed, and the next page starts at its heading
        try { window.sessionStorage.setItem("ts-tour-ended", "1"); } catch (e) { /* no storage: skip */ }
        clearInert(); root.remove();
      }
      const f = el("form"); f.method = "post"; f.action = action; f.hidden = true;
      Object.keys(fields || {}).forEach(k => { const i = el("input"); i.type = "hidden"; i.name = k; i.value = fields[k]; f.appendChild(i); });
      document.body.appendChild(f); f.submit();
    }
    function btn(label, cls, fn) {
      const b = el("button", "btn " + (cls || ""), label); b.type = "button"; b.addEventListener("click", fn); return b;
    }
    const here = s => !s.path || s.path === window.location.pathname;

    function go(n) {
      n = Math.max(0, Math.min(n, steps.length - 1));
      const next = steps[n];
      const save = window.fetch ? fetch("/tour/step", {method: "POST", credentials: "same-origin",
        body: new URLSearchParams({step: String(n)})}).catch(() => {}) : Promise.resolve();
      save.then(() => {
        idx = n; confirming = false;
        if (next.path && next.path !== window.location.pathname) window.location.assign(next.path);
        else render();
      });
    }
    function skip() { postForm("/tour/skip", {}); }  // "Skip tour" ends it in one click
    function askToEnd() { confirming = true; render(); }  // Esc asks first, so a stray key doesn't end it
    // The finish step and the "End the tour?" question offer to delete the demo people the recruiter worked with and
    // the saved searches they made during the demo. What they tick is posted with Finish or Skip.
    function addPicks() {
      picks.length = 0; extras.textContent = "";
      if (!data.demo) return;
      const ex = data.extras || {};
      const add = (text, field, id) => {
        const lab = el("label", "check"), inp = el("input"); inp.type = "checkbox";
        lab.append(inp, el("span", "", text)); extras.appendChild(lab); picks.push({input: inp, field, id});
      };
      (ex.people || []).forEach(p => add("Also delete " + p.name + " (you worked with them)", "delete_people", p.id));
      // Saved searches made on the demo people go with them on their own: one short line, not a box for each.
      if ((ex.searches || []).length) extras.appendChild(el("p", "fine",
        "Saved searches you made that only find demo people are removed with them."));
    }
    function chosen() {
      const out = {};
      picks.forEach(p => { if (p.input.checked) out[p.field] = out[p.field] ? out[p.field] + "," + p.id : String(p.id); });
      return out;
    }

    // While the popover is open, the page behind it can't be reached (inert), except the lit-up part.
    function clearInert() {
      document.querySelectorAll("[data-tour-inert]").forEach(n => { n.inert = false; n.removeAttribute("data-tour-inert"); });
    }
    function applyInert(keep) {
      clearInert();
      const keepers = keep.filter(Boolean).concat([root]);
      const chain = new Set();
      keepers.forEach(k => { for (let n = k; n && n !== document.body; n = n.parentElement) chain.add(n); });
      const mark = parent => Array.from(parent.children).forEach(ch => {
        if (chain.has(ch)) { if (!keepers.includes(ch)) mark(ch); }
        else if (!ch.matches("script, style, link") && ch.getAttribute("role") !== "status") { ch.inert = true; ch.setAttribute("data-tour-inert", ""); }
      });
      mark(document.body);
    }

    function findTarget(s) {
      for (const sel of s.target || []) {
        let n = null;
        try { n = document.querySelector(sel); } catch (e) { /* a bad selector just misses */ }
        if (n) { const r = n.getBoundingClientRect(); if (r.width || r.height) return n; }
      }
      return null;
    }
    function findReach(s) {
      if (!s.reach) return null;
      let n = null;
      try { n = document.querySelector(s.reach); } catch (e) { /* a bad selector just misses */ }
      if (n) { const r = n.getBoundingClientRect(); if (r.width || r.height) return n; }
      return null;
    }
    function focusables(scope) {
      return Array.from(scope.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select, textarea, summary"))
        .filter(n => n.offsetParent !== null || n === document.activeElement);
    }

    function place() {
      const vw = document.documentElement.clientWidth, vh = window.innerHeight, pad = 6, gap = 12;
      let r = null;
      if (target) {
        const b = target.getBoundingClientRect();
        let l = b.left, t = b.top, rt = b.right, bt = b.bottom;
        if (reach) { const q = reach.getBoundingClientRect(); l = Math.min(l, q.left); t = Math.min(t, q.top); rt = Math.max(rt, q.right); bt = Math.max(bt, q.bottom); }
        r = {l: Math.max(0, l - pad), t: Math.max(0, t - pad), r: Math.min(vw, rt + pad), b: Math.min(vh, bt + pad)};
      }
      const box = (n, l, t, w, h) => { n.style.left = l + "px"; n.style.top = t + "px"; n.style.width = Math.max(0, w) + "px"; n.style.height = Math.max(0, h) + "px"; };
      if (r) {
        box(dims[0], 0, 0, vw, r.t); box(dims[1], 0, r.b, vw, vh - r.b);
        box(dims[2], 0, r.t, r.l, r.b - r.t); box(dims[3], r.r, r.t, vw - r.r, r.b - r.t);
        box(ring, r.l, r.t, r.r - r.l, r.b - r.t); ring.hidden = false;
      } else {
        box(dims[0], 0, 0, vw, vh); dims.slice(1).forEach(d => box(d, 0, 0, 0, 0)); ring.hidden = true;
      }
      const pw = Math.min(380, vw - 24);
      pop.style.width = pw + "px";
      const ph = pop.offsetHeight;
      let left, top;
      if (!r) { left = (vw - pw) / 2; top = Math.max(12, (vh - ph) / 2); }
      else {
        const cx = Math.min(Math.max(12, (r.l + r.r) / 2 - pw / 2), vw - pw - 12);
        const tries = [
          [cx, r.b + gap, r.b + gap + ph <= vh - 8],
          [cx, r.t - gap - ph, r.t - gap - ph >= 8],
          [r.r + gap, Math.min(Math.max(8, r.t), vh - ph - 8), r.r + gap + pw <= vw - 8],
          [r.l - gap - pw, Math.min(Math.max(8, r.t), vh - ph - 8), r.l - gap - pw >= 8]];
        const ok = tries.find(t => t[2]);
        if (ok) { left = ok[0]; top = ok[1]; }
        else { left = (vw - pw) / 2; top = Math.max(8, vh - ph - 16); }  // no room beside it: dock at the bottom
      }
      pop.style.left = Math.round(left) + "px"; pop.style.top = Math.round(top) + "px";
    }
    let queued = false;
    function replace() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(() => {
        queued = false; target = here(steps[idx]) ? findTarget(steps[idx]) : null;
        reach = target ? findReach(steps[idx]) : null; place();
      });
    }

    function render() {
      const s = steps[idx];
      if (pulsed) { pulsed.classList.remove("tour-pulse"); pulsed = null; }
      target = here(s) ? findTarget(s) : null;
      reach = target ? findReach(s) : null;
      if (target && target.tagName === "DETAILS") target.open = true;
      if (target && shown !== idx) {
        // A target taller than the window is shown from its top (its heading and first button), not from its middle.
        const tb = target.getBoundingClientRect(), rb = reach ? reach.getBoundingClientRect() : tb;
        const top = Math.min(tb.top, rb.top), tall = Math.max(tb.bottom, rb.bottom) - top;
        if (tall > window.innerHeight * 0.7) {
          const bar = document.querySelector("body > header, header.top, .topbar");
          const sticky = bar && getComputedStyle(bar).position === "sticky" ? bar.offsetHeight : 0;
          window.scrollTo({top: Math.max(0, top + window.scrollY - sticky - 16), behavior: calm() ? "auto" : "smooth"});
        } else window.scrollTo({top: Math.max(0, top + window.scrollY - (window.innerHeight - tall) / 2), behavior: calm() ? "auto" : "smooth"});
      }
      if (target && s.pulse) {
        try { pulsed = document.querySelector(s.pulse); } catch (e) { pulsed = null; }
        if (pulsed && pulsed.disabled) pulsed = null;  // a button that can't be pressed: the lit-up panel is the cue
        if (pulsed) pulsed.classList.add("tour-pulse");
      }
      acts.textContent = ""; note.textContent = ""; extras.textContent = ""; picks.length = 0;
      if (confirming) {
        count.textContent = "Before you go"; title.textContent = "End the tour?";
        body.textContent = data.demo
          ? "The demo people the tour added will be cleared, and so will anyone you only gave a color to. A backup is saved first. Anyone else you worked with is kept as yours unless you tick the box below."
          : "The tour closes. You can start it again any time from Take the tour in the footer or in Settings.";
        addPicks();
        acts.append(btn("Keep going", "primary", () => { confirming = false; render(); }),
                    btn(data.demo ? "End tour and clear demo data" : "End the tour", "", () => postForm("/tour/skip", chosen())));
      } else {
        count.textContent = (idx + 1) + " of " + steps.length;
        title.textContent = s.title; body.textContent = s.body;
        if (!here(s)) {
          note.textContent = "This step is on another page.";
          acts.append(btn("Go to this page", "small", () => window.location.assign(s.path)));
        }
        const nav = el("div", "tour-nav");
        if (idx > 0) nav.append(btn("Back", "", () => go(idx - 1)));
        if (s.action === "load") nav.append(btn("Load the demo people", "primary", () => postForm("/tour/load")), btn("Not now", "", () => go(idx + 1)));
        else if (s.action === "finish") { addPicks(); nav.append(btn(s.cta || "Finish the tour", "primary", () => postForm("/tour/finish", chosen()))); }
        else nav.append(btn("Next", "primary", () => go(idx + 1)));
        acts.append(nav);
        acts.append(btn("Skip tour", "ghost small tour-skip", skip));
      }
      shown = idx;
      applyInert([target, reach]);
      place();
      pop.focus({preventScroll: true});  // Tab then reaches the popover's buttons
      place();
    }

    // The tour never sits over a box the person is typing in: while focus is in a field of the page, the tour
    // steps aside (hidden, page usable); it comes back when focus leaves, and nothing typed is touched.
    let paused = false;
    const isField = n => !!(n && n.matches && !root.contains(n)
      && n.matches("input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]):not([type=hidden]), textarea, select, [contenteditable=true]"));
    function pause() { if (paused) return; paused = true; root.hidden = true; clearInert(); }
    function resume() {
      if (!paused || isField(document.activeElement)) return;
      paused = false; root.hidden = false; render();
    }
    document.addEventListener("focusin", e => { if (isField(e.target)) pause(); });
    document.addEventListener("focusout", () => { if (paused) window.setTimeout(resume, 0); });
    document.addEventListener("keydown", e => {
      if (!document.body.contains(root) || paused) return;
      // Esc on a menu closes the menu (a native dropdown may be open), not the tour.
      if (e.key === "Escape" && e.target && e.target.tagName === "SELECT") return;
      if (e.key === "Escape") { e.preventDefault(); if (confirming) { confirming = false; render(); } else askToEnd(); return; }  // Esc asks first
      if (e.key !== "Tab") return;
      // Keep Tab inside the tour: its own buttons, then the lit-up area (so a Connect button can be reached by keyboard).
      const list = focusables(pop).concat(target ? focusables(target) : [], reach && reach !== target && !(target && target.contains(reach)) ? focusables(reach).concat(reach.matches("button, a[href]") ? [reach] : []) : []);
      if (!list.length) return;
      const at = list.indexOf(document.activeElement);
      const next = e.shiftKey ? (at <= 0 ? list[list.length - 1] : list[at - 1]) : (at === -1 || at === list.length - 1 ? list[0] : list[at + 1]);
      e.preventDefault(); next.focus();
    }, true);
    window.addEventListener("resize", replace);
    window.addEventListener("scroll", replace, true);
    const main = document.querySelector("main");
    if (main && window.MutationObserver) new MutationObserver(replace).observe(main, {childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "open"]});
    if (window.ResizeObserver) new ResizeObserver(replace).observe(document.body);
    if (isField(document.activeElement)) pause(); else render();  // never start over a box that has focus
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startTour);
  else startTour();
})();

// Candidates table, skill highlights: after a pick or a remove the page reloads, so put keyboard focus back on the
// chip the #fragment names (the same chip, else the highlight bar) and say in words what changed.
(function () {
  function say(text) { const s = document.getElementById("hl-status"); if (s) s.textContent = text; return s; }
  function people(n) { return n === 1 ? "1 person has it and is" : Number(n).toLocaleString("en-US") + " people have it and are"; }
  function start() {
    const status = document.getElementById("hl-status");
    if (!status) return;
    const hash = decodeURIComponent((location.hash || "").slice(1));
    if (!hash) return;
    const bar = document.getElementById("hl-bar");
    const fallback = bar || status;
    let target = null, text = "";
    if (hash === "hlcleared") { text = "Cleared the highlights. The usual order is back."; target = status; }
    else if (hash.indexOf("removed-") === 0) { text = "Removed the " + hash.slice(8) + " highlight."; target = fallback; }
    else {
      target = document.getElementById(hash);
      const skill = target && target.getAttribute("data-skill");
      if (skill) {
        const lit = bar && Array.prototype.find.call(bar.querySelectorAll("[data-skill]"), el => el.getAttribute("data-skill") === skill);
        if (lit) text = skill + " highlighted " + lit.getAttribute("data-hl") + ". " + people(lit.getAttribute("data-count") || 0) + " at the top.";
        else text = "Removed the " + skill + " highlight.";
      } else { target = fallback; }
    }
    if (!target) target = fallback;
    if (text) window.setTimeout(() => say(text), 50);  // after load, so the live region speaks it
    target.focus();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();

// Compare, after Remove (x): the page reloads, so put keyboard focus on the Remove button the #fragment names (the
// next person's), or on the table region when no Remove buttons are left.
(function () {
  function start() {
    const hash = decodeURIComponent((location.hash || "").slice(1));
    if (!/^(rm-\d+|compare-region)$/.test(hash)) return;
    const t = document.getElementById(hash) || document.getElementById("compare-region");
    if (!t) return;
    try { t.focus({preventScroll: false}); } catch (e) { t.focus(); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
