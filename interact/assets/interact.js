/* PluginInteract page SDK — injected into every page. Exposes window.Interact.
   Static by design: state lives in localStorage and the final answer goes back through
   Telegram.WebApp.sendData() (the Mini App is opened from the bot's keyboard button). */
(function () {
  "use strict";

  var MAX_BYTES = 4096; // Telegram's sendData limit
  var cfg = window.__INTERACT__ || {};
  var msg = cfg.messages || {};
  var tg = (window.Telegram && window.Telegram.WebApp) || null;
  // Keyboard-button Mini Apps (the only ones allowed to sendData) get an EMPTY initData, so detect
  // Telegram by its launch platform instead ("unknown" in a plain browser).
  var inTelegram = !!(tg && (tg.initData || (tg.platform && tg.platform !== "unknown")));
  var storageKey = "interact:" + (cfg.pageId || "page");
  var FORBIDDEN_KEYS = ["__proto__", "constructor", "prototype"];
  var BINDABLE = "[data-bind],[data-action],[data-set],[data-toggle],form[data-interact]";

  var saved = loadLocal();
  var state = Object.assign({}, clone(cfg.initialState) || {}, (saved && saved.state) || {});
  var submitted = !!(saved && saved.submitted);
  var expired = !!(cfg.expiresAt && Date.now() > cfg.expiresAt);
  var locked = false;
  var listeners = [];
  var seen = typeof WeakSet === "function" ? new WeakSet() : null;
  var saveTimer = null;
  var notifying = false;

  // ---------------------------------------------------------------- helpers

  function loadLocal() {
    try {
      var raw = window.localStorage.getItem(storageKey);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null; // storage disabled or corrupt: start from the initial state
    }
  }

  function saveLocal() {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({ state: state, submitted: submitted, savedAt: Date.now() }));
      return true;
    } catch (e) {
      return false;
    }
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function keysOf(path) {
    var keys = String(path == null ? "" : path).split(".").filter(Boolean);
    keys.forEach(function (key) {
      if (FORBIDDEN_KEYS.indexOf(key) !== -1) throw new Error("Interact: forbidden key " + key);
    });
    return keys;
  }

  function getPath(obj, path) {
    var keys = keysOf(path);
    var cur = obj;
    for (var i = 0; i < keys.length; i++) {
      if (cur == null) return undefined;
      cur = cur[keys[i]];
    }
    return cur;
  }

  function setPath(obj, path, value) {
    var keys = keysOf(path);
    if (!keys.length) return;
    var cur = obj;
    for (var i = 0; i < keys.length - 1; i++) {
      if (cur[keys[i]] == null || typeof cur[keys[i]] !== "object") {
        cur[keys[i]] = /^\d+$/.test(keys[i + 1]) ? [] : {};
      }
      cur = cur[keys[i]];
    }
    cur[keys[keys.length - 1]] = value;
  }

  function parseValue(raw) {
    if (raw == null) return raw;
    try {
      return JSON.parse(raw);
    } catch (e) {
      return raw;
    }
  }

  function truthy(value) {
    return Array.isArray(value) ? value.length > 0 : !!value;
  }

  function byteLength(text) {
    return typeof TextEncoder === "function" ? new TextEncoder().encode(text).length : unescape(encodeURIComponent(text)).length;
  }

  function emit(name, detail) {
    try {
      document.dispatchEvent(new CustomEvent(name, { detail: detail }));
    } catch (e) { /* old webview */ }
  }

  // ---------------------------------------------------------------- fields & rendering

  function isField(el) {
    return el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA";
  }

  function fieldType(el) {
    return (el.getAttribute("type") || "").toLowerCase();
  }

  function boundTo(key) {
    return Array.prototype.filter.call(document.querySelectorAll("[data-bind]"), function (node) {
      return node.getAttribute("data-bind") === key;
    });
  }

  function readField(el) {
    var type = fieldType(el);
    var key = el.getAttribute("data-bind");
    if (type === "checkbox") {
      var group = boundTo(key).filter(function (node) { return fieldType(node) === "checkbox"; });
      if (group.length > 1) {
        return group.filter(function (node) { return node.checked; }).map(function (node) { return node.value; });
      }
      return el.checked;
    }
    if (type === "number" || type === "range") return el.value === "" ? null : Number(el.value);
    if (el.tagName === "SELECT" && el.multiple) {
      return Array.prototype.filter.call(el.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
    }
    return el.hasAttribute("data-json") ? parseValue(el.value) : el.value;
  }

  function writeField(el, value) {
    var type = fieldType(el);
    if (type === "checkbox") {
      el.checked = Array.isArray(value) ? value.map(String).indexOf(el.value) !== -1 : !!value;
      return;
    }
    if (type === "radio") {
      el.checked = value != null && String(value) === el.value;
      return;
    }
    if (el.tagName === "SELECT" && el.multiple) {
      var selected = (Array.isArray(value) ? value : []).map(String);
      each(el.options, function (o) { o.selected = selected.indexOf(o.value) !== -1; });
      return;
    }
    if (el === document.activeElement) return; // never fight the user's typing
    var text = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    if (el.value !== text) el.value = text;
  }

  function format(value, kind, el) {
    if (value == null) return "";
    var locale = cfg.locale || navigator.language || "pt-BR";
    try {
      if (kind === "currency") {
        var currency = el.getAttribute("data-currency") || cfg.currency || "BRL";
        return new Intl.NumberFormat(locale, { style: "currency", currency: currency }).format(Number(value));
      }
      if (kind === "number") return new Intl.NumberFormat(locale).format(Number(value));
      if (kind === "percent") {
        return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 2 }).format(Number(value));
      }
    } catch (e) { /* fall through to plain text */ }
    if (kind === "json" || typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }

  function render(el) {
    var value = getPath(state, el.getAttribute("data-bind"));
    if (isField(el)) writeField(el, value);
    else el.textContent = format(value, el.getAttribute("data-format"), el);
  }

  function showIf(expr) {
    var negate = expr.charAt(0) === "!";
    if (negate) expr = expr.slice(1);
    var eq = expr.indexOf("=");
    var result = eq === -1
      ? truthy(getPath(state, expr.trim()))
      : String(getPath(state, expr.slice(0, eq).trim())) === expr.slice(eq + 1).trim();
    return negate ? !result : result;
  }

  function isPressed(el) {
    if (el.hasAttribute("data-toggle")) return !!getPath(state, el.getAttribute("data-toggle"));
    var current = getPath(state, el.getAttribute("data-set"));
    return JSON.stringify(current) === JSON.stringify(parseValue(el.getAttribute("data-value")));
  }

  function sync() {
    each(document.querySelectorAll("[data-bind]"), render);
    each(document.querySelectorAll("[data-show-if]"), function (el) {
      el.hidden = !showIf(el.getAttribute("data-show-if"));
    });
    each(document.querySelectorAll("[data-toggle],[data-set]"), function (el) {
      el.setAttribute("aria-pressed", String(isPressed(el)));
    });
  }

  // ---------------------------------------------------------------- binding

  function bindAll(root, skipSync) {
    root = root || document;
    var nodes = [];
    if (root.nodeType === 1 && root.matches(BINDABLE)) nodes.push(root);
    if (root.querySelectorAll) Array.prototype.push.apply(nodes, root.querySelectorAll(BINDABLE));
    nodes.forEach(bindOne);
    if (!skipSync) sync();
  }

  function bindOne(el) {
    if (seen) {
      if (seen.has(el)) return;
      seen.add(el);
    } else if (el.__interactBound) {
      return;
    } else {
      el.__interactBound = true;
    }
    if (el.tagName === "FORM") {
      bindForm(el);
      return;
    }
    if (el.hasAttribute("data-bind") && isField(el)) bindField(el);
    if (el.hasAttribute("data-action")) el.addEventListener("click", onAction);
    if (el.hasAttribute("data-set") || el.hasAttribute("data-toggle")) el.addEventListener("click", onSet);
    if (locked && (isField(el) || el.tagName === "BUTTON")) el.disabled = true;
  }

  function bindField(el) {
    var key = el.getAttribute("data-bind");
    var type = fieldType(el);
    var eventName = el.tagName === "SELECT" || type === "checkbox" || type === "radio" ? "change" : "input";
    el.addEventListener(eventName, function () {
      if (type === "radio" && !el.checked) return;
      set(key, readField(el));
    });
    // Seed state with the page's own defaults (value="…", checked) unless state already has them.
    if (getPath(state, key) === undefined && !(type === "radio" && !el.checked)) {
      setPath(state, key, readField(el));
    }
  }

  function collectForm(form) {
    var named = {};
    new FormData(form).forEach(function (value, name) {
      if (Object.prototype.hasOwnProperty.call(named, name)) named[name] = [].concat(named[name], value);
      else named[name] = value;
    });
    Object.keys(named).forEach(function (name) { setPath(state, name, named[name]); });
  }

  function bindForm(form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      collectForm(form);
      var submitter = ev.submitter;
      submit((submitter && submitter.getAttribute("data-action")) || form.getAttribute("data-action") || "submit", null);
    });
  }

  function onAction(ev) {
    ev.preventDefault();
    var el = ev.currentTarget;
    if (el.form && el.form.hasAttribute("data-interact")) collectForm(el.form);
    var action = el.getAttribute("data-action") || "submit";
    var extra = el.hasAttribute("data-extra") ? parseValue(el.getAttribute("data-extra")) : null;
    var question = el.getAttribute("data-confirm");
    if (!question) {
      submit(action, extra);
      return;
    }
    confirmDialog(question, function (ok) {
      if (ok) submit(action, extra);
    });
  }

  function onSet(ev) {
    ev.preventDefault();
    var el = ev.currentTarget;
    if (el.hasAttribute("data-toggle")) {
      var key = el.getAttribute("data-toggle");
      set(key, !getPath(state, key));
    } else {
      set(el.getAttribute("data-set"), parseValue(el.getAttribute("data-value")));
    }
    haptic("selection");
  }

  function observe() {
    if (typeof MutationObserver !== "function") return;
    new MutationObserver(function (records) {
      var added = false;
      records.forEach(function (record) {
        each(record.addedNodes, function (node) {
          if (node.nodeType === 1) {
            bindAll(node, true);
            added = true;
          }
        });
      });
      if (added) sync();
    }).observe(document.body, { childList: true, subtree: true });
  }

  // ---------------------------------------------------------------- state

  function set(key, value) {
    setPath(state, key, value);
    changed();
  }

  function update(patch) {
    Object.keys(patch || {}).forEach(function (key) { setPath(state, key, patch[key]); });
    changed();
  }

  function changed() {
    sync();
    notify();
    scheduleSave();
  }

  function notify() {
    if (notifying) return; // a listener calling Interact.set() must not re-enter the listeners
    notifying = true;
    var snapshot = clone(state);
    try {
      listeners.forEach(function (fn) {
        try {
          fn(snapshot);
        } catch (e) {
          console.error(e);
        }
      });
    } finally {
      notifying = false;
    }
  }

  function scheduleSave() {
    if (locked) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, 250);
  }

  function flush() {
    clearTimeout(saveTimer);
    saveTimer = null;
    indicator(saveLocal() ? "saved" : "error");
  }

  function indicator(kind) {
    each(document.querySelectorAll("[data-interact-status]"), function (el) {
      el.setAttribute("data-state", kind);
      el.textContent = msg[kind] || "";
    });
  }

  // ---------------------------------------------------------------- submit

  function payloadFor(action, extra) {
    var body = { v: 1, p: cfg.pageId, a: action || "submit", s: state };
    if (extra != null) body.x = extra;
    return JSON.stringify(body);
  }

  function submit(action, extra) {
    if (locked) {
      toast(expired ? msg.expired : msg.alreadySent, "error");
      return false;
    }
    var payload = payloadFor(action, extra);
    var size = byteLength(payload);
    if (size > MAX_BYTES) {
      haptic("error");
      toast(String(msg.tooLarge || "").replace("{n}", size), "error");
      return false;
    }
    if (!inTelegram || typeof tg.sendData !== "function") {
      preview(payload); // outside Telegram: show what would be sent
      return false;
    }
    submitted = true;
    flush();
    haptic("success");
    try {
      tg.sendData(payload); // closes the Mini App; the bot receives a web_app_data message
    } catch (e) {
      submitted = false;
      flush();
      toast(msg.notKeyboard, "error");
      return false;
    }
    lock();
    toast(msg.submitted, "ok");
    emit("interact:submitted", JSON.parse(payload));
    return true;
  }

  function preview(payload) {
    var data = JSON.parse(payload);
    var overlay = document.createElement("div");
    overlay.className = "ix-preview";
    var box = document.createElement("div");
    box.className = "ix-card ix-stack-sm";
    var title = document.createElement("strong");
    title.textContent = msg.preview;
    var pre = document.createElement("pre");
    pre.textContent = JSON.stringify(data, null, 2);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "ix-btn ix-secondary";
    close.textContent = msg.close;
    close.addEventListener("click", function () { overlay.remove(); });
    box.appendChild(title);
    box.appendChild(pre);
    box.appendChild(close);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    emit("interact:preview", data);
  }

  // ---------------------------------------------------------------- UI chrome

  function lock() {
    locked = true;
    document.documentElement.classList.add("ix-locked");
    each(document.querySelectorAll("[data-action],[data-set],[data-toggle],[data-bind],form[data-interact] button"), function (el) {
      if (isField(el) || el.tagName === "BUTTON") el.disabled = true;
    });
    if (tg && inTelegram && tg.MainButton) tg.MainButton.hide();
    var bar = document.querySelector(".ix-mainbar");
    if (bar) bar.hidden = true;
  }

  function confirmDialog(text, callback) {
    if (tg && inTelegram && typeof tg.showConfirm === "function" && (!tg.isVersionAtLeast || tg.isVersionAtLeast("6.2"))) {
      try {
        tg.showConfirm(text, callback);
        return;
      } catch (e) { /* fall back */ }
    }
    callback(window.confirm(text));
  }

  function toast(text, kind) {
    if (!text || !document.body) return;
    var box = document.createElement("div");
    box.className = "ix-toast ix-toast-" + (kind || "info");
    box.setAttribute("role", kind === "error" ? "alert" : "status");
    box.textContent = text;
    document.body.appendChild(box);
    setTimeout(function () {
      box.classList.add("ix-toast-out");
      setTimeout(function () { box.remove(); }, 300);
    }, kind === "error" ? 6000 : 3500);
  }

  function haptic(kind) {
    if (!tg || !inTelegram || !tg.HapticFeedback) return;
    try {
      if (kind === "selection") tg.HapticFeedback.selectionChanged();
      else tg.HapticFeedback.notificationOccurred(kind);
    } catch (e) { /* unsupported client */ }
  }

  function setupMainButton() {
    var main = cfg.mainButton;
    if (!main || !main.text || locked) return;
    var run = function () { submit(main.action || "submit", null); };
    if (tg && inTelegram && tg.MainButton) {
      tg.MainButton.setText(main.text);
      tg.MainButton.onClick(run);
      tg.MainButton.show();
      return;
    }
    var bar = document.createElement("div");
    bar.className = "ix-mainbar";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "ix-btn ix-block";
    button.textContent = main.text;
    button.addEventListener("click", run);
    bar.appendChild(button);
    document.body.appendChild(bar);
    document.documentElement.classList.add("ix-has-mainbar");
  }

  function applyTheme() {
    if (tg && tg.colorScheme) document.documentElement.setAttribute("data-theme", tg.colorScheme);
  }

  // ---------------------------------------------------------------- boot

  function boot() {
    document.documentElement.classList.add(inTelegram ? "ix-tg" : "ix-web");
    if (tg) {
      try {
        tg.ready();
        tg.expand();
      } catch (e) { /* not inside Telegram */ }
      applyTheme();
      if (tg.onEvent) tg.onEvent("themeChanged", applyTheme);
    }
    if (expired || submitted) locked = true;
    bindAll(document);
    observe();
    setupMainButton();
    if (expired) {
      lock();
      toast(msg.expired, "error");
    } else if (submitted) {
      lock();
      toast(msg.alreadySent, "info");
    }
    notify();
    emit("interact:ready", { state: clone(state), submitted: submitted, expired: expired });
    window.addEventListener("pagehide", function () {
      if (saveTimer) flush();
    });
  }

  window.Interact = {
    get: function (path) { return clone(path ? getPath(state, path) : state); },
    set: set,
    update: update,
    onChange: function (fn) {
      listeners.push(fn);
      return function () {
        listeners = listeners.filter(function (other) { return other !== fn; });
      };
    },
    submit: submit,
    bind: function (root) { bindAll(root || document); },
    toast: toast,
    close: function () { if (tg && inTelegram) tg.close(); },
    reset: function () {
      try { window.localStorage.removeItem(storageKey); } catch (e) { /* ignore */ }
    },
    telegram: tg,
    inTelegram: inTelegram,
    config: cfg
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
