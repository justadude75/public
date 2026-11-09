// Small progressive enhancements: entity picker, duplicate hint, follow-up rows.
(function () {
  "use strict";

  const TYPE_LABEL = { person: "person", organization: "organization", place: "place", event: "event" };

  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    if (text != null) node.textContent = text;
    return node;
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  async function search(q, type) {
    const url = `/api/search?q=${encodeURIComponent(q)}${type ? `&type=${encodeURIComponent(type)}` : ""}`;
    const res = await fetch(url);
    return res.ok ? res.json() : [];
  }

  // ── entity picker ────────────────────────────────────────────────────────
  function initPicker(root) {
    const name = root.dataset.name;
    const multi = root.dataset.multi === "1";
    const input = root.querySelector(".ac-input");
    const list = root.querySelector(".ac-list");
    const chips = root.querySelector(".ac-chips");
    let items = [];
    let active = -1;

    function createTypes() {
      return (root.dataset.create || "person").split(",").filter(Boolean);
    }

    function addChip(value, label) {
      if (!multi) chips.innerHTML = "";
      if ([...chips.querySelectorAll("input")].some((i) => i.value === value)) return;
      const chip = el("span", { class: "chip" }, label);
      chip.appendChild(el("input", { type: "hidden", name, value }));
      const x = el("button", { type: "button", class: "x", "aria-label": "Remove" }, "×");
      chip.appendChild(x);
      chips.appendChild(chip);
      input.value = "";
      if (!multi) input.hidden = true;
      close();
    }

    function close() { list.hidden = true; list.innerHTML = ""; items = []; active = -1; }

    function highlight(i) {
      [...list.children].forEach((li, j) => li.classList.toggle("active", j === i));
      active = i;
    }

    function render(results, q) {
      list.innerHTML = "";
      items = [];
      results.forEach((r) => {
        const li = el("li", { role: "option" });
        li.appendChild(el("span", { class: "ac-name" }, r.name));
        if (r.context) li.appendChild(el("span", { class: "ac-ctx" }, r.context));
        li.appendChild(el("span", { class: "badge muted" }, r.engagement || TYPE_LABEL[r.type] || r.type));
        items.push({ value: r.id, label: r.name });
        list.appendChild(li);
      });
      const exact = results.some((r) => r.name.toLowerCase() === q.toLowerCase());
      if (q && !exact) {
        createTypes().forEach((t) => {
          const li = el("li", { role: "option", class: "ac-new" }, `Create “${q}” as new ${t}`);
          items.push({ value: `new:${t}:${q}`, label: `${q} (new)` });
          list.appendChild(li);
        });
      }
      [...list.children].forEach((li, i) => {
        li.addEventListener("mousedown", (e) => { e.preventDefault(); addChip(items[i].value, items[i].label); });
      });
      list.hidden = items.length === 0;
      highlight(items.length ? 0 : -1);
    }

    const run = debounce(async () => {
      const q = input.value.trim();
      if (!q) { close(); return; }
      render(await search(q, root.dataset.type), q);
    }, 120);

    input.addEventListener("input", run);
    input.addEventListener("blur", () => setTimeout(close, 100));
    input.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); highlight(Math.min(active + 1, items.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (e.key === "Enter") { if (active >= 0) { e.preventDefault(); addChip(items[active].value, items[active].label); } }
      else if (e.key === "Escape") { close(); }
    });
    chips.addEventListener("click", (e) => {
      if (e.target.classList.contains("x")) {
        e.target.parentElement.remove();
        input.hidden = false;
        input.focus();
      }
    });
  }

  // Relationship form: restrict the picker to the types the relationship allows.
  function initRelationForms() {
    document.querySelectorAll("[data-relation-form]").forEach((form) => {
      const select = form.querySelector("[data-relation-select]");
      const picker = form.querySelector(".ac");
      if (!select || !picker) return;
      const apply = () => {
        const opt = select.selectedOptions[0];
        let types = [];
        try { types = JSON.parse(opt.dataset.rangeTypes || "[]") || []; } catch (_) { types = []; }
        types = types.filter((t) => t !== "*");
        picker.dataset.type = types.length === 1 ? types[0] : "";
        picker.dataset.create = (types.length ? types : ["person", "organization", "place"]).join(",");
      };
      select.addEventListener("change", apply);
      apply();
    });
  }

  // ── duplicate hint while typing a new name ───────────────────────────────
  function initDupCheck() {
    const input = document.querySelector("[data-dupcheck]");
    const hint = document.getElementById("dup-hint");
    if (!input || !hint) return;
    const run = debounce(async () => {
      const q = input.value.trim();
      if (q.length < 2) { hint.hidden = true; return; }
      const results = await search(q, input.dataset.dupcheck);
      hint.innerHTML = "";
      if (!results.length) { hint.hidden = true; return; }
      hint.appendChild(el("span", {}, "Already in your network: "));
      results.slice(0, 5).forEach((r, i) => {
        const a = el("a", { href: `/e/${r.id}` }, r.name);
        hint.appendChild(a);
        if (r.context) hint.appendChild(el("span", { class: "muted" }, ` (${r.context})`));
        if (i < Math.min(results.length, 5) - 1) hint.appendChild(document.createTextNode(", "));
      });
      hint.hidden = false;
    }, 200);
    input.addEventListener("input", run);
  }

  // ── small form helpers ───────────────────────────────────────────────────
  function initFollowUps() {
    document.querySelectorAll("[data-fu-add]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const rows = document.querySelector("[data-fu-rows]");
        const row = rows.querySelector(".fu-row").cloneNode(true);
        row.querySelectorAll("input").forEach((i) => { i.value = ""; });
        rows.appendChild(row);
        row.querySelector("input").focus();
      });
    });
  }

  function initKindChannel() {
    const kind = document.querySelector("[data-kind]");
    const channel = document.querySelector("[data-channel]");
    if (!kind || !channel) return;
    const map = { call: "call", video: "video", message: "message", email: "email" };
    kind.addEventListener("change", () => { channel.value = map[kind.value] || "in_person"; });
  }

  function initContactToggle() {
    document.querySelectorAll("[data-toggle]").forEach((select) => {
      const target = document.getElementById(select.dataset.toggle);
      const apply = () => { if (target) target.hidden = select.value !== "contact"; };
      select.addEventListener("change", apply);
      apply();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".ac").forEach(initPicker);
    initRelationForms();
    initDupCheck();
    initFollowUps();
    initKindChannel();
    initContactToggle();
  });
})();
