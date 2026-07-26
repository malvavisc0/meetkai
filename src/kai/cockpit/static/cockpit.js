(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.dataset.confirm) return;
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  document.addEventListener("input", function (event) {
    var field = event.target;
    if (!(field instanceof HTMLTextAreaElement)) return;
    if (!field.dataset.autosize) return;
    field.style.height = "auto";
    field.style.height = field.scrollHeight + "px";
  });

  // Trigger initial sizing for all autosize textareas on load.
  document.addEventListener("DOMContentLoaded", function () {
    var textareas = document.querySelectorAll("textarea[data-autosize]");
    for (var i = 0; i < textareas.length; i++) {
      var field = textareas[i];
      field.style.height = "auto";
      field.style.height = field.scrollHeight + "px";
    }
  });

  // --- Live polling without the meta-refresh flash ---
  // Pages that used <meta http-equiv="refresh"> to self-update while
  // waiting on an async backend state (Brain document ingest) now wrap
  // just the reloadable region in an element carrying data-poll="<ms>"
  // instead. We refetch the same URL, and swap only that element's own
  // markup in place — never the whole page — so unrelated content
  // elsewhere on the page (e.g. the Brain ingestion forms, which sit
  // outside this wrapper) is never touched by a poll tick and any
  // in-progress input there is preserved. Polling stops on its own once
  // the server stops rendering a data-poll wrapper at all.
  function initPoll() {
    var target = document.querySelector("[data-poll]");
    if (!target) return;
    var interval = parseInt(target.dataset.poll, 10) || 3000;

    var timer = window.setInterval(async function () {
      var res;
      try {
        res = await fetch(window.location.href, { credentials: "same-origin" });
      } catch (e) {
        return; // transient network error — try again next tick
      }
      if (!res.ok) return;
      var html = await res.text();
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.querySelector("[data-poll]");
      if (!fresh) {
        window.clearInterval(timer);
        return;
      }
      if (fresh.innerHTML !== target.innerHTML) {
        target.innerHTML = fresh.innerHTML;
        initCountUp();
      }
    }, interval);
  }

  document.addEventListener("DOMContentLoaded", initPoll);

  // --- File upload filename display (Brain Sources) ---
  // Progressive enhancement: shows the chosen file name next to the styled
  // upload button. Each <span data-filename-for="<inputId>"> mirrors the
  // selected file. If JS is unavailable, the static "Choose document"
  // button label remains and the control never implies a broken state.
  function initUploadFilename() {
    var spans = document.querySelectorAll("[data-filename-for]");
    for (var i = 0; i < spans.length; i++) {
      (function (span) {
        var input = document.getElementById(span.dataset.filenameFor);
        if (!input) return;
        input.addEventListener("change", function () {
          var name = (input.files && input.files[0] && input.files[0].name) || "";
          span.textContent = name;
        });
      })(spans[i]);
    }
  }

  document.addEventListener("DOMContentLoaded", initUploadFilename);
})();
