(function () {
  "use strict";

  const DEFAULT_SYMBOL = "XAUUSD";
  const SYMBOLS = ["XAUUSD", "USDJPY", "EURUSD", "US30", "GBPUSD", "USTEC"];
  const REQUIRED_HOTSPOTS = [
    {
      id: "strait-hormuz-required",
      latitude: 26.5667,
      longitude: 56.45,
      severity: "critical",
      title: "Strait of Hormuz Chokepoint",
      label: "Hormuz Supply Compression",
      category: "geopolitical_shock",
      symbol: "XAUUSD",
      context: "Oil transit stress is compressing physical supply lanes and lifting safe-haven volatility correlations.",
      market_vector: "XAUUSD safe-haven volatility + energy inflation impulse",
      source: "macro_geopolitical_intelligence_proxy",
      timestamp: "14:02:11",
      default_open: true
    },
    {
      id: "us-east-nfp-required",
      latitude: 38.9072,
      longitude: -77.0369,
      severity: "market",
      title: "United States East Coast Macro Node",
      label: "NFP / FOMC Macro Matrix",
      category: "macro_event",
      symbol: "USDJPY",
      context: "NFP actual, consensus, and previous data are mapped into USDJPY, US30, and USTEC reaction windows.",
      market_vector: "USDJPY / US30 / USTEC affected instruments",
      source: "macro_calendar_proxy",
      timestamp: "08:30 NY",
      default_open: true
    }
  ];

  const DEFAULT_PAYLOAD = {
    symbol: DEFAULT_SYMBOL,
    latency_ms: 12,
    uptime_pct: 99.9,
    metrics: {
      geopolitical_vector: { label: "Middle_East_Tension", status: "Critical", score: 88 },
      supply_chain_skew: { label: "Energy_Flows", status: "Compressed", score: 58 },
      liquidity_depth: { label: "Global_Synchrony", status: "Optimal", score: 82 },
      system_status: { node: "NY4", status: "INIT_DIAGNOSTIC", score: 99 }
    },
    symbol_impact: [
      { symbol: "XAUUSD", impact_score: 0.82, severity: "high", descriptor: "High confluence" },
      { symbol: "USDJPY", impact_score: 0.74, severity: "high", descriptor: "Dominant flow" },
      { symbol: "EURUSD", impact_score: -0.41, severity: "medium", descriptor: "Inverse skew" },
      { symbol: "US30", impact_score: 0.68, severity: "high", descriptor: "Macro adoption" },
      { symbol: "GBPUSD", impact_score: 0.12, severity: "low", descriptor: "Neutral vol" },
      { symbol: "USTEC", impact_score: 0.63, severity: "high", descriptor: "Beta flow" }
    ],
    hotspots: REQUIRED_HOTSPOTS.slice(),
    calendar: [
      {
        event: "NFP",
        title: "US Non-Farm Payrolls",
        country: "US",
        metric: { actual: "+215K", consensus: "+180K", previous: "+165K" },
        latitude: 38.9072,
        longitude: -77.0369,
        importance: "high",
        symbols: ["USDJPY", "US30", "USTEC"]
      }
    ],
    feed: [
      {
        severity: "critical",
        tag: "Critical",
        timestamp: "14:02:11",
        title: "Iranian navy deploys fast-attack craft to Hormuz chokepoint.",
        latitude: 26.5667,
        longitude: 56.45,
        source: "stratfor_core",
        category: "geopolitical_shock"
      },
      {
        severity: "high",
        tag: "High Alert",
        timestamp: "13:58:44",
        title: "WTI crude spikes 2.4% on supply disruption fears.",
        latitude: 29.7604,
        longitude: -95.3698,
        source: "energy_proxy",
        category: "commodity_flow"
      },
      {
        severity: "market",
        tag: "Market Skew",
        timestamp: "13:55:02",
        title: "Safe haven flows detected: USDJPY short covering initiated.",
        latitude: 35.6762,
        longitude: 139.6503,
        source: "macro_flow_proxy",
        category: "market_skew"
      }
    ]
  };

  const SHIPPING_LANES = [
    [[1.29, 103.85], [5.4, 95.3], [6.9, 79.8], [12.6, 43.3], [26.5667, 56.45], [29.9, 48.0]],
    [[51.5, -0.1], [40.7, -74.0], [25.76, -80.19], [9.08, -79.68], [-33.9, 18.4]],
    [[35.68, 139.65], [22.31, 114.16], [1.29, 103.85], [-33.86, 151.21]],
    [[25.76, -80.19], [29.76, -95.36], [34.05, -118.24], [35.68, 139.65]],
    [[31.2, 121.47], [22.31, 114.16], [19.07, 72.87], [25.28, 55.29], [26.5667, 56.45]]
  ];

  const FINANCIAL_HUBS = [
    [40.7128, -74.006], [51.5074, -0.1278], [35.6762, 139.6503], [22.3193, 114.1694],
    [1.3521, 103.8198], [25.2048, 55.2708], [-26.2041, 28.0473], [48.8566, 2.3522],
    [52.52, 13.405], [41.9028, 12.4964], [28.6139, 77.209], [-33.8688, 151.2093]
  ];

  let config = {};
  let frame;
  let globeEl;
  let globe;
  let activePayload = DEFAULT_PAYLOAD;
  let activeMarker;
  let activeHotspot;
  let resizeObserver;
  let telemetrySeed = 0;
  let latestSentimentBySymbol = {};
  let latestSentimentState = null;
  let latestYieldSpreadsBySymbol = {};
  let latestLiquidityState = null;
  let latestYieldCurveState = null;
  let latestMacroRegimeState = null;
  let sentimentPollTimer;
  let macroFinancialPollTimer;
  let intelRequestSequence = 0;
  const macroServiceWarnings = new Map();

  const $ = (id) => document.getElementById(id);
  const safeNumber = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const activeSymbol = () => (document.querySelector(".symbol-tab.active")?.textContent || DEFAULT_SYMBOL).trim().toUpperCase();

  function apiUrl(path) {
    const base = (config.apiBaseUrl || "").replace(/\/$/, "");
    return `${base}${path}`;
  }

  async function decodeResponse(response) {
    const bridge = window.TBBFXMacroOverlayBridge;
    if (bridge && typeof bridge.decodeMacroResponse === "function") {
      return bridge.decodeMacroResponse(response);
    }
    return response.json();
  }

  // Keep MessagePack on the live bridge, while allowing the standalone static terminal to use JSON safely.
  function apiHeaders() {
    const bridge = window.TBBFXMacroOverlayBridge;
    return {
      Accept: bridge && typeof bridge.decodeMacroResponse === "function"
        ? "application/x-msgpack, application/json"
        : "application/json"
    };
  }

  function unwrapEnvelope(decoded) {
    return decoded && Array.isArray(decoded.results) ? decoded.results[0] || {} : decoded || {};
  }

  async function fetchMacroEndpoint(path) {
    const response = await fetch(apiUrl(path), {
      headers: apiHeaders(),
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`Macro endpoint failed: ${response.status}`);
    return unwrapEnvelope(await decodeResponse(response));
  }

  const MACRO_SERVICE_LABELS = {
    "FRED LIQUIDITY": "FRED LIQUIDITY // CONTEXT INACTIVE",
    "SOVEREIGN YIELDS": "SOVEREIGN YIELDS // CONTEXT INACTIVE",
    "CFTC COT": "CFTC POSITIONING // CONTEXT INACTIVE",
    "YIELD CURVE": "YIELD CURVE // CONTEXT INACTIVE",
    "MACRO REGIME": "MACRO REGIME // CONTEXT INACTIVE",
    "NEWS SENTIMENT": "NEWS SENTIMENT // CONTEXT INACTIVE"
  };

  function macroServiceTitle(service) {
    const key = String(service || "MACRO SERVICE").toUpperCase();
    return MACRO_SERVICE_LABELS[key] || `${key} // CONTEXT INACTIVE`;
  }

  function safeMacroTimestamp(value) {
    const raw = String(value || "").trim();
    const parsed = raw ? new Date(raw) : null;
    if (parsed && Number.isFinite(parsed.getTime())) {
      return parsed.toLocaleTimeString([], { hour12: false });
    }
    const clock = raw.match(/\b\d{1,2}:\d{2}(?::\d{2})?\b/);
    return clock ? clock[0] : new Date().toLocaleTimeString([], { hour12: false });
  }

  function sanitizedMacroSummary(raw, kind) {
    if (kind === "fallback") {
      return "Primary gateway is unavailable. A verified cached or redundant baseline is active while the terminal retries.";
    }

    const text = String(raw || "").trim();
    const isTechnicalFailure = /networkerror|failed to fetch|fetch failed|macro endpoint failed|httpconnectionpool|traceback|exception|stack trace|timed? ?out|timeout|429|503|econnrefused|connection refused/i.test(text);
    if (isTechnicalFailure) {
      return "Primary gateway connection timed out. Displaying the last-known cached baseline where available.";
    }

    const cleaned = text
      .replace(/https?:\/\/\S+/gi, "upstream gateway")
      .replace(/\s+/g, " ")
      .trim();
    return cleaned && cleaned.length <= 190
      ? cleaned
      : "Primary gateway is temporarily unavailable. The terminal will retry automatically.";
  }

  function macroServiceState(payload, error) {
    if (error) {
      return {
        kind: "offline",
        status: "SERVICE_TEMPORARILY_OFFLINE",
        detail: error instanceof Error ? error.message : String(error || "Service unavailable"),
        lastUpdated: new Date().toLocaleTimeString()
      };
    }

    const status = String(payload?.status_warning || payload?.data_status || "").toUpperCase();
    if (!status || status === "LIVE_PRIMARY" || status === "CACHE_HIT") return null;
    if (status !== "FALLBACK_REDUNDANCY_ACTIVE" && status !== "SERVICE_TEMPORARILY_OFFLINE") return null;
    const warnings = Array.isArray(payload?.warnings) ? payload.warnings : [];
    return {
      kind: status === "FALLBACK_REDUNDANCY_ACTIVE" ? "fallback" : "offline",
      status,
      detail: warnings[0] || (status === "FALLBACK_REDUNDANCY_ACTIVE"
        ? "Primary source unavailable; verified redundant data is active."
        : "No upstream or durable snapshot is currently available."),
      lastUpdated: payload?.last_updated || payload?.generated_at || new Date().toLocaleTimeString()
    };
  }

  function setMacroServiceStatus(service, payload, error = null) {
    const state = macroServiceState(payload, error);
    if (state) {
      state.detail = sanitizedMacroSummary(state.detail, state.kind);
      state.lastUpdated = safeMacroTimestamp(state.lastUpdated);
      state.title = macroServiceTitle(service);
      macroServiceWarnings.set(service, state);
    }
    else macroServiceWarnings.delete(service);
    refreshMacroWarningRail();
  }

  function setSettledMacroStatus(service, result) {
    if (result.status === "fulfilled") setMacroServiceStatus(service, result.value);
    else setMacroServiceStatus(service, null, result.reason);
  }

  function ensureMacroStatusStyles() {
    if (document.getElementById("tbbfx-macro-service-status-styles")) return;
    const style = document.createElement("style");
    style.id = "tbbfx-macro-service-status-styles";
    style.textContent = `
      #macro-intel-list .macro-service-status-rail { display:grid; gap:10px; flex:0 0 auto; margin-bottom:2px; }
      #macro-intel-list .macro-service-status.intel-card { min-width:0; overflow:hidden; padding:12px; border:1px solid rgba(245,175,66,.52); background:linear-gradient(135deg, rgba(245,175,66,.09), rgba(5,12,10,.9)); box-shadow:inset 0 0 20px rgba(245,175,66,.035), 0 8px 24px rgba(0,0,0,.22); color:#bba881; font:500 9px/1.5 var(--mono, monospace); letter-spacing:.035em; }
      #macro-intel-list .macro-service-status.offline-state { border-color:rgba(226,103,87,.5); background:linear-gradient(135deg, rgba(226,103,87,.085), rgba(8,10,9,.92)); }
      #macro-intel-list .intel-state-head { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; flex-wrap:wrap; }
      #macro-intel-list .status-badge { display:inline-flex; border:1px solid currentColor; padding:2px 5px; color:#f2ae42; font-size:8px; font-weight:900; letter-spacing:.1em; }
      #macro-intel-list .offline-state .status-badge { color:#e57868; }
      #macro-intel-list .indicator-title { margin:9px 0 5px; color:#dbc89b; font-size:9px; letter-spacing:.09em; overflow-wrap:anywhere; }
      #macro-intel-list .offline-state .indicator-title { color:#d3a29a; }
      #macro-intel-list .summary-text { margin:0; color:#9eaaa3; font-size:9px; overflow-wrap:anywhere; }
      #macro-intel-list .timestamp-footer { color:#65766d; font-size:8px; letter-spacing:.06em; white-space:nowrap; }
    `;
    document.head.appendChild(style);
  }

  function refreshMacroWarningRail() {
    const list = $("macro-intel-list");
    if (!list) return;
    ensureMacroStatusStyles();
    list.querySelector(".macro-service-status-rail")?.remove();
    if (!macroServiceWarnings.size) return;

    const rail = document.createElement("section");
    rail.className = "macro-service-status-rail";
    macroServiceWarnings.forEach((state, service) => {
      const card = document.createElement("article");
      const stateClass = state.kind === "fallback" ? "fallback-state" : "offline-state";
      const badgeLabel = state.kind === "fallback" ? "[REDUNDANT FEED]" : "[SERVICE OFFLINE]";
      card.className = `macro-service-status intel-card ${stateClass}`;
      card.innerHTML = `
        <div class="intel-state-head">
          <span class="status-badge">${escapeHtml(badgeLabel)}</span>
          <time class="timestamp-footer">LAST VERIFIED: ${escapeHtml(String(state.lastUpdated))}</time>
        </div>
        <h4 class="indicator-title">${escapeHtml(state.title || macroServiceTitle(service))}</h4>
        <p class="summary-text">${escapeHtml(state.detail)}</p>`;
      rail.appendChild(card);
    });
    list.prepend(rail);
  }

  async function fetchPayload(symbol) {
    const encoded = encodeURIComponent(symbol || DEFAULT_SYMBOL);
    const endpoints = [
      `/api/macro/geospatial-nodes?symbol=${encoded}&limit=150`,
      `/api/macro/geopolitical-intelligence/${encoded}?limit=150`
    ];

    for (const endpoint of endpoints) {
      try {
        return unwrapPayload(await fetchMacroEndpoint(endpoint));
      } catch (error) {
        console.warn("[TBBFX MacroMap] endpoint unavailable", endpoint, error);
      }
    }
    return { ...DEFAULT_PAYLOAD, symbol };
  }

  function unwrapPayload(decoded) {
    const packet = unwrapEnvelope(decoded);
    return {
      ...DEFAULT_PAYLOAD,
      ...(packet || {}),
      metrics: { ...DEFAULT_PAYLOAD.metrics, ...((packet && packet.metrics) || {}) },
      hotspots: mergeRequiredHotspots((packet && packet.hotspots) || []),
      feed: (packet && packet.feed && packet.feed.length ? packet.feed : DEFAULT_PAYLOAD.feed).slice(0, 150),
      symbol_impact: (packet && packet.symbol_impact && packet.symbol_impact.length ? packet.symbol_impact : DEFAULT_PAYLOAD.symbol_impact)
    };
  }

  function mergeRequiredHotspots(hotspots) {
    const byId = new Map();
    [...hotspots, ...REQUIRED_HOTSPOTS].forEach((item, index) => {
      const normalized = normalizeHotspot(item, index);
      byId.set(normalized.id, normalized);
    });
    return Array.from(byId.values());
  }

  function normalizedSymbols(value) {
    const candidates = Array.isArray(value)
      ? value
      : String(value || "").split(/[\s,|/]+/);
    return Array.from(new Set(candidates
      .map((item) => String(item || "").trim().toUpperCase())
      .filter((item) => SYMBOLS.includes(item))));
  }

  function isCriticalGeopolitical(item) {
    const category = String(item.category || "").toLowerCase();
    const text = `${item.title || ""} ${item.label || ""} ${item.context || ""}`.toLowerCase();
    return Boolean(item.criticalGeopolitical)
      || (category.includes("geopolitical")
        && (String(item.severity || "").toLowerCase() === "critical"
          || /hormuz|war|military|geopolitical|supply disruption|oil supply/.test(text)));
  }

  function isPositiveSurprise(item) {
    return Boolean(item.positiveSurprise)
      || String(item.surprise_direction || item.metric?.surprise_direction || "").toLowerCase() === "positive"
      || safeNumber(item.surprise_delta_value ?? item.metric?.surprise_delta_value, 0) > 0;
  }

  function nodeColor(item) {
    if (isCriticalGeopolitical(item)) return "#ff9f1a";
    if (isPositiveSurprise(item)) return "#56e39f";
    return "#22d3ff";
  }

  function rgbaFromHex(hex, alpha) {
    const value = String(hex || "#22d3ff").replace("#", "");
    const red = parseInt(value.slice(0, 2), 16) || 34;
    const green = parseInt(value.slice(2, 4), 16) || 211;
    const blue = parseInt(value.slice(4, 6), 16) || 255;
    return `rgba(${red},${green},${blue},${Math.max(0, Math.min(1, alpha))})`;
  }

  function hotspotScore(item) {
    const severity = String(item.severity || "").toLowerCase();
    const severityScore = severity === "critical" ? 100 : severity === "high" ? 75 : severity === "medium" ? 45 : 20;
    return severityScore + (isCriticalGeopolitical(item) ? 35 : 0) + (isPositiveSurprise(item) ? 14 : 0);
  }

  // Keep every source point in the globe data set, while clustering nearby labels so high-density feeds stay legible.
  function clusterHotspots(hotspots, maximum = 72) {
    const clusters = new Map();
    [...hotspots].sort((left, right) => hotspotScore(right) - hotspotScore(left)).forEach((hotspot) => {
      const key = `${String(hotspot.category || "macro").toLowerCase()}:${Math.round(hotspot.latitude / 4)}:${Math.round(hotspot.longitude / 4)}`;
      const existing = clusters.get(key);
      if (!existing) {
        clusters.set(key, { ...hotspot, clusterSize: 1, symbols: normalizedSymbols(hotspot.symbols || hotspot.symbol) });
        return;
      }

      const clusterSize = existing.clusterSize + 1;
      const symbols = Array.from(new Set([
        ...normalizedSymbols(existing.symbols || existing.symbol),
        ...normalizedSymbols(hotspot.symbols || hotspot.symbol)
      ]));
      if (hotspotScore(hotspot) > hotspotScore(existing)) {
        Object.assign(existing, hotspot, { clusterSize, symbols });
      } else {
        existing.clusterSize = clusterSize;
        existing.symbols = symbols;
      }
    });
    return Array.from(clusters.values())
      .sort((left, right) => hotspotScore(right) - hotspotScore(left))
      .slice(0, maximum);
  }

  function normalizeHotspot(item, index) {
    const latitude = safeNumber(item.latitude ?? item.lat, REQUIRED_HOTSPOTS[index % REQUIRED_HOTSPOTS.length].latitude);
    const longitude = safeNumber(item.longitude ?? item.lng ?? item.lon, REQUIRED_HOTSPOTS[index % REQUIRED_HOTSPOTS.length].longitude);
    const severity = String(item.severity || item.importance || "market").toLowerCase();
    const symbols = normalizedSymbols(item.symbols || item.symbol);
    const positiveSurprise = isPositiveSurprise(item);
    const criticalGeopolitical = isCriticalGeopolitical({ ...item, severity });
    return {
      ...item,
      id: item.id || `${item.category || "node"}-${index}-${latitude.toFixed(2)}-${longitude.toFixed(2)}`,
      latitude,
      longitude,
      severity,
      symbols,
      symbol: symbols[0] || item.symbol || "GLOBAL",
      positiveSurprise,
      criticalGeopolitical,
      surprise_delta: item.surprise_delta || item.metric?.surprise_delta || "n/a",
      surprise_delta_value: safeNumber(item.surprise_delta_value ?? item.metric?.surprise_delta_value, 0),
      surprise_direction: item.surprise_direction || item.metric?.surprise_direction || "neutral",
      color: nodeColor({ ...item, severity, positiveSurprise, criticalGeopolitical }),
      title: item.title || item.label || "Macro Intelligence Node",
      label: item.label || item.title || "Macro Node",
      category: item.category || (severity === "critical" ? "geopolitical_shock" : "macro_event"),
      context: item.context || item.summary || item.title || "Live macro intelligence context is active.",
      market_vector: item.market_vector || item.marketVector || "Multi-asset macro pressure vector",
      source: item.source || "macro_geospatial_nodes",
      timestamp: item.timestamp || item.time || new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    };
  }

  function buildTelemetryPoints(payload) {
    const points = [];
    telemetrySeed = (telemetrySeed + 1) % 100000;

    SHIPPING_LANES.forEach((lane, laneIndex) => {
      for (let i = 0; i < lane.length - 1; i += 1) {
        const [latA, lngA] = lane[i];
        const [latB, lngB] = lane[i + 1];
        for (let step = 0; step < 130; step += 1) {
          const t = step / 130;
          const wobble = Math.sin((step + telemetrySeed + laneIndex * 17) * 0.37) * 0.22;
          points.push({
            latitude: latA + (latB - latA) * t + wobble,
            longitude: lngA + (lngB - lngA) * t + Math.cos((step + laneIndex) * 0.29) * 0.22,
            radius: 0.055 + ((step + laneIndex) % 5) * 0.008,
            altitude: 0.012 + (laneIndex % 3) * 0.004,
            color: step % 11 === 0 ? "rgba(255,177,26,.88)" : "rgba(34,211,255,.64)"
          });
        }
      }
    });

    FINANCIAL_HUBS.forEach(([lat, lng], hubIndex) => {
      for (let i = 0; i < 36; i += 1) {
        const angle = (Math.PI * 2 * i) / 36;
        const radius = 1.8 + (i % 6) * 0.28;
        points.push({
          latitude: lat + Math.sin(angle) * radius,
          longitude: lng + Math.cos(angle) * radius,
          radius: 0.085,
          altitude: 0.02 + (hubIndex % 2) * 0.006,
          color: "rgba(0,255,136,.72)"
        });
      }
    });

    (payload.hotspots || []).forEach((spot) => {
      points.push({
        latitude: spot.latitude,
        longitude: spot.longitude,
        radius: 0.14,
        altitude: 0.026,
        color: spot.color || "#22d3ff"
      });
    });
    return points;
  }

  async function initGlobe() {
    frame = $("macro-map-frame");
    globeEl = $("macro-globe");
    if (!frame || !globeEl || typeof window.Globe !== "function") {
      renderFlatFallback(activePayload);
      return false;
    }

    frame.classList.add("is-globe");
    globe = window.Globe()(globeEl)
      .backgroundColor("#000000")
      .showAtmosphere(false)
      .width(frame.clientWidth)
      .height(frame.clientHeight)
      .polygonAltitude(0.006)
      .polygonCapColor(() => "rgba(18,20,20,.96)")
      .polygonSideColor(() => "rgba(0,255,136,.08)")
      .polygonStrokeColor(() => "rgba(0,255,136,.95)")
      .pointsMerge(false)
      .pointLat((d) => d.latitude)
      .pointLng((d) => d.longitude)
      .pointAltitude((d) => d.altitude)
      .pointRadius((d) => d.radius)
      .pointColor((d) => d.color)
      .ringsData([])
      .ringLat((d) => d.latitude)
      .ringLng((d) => d.longitude)
      .ringMaxRadius((d) => (d.criticalGeopolitical ? 5.8 : d.positiveSurprise ? 5.1 : 4.4))
      .ringPropagationSpeed((d) => (d.criticalGeopolitical ? 1.5 : d.positiveSurprise ? 1.25 : 1.05))
      .ringRepeatPeriod((d) => (d.criticalGeopolitical ? 1200 : d.positiveSurprise ? 1450 : 1700))
      .ringColor((d) => (t) => {
        const alpha = Math.max(0, 0.75 - t);
        return rgbaFromHex(d.color || nodeColor(d), alpha);
      });

    try {
      const material = globe.globeMaterial && globe.globeMaterial();
      if (material && window.THREE) {
        material.color = new window.THREE.Color("#000000");
        material.emissive = new window.THREE.Color("#020705");
        material.emissiveIntensity = 0.45;
        material.shininess = 0;
      }
    } catch (error) {
      console.warn("[TBBFX MacroMap] material tuning skipped", error);
    }

    try {
      const controls = globe.controls && globe.controls();
      if (controls) {
        // Desk navigation should track the pointer immediately, not ease behind it.
        controls.enableDamping = false;
        controls.dampingFactor = 0.15;
        controls.rotateSpeed = 0.86;
        controls.zoomSpeed = 1.05;
        controls.minDistance = 125;
        controls.maxDistance = 620;
        controls.autoRotate = false;
        frame.addEventListener("pointermove", () => controls.update(), { passive: true });
        frame.addEventListener("dblclick", () => resetView(), { passive: true });
      }
    } catch (error) {
      console.warn("[TBBFX MacroMap] control tuning skipped", error);
    }

    await loadWorldPolygons();
    resizeObserver = new ResizeObserver(() => {
      if (!globe || !frame) return;
      globe.width(frame.clientWidth).height(frame.clientHeight);
    });
    resizeObserver.observe(frame);
    return true;
  }

  async function loadWorldPolygons() {
    if (!globe || !window.topojson) return;
    try {
      const response = await fetch("https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json", { cache: "force-cache" });
      const world = await response.json();
      const countries = window.topojson.feature(world, world.objects.countries).features;
      globe.polygonsData(countries);
    } catch (error) {
      console.warn("[TBBFX MacroMap] GeoJSON landmass load failed", error);
    }
  }

  function updateGlobe(payload) {
    if (!globe) {
      renderFlatFallback(payload);
      return;
    }

    const hotspots = mergeRequiredHotspots(payload.hotspots || []);
    const clusters = clusterHotspots(hotspots);
    const points = buildTelemetryPoints({ ...payload, hotspots });
    globe.pointsData(points).ringsData(clusters);

    if (typeof globe.htmlElementsData === "function") {
      globe
        .htmlElementsData(clusters)
        .htmlLat((d) => d.latitude)
        .htmlLng((d) => d.longitude)
        .htmlAltitude((d) => 0.055)
        .htmlElement((d) => createGlobeMarker(d));
    }
  }

  function createGlobeMarker(hotspot) {
    const button = document.createElement("button");
    button.type = "button";
    const tone = hotspot.criticalGeopolitical ? "is-critical" : hotspot.positiveSurprise ? "is-positive" : "is-market";
    button.className = `macro-globe-marker ${tone}`;
    button.title = hotspot.title;
    button.setAttribute("aria-label", `Focus intelligence: ${hotspot.title}`);
    button.innerHTML = `<span class="macro-globe-tag">${escapeHtml(minimalHotspotTag(hotspot))}</span>`;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      focusHotspot(hotspot, button);
    });
    return button;
  }

  function focusHotspot(hotspot, marker, animate = true) {
    if (activeMarker && activeMarker !== marker) activeMarker.classList.remove("is-active");
    activeMarker = marker;
    activeHotspot = hotspot;
    marker?.classList.add("is-active");

    if (globe && typeof globe.pointOfView === "function") {
      globe.pointOfView({
        lat: hotspot.latitude,
        lng: hotspot.longitude,
        altitude: hotspot.criticalGeopolitical ? 0.82 : 0.94
      }, animate ? 520 : 0);
    }

    renderSelectedIntelContext(hotspot);
  }

  function minimalHotspotTag(hotspot) {
    const prefix = hotspot.criticalGeopolitical ? "Shock" : hotspot.positiveSurprise ? "Beat" : "Event";
    const clusterSuffix = hotspot.clusterSize > 1 ? ` +${hotspot.clusterSize - 1}` : "";
    return `${prefix}: ${hotspot.label || hotspot.title || "Macro node"}${clusterSuffix}`;
  }

  function updatePanels(payload) {
    activePayload = payload;
    updateImpacts(
      payload.symbol_impact || DEFAULT_PAYLOAD.symbol_impact,
      latestSentimentBySymbol,
      latestYieldSpreadsBySymbol
    );
    updateMetrics(payload.metrics || DEFAULT_PAYLOAD.metrics);
    updateIntelFeed(payload.feed || DEFAULT_PAYLOAD.feed);
    updateBottomBar(payload);
  }

  function updateImpacts(items, sentimentBySymbol = {}, yieldSpreadsBySymbol = {}) {
    const list = $("macro-impact-list");
    if (!list) return;
    list.innerHTML = "";
    items.slice(0, 7).forEach((item) => {
      const symbol = String(item.symbol || "GLOBAL").toUpperCase();
      const sentiment = sentimentBySymbol[symbol];
      const score = sentiment
        ? safeNumber(sentiment.weighted_sentiment_score ?? sentiment.sentiment_polarity)
        : safeNumber(item.impact_score ?? item.score);
      const abs = Math.min(1, Math.abs(score));
      const severity = String(item.severity || (abs > 0.75 ? "high" : abs > 0.35 ? "medium" : "low")).toLowerCase();
      const flow = score > 0.3 ? "bullish" : score < -0.3 ? "bearish" : "neutral";
      const status = flow === "bullish" ? "BULLISH FLOW" : flow === "bearish" ? "BEARISH SKEW" : "NEUTRAL FLOW";
      const caption = sentiment
        ? `${status} | ${safeNumber(sentiment.story_count)} STORIES`
        : item.descriptor || "Macro vector";
      const card = document.createElement("div");
      card.className = `macro-impact-card ${severity === "critical" ? "crit" : severity === "medium" ? "med" : ""} ${flow}-flow`;
      card.innerHTML = `
        <div class="row"><strong>${escapeHtml(symbol)}</strong><span class="score">${score >= 0 ? "+" : ""}${score.toFixed(2)} ${status}</span></div>
        <div class="macro-bar"><span style="width:${Math.round(abs * 100)}%"></span></div>
        <div class="caption">${escapeHtml(caption)}</div>`;
      const yieldReadout = createYieldReadout(symbol, yieldSpreadsBySymbol[symbol]);
      if (yieldReadout) card.appendChild(yieldReadout);
      const curveReadout = createCurveReadout(symbol, latestYieldCurveState);
      if (curveReadout) card.appendChild(curveReadout);
      const refreshed = document.createElement("div");
      refreshed.className = "macro-refresh-stamp";
      refreshed.textContent = freshnessText(latestSentimentState, "NEWS_INTRADAY");
      card.appendChild(refreshed);
      list.appendChild(card);
    });
  }

  function createYieldReadout(symbol, spread) {
    if (!spread || !["EURUSD", "GBPUSD", "USDJPY"].includes(symbol)) return null;
    const readout = document.createElement("div");
    readout.className = "macro-yield-readout";
    if (String(spread.status || "").toLowerCase() !== "available") {
      readout.classList.add("unavailable");
      readout.textContent = "YLD SPRD: UNAVAILABLE";
      return readout;
    }

    const spreadPct = safeNumber(spread.yield_spread_pct);
    const deltaBps = safeNumber(spread.delta_bps_24h);
    const favorable = Boolean(spread.favors_base_asset);
    const deltaClass = Math.abs(deltaBps) < 0.01 ? "neutral" : favorable ? "favourable" : "adverse";
    readout.innerHTML = `YLD SPRD: <strong class="${deltaClass}">${spreadPct >= 0 ? "+" : ""}${spreadPct.toFixed(2)}% [${deltaBps >= 0 ? "+" : ""}${deltaBps.toFixed(1)} bps]</strong>`;
    return readout;
  }

  function createCurveReadout(symbol, curve) {
    if (symbol !== "XAUUSD") return null;
    const readout = document.createElement("div");
    readout.className = "macro-curve-readout";
    if (!curve || String(curve.status || "").toLowerCase() !== "available") {
      readout.classList.add("unavailable");
      readout.textContent = "US CURVE: UNAVAILABLE | ADVISORY ONLY";
      return readout;
    }

    const slopeBps = safeNumber(curve.calculated_slope_bps);
    if (curve.yield_curve_inverted) {
      readout.classList.add("inverted");
      readout.textContent = `US CURVE INVERTED ${slopeBps.toFixed(1)} BPS | SAFE-HAVEN SHIELD ADVISORY`;
    } else {
      readout.classList.add("normal");
      readout.textContent = `US CURVE ${slopeBps >= 0 ? "+" : ""}${slopeBps.toFixed(1)} BPS | NORMAL SLOPE`;
    }
    return readout;
  }

  function updateMetrics(metrics) {
    setMetric("macro-metric-geopolitical", metrics.geopolitical_vector, "geopolitical_vector");
    setMetric("macro-metric-supply", metrics.supply_chain_skew, "supply_chain_skew");
    const liquidityDirection = String(latestLiquidityState?.liquidity_momentum_direction || "").toLowerCase();
    const liquidityMetric = latestLiquidityState && String(latestLiquidityState.status || "").toLowerCase() === "available"
      ? {
        label: "GLOBAL_SYNCHRONY",
        status: latestLiquidityState.liquidity_momentum || "STABLE",
        score: liquidityDirection === "expanding" ? 92
          : liquidityDirection === "contracting" ? 28
            : 54
      }
      : metrics.liquidity_depth;
    setMetric("macro-metric-liquidity", liquidityMetric, "liquidity_depth");
    setMetric("macro-metric-status", metrics.system_status, "system_status");
    updateRegimeMetric(metrics.system_status);
    setFreshness("macro-liquidity-refreshed", latestLiquidityState, "FRED_WEEKLY_MIXED");
    setFreshness("macro-system-refreshed", latestMacroRegimeState, "MIXED_FREQUENCY");
    setFreshness("macro-filter-refreshed", latestMacroRegimeState, "MIXED_FREQUENCY");
  }

  function updateRegimeMetric(fallbackMetric) {
    const node = $("macro-metric-status");
    if (!node) return;
    const rawState = String(latestMacroRegimeState?.state || "").toUpperCase();
    const allowedStates = new Set(["RISK_ON", "RISK_OFF", "SYSTEMIC_STRESS"]);
    const state = allowedStates.has(rawState) ? rawState : "SYSTEM_MONITORING";
    const value = node.querySelector(".value");
    const meter = node.querySelector(".macro-meter span");
    const badge = $("macro-regime-badge");
    const score = state === "RISK_ON" ? 88 : state === "RISK_OFF" ? 54 : state === "SYSTEMIC_STRESS" ? 28 : safeNumber(fallbackMetric?.score, 70);

    node.classList.remove("regime-risk-on", "regime-risk-off", "regime-systemic-stress");
    if (state === "RISK_ON") node.classList.add("regime-risk-on");
    else if (state === "RISK_OFF") node.classList.add("regime-risk-off");
    else if (state === "SYSTEMIC_STRESS") node.classList.add("regime-systemic-stress");

    if (value && state !== "SYSTEM_MONITORING") value.innerHTML = `MACRO_REGIME: <strong>${state.replace(/_/g, " ")}</strong>`;
    if (meter) meter.style.width = `${score}%`;
    if (badge) {
      badge.className = `macro-regime-badge ${state.toLowerCase().replace(/_/g, "-")}`;
      badge.textContent = state === "SYSTEM_MONITORING" ? "READ-ONLY MONITOR" : "READ-ONLY HANDSHAKE";
    }
  }

  function setMetric(id, metric, fallbackLabel) {
    const node = $(id);
    if (!node || !metric) return;
    const value = node.querySelector(".value");
    const meter = node.querySelector(".macro-meter span");
    if (value) {
      if (metric.node) {
        value.innerHTML = `<span class="dot"></span> NODE: ${escapeHtml(metric.node)} <strong>${escapeHtml(metric.status || "ACTIVE")}</strong>`;
      } else {
        value.innerHTML = `${escapeHtml(metric.label || fallbackLabel)}: <strong>${escapeHtml(metric.status || "ACTIVE")}</strong>`;
      }
    }
    if (meter) meter.style.width = `${Math.max(8, Math.min(100, safeNumber(metric.score, 70)))}%`;
    if (id === "macro-metric-liquidity") {
      node.classList.remove("is-expanding", "is-contracting", "is-unavailable");
      if (latestLiquidityState) {
        const direction = String(latestLiquidityState.liquidity_momentum_direction || "").toLowerCase();
        if (String(latestLiquidityState.status || "").toLowerCase() !== "available") node.classList.add("is-unavailable");
        else if (direction === "expanding") node.classList.add("is-expanding");
        else if (direction === "contracting") node.classList.add("is-contracting");
      }
    }
  }

  function updateIntelFeed(feed) {
    const list = $("macro-intel-list");
    if (!list) return;
    list.innerHTML = "";
    if (activeHotspot) list.appendChild(createIntelContextCard(activeHotspot));
    feed.slice(0, 7).forEach((item) => {
      const severity = String(item.severity || "neutral").toLowerCase();
      const card = document.createElement("div");
      card.className = `macro-intel-card ${severity}`;
      card.innerHTML = `
        <div class="top"><span class="tag">${escapeHtml(item.tag || severity || "Intel")}</span><span class="time">${escapeHtml(item.timestamp || "")}</span></div>
        <div class="headline">${escapeHtml(item.title || item.headline || "Macro intelligence update")}</div>`;
      list.appendChild(card);
    });
    refreshMacroWarningRail();
  }

  function renderSelectedIntelContext(hotspot) {
    const list = $("macro-intel-list");
    if (!list) return;
    // A focused node owns the rail: replace stale intelligence instead of stacking competing stories.
    const card = createIntelContextCard(hotspot);
    const requestId = ++intelRequestSequence;
    list.replaceChildren(card);
    refreshMacroWarningRail();
    list.scrollTo({ top: 0, behavior: "smooth" });
    void appendCotPositioning(hotspot, card, requestId);
    void appendMacroFinancialFramework(hotspot, card, requestId);
  }

  async function appendCotPositioning(hotspot, card, requestId) {
    const symbol = normalizedSymbols(hotspot.symbols || hotspot.symbol)[0] || activeSymbol();
    const pending = document.createElement("div");
    pending.className = "cot-positioning-gauge is-loading";
    pending.textContent = `COT POSITIONING | ${symbol} | LOADING...`;
    card.appendChild(pending);

    try {
      const payload = await fetchMacroEndpoint(`/api/macro/cot-positioning?symbol=${encodeURIComponent(symbol)}`);
      if (requestId !== intelRequestSequence) return;
      setMacroServiceStatus("CFTC COT", payload);
      const position = (payload.positions || []).find((item) => String(item.symbol || "").toUpperCase() === symbol) || {};
      pending.replaceWith(createCotGauge(symbol, position, payload.warnings || []));
    } catch (error) {
      if (requestId !== intelRequestSequence) return;
      pending.classList.remove("is-loading");
      pending.classList.add("is-unavailable");
      pending.textContent = `COT POSITIONING | ${symbol} | UNAVAILABLE`;
      setMacroServiceStatus("CFTC COT", null, error);
      console.warn("[TBBFX MacroMap] COT positioning unavailable", error);
    }
  }

  function createCotGauge(symbol, position, warnings) {
    const gauge = document.createElement("section");
    gauge.className = "cot-positioning-gauge";
    if (String(position.status || "").toLowerCase() !== "available") {
      gauge.classList.add("is-unavailable");
      const summary = sanitizedMacroSummary(warnings[0] || "CFTC source unavailable; no positioning estimate displayed.", "offline");
      gauge.innerHTML = `<strong>COT POSITIONING | ${escapeHtml(symbol)}</strong><span>${escapeHtml(summary.toUpperCase())}</span>`;
      return gauge;
    }

    const ratio = safeNumber(position.leveraged_to_commercial_ratio);
    const skew = Math.max(0, Math.min(100, safeNumber(position.percentile_skew_52w, 50)));
    const bias = ratio >= 0 ? "NET LEVERAGED LONG" : "NET LEVERAGED SHORT";
    const netContracts = safeNumber(position.net_speculative_contracts);
    gauge.classList.add(ratio >= 0 ? "is-long" : "is-short");
    gauge.innerHTML = `
      <div class="cot-head"><strong>COT POSITIONING | ${escapeHtml(symbol)}</strong><span>${escapeHtml(bias)}</span></div>
      <div class="cot-track"><span style="width:${skew.toFixed(1)}%"></span><i style="left:${skew.toFixed(1)}%"></i></div>
      <div class="cot-meta"><span>52W SKEW ${skew.toFixed(0)}%</span><span>NET ${netContracts >= 0 ? "+" : ""}${netContracts.toLocaleString()}</span></div>
      <div class="cot-note">NON-COMMERCIAL / COMMERCIAL RATIO ${ratio >= 0 ? "+" : ""}${ratio.toFixed(2)}</div>`;
    return gauge;
  }

  async function appendMacroFinancialFramework(hotspot, card, requestId) {
    const symbol = normalizedSymbols(hotspot.symbols || hotspot.symbol)[0] || activeSymbol();
    const pending = document.createElement("section");
    pending.className = "macro-financial-framework is-loading";
    pending.textContent = "MACRO FINANCIAL FRAMEWORK | LOADING...";
    card.appendChild(pending);

    try {
      const [liquidityResult, yieldsResult, curveResult, regimeResult] = await Promise.allSettled([
        fetchMacroEndpoint("/api/macro/liquidity-index"),
        fetchMacroEndpoint(`/api/macro/yield-spreads?symbol=${encodeURIComponent(symbol)}`),
        fetchMacroEndpoint("/api/macro/yield-curve"),
        fetchMacroEndpoint("/api/macro/regime-state")
      ]);
      if (requestId !== intelRequestSequence) return;
      setSettledMacroStatus("FRED LIQUIDITY", liquidityResult);
      setSettledMacroStatus("SOVEREIGN YIELDS", yieldsResult);
      setSettledMacroStatus("YIELD CURVE", curveResult);
      setSettledMacroStatus("MACRO REGIME", regimeResult);
      const liquidity = liquidityResult.status === "fulfilled" ? liquidityResult.value : latestLiquidityState;
      const spreads = yieldsResult.status === "fulfilled" ? yieldsResult.value : {};
      const curve = curveResult.status === "fulfilled" ? curveResult.value : latestYieldCurveState;
      const regime = regimeResult.status === "fulfilled" ? regimeResult.value : latestMacroRegimeState;
      pending.replaceWith(createMacroFinancialFramework(symbol, liquidity, spreads, curve, regime));
    } catch (error) {
      if (requestId !== intelRequestSequence) return;
      pending.classList.remove("is-loading");
      pending.classList.add("is-unavailable");
      pending.textContent = "MACRO FINANCIAL FRAMEWORK | UNAVAILABLE";
      console.warn("[TBBFX MacroMap] macro financial framework unavailable", error);
    }
  }

  function createMacroFinancialFramework(symbol, liquidity, spreads, curve, regime) {
    const section = document.createElement("section");
    section.className = "macro-financial-framework";
    const selectedSpread = (spreads.spreads || []).find((item) => String(item.symbol || "").toUpperCase() === symbol);
    const liquidityAvailable = String(liquidity?.status || "").toLowerCase() === "available";
    const spreadAvailable = String(selectedSpread?.status || "").toLowerCase() === "available";
    const curveAvailable = String(curve?.status || "").toLowerCase() === "available";
    const allowedRegimes = new Set(["RISK_ON", "RISK_OFF", "SYSTEMIC_STRESS"]);
    const regimeState = String(regime?.state || "").toUpperCase();
    const regimeAvailable = allowedRegimes.has(regimeState);
    if (!liquidityAvailable && !spreadAvailable && !curveAvailable && !regimeAvailable) section.classList.add("is-unavailable");

    const momentum = liquidityAvailable ? liquidity.liquidity_momentum || "STABLE" : "UNAVAILABLE";
    const liquidityDetail = liquidityAvailable
      ? `NET ${safeNumber(liquidity.net_liquidity_billions).toFixed(1)}B ZAR-NEUTRAL USD LIQUIDITY`
      : "FRED liquidity data unavailable";
    const spreadDetail = spreadAvailable
      ? `${safeNumber(selectedSpread.yield_spread_pct) >= 0 ? "+" : ""}${safeNumber(selectedSpread.yield_spread_pct).toFixed(2)}% | ${safeNumber(selectedSpread.delta_bps_24h) >= 0 ? "+" : ""}${safeNumber(selectedSpread.delta_bps_24h).toFixed(1)} BPS`
      : ["EURUSD", "GBPUSD", "USDJPY"].includes(symbol) ? "Yield spread unavailable" : "No direct sovereign pair mapping";
    const bias = spreadAvailable
      ? selectedSpread.favors_base_asset ? "BASE-ASSET SUPPORTIVE" : "BASE-ASSET HEADWIND"
      : "READ-ONLY CONTEXT";
    const curveState = curveAvailable
      ? curve.yield_curve_inverted ? "INVERTED / SAFE-HAVEN ADVISORY" : "NORMAL SLOPE"
      : "UNAVAILABLE";
    const curveDetail = curveAvailable
      ? `${safeNumber(curve.calculated_slope_bps) >= 0 ? "+" : ""}${safeNumber(curve.calculated_slope_bps).toFixed(1)} BPS | DGS10 - DGS2`
      : "FRED DGS2/DGS10/T10Y2Y awaiting refresh";
    const regimeLabel = regimeAvailable ? regimeState.replace(/_/g, " ") : "MONITORING";
    const regimeDetail = regimeAvailable
      ? `CONFIDENCE ${Math.round(safeNumber(regime.confidence, 0) * 100)}% | NO AUTOMATIC EXECUTION MUTATION`
      : "Read-only macro handshake awaiting mixed-frequency sources";
    const refreshDetail = freshnessText(regimeAvailable ? regime : curveAvailable ? curve : liquidity, "MIXED_FREQUENCY");

    section.innerHTML = `
      <div class="framework-head"><strong>MACRO FINANCIAL FRAMEWORK</strong><span>${escapeHtml(symbol)}</span></div>
      <div class="framework-row"><span>USD NET LIQUIDITY</span><strong>${escapeHtml(momentum)}</strong></div>
      <div class="framework-detail">${escapeHtml(liquidityDetail)}</div>
      <div class="framework-row"><span>SOVEREIGN YLD SPREAD</span><strong>${escapeHtml(bias)}</strong></div>
      <div class="framework-detail">${escapeHtml(spreadDetail)}</div>
      <div class="framework-row"><span>US YIELD CURVE</span><strong>${escapeHtml(curveState)}</strong></div>
      <div class="framework-detail">${escapeHtml(curveDetail)}</div>
      <div class="framework-row"><span>MACRO REGIME</span><strong>${escapeHtml(regimeLabel)}</strong></div>
      <div class="framework-detail">${escapeHtml(regimeDetail)}</div>
      <div class="framework-refresh">${escapeHtml(refreshDetail)}</div>`;
    return section;
  }

  function createIntelContextCard(hotspot) {
    const isCritical = hotspot.criticalGeopolitical || hotspot.category === "geopolitical_shock";
    const isPositive = hotspot.positiveSurprise && !isCritical;
    const card = document.createElement("article");
    card.className = `macro-intel-context ${isCritical ? "critical" : isPositive ? "positive" : "market"}`;
    const metric = hotspot.metric || {};
    const metricSummary = metric.actual || metric.consensus || metric.previous
      ? `Actual ${metric.actual || "n/a"} | Consensus ${metric.consensus || "n/a"} | Previous ${metric.previous || "n/a"}`
      : "Live intelligence context selected from the geographic event layer.";
    const symbols = normalizedSymbols(hotspot.symbols || hotspot.symbol).join(" / ") || "GLOBAL";
    const surprise = hotspot.surprise_delta && hotspot.surprise_delta !== "n/a"
      ? `${hotspot.surprise_delta} (${String(hotspot.surprise_direction || "neutral").toUpperCase()})`
      : "n/a";
    const tone = isCritical ? "Critical shock" : isPositive ? "Positive surprise" : "Macro event";
    card.innerHTML = `
      <div class="context-kicker">${tone}<span>${escapeHtml(hotspot.timestamp || "LIVE")}</span></div>
      <div class="context-title">${escapeHtml(hotspot.label || hotspot.title || "Market intelligence")}</div>
      <div class="context-field"><span>Focus tags</span><strong>${escapeHtml(symbols)}</strong></div>
      <div class="context-field"><span>Lineage / region</span><strong>${escapeHtml(`${hotspot.source || "macro_intelligence_router"} | ${hotspot.latitude.toFixed(3)}, ${hotspot.longitude.toFixed(3)}`)}</strong></div>
      <p class="context-summary">${escapeHtml(hotspot.context || hotspot.title || metricSummary)}</p>
      <div class="context-metrics">${escapeHtml(metricSummary)}</div>
      <div class="context-field"><span>Delta matrix</span><strong>${escapeHtml(surprise)}</strong></div>
      <div class="context-vector">${escapeHtml(hotspot.market_vector || `Affected: ${symbols}`)}</div>`;
    return card;
  }

  function updateBottomBar(payload) {
    const latency = $("macro-latency");
    const uptime = $("macro-uptime");
    if (latency) latency.textContent = `${safeNumber(payload.latency_ms, 12).toFixed(0)}ms`;
    if (uptime) uptime.textContent = `${safeNumber(payload.uptime_pct, 99.9).toFixed(1)}%`;
  }

  function renderFlatFallback(payload) {
    const layer = $("macro-hotspot-layer");
    if (!layer) return;
    layer.innerHTML = "";
    const hotspots = clusterHotspots(mergeRequiredHotspots(payload.hotspots || []), 36);
    hotspots.forEach((hotspot, index) => {
      const button = document.createElement("button");
      button.className = `macro-hotspot ${hotspot.criticalGeopolitical ? "critical" : hotspot.positiveSurprise ? "positive" : ""}`;
      button.style.left = `${28 + (index * 19) % 48}%`;
      button.style.top = `${42 + (index * 13) % 30}%`;
      button.title = hotspot.title;
      button.addEventListener("click", () => focusHotspot(hotspot, button));
      layer.appendChild(button);
    });
  }

  function resetView() {
    if (!globe || typeof globe.pointOfView !== "function") return;
    globe.pointOfView({ lat: 18, lng: 18, altitude: 1.65 }, 950);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function sourceTimestamp(payload) {
    return payload?.last_updated
      || payload?.as_of
      || payload?.generated_at
      || payload?.timestamp
      || "";
  }

  function formatRefreshTimestamp(value) {
    const raw = String(value || "").trim();
    if (!raw) return "WAITING";
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return `${raw} UTC`;
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw.slice(0, 24).toUpperCase();
    return parsed.toISOString().replace("T", " ").slice(0, 19) + "Z";
  }

  function freshnessText(payload, fallbackFrequency) {
    const frequency = String(payload?.source_frequency || fallbackFrequency || "SOURCE")
      .trim()
      .toUpperCase();
    return `REFRESHED: ${formatRefreshTimestamp(sourceTimestamp(payload))} [${frequency}]`;
  }

  function setFreshness(id, payload, fallbackFrequency) {
    const node = $(id);
    if (node) node.textContent = freshnessText(payload, fallbackFrequency);
  }

  async function refresh(symbol = activeSymbol()) {
    const payload = await fetchPayload(symbol);
    updatePanels(payload);
    updateGlobe(payload);
  }

  async function refreshSentiment() {
    try {
      const payload = await fetchMacroEndpoint("/api/macro/sentiment?limit=150");
      setMacroServiceStatus("NEWS SENTIMENT", payload);
      latestSentimentState = payload;
      const symbols = Array.isArray(payload.symbols) ? payload.symbols : [];
      latestSentimentBySymbol = symbols.reduce((index, item) => {
        const symbol = String(item.symbol || "").toUpperCase();
        if (symbol) index[symbol] = item;
        return index;
      }, {});
      updateImpacts(
        activePayload.symbol_impact || DEFAULT_PAYLOAD.symbol_impact,
        latestSentimentBySymbol,
        latestYieldSpreadsBySymbol
      );
    } catch (error) {
      // The map stays usable with its macro impact payload when an optional analytics source is offline.
      setMacroServiceStatus("NEWS SENTIMENT", null, error);
      console.warn("[TBBFX MacroMap] sentiment feed unavailable", error);
    }
  }

  async function refreshMacroFinancials() {
    const [liquidityResult, yieldsResult, curveResult, regimeResult] = await Promise.allSettled([
      fetchMacroEndpoint("/api/macro/liquidity-index"),
      fetchMacroEndpoint("/api/macro/yield-spreads"),
      fetchMacroEndpoint("/api/macro/yield-curve"),
      fetchMacroEndpoint("/api/macro/regime-state")
    ]);

    setSettledMacroStatus("FRED LIQUIDITY", liquidityResult);
    setSettledMacroStatus("SOVEREIGN YIELDS", yieldsResult);
    setSettledMacroStatus("YIELD CURVE", curveResult);
    setSettledMacroStatus("MACRO REGIME", regimeResult);

    if (liquidityResult.status === "fulfilled") {
      latestLiquidityState = liquidityResult.value;
    } else {
      console.warn("[TBBFX MacroMap] liquidity feed unavailable", liquidityResult.reason);
    }

    if (yieldsResult.status === "fulfilled") {
      const spreads = Array.isArray(yieldsResult.value.spreads) ? yieldsResult.value.spreads : [];
      latestYieldSpreadsBySymbol = spreads.reduce((index, item) => {
        const symbol = String(item.symbol || "").toUpperCase();
        if (symbol) index[symbol] = item;
        return index;
      }, {});
    } else {
      console.warn("[TBBFX MacroMap] yield-spread feed unavailable", yieldsResult.reason);
    }

    if (curveResult.status === "fulfilled") {
      latestYieldCurveState = curveResult.value;
    } else {
      console.warn("[TBBFX MacroMap] yield-curve feed unavailable", curveResult.reason);
    }

    if (regimeResult.status === "fulfilled") {
      latestMacroRegimeState = regimeResult.value;
    } else {
      console.warn("[TBBFX MacroMap] macro-regime feed unavailable", regimeResult.reason);
    }

    updateImpacts(
      activePayload.symbol_impact || DEFAULT_PAYLOAD.symbol_impact,
      latestSentimentBySymbol,
      latestYieldSpreadsBySymbol
    );
    updateMetrics(activePayload.metrics || DEFAULT_PAYLOAD.metrics);
  }

  async function init(options = {}) {
    config = { ...(window.TBBFX_PUBLIC_CONFIG || {}), ...options };
    await initGlobe();
    resetView();
    await refresh(activeSymbol());
    await refreshSentiment();
    void refreshMacroFinancials();
    if (sentimentPollTimer) window.clearInterval(sentimentPollTimer);
    if (macroFinancialPollTimer) window.clearInterval(macroFinancialPollTimer);
    sentimentPollTimer = window.setInterval(refreshSentiment, 60000);
    macroFinancialPollTimer = window.setInterval(refreshMacroFinancials, 300000);
  }

  window.TBBFXMacroMap = {
    init,
    refresh,
    resetView,
    focusHormuz: () => {
      const hotspot = mergeRequiredHotspots(activePayload.hotspots || []).find((item) => item.id === "strait-hormuz-required");
      if (hotspot) focusHotspot(hotspot, activeMarker || null);
    }
  };
})();
