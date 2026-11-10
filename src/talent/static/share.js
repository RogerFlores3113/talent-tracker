// The Share page: keeps the preview (file name, columns, first names) in step with the form, and says when the
// file is on its way. Nothing here leaves the page.
(function () {
  var form = document.getElementById("share-form");
  if (!form) return;
  var data = {};
  try { data = JSON.parse(form.getAttribute("data-preview") || "{}"); } catch (e) { data = {}; }
  var cols = data.cols || [];
  var today = form.getAttribute("data-today") || "";
  var job = document.getElementById("share-job");
  var note = document.getElementById("share-note");
  var contact = form.querySelector("input[name=include_contact]");
  var radios = form.querySelectorAll("input[name=notes]");
  var warn = document.getElementById("notes-warn");
  var rejected = document.getElementById("rejected-row");
  var file = document.getElementById("pv-file");
  var colsOut = document.getElementById("pv-cols");
  var namesOut = document.getElementById("pv-names");
  var countOut = document.getElementById("pv-count");
  var rejBox = form.querySelector("input[name=include_rejected]");
  var total = parseInt(form.getAttribute("data-count") || "0", 10);
  var jobs = data.jobs || {};

  function slug(text) {
    var s = (text || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
    s = s.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60).replace(/-+$/, "");
    return s;
  }
  function notesChoice() {
    for (var i = 0; i < radios.length; i++) if (radios[i].checked) return radios[i].value;
    return "none";
  }
  function update() {
    var opt = job && job.options[job.selectedIndex];
    var title = opt ? opt.getAttribute("data-title") || "" : "";
    var name = slug(title);  // only the job: the note at the top is free text and never goes in a file name
    if (file) file.textContent = "shortlist-" + (name ? name + "-" : "") + today + ".xlsx";
    if (note) note.placeholder = title ? "Shortlist for " + title : "A line for the top of the file";
    var hasJob = !!(job && job.value);
    var n = notesChoice();
    var pick = (jobs[hasJob ? job.value : ""] || {})[hasJob && rejBox && rejBox.checked ? "in" : "out"] ||
      {names: data.names || [], count: total, stage: false};
    var shown = [];
    cols.forEach(function (c) {
      if (c.kind === "base" || (c.kind === "stage" && pick.stage) || (c.kind === "contact" && contact && contact.checked) ||
          (c.kind === "notes" && n !== "none")) shown.push(c.name);
    });
    if (colsOut) colsOut.textContent = shown.join(", ");
    if (namesOut) namesOut.textContent = pick.count ? pick.names.join(", ") + (pick.count > 3 ? " and " + (pick.count - 3) + " more" : "") : "Nobody";
    if (countOut) countOut.textContent = pick.count + (pick.count !== total ? " of " + total + " (rejected people are left out)" : "");
    if (warn) warn.hidden = n === "none";
    if (rejected) rejected.hidden = !hasJob;
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.addEventListener("submit", function () {
    var live = document.getElementById("share-live");
    if (live) live.textContent = "Your spreadsheet is downloading. Look in your Downloads folder.";
  });
  update();
})();
