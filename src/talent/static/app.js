// Talent Search app window. All page scripts live here so the Content-Security-Policy
// can forbid inline scripts. Pages opt in with data- attributes:
//   data-confirm="question"   on a form: ask before submitting
//   data-autosubmit           on a select: submit its form when it changes
//   data-recipe="run|copy"    on a button with data-prompt (and optional data-fill)
//   data-copy-text="..." or data-copy-from="element id"   on a button: copy that text
//   data-copy-note="next"     show the "Copied" message in the next element instead of the button
//   data-move-url="/jobs/1/move"   on the pipeline: drag a card to another column to move it
//   data-stage-pick           on a stage menu: move at once, or ask for a reason first for an out stage
(function () {
  "use strict";

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

  document.addEventListener("submit", e => {
    const form = e.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });

  document.addEventListener("change", e => {
    const el = e.target;
    if (el.matches && el.matches("select[data-autosubmit]") && el.form) el.form.submit();
    // Stage menus: an out stage shows the optional reason first; any other pick (or a reason) moves at once.
    const form = el.closest && el.closest("form.stage-form");
    if (!form) return;
    if (el.matches("select[data-stage-pick]")) {
      const o = el.selectedOptions && el.selectedOptions[0];
      const reason = form.querySelector(".reason");
      if (o && o.dataset.out && reason) { reason.hidden = false; return; }
      if (reason) reason.hidden = true;
    }
    form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
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
})();
