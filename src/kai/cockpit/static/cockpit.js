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
})();
