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

  // --- Chat picker (Settings page) ---
  // Rows are rendered from /deployments/{id}/chats.json; the raw
  // #whitelist/#blacklist textareas remain the source of truth the form
  // POSTs, so toggling a row rewrites them. Ids in the textareas for chats
  // not currently loaded in the picker are preserved across re-syncs.

  var CHAT_PAGE_SIZE = 20;
  // Cap how many rows the picker keeps in the DOM at once. Without this,
  // repeated "Load more" clicks on an account with many chats would grow
  // the row count (and the cost of every search/toggle scan over it)
  // without bound. Evicting the oldest page is safe: toggled ids for rows
  // no longer loaded are already preserved via the textarea Set logic.
  var MAX_LOADED_ROWS = 200;

  function chatIdsFromTextarea(el) {
    return new Set(
      (el.value || "")
        .split("\n")
        .map(function (s) { return s.trim(); })
        .filter(Boolean)
    );
  }

  function resizeAutosize(el) {
    if (!el || !el.dataset.autosize) return;
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
  }

  function setChatButtonPressed(btn, pressed) {
    btn.setAttribute("aria-pressed", pressed ? "true" : "false");
    if (btn.dataset.action === "whitelist") {
      btn.classList.toggle("button--primary", pressed);
    } else {
      btn.classList.toggle("button--danger", pressed);
    }
  }

  function buildChatRow(chat, whitelisted, blacklisted) {
    var row = document.createElement("div");
    row.className = "chat-picker__row";
    row.dataset.chatId = chat.id;
    row.dataset.chatName = chat.name || "";

    var avatar = document.createElement("span");
    avatar.className = "chat-picker__avatar";
    avatar.textContent = chat.avatar_initial || (chat.id || "?")[0].toUpperCase();

    var copy = document.createElement("span");
    copy.className = "chat-picker__copy";
    var name = document.createElement("strong");
    name.textContent = chat.name || chat.id;
    var idSpan = document.createElement("span");
    idSpan.className = "mono muted text-sm";
    idSpan.textContent = chat.id;
    copy.appendChild(name);
    copy.appendChild(idSpan);

    var actions = document.createElement("span");
    actions.className = "chat-picker__actions";

    var wl = document.createElement("button");
    wl.type = "button";
    wl.className = "button button--sm";
    wl.dataset.action = "whitelist";
    wl.textContent = "Whitelist";
    setChatButtonPressed(wl, whitelisted);

    var bl = document.createElement("button");
    bl.type = "button";
    bl.className = "button button--sm";
    bl.dataset.action = "blacklist";
    bl.textContent = "Blacklist";
    setChatButtonPressed(bl, blacklisted);

    actions.appendChild(wl);
    actions.appendChild(bl);

    row.appendChild(avatar);
    row.appendChild(copy);
    row.appendChild(actions);
    return row;
  }

  // Updates only the single toggled id in the appropriate textarea, leaving
  // every other line (including manual edits for other loaded chats)
  // untouched. Rebuilding from all loaded rows on every click would clobber
  // manual textarea edits the user makes for chats also shown in the picker.
  function syncChatTextarea(action, id, pressed) {
    var el = document.getElementById(action === "whitelist" ? "whitelist" : "blacklist");
    if (!el) return;
    var ids = chatIdsFromTextarea(el);
    if (pressed) ids.add(id); else ids.delete(id);
    el.value = Array.from(ids).join("\n");
    // Programmatic value changes don't fire `input`, so nudge autosize manually.
    resizeAutosize(el);
  }

  function filterChatRows(container, query) {
    var q = query.trim().toLowerCase();
    var rows = container.querySelectorAll("[data-chat-id]");
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      if (!q) { row.style.display = ""; continue; }
      var name = (row.dataset.chatName || "").toLowerCase();
      var id = (row.dataset.chatId || "").toLowerCase();
      row.style.display = name.indexOf(q) !== -1 || id.indexOf(q) !== -1 ? "" : "none";
    }
  }

  async function loadChatPage(depId, offset, limit) {
    var res = await fetch(
      "/deployments/" + depId + "/chats.json?limit=" + limit + "&offset=" + offset
    );
    if (!res.ok) return { chats: [], has_more: false, error: "WhatsApp API is not reachable" };
    return res.json();
  }

  function showChatError(container, message) {
    var row = document.createElement("div");
    row.className = "chat-picker__row";
    var copy = document.createElement("span");
    copy.className = "chat-picker__copy";
    copy.textContent = message + " — see /dependencies";
    row.appendChild(copy);
    container.appendChild(row);
  }

  function showChatLoading(container) {
    clearChatLoading(container);
    var row = document.createElement("div");
    row.className = "chat-picker__loading";
    row.dataset.chatLoading = "true";
    var spinner = document.createElement("span");
    spinner.className = "chat-picker__spinner";
    spinner.setAttribute("aria-hidden", "true");
    var text = document.createElement("span");
    text.textContent = "Loading chats…";
    row.appendChild(spinner);
    row.appendChild(text);
    container.appendChild(row);
    return row;
  }

  function clearChatLoading(container) {
    var rows = container.querySelectorAll("[data-chat-loading]");
    for (var i = 0; i < rows.length; i++) rows[i].remove();
  }

  function showChatEmpty(container) {
    clearChatEmpty(container);
    var row = document.createElement("div");
    row.className = "chat-picker__empty";
    row.dataset.chatEmpty = "true";
    row.textContent = "No chats found.";
    container.appendChild(row);
  }

  function clearChatEmpty(container) {
    var rows = container.querySelectorAll("[data-chat-empty]");
    for (var i = 0; i < rows.length; i++) rows[i].remove();
  }

  function initChatPicker() {
    var container = document.querySelector("[data-chat-picker]");
    if (!container) return;
    var depId = container.dataset.depId;
    var search = document.getElementById("chat_search");
    var loadMore = document.getElementById("chat_load_more");

    var offset = 0;
    var hasMore = false;

    function currentPressed() {
      var wlEl = document.getElementById("whitelist");
      var blEl = document.getElementById("blacklist");
      return {
        wl: wlEl ? chatIdsFromTextarea(wlEl) : new Set(),
        bl: blEl ? chatIdsFromTextarea(blEl) : new Set(),
      };
    }

    function appendRows(chats) {
      var pressed = currentPressed();
      var frag = document.createDocumentFragment();
      for (var i = 0; i < chats.length; i++) {
        var c = chats[i];
        frag.appendChild(buildChatRow(c, pressed.wl.has(c.id), pressed.bl.has(c.id)));
      }
      container.appendChild(frag);
      evictOldestRows();
    }

    function evictOldestRows() {
      var rows = container.querySelectorAll("[data-chat-id]");
      var excess = rows.length - MAX_LOADED_ROWS;
      for (var i = 0; i < excess; i++) rows[i].remove();
    }

    async function fetchAndRender() {
      clearChatEmpty(container);
      showChatLoading(container);
      if (search) search.disabled = true;
      if (loadMore) loadMore.disabled = true;
      try {
        var data = await loadChatPage(depId, offset, CHAT_PAGE_SIZE);
        clearChatLoading(container);
        if (data.error) {
          showChatError(container, data.error);
          hasMore = false;
        } else {
          appendRows(data.chats || []);
          hasMore = !!data.has_more;
          if (!data.chats || data.chats.length === 0) {
            showChatEmpty(container);
          }
        }
      } finally {
        if (search) search.disabled = false;
        if (loadMore) {
          loadMore.disabled = false;
          loadMore.textContent = "Load more chats";
        }
      }
      if (loadMore) loadMore.style.display = hasMore ? "" : "none";
      if (search) filterChatRows(container, search.value);
    }

    fetchAndRender();

    // Delegated click for whitelist/blacklist toggles.
    container.addEventListener("click", function (event) {
      var btn = event.target.closest("[data-action]");
      if (!btn || !container.contains(btn)) return;
      var row = btn.closest("[data-chat-id]");
      if (!row) return;
      var id = row.dataset.chatId;
      var pressed = btn.getAttribute("aria-pressed") === "true";
      var otherAction = btn.dataset.action === "whitelist" ? "blacklist" : "whitelist";
      var other = row.querySelector('[data-action="' + otherAction + '"]');
      var otherWasPressed = other && other.getAttribute("aria-pressed") === "true";

      setChatButtonPressed(btn, !pressed);
      syncChatTextarea(btn.dataset.action, id, !pressed);

      if (!pressed && other && otherWasPressed) {
        setChatButtonPressed(other, false);
        syncChatTextarea(otherAction, id, false);
      }
    });

    if (search) {
      search.addEventListener("input", function () {
        filterChatRows(container, search.value);
      });
    }

    if (loadMore) {
      loadMore.style.display = "none";
      loadMore.addEventListener("click", function () {
        offset += CHAT_PAGE_SIZE;
        loadMore.textContent = "Loading…";
        fetchAndRender();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", initChatPicker);

  // --- Live polling without the meta-refresh flash ---
  // Pages that used <meta http-equiv="refresh"> to self-update while
  // waiting on an async backend state (WhatsApp QR scan, Brain document
  // ingest) now wrap just the reloadable region in an element carrying
  // data-poll="<ms>" instead. We refetch the same URL, and swap only that
  // element's own markup in place — never the whole page — so unrelated
  // content elsewhere on the page (e.g. the Brain ingestion forms, which
  // sit outside this wrapper) is never touched by a poll tick and any
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
