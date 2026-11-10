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
    var name = slug(title) || slug(note ? note.value : "");
    name = name.replace(/^shortlist(-for)?(-|$)/, "");
    if (file) file.textContent = "shortlist-" + (name ? name + "-" : "") + today + ".xlsx";
    var hasJob = !!(job && job.value);
    var n = notesChoice();
    var shown = [];
    cols.forEach(function (c) {
      if (c.kind === "base" || (c.kind === "stage" && hasJob) || (c.kind === "contact" && contact && contact.checked) ||
          (c.kind === "notes" && n !== "none")) shown.push(c.name);
    });
    if (colsOut) colsOut.textContent = shown.join(", ");
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
