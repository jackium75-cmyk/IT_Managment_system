// Small progressive enhancements. The application works without JavaScript
// except for charts.
document.addEventListener("DOMContentLoaded", function () {
  // Confirm destructive or important actions declared with data-confirm.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Print buttons and auto-submitting filters (no inline handlers, so the
  // Content-Security-Policy can forbid inline scripts).
  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });
  document.querySelectorAll("[data-autosubmit]").forEach(function (field) {
    field.addEventListener("change", function () { field.form.submit(); });
  });

  // Unplanned-work form: show the "already done" fields only when relevant.
  var modes = document.querySelectorAll('input[name="mode"]');
  if (modes.length) {
    var syncMode = function () {
      var done = document.querySelector('input[name="mode"][value="done"]');
      document.querySelectorAll(".when-done").forEach(function (el) { el.hidden = !(done && done.checked); });
    };
    modes.forEach(function (r) { r.addEventListener("change", syncMode); });
    syncMode();
  }

  // Render dashboard charts from the JSON embedded by json_script.
  if (window.Chart) {
    var palette = ["#1f6feb", "#d9480f", "#2b8a3e", "#ae3ec9", "#f59f00", "#0b7285", "#c2255c"];
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var spec = JSON.parse(document.getElementById(canvas.dataset.chart).textContent);
      var datasets = spec.datasets.map(function (ds, i) {
        var colour = palette[i % palette.length];
        var round = spec.type === "doughnut" || spec.type === "pie";
        return Object.assign({
          borderColor: round ? "#fff" : colour,
          backgroundColor: round ? palette.slice(0, spec.labels.length).map(function (c) { return c + "cc"; })
            : (spec.type === "line" ? colour + "22" : colour + "cc"),
          tension: 0.25, fill: spec.type === "line" && spec.datasets.length === 1, borderWidth: 2,
        }, ds);
      });
      new Chart(canvas, {
        type: spec.type,
        data: { labels: spec.labels, datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          indexAxis: spec.horizontal ? "y" : "x",
          plugins: { legend: { display: datasets.length > 1 || spec.type === "doughnut" || spec.type === "pie" } },
          scales: spec.type === "pie" || spec.type === "doughnut" ? {} : {
            x: { stacked: !!spec.stacked, beginAtZero: true },
            y: { stacked: !!spec.stacked, beginAtZero: true, ticks: { precision: spec.horizontal ? undefined : 0 } },
          },
        },
      });
    });
  }

  // ---- Evidence uploads: list chosen files, flag problems early, show upload progress ----
  function humanSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.ceil(b / 1024) + " KB"; }
  document.querySelectorAll("input.evidence-input").forEach(function (input) {
    var wrap = input.closest(".evidence-upload");
    var list = wrap.querySelector(".evidence-list");
    var allowed = (input.getAttribute("accept") || "").split(",").filter(function (a) { return a.charAt(0) === "."; });
    var maxBytes = parseInt(input.dataset.maxMb, 10) * 1048576;
    var maxFiles = parseInt(input.dataset.maxFiles, 10);
    input.addEventListener("change", function () {
      list.innerHTML = "";
      var bad = 0;
      var files = Array.prototype.slice.call(input.files);
      if (files.length > maxFiles) { bad++; list.insertAdjacentHTML("beforeend", '<li class="text-danger">Choose at most ' + maxFiles + " files.</li>"); }
      files.forEach(function (f) {
        var ext = "." + (f.name.split(".").pop() || "").toLowerCase();
        var problem = allowed.indexOf(ext) < 0 ? "file type not accepted" : (f.size > maxBytes ? "larger than " + input.dataset.maxMb + " MB" : "");
        if (problem) bad++;
        var li = document.createElement("li");
        li.className = problem ? "text-danger" : "text-muted";
        li.textContent = (problem ? "✖ " : "✔ ") + f.name + " (" + humanSize(f.size) + ")" + (problem ? " – " + problem : "");
        list.appendChild(li);
      });
      input.setCustomValidity(bad ? "Remove the files marked in red." : "");
      input.classList.toggle("is-invalid", !!bad);
    });
  });
  document.querySelectorAll("form.evidence-form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var input = form.querySelector("input.evidence-input");
      if (!window.FormData || !input || !input.files.length) return;  // normal submit
      if (!form.checkValidity()) return;
      event.preventDefault();
      var bar = form.querySelector(".progress"), inner = bar.querySelector(".progress-bar");
      var buttons = form.querySelectorAll("button[type=submit]");
      buttons.forEach(function (b) { b.disabled = true; });
      bar.classList.remove("d-none");
      var xhr = new XMLHttpRequest();
      xhr.open("POST", form.action || window.location.href);
      xhr.upload.addEventListener("progress", function (e) {
        if (!e.lengthComputable) return;
        var pct = Math.round(e.loaded / e.total * 100);
        inner.style.width = pct + "%"; inner.textContent = pct + "%"; bar.setAttribute("aria-valuenow", pct);
      });
      xhr.addEventListener("load", function () {
        // Show the page the server answered with (the ticket after a redirect, or the form with errors).
        // Rendering the response directly keeps the confirmation message, which a second request would lose.
        var target = xhr.responseURL || window.location.href;
        document.open(); document.write(xhr.responseText); document.close();
        if (target !== window.location.href) history.replaceState(null, "", target);
      });
      xhr.addEventListener("error", function () {
        buttons.forEach(function (b) { b.disabled = false; });
        inner.classList.add("bg-danger"); inner.textContent = "Upload failed – check the connection and try again";
      });
      xhr.send(new FormData(form));
    });
  });

  // ---- Dependent subcategory list on the ticket form ----
  var subMapEl = document.getElementById("subcategory-map");
  var catSelect = document.getElementById("id_category"), subSelect = document.getElementById("id_subcategory");
  if (subMapEl && catSelect && subSelect) {
    var subMap = JSON.parse(subMapEl.textContent), keep = subSelect.value;
    var fill = function () {
      var subs = subMap[catSelect.value] || [];
      subSelect.innerHTML = '<option value="">' + (subs.length ? "Choose (optional)…" : "—") + "</option>";
      subs.forEach(function (s) {
        var o = document.createElement("option"); o.value = s[0]; o.textContent = s[1];
        if (String(s[0]) === keep) o.selected = true;
        subSelect.appendChild(o);
      });
      subSelect.disabled = !subs.length;
      subSelect.closest(".subcat-wrap").hidden = !subs.length;
    };
    catSelect.addEventListener("change", function () { keep = ""; fill(); });
    fill();
  }

  // ---- Ticket status form: show only the fields relevant to the chosen status ----
  var statusSelect = document.querySelector("form.ticket-status-form #id_status");
  if (statusSelect) {
    var sync = function () {
      document.querySelectorAll("[data-show-for]").forEach(function (el) {
        el.hidden = el.dataset.showFor.split(" ").indexOf(statusSelect.value) < 0;
      });
    };
    statusSelect.addEventListener("change", sync); sync();
  }
});
