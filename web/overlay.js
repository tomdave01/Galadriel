(() => {
  "use strict";

  const apiBase = String(window.OVERSEER_API_BASE_URL || "").replace(/\/+$/, "");
  const apiUrl = (path) => `${apiBase}${path}`;
  const active = new Map();
  const pending = new Map();
  let matchClock = 0;
  const stack = document.querySelector("#overlay-stack");
  const svg = document.querySelector("#marking-layer");
  const connection = document.querySelector("#connection-state");

  function tcToSeconds(timecode) {
    const match = /^(\d+):(\d{2}):(\d{2})\.(\d{3})$/.exec(String(timecode || ""));
    if (!match) return 0;
    return Number(match[1]) * 3600 + Number(match[2]) * 60 +
      Number(match[3]) + Number(match[4]) / 1000;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function renderActive() {
    const overlays = [...active.values()].sort((a, b) =>
      b.priority - a.priority || b.seq - a.seq
    ).slice(0, 3);
    stack.innerHTML = overlays.map((overlay) => `
      <article class="broadcast-card" data-overlay-id="${escapeHtml(overlay.overlay_id)}"
        style="order:${100 - Number(overlay.priority || 0)}">
        <div class="broadcast-meta"><span>${escapeHtml(overlay.type)}</span><span>${escapeHtml(overlay.start_tc)}</span></div>
        <h1>${escapeHtml(overlay.headline)}</h1>
        ${overlay.body ? `<p>${escapeHtml(overlay.body)}</p>` : ""}
        ${overlay.stat ? `<span class="broadcast-stat">${escapeHtml(overlay.stat.label)} · ${escapeHtml(overlay.stat.value)}</span>` : ""}
      </article>
    `).join("");
    renderMarkings(overlays);
  }

  function renderMarkings(overlays) {
    const items = overlays.flatMap((overlay) =>
      (overlay.markings || []).map((marking) => ({ overlay, marking }))
    );
    svg.innerHTML = items.map(({ overlay, marking }) => {
      const points = marking.points || [];
      if (!points.length) return "";
      const coordinates = points.map((point) => `${Number(point.x)},${Number(point.y)}`).join(" ");
      if (marking.kind === "spotlight") {
        const point = points[0];
        return `<circle cx="${Number(point.x)}" cy="${Number(point.y)}" r="2.1"
          fill="none" stroke="#b6f35b" stroke-width=".28" opacity=".92"/>`;
      }
      if (marking.kind === "zone") {
        return `<polygon points="${coordinates}" fill="#b6f35b" fill-opacity=".12"
          stroke="#b6f35b" stroke-width=".28"/>`;
      }
      if (marking.kind === "line") {
        return `<polyline points="${coordinates}" fill="none" stroke="#b6f35b"
          stroke-width=".32" stroke-dasharray="1.2 .8"/>`;
      }
      return `<polyline points="${coordinates}" fill="none" stroke="#b6f35b"
        stroke-width=".42" stroke-linecap="round" stroke-linejoin="round"/>`;
    }).join("");
  }

  function activate(overlay) {
    if (!overlay || typeof overlay.overlay_id !== "string" || active.has(overlay.overlay_id)) return;
    pending.delete(overlay.overlay_id);
    const expiresAt = performance.now() + Math.max(250, Number(overlay.duration_ms) || 8000);
    active.set(overlay.overlay_id, overlay);
    renderActive();
    window.setTimeout(() => {
      if (!active.has(overlay.overlay_id)) return;
      active.delete(overlay.overlay_id);
      const card = [...stack.children].find((item) =>
        item.dataset.overlayId === overlay.overlay_id
      );
      if (card) {
        card.classList.add("exiting");
        window.setTimeout(renderActive, 260);
      } else {
        renderActive();
      }
    }, Math.max(0, expiresAt - performance.now()));
  }

  function acceptOverlay(overlay) {
    if (!overlay || typeof overlay.overlay_id !== "string") return;
    pending.set(overlay.overlay_id, overlay);
    scheduleReadyOverlays();
  }

  function scheduleReadyOverlays() {
    for (const [id, overlay] of pending) {
      if (tcToSeconds(overlay.start_tc) <= matchClock + 0.05) activate(overlay);
    }
  }

  function acceptSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    const snapshotClock = Number(snapshot.match_clock);
    const updatedClock = Number.isFinite(snapshotClock)
      ? snapshotClock
      : Number(snapshot.minute || 0) * 60;
    matchClock = updatedClock < matchClock - 5 ? updatedClock : Math.max(matchClock, updatedClock);
    scheduleReadyOverlays();
  }

  async function connect() {
    const [snapshotResponse, recentResponse] = await Promise.all([
      fetch(apiUrl("/state")),
      fetch(apiUrl("/overlays/recent?limit=100"))
    ]);
    if (!snapshotResponse.ok || !recentResponse.ok) {
      throw new Error("Unable to load current match state and recent overlays");
    }
    acceptSnapshot(await snapshotResponse.json());
    (await recentResponse.json()).forEach(acceptOverlay);

    const stream = new EventSource(apiUrl("/stream"));
    stream.onopen = () => {
      connection.textContent = "LIVE";
      fetch(apiUrl("/overlays/recent?limit=100"))
        .then((response) => {
          if (!response.ok) throw new Error("Unable to refresh recent overlays");
          return response.json();
        })
        .then((overlays) => overlays.forEach(acceptOverlay))
        .catch((error) => console.error(error));
    };
    stream.onmessage = (event) => {
      try { acceptSnapshot(JSON.parse(event.data)); }
      catch (error) { console.error("Invalid state snapshot", error); }
    };
    stream.addEventListener("overlay", (event) => {
      try { acceptOverlay(JSON.parse(event.data)); }
      catch (error) { console.error("Invalid overlay message", error); }
    });
    stream.onerror = () => { connection.textContent = "RECONNECTING"; };
  }

  connect().catch((error) => {
    connection.textContent = "OFFLINE";
    console.error(error);
  });
})();
