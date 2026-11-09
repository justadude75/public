(function () {
  "use strict";
  const box = document.getElementById("cy");
  if (!box || typeof cytoscape === "undefined") return;
  const css = getComputedStyle(document.documentElement);
  const v = (n) => css.getPropertyValue(n).trim();
  const color = { person: v("--accent"), organization: v("--theirs"), place: v("--warn") };
  let cy;

  function params() {
    const types = ["person", "organization"].concat(document.getElementById("g-places").checked ? ["place"] : []);
    return new URLSearchParams({
      center: box.dataset.center || "",
      depth: document.getElementById("g-depth").value,
      inferred: document.getElementById("g-inferred").checked ? 1 : 0,
      together: document.getElementById("g-together").checked ? 1 : 0,
      me: document.getElementById("g-me").checked ? 1 : 0,
      types: types.join(","),
    });
  }

  async function load() {
    const res = await fetch(`/api/graph?${params()}`);
    const data = await res.json();
    const path = new Set((box.dataset.path || "").split(",").filter(Boolean));
    const elements = data.nodes.map((n) => ({ data: { ...n, size: 18 + 40 * (n.strength || 0) }, classes: [n.type, n.due ? "due" : "", n.center ? "center" : "", path.has(n.id) ? "onpath" : ""].join(" ") }))
      .concat(data.edges.map((e) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.label, w: e.weight }, classes: [e.inferred ? "inferred" : "", e.kind].join(" ") })));
    if (cy) cy.destroy();
    cy = cytoscape({
      container: box,
      elements,
      wheelSensitivity: 0.25,
      style: [
        { selector: "node", style: { "background-color": (n) => color[n.data("type")] || "#888", label: "data(label)", "font-size": 10, color: v("--text"), "text-valign": "bottom", "text-margin-y": 3, width: "data(size)", height: "data(size)" } },
        { selector: "node.organization", style: { shape: "round-rectangle" } },
        { selector: "node.place", style: { shape: "diamond" } },
        { selector: "node.due", style: { "border-width": 3, "border-color": v("--late") } },
        { selector: "node.center, node.onpath", style: { "border-width": 4, "border-color": v("--text") } },
        { selector: "edge", style: { width: (e) => 1 + 3 * (e.data("w") || 0.2), "line-color": v("--line"), "curve-style": "bezier", opacity: 0.9 } },
        { selector: "edge.inferred", style: { "line-style": "dashed", "line-color": v("--warn") } },
        { selector: "edge.together", style: { "line-color": v("--muted"), "line-style": "dotted" } },
        { selector: "edge:selected, edge.hover", style: { label: "data(label)", "font-size": 9, color: v("--muted") } },
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 90, padding: 20 },
    });
    cy.on("tap", "node", (ev) => {
      const d = ev.target.data();
      const info = document.getElementById("g-info");
      info.innerHTML = "";
      const a = document.createElement("a");
      a.href = `/e/${d.id}`;
      a.textContent = d.label;
      info.append(a, ` · ${d.type}${d.tier ? " · " + d.tier : ""}${d.strength ? " · strength " + Math.round(d.strength * 100) + "%" : ""}`);
    });
    cy.on("dbltap", "node", (ev) => { box.dataset.center = ev.target.id(); load(); });
    cy.on("mouseover", "edge", (ev) => ev.target.addClass("hover"));
    cy.on("mouseout", "edge", (ev) => ev.target.removeClass("hover"));
  }

  document.querySelectorAll(".graph-controls input, .graph-controls select").forEach((el) => el.addEventListener("change", load));
  load();
})();
