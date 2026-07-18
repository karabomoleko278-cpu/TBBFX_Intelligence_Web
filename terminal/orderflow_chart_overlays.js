(function () {
  "use strict";

  var symbols = ["EURUSD", "GBPUSD", "XAUUSD", "US30", "USTEC", "USDJPY"];
  var profiles = {
    EURUSD: { price: 1.17394, step: 0.0001, decimals: 5, spread: 0.00012, pip: 0.0005 },
    GBPUSD: { price: 1.33550, step: 0.0001, decimals: 5, spread: 0.00014, pip: 0.0005 },
    XAUUSD: { price: 2375.50, step: 0.10, decimals: 2, spread: 0.25, pip: 0.50 },
    US30: { price: 49370.90, step: 1.0, decimals: 2, spread: 1.20, pip: 5.0 },
    USTEC: { price: 26631.01, step: 0.50, decimals: 2, spread: 0.80, pip: 2.5 },
    USDJPY: { price: 159.390, step: 0.01, decimals: 3, spread: 0.018, pip: 0.05 }
  };

  var tfSecondsMap = { M5: 300, M15: 900, H1: 3600, H4: 14400, D1: 86400, W1: 604800 };
  var tfBarsMap = { M5: 58, M15: 58, H1: 64, H4: 72, D1: 90, W1: 104 };
  var tfRenderMap = { M5: 18, M15: 18, H1: 22, H4: 24, D1: 26, W1: 26 };

  var state = {
    symbol: "GBPUSD",
    chart: null,
    series: null,
    candleSource: "loading",
    candleRefreshTimer: null,
    loadingCandles: false,
    microLine: null,
    gammaLine: null,
    connection: null,
    bars: [],
    levels: [],
    timeframe: "M5",
    userScaled: false,
    manualY: null,
    axisDrag: null,
    xAxisDrag: null,
    chartDrag: null,
    hasLiveAnchor: false,
    renderFrame: null,
    price: profiles.GBPUSD.price,
    lastCvd: null,
    lastFeature: null,
    sim: null,
    resizeTimer: null,
    timezoneOffset: 0
  };

  var el = {};
  function q(id) { return document.getElementById(id); }
  function p(sym) { return profiles[sym] || profiles.GBPUSD; }
  function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }
  function any(o, keys, fallback) {
    for (var i = 0; i < keys.length; i += 1) {
      if (o && o[keys[i]] !== undefined && o[keys[i]] !== null) return o[keys[i]];
    }
    return fallback;
  }
  function fmt(v, d) {
    return (Number.isFinite(v) ? v : 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function signed(v, d) { return (v >= 0 ? "+" : "-") + fmt(Math.abs(v || 0), d); }
  function compact(v) {
    var n = Math.abs(v || 0); var s = v >= 0 ? "+" : "-";
    if (n >= 1000000000) return s + fmt(n / 1000000000, 1) + "B";
    if (n >= 1000000) return s + fmt(n / 1000000, 1) + "M";
    if (n >= 1000) return s + fmt(n / 1000, 1) + "K";
    return s + fmt(n, 0);
  }
  function secs(t) {
    var n = Number(t || Date.now() / 1000);
    return n > 10000000000 ? Math.floor(n / 1000) : Math.floor(n);
  }

  function normalizeTf(tf) {
    var t = String(tf || "M5").toUpperCase().replace(/\s+/g, "");
    if (t === "5M") return "M5";
    if (t === "15M") return "M15";
    if (t === "1H" || t === "1HR") return "H1";
    if (t === "4H" || t === "4HR") return "H4";
    if (t === "1D" || t === "D") return "D1";
    if (t === "1W" || t === "W") return "W1";
    return tfSecondsMap[t] ? t : "M5";
  }

  function tfSeconds() { return tfSecondsMap[state.timeframe] || tfSecondsMap.M5; }
  function tfBarCount() { return tfBarsMap[state.timeframe] || tfBarsMap.M5; }
  function tfRenderCount() { return tfRenderMap[state.timeframe] || tfRenderMap.M5; }
  function runtimeConfig() { return window.TBBFX_PUBLIC_CONFIG || {}; }
  function trimSlash(v) { return String(v || "").replace(/\/$/, ""); }
  function normaliseHttpsUrl(value) {
    var raw = trimSlash(String(value || "").trim());
    if (!raw) return "";
    try {
      var url = new URL(raw);
      return url.protocol === "https:" ? url.origin : "";
    } catch (_) {
      return "";
    }
  }
  function storageGet(key) {
    try { return normaliseHttpsUrl(window.localStorage.getItem(key)); }
    catch (_) { return ""; }
  }
  function storageSet(key, value) {
    if (!value) return;
    try { window.localStorage.setItem(key, value); } catch (_) {}
  }
  function secureBridgeConfig() { return runtimeConfig().secureBridge || {}; }
  function secureBridgeUrl() {
    var bridge = secureBridgeConfig();
    var params = new URLSearchParams(window.location.search);
    var key = bridge.storageKey || "tbbfx.secureBridgeUrl";
    var url = normaliseHttpsUrl(params.get(bridge.queryParam || "bridge")) || storageGet(key);
    storageSet(key, url);
    return url;
  }
  function secureFeatureBridgeUrl() {
    var bridge = secureBridgeConfig();
    var params = new URLSearchParams(window.location.search);
    var key = bridge.featureStorageKey || "tbbfx.featureBridgeUrl";
    var url = normaliseHttpsUrl(params.get(bridge.featureQueryParam || "featureBridge")) || storageGet(key) || secureBridgeUrl();
    if (url && url !== secureBridgeUrl()) storageSet(key, url);
    return url;
  }
  function localBridgeConfig() { return runtimeConfig().localBridge || {}; }
  function explicitLocalBridgeRequested() {
    var bridge = localBridgeConfig();
    var param = bridge.queryParam || "local";
    var value = String(bridge.queryValue || "1");
    return bridge.enabledByQuery !== false && new URLSearchParams(window.location.search).get(param) === value;
  }
  function localBridgeActive() {
    var cfg = runtimeConfig();
    if (cfg.publicMode === false) return false;
    return Boolean(secureBridgeUrl() || explicitLocalBridgeRequested());
  }
  function secureBridgeActive() { return Boolean(secureBridgeUrl()); }

  function apiBase() {
    var cfg = runtimeConfig();
    var bridge = localBridgeConfig();
    if (secureBridgeActive()) return secureBridgeUrl();
    if (explicitLocalBridgeRequested() && bridge.apiBase) return trimSlash(bridge.apiBase);
    if (cfg.apiBase) return trimSlash(cfg.apiBase);
    return window.location.protocol.indexOf("http") === 0 ? window.location.origin : "http://127.0.0.1:5000";
  }

  function secureBridgeKey() {
    try {
      var searchParams = new URLSearchParams(window.location.search);
      var key = searchParams.get("key");
      if (key) {
        window.localStorage.setItem("tbbfx.secureBridgeKey", key);
        return key;
      }
      return window.localStorage.getItem("tbbfx.secureBridgeKey") || "";
    } catch (_) {
      return "";
    }
  }

  function apiUrl(path) {
    var base = apiBase();
    if (!base) return "";
    var url = base + path;
    var key = secureBridgeKey();
    if (key) {
      url += (url.indexOf("?") >= 0 ? "&" : "?") + "key=" + encodeURIComponent(key);
    }
    return url;
  }

  function unwrapTbbFxObject(payload) {
    if (payload && Array.isArray(payload.results)) {
      return payload.results.length === 1 ? payload.results[0] : payload.results;
    }
    return payload;
  }

  function featureFactoryUrl(path) {
    var cfg = runtimeConfig();
    var bridge = localBridgeConfig();
    if (secureBridgeActive()) return secureFeatureBridgeUrl() + path;
    if (explicitLocalBridgeRequested()) return trimSlash(bridge.featureFactoryBase || "http://127.0.0.1:8000") + path;
    if (!cfg.featureFactoryBase && cfg.publicMode !== false) return "";
    return trimSlash(cfg.featureFactoryBase || "http://127.0.0.1:8000") + path;
  }

  function signalRUrl(path) {
    var cfg = runtimeConfig();
    var bridge = localBridgeConfig();
    if (secureBridgeActive()) return secureBridgeUrl() + path;
    if (explicitLocalBridgeRequested() && bridge.signalRUrl) return trimSlash(bridge.signalRUrl);
    if (cfg.signalRUrl) return trimSlash(cfg.signalRUrl);
    if (!cfg.signalRBase && cfg.publicMode !== false) return "";
    return trimSlash(cfg.signalRBase || apiBase()) + path;
  }


  function sanePrice(levelPrice, sym) {
    var prof = p(sym || state.symbol);
    var current = state.price || prof.price;
    if (!levelPrice || !current) return false;
    var maxDistance = Math.max(prof.pip * 160, current * ((sym || state.symbol).indexOf("US") === 0 || (sym || state.symbol).indexOf("XAU") === 0 ? 0.15 : 0.08));
    return Math.abs(levelPrice - current) <= maxDistance;
  }

  function normRows(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.map(function (r) {
      var bid = Number(any(r, ["bid_volume", "bidVolume", "BidVolume"], 0)) || 0;
      var ask = Number(any(r, ["ask_volume", "askVolume", "AskVolume"], 0)) || 0;
      return {
        price: Number(any(r, ["price", "Price"], 0)) || 0,
        bidVolume: bid,
        askVolume: ask,
        delta: Number(any(r, ["delta", "Delta"], ask - bid)) || (ask - bid)
      };
    }).filter(function (r) { return r.price > 0; });
  }

  function normFeature(raw) {
    var sym = String(any(raw, ["symbol", "Symbol"], state.symbol)).toUpperCase();
    return {
      symbol: sym,
      cvd: Number(any(raw, ["cvd", "Cvd"], state.lastCvd || 0)) || 0,
      microprice: Number(any(raw, ["microprice", "Microprice", "microPrice", "MicroPrice"], 0)) || 0,
      price: Number(any(raw, ["price", "Price"], 0)) || 0,
      obi: Number(any(raw, ["obi", "Obi"], 0)) || 0,
      netGex: Number(any(raw, ["net_gex", "netGex", "NetGex"], 0)) || 0,
      gammaFlip: Number(any(raw, ["gamma_flip", "gammaFlip", "GammaFlip"], 0)) || 0,
      timestamp: secs(any(raw, ["timestamp", "Timestamp"], Date.now() / 1000)),
      rows: normRows(any(raw, ["footprint_rows", "footprintRows", "FootprintRows"], [])),
      pins: (any(raw, ["open_interest_pins", "openInterestPins", "OpenInterestPins"], []) || []).map(Number).filter(Boolean),
      depth: any(raw, ["depth", "Depth"], null)
    };
  }

  function normLevel(raw) {
    var type = String(any(raw, ["typeName", "TypeName", "type", "Type"], "AbsorptionNode"));
    if (/^\d+$/.test(type)) type = ["GammaFlip", "OpenInterestPin", "BuyerImbalance", "SellerImbalance", "AbsorptionNode"][Number(type)] || "AbsorptionNode";
    return {
      symbol: String(any(raw, ["symbol", "Symbol"], state.symbol)).toUpperCase(),
      price: Number(any(raw, ["price", "Price"], 0)) || 0,
      type: type,
      strength: Number(any(raw, ["strengthScore", "StrengthScore"], 70)) || 70,
      timestamp: new Date(any(raw, ["timestamp", "Timestamp"], new Date().toISOString())).getTime() || Date.now(),
      description: String(any(raw, ["description", "Description"], "Institutional confluence level")),
      source: String(any(raw, ["source", "Source"], "SIGNALR"))
    };
  }

  function boot() {
    cache();
    wireSymbols();
    initChart();
    loadCandles(state.symbol);
    updatePanels();
    render();
    connect();
    loadLatest(state.symbol);
    setInterval(ageLevels, 60000);
    window.addEventListener("resize", resizeSoon);
  }

  function cache() {
    ["gamma-regime", "gamma-net", "gamma-meter", "dex-value", "dex-bar", "vex-value", "vex-bar", "chex-value", "chex-bar", "momentum-ring", "momentum-score", "momentum-label", "instrument-symbol", "instrument-change", "ohlc", "vol-delta", "chart-shell", "tv-chart", "footprint-canvas", "y-axis-hotzone", "x-axis-hotzone", "delta-canvas", "microprice-badge", "chart-status-text", "cvd-value", "cvd-bias", "cvd-bar", "book-rows", "book-mid", "spread-value", "alerts-feed", "sync-badge"].forEach(function (id) { el[id] = q(id); });
  }

  function wireSymbols() {
    document.querySelectorAll(".asset-tab").forEach(function (b) {
      b.addEventListener("click", function () { route(b.getAttribute("data-symbol")); });
    });
    document.querySelectorAll(".time-button").forEach(function (b) {
      b.addEventListener("click", function () { setTimeframe(b.getAttribute("data-tf") || b.textContent); });
    });
    document.querySelectorAll("[data-preserve-query]").forEach(function (a) {
      var href = a.getAttribute("href") || "";
      var parts = href.split("#");
      var query = window.location.search || "";
      a.setAttribute("href", parts[0] + query + (parts[1] ? "#" + parts[1] : ""));
    });
    document.querySelectorAll(".mode-button").forEach(function (b) {
      b.addEventListener("click", function () {
        var label = (b.textContent || "").toLowerCase();
        var query = window.location.search || "";
        var terminalBase = window.location.protocol.indexOf("http") === 0
          ? window.location.origin + "/"
          : "./TBBFX Intelligence Terminal.html";
        if (label.indexOf("live") >= 0) window.location.href = terminalBase + query;
        if (label.indexOf("validation") >= 0) window.location.href = terminalBase + query + "#validation";
      });
    });
  }

  function setTimeframe(tf) {
    var next = normalizeTf(tf);
    if (next === state.timeframe) { fitChart(); return; }
    state.timeframe = next;
    state.userScaled = false;
    state.manualY = null;
    state.chartDrag = null;
    state.hasLiveAnchor = false;
    if (el["chart-shell"]) el["chart-shell"].classList.remove("is-free-panning");
    document.querySelectorAll(".time-button").forEach(function (b) { b.classList.toggle("active", normalizeTf(b.getAttribute("data-tf") || b.textContent) === next); });
    loadCandles(state.symbol);
    updatePanels();
    renderSoon();
    loadLatest(state.symbol);
  }

  function initChart() {
    if (!window.LightweightCharts || !el["tv-chart"]) {
      setBadge("degraded", "LIVE_SIGNAL: CANVAS MODE - CHART LIBRARY OFFLINE");
      return;
    }
    var normalPriceMode = LightweightCharts.PriceScaleMode ? LightweightCharts.PriceScaleMode.Normal : 0;
    state.chart = LightweightCharts.createChart(el["tv-chart"], {
      autoSize: true,
      layout: { background: { type: "solid", color: "#030604" }, textColor: "rgba(216,238,224,.72)", fontFamily: "JetBrains Mono, monospace" },
      grid: { vertLines: { color: "rgba(40,255,156,.055)" }, horzLines: { color: "rgba(40,255,156,.07)" } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
      handleScale: { axisPressedMouseMove: { time: true, price: true }, mouseWheel: true, pinch: true },
      rightPriceScale: { autoScale: false, mode: normalPriceMode, borderColor: "rgba(40,255,156,.18)", scaleMargins: { top: 0.12, bottom: 0.18 } },
      timeScale: { borderColor: "rgba(40,255,156,.14)", timeVisible: true, secondsVisible: false, rightOffset: 14, barSpacing: 20, minBarSpacing: 2, fixLeftEdge: false, fixRightEdge: false, lockVisibleTimeRangeOnResize: false }
    });
    state.chart.applyOptions({
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true,
      },
      handleScale: {
        axisPressedMouseMove: { time: true, price: true },
        mouseWheel: true,
        pinch: true,
      },
      rightPriceScale: { autoScale: false, mode: normalPriceMode }
    });
    state.series = state.chart.addCandlestickSeries({
      upColor: "rgba(40,255,156,.58)",
      downColor: "rgba(239,89,80,.54)",
      borderUpColor: "rgba(40,255,156,.95)",
      borderDownColor: "rgba(239,89,80,.86)",
      wickUpColor: "rgba(40,255,156,.72)",
      wickDownColor: "rgba(239,89,80,.70)",
      priceLineVisible: false,
      lastValueVisible: false,
      autoscaleInfoProvider: function () {
        var range = priceRange(800);
        return { priceRange: { minValue: range.min, maxValue: range.max } };
      }
    });
    state.chart.timeScale().subscribeVisibleLogicalRangeChange(function () { renderSoon(); });
    wireChartRescaleHooks();
  }

  function wireChartRescaleHooks() {
    var target = el["chart-shell"] || el["tv-chart"];
    if (!target) return;
    ["wheel", "pointermove", "pointerup", "mouseleave"].forEach(function (evt) {
      target.addEventListener(evt, renderSoon, { passive: true });
    });
    target.addEventListener("pointerdown", function () { state.userScaled = true; }, { passive: true });
    target.addEventListener("dblclick", function () { resetYAxis(); });

    var chartTarget = el["tv-chart"];
    if (chartTarget) {
      chartTarget.addEventListener("pointerdown", startChartPan, { passive: true });
      window.addEventListener("pointermove", moveChartPan, { passive: true });
      window.addEventListener("pointerup", stopChartPan, { passive: true });
      window.addEventListener("pointercancel", stopChartPan, { passive: true });
    }

    var axis = el["y-axis-hotzone"];
    if (axis) {
      axis.addEventListener("pointerdown", startYAxisDrag);
      axis.addEventListener("pointermove", moveYAxisDrag);
      axis.addEventListener("pointerup", stopYAxisDrag);
      axis.addEventListener("pointercancel", stopYAxisDrag);
      axis.addEventListener("mouseleave", stopYAxisDrag);
      axis.addEventListener("wheel", wheelYAxis, { passive: false });
      axis.addEventListener("dblclick", function (e) { e.preventDefault(); resetYAxis(); });
    }

    var xAxis = el["x-axis-hotzone"];
    if (xAxis) {
      xAxis.addEventListener("pointerdown", startXAxisDrag);
      xAxis.addEventListener("pointermove", moveXAxisDrag);
      xAxis.addEventListener("pointerup", stopXAxisDrag);
      xAxis.addEventListener("pointercancel", stopXAxisDrag);
      xAxis.addEventListener("mouseleave", stopXAxisDrag);
      xAxis.addEventListener("wheel", wheelXAxis, { passive: false });
      xAxis.addEventListener("dblclick", function (e) { e.preventDefault(); resetXAxis(); });
    }
  }

  function seed(sym) {
    // Legacy name kept for callers: never fabricate a full chart as if it were real.
    state.bars = [];
    state.price = p(sym).price;
    state.candleSource = "offline";
    if (state.series) state.series.setData([]);
    setBadge("degraded", "LIVE_SIGNAL: REAL CANDLE HISTORY OFFLINE - WAITING FOR MT5");
    renderSoon();
  }

  function normalizeCandle(raw) {
    var open = Number(any(raw, ["open", "Open", "o", "O"], 0)) || 0;
    var high = Number(any(raw, ["high", "High", "h", "H"], 0)) || 0;
    var low = Number(any(raw, ["low", "Low", "l", "L"], 0)) || 0;
    var close = Number(any(raw, ["close", "Close", "c", "C"], 0)) || 0;
    var time = secs(any(raw, ["time", "Time", "t", "T", "timestamp", "Timestamp"], 0));
    var volume = Number(any(raw, ["volume", "Volume", "v", "V"], 0)) || 0;
    if (!time || !open || !high || !low || !close) return null;
    return { time: time, open: open, high: high, low: low, close: close, price: close, volume: volume, delta: 0, cvd: 0, obi: 0, rows: [] };
  }

  function enrichCandle(bar, prev) {
    var dir = prev ? bar.close - prev.close : bar.close - bar.open;
    var spread = Math.max(Math.abs(bar.high - bar.low), p(state.symbol).step);
    var body = Math.abs(bar.close - bar.open);
    var signedVol = Math.round((bar.volume || 1) * (dir >= 0 ? 1 : -1) * clamp(body / spread + 0.35, 0.25, 1.35));
    bar.delta = bar.delta || signedVol;
    bar.cvd = (prev ? prev.cvd || 0 : 0) + bar.delta;
    bar.obi = clamp(bar.delta / Math.max(Math.abs(bar.delta) + 800, 1), -1, 1);
    bar.rows = bar.rows && bar.rows.length ? bar.rows : rowsFromCandle(bar, state.symbol);
    return bar;
  }

  function rowsFromCandle(bar, sym) {
    var prof = p(sym || state.symbol);
    var levels = [];
    var rowCount = 7;
    var min = Math.min(bar.low, bar.open, bar.close);
    var max = Math.max(bar.high, bar.open, bar.close);
    var span = Math.max(max - min, prof.step * rowCount);
    for (var i = 0; i < rowCount; i += 1) {
      var price = min + span * (i / Math.max(1, rowCount - 1));
      var nearClose = 1 - Math.min(1, Math.abs(price - bar.close) / span);
      var nearBody = price >= Math.min(bar.open, bar.close) && price <= Math.max(bar.open, bar.close) ? 1.35 : 0.72;
      var energy = Math.max(12, (bar.volume || 60) / rowCount * (0.6 + nearClose) * nearBody);
      var askSkew = bar.close >= bar.open ? 0.62 + nearClose * 0.12 : 0.36 - nearClose * 0.08;
      askSkew = clamp(askSkew + (bar.obi || 0) * 0.10, 0.16, 0.86);
      var ask = Math.max(2, Math.round(energy * askSkew));
      var bid = Math.max(2, Math.round(energy * (1 - askSkew)));
      levels.push({ price: price, bidVolume: bid, askVolume: ask, delta: ask - bid });
    }
    return levels;
  }

  function candleHistoryEndpoints(sym) {
    var tf = state.timeframe;
    var count = Math.max(tfBarCount() * 4, 220);
    return [
      apiUrl("/api/market/candles/" + encodeURIComponent(sym) + "/" + tf + "?count=" + count),
      featureFactoryUrl("/api/candles/" + encodeURIComponent(sym) + "/" + tf + "?count=" + count)
    ].filter(Boolean);
  }

  function fetchJsonFast(url, timeoutMs) {
    var controller = window.AbortController ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, timeoutMs || 3500) : null;
    return fetch(url, { cache: "no-store", signal: controller ? controller.signal : undefined })
      .then(function (r) {
        if (!r.ok) throw new Error("candle endpoint offline");
        return r.json();
      })
      .then(unwrapTbbFxObject)
      .finally(function () { if (timer) clearTimeout(timer); });
  }

  function loadCandles(sym) {
    sym = String(sym || state.symbol).toUpperCase();
    state.loadingCandles = true;
    state.bars = [];
    state.candleSource = "loading";
    if (state.series) state.series.setData([]);
    setBadge("degraded", "LIVE_SIGNAL: LOADING REAL " + sym + " " + state.timeframe + " CANDLES");
    renderSoon();
    var endpoints = candleHistoryEndpoints(sym);
    var chain = Promise.reject();
    endpoints.forEach(function (url) {
      chain = chain.catch(function () {
        return fetchJsonFast(url, url.indexOf(":8000") >= 0 ? 2800 : 6000);
      });
    });
    chain.then(function (payload) { applyCandleHistory(payload, sym); })
      .catch(function () {
        seed(sym);
        loadLatest(sym);
      })
      .finally(function () { state.loadingCandles = false; scheduleCandleRefresh(); });
  }

  function applyCandleHistory(payload, sym) {
    if (sym !== state.symbol) return;
    var candles = (payload && payload.candles) || [];
    var parsed = candles.map(normalizeCandle).filter(Boolean).sort(function (a, b) { return a.time - b.time; });
    
    // Timezone alignment offset: delta between latest historical candle time and current UTC clock
    if (parsed.length > 0) {
      var latestCandleTime = parsed[parsed.length - 1].time;
      var seconds = tfSeconds();
      var currentLocalTime = Math.floor(Date.now() / 1000);
      var currentRounded = Math.floor(currentLocalTime / seconds) * seconds;
      state.timezoneOffset = latestCandleTime - currentRounded;
    } else {
      state.timezoneOffset = 0;
    }

    var enriched = [];
    parsed.forEach(function (bar) { enriched.push(enrichCandle(bar, enriched[enriched.length - 1])); });
    state.bars = enriched;
    state.candleSource = payload && payload.source ? payload.source : "unknown";
    state.hasLiveAnchor = state.candleSource === "mt5";
    state.price = enriched.length ? enriched[enriched.length - 1].close : p(sym).price;
    p(sym).price = state.price;
    updateSeries();
    updatePanels();
    fitChart();
    var source = state.candleSource === "mt5" ? "MT5 REAL CANDLES" : "HISTORICAL CSV CANDLES - MT5 HISTORY OFFLINE";
    setBadge(state.candleSource === "mt5" ? "online" : "degraded", "LIVE_SIGNAL: " + sym + " " + source);
    loadLatest(sym);
  }

  function scheduleCandleRefresh() {
    if (state.candleRefreshTimer) clearTimeout(state.candleRefreshTimer);
    var seconds = tfSeconds();
    var now = Math.floor(Date.now() / 1000);
    var nextClose = (Math.floor(now / seconds) + 1) * seconds + 2;
    state.candleRefreshTimer = setTimeout(function () {
      if (state.symbol) loadCandles(state.symbol);
    }, Math.max(5000, (nextClose - now) * 1000));
  }

  function synthRows(price, delta, obi, sym) {
    var prof = p(sym || state.symbol); var rows = [];
    for (var i = 0; i < 6; i += 1) {
      var off = i - 3; var rowPrice = price + off * prof.step * (sym && sym.indexOf("US") === 0 ? 2 : 1);
      var energy = Math.abs(delta) * (0.45 + (6 - Math.abs(off)) / 6) + 80 + i * 10;
      var askSkew = delta >= 0 ? 0.66 + obi * 0.12 : 0.30 + obi * 0.07;
      askSkew = clamp(askSkew, 0.14, 0.88);
      var ask = Math.max(8, Math.round(energy * askSkew));
      var bid = Math.max(8, Math.round(energy * (1 - askSkew)));
      rows.push({ price: rowPrice, bidVolume: bid, askVolume: ask, delta: ask - bid });
    }
    return rows;
  }

  function updateSeries() {
    if (!state.series) return;
    state.series.setData(state.bars.map(function (b) { return { time: b.time, open: b.open || b.price, high: b.high || b.price, low: b.low || b.price, close: b.close || b.price }; }));
    if (!state.userScaled) state.chart.timeScale().scrollToRealTime();
    renderSoon();
  }

  function fitChart() {
    if (!state.chart) return;
    if (state.userScaled) {
      renderSoon();
      return;
    }
    state.manualY = null;
    state.chart.timeScale().fitContent();
    renderSoon();
  }

  function connect() {
    if (!window.signalR) { setBadge("offline", "LIVE_SIGNAL: SIGNALR CLIENT MISSING - REAL FEED OFFLINE"); return; }
    var hubUrl = signalRUrl("/hub/marketpulse");
    if (!hubUrl) {
      setBadge("degraded", localBridgeActive() ? (secureBridgeActive() ? "LIVE_SIGNAL: SECURE BRIDGE OFFLINE - CHECK TUNNEL" : "LIVE_SIGNAL: LOCAL BRIDGE OFFLINE - START SIGNALRFEATURESTORE") : "LIVE_SIGNAL: PUBLIC READ-ONLY - LIVE HUB NOT CONFIGURED");
      return;
    }

    state.connection = new signalR.HubConnectionBuilder().withUrl(hubUrl).withAutomaticReconnect().build();
    state.connection.on("PulseWelcome", function () { setBadge("online", "LIVE_SIGNAL: GAMMA & ORDER FLOW SYNCED"); });
    state.connection.on("Subscribed", function (x) { if (x && x.symbol === state.symbol) setBadge("online", "LIVE_SIGNAL: " + state.symbol + " GAMMA & ORDER FLOW SYNCED"); });
    state.connection.on("ReceivePulse", applyFeature);
    state.connection.on("ReceiveAnyPulse", applyFeature);
    state.connection.on("ReceiveVolumeDelta", volumeDelta);
    state.connection.on("ReceiveObiUpdate", obiUpdate);
    state.connection.on("ReceiveInstitutionalConfluenceLevel", applyLevel);
    state.connection.on("ReceiveAnyInstitutionalConfluenceLevel", applyLevel);
    state.connection.on("ReceiveInstitutionalConfluenceLevels", applyLevels);
    state.connection.on("ReceiveAnyInstitutionalConfluenceLevels", applyLevels);
    state.connection.onreconnecting(function () { setBadge("degraded", "LIVE_SIGNAL: RECONNECTING ORDER FLOW STREAM"); });
    state.connection.onreconnected(function () { join(state.symbol); setBadge("online", "LIVE_SIGNAL: GAMMA & ORDER FLOW SYNCED"); });
    state.connection.onclose(function () { setBadge("offline", "LIVE_SIGNAL: OFFLINE - REAL ORDER FLOW STREAM DISCONNECTED"); });
    state.connection.start().then(function () { stopSim(); join(state.symbol); setBadge("online", secureBridgeActive() ? "LIVE_SIGNAL: SECURE BRIDGE GAMMA & ORDER FLOW SYNCED" : "LIVE_SIGNAL: GAMMA & ORDER FLOW SYNCED"); }).catch(function () { setBadge("offline", localBridgeActive() ? (secureBridgeActive() ? "LIVE_SIGNAL: SECURE BRIDGE OFFLINE - CHECK CLOUDFLARE TUNNEL" : "LIVE_SIGNAL: LOCAL BRIDGE OFFLINE - CHECK PORT 5000") : "LIVE_SIGNAL: FEATURE STORE OFFLINE - REAL ORDER FLOW UNAVAILABLE"); });
  }

  function isServerlessSignalR() {
    if (localBridgeActive()) return (localBridgeConfig().signalRMode || "aspnetcore-hub") === "azure-signalr-serverless";
    return runtimeConfig().signalRMode === "azure-signalr-serverless";
  }
  function join(sym) {
    if (isServerlessSignalR()) return;
    if (state.connection && state.connection.state === "Connected") state.connection.invoke("JoinSymbolGroup", sym).catch(function () {});
  }
  function leave(sym) {
    if (isServerlessSignalR()) return;
    if (state.connection && state.connection.state === "Connected") state.connection.invoke("LeaveSymbolGroup", sym).catch(function () {});
  }

  function setBadge(mode, text) {
    var badge = el["sync-badge"]; if (!badge) return;
    badge.classList.remove("degraded", "offline");
    if (mode === "degraded") badge.classList.add("degraded");
    if (mode === "offline") badge.classList.add("offline");
    var label = badge.querySelector("span:last-child"); if (label) label.textContent = text;
    if (el["chart-status-text"]) el["chart-status-text"].textContent = text.replace("LIVE_SIGNAL: ", "");
  }

  function route(sym) {
    sym = String(sym || "GBPUSD").toUpperCase(); if (symbols.indexOf(sym) < 0) return;
    var old = state.symbol;
    state.symbol = sym;
    state.lastCvd = null;
    state.lastFeature = null;
    state.manualY = null;
    state.userScaled = false;
    state.chartDrag = null;
    state.hasLiveAnchor = false;
    if (el["chart-shell"]) el["chart-shell"].classList.remove("is-free-panning");
    clearLines();
    loadCandles(sym);
    document.querySelectorAll(".asset-tab").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-symbol") === sym); });
    if (old !== sym) { leave(old); join(sym); }
    updatePanels(); render(); loadLatest(sym);
  }

  function loadLatest(sym) {
    fetch(apiUrl("/features/latest/" + encodeURIComponent(sym)), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(unwrapTbbFxObject)
      .then(function (x) { if (x) applyFeature(x); })
      .catch(function () {})
      .then(function () {
        return fetch(apiUrl("/api/orderflow/levels/" + encodeURIComponent(sym)), { cache: "no-store" });
      })
      .then(function (r) { return r && r.ok ? r.json() : []; })
      .then(unwrapTbbFxObject)
      .then(applyLevels)
      .catch(function () {});
  }

  function shouldRebaseToLive(price, sym) {
    var prof = p(sym);
    if (!price || !prof.price) return false;
    var tolerance = Math.max(prof.pip * 24, prof.price * (sym.indexOf("US") === 0 || sym.indexOf("XAU") === 0 ? 0.015 : 0.0035));
    return Math.abs(price - prof.price) > tolerance;
  }

  function rebaseToLiveAnchor(price, sym) {
    p(sym).price = price;
    state.price = price;
    if (!state.userScaled) state.manualY = null;
    state.levels = state.levels.filter(function (l) { return sanePrice(l.price, sym); });
    state.hasLiveAnchor = true;
    if (!state.bars.length) loadCandles(sym);
  }

  function applyFeature(raw) {
    raw = unwrapTbbFxObject(raw);
    var f = normFeature(raw || {}); if (f.symbol !== state.symbol) return;
    var price = f.microprice || f.price || state.price;
    if (!state.hasLiveAnchor && shouldRebaseToLive(price, f.symbol)) rebaseToLiveAnchor(price, f.symbol);
    state.hasLiveAnchor = true;
    var previous = state.lastCvd; var delta = previous === null ? 0 : f.cvd - previous;
    state.lastCvd = f.cvd; state.price = price; state.lastFeature = f;
    var seconds = tfSeconds();
    var chartTime = Math.floor(f.timestamp / seconds) * seconds + (state.timezoneOffset || 0);
    var lastBar = state.bars[state.bars.length - 1];
    var rows = f.rows.length ? f.rows : synthRows(price, delta, f.obi, f.symbol);
    if (!lastBar || chartTime > lastBar.time) {
      var prevClose = lastBar ? (lastBar.close || lastBar.price || price) : price;
      lastBar = { time: chartTime, open: prevClose, high: Math.max(prevClose, price), low: Math.min(prevClose, price), close: price, price: price, volume: 0, delta: 0, cvd: f.cvd, obi: f.obi, rows: [] };
      state.bars.push(lastBar);
    } else if (chartTime < lastBar.time) {
      // Historical/late tick: annotate the matching candle instead of corrupting the active bar.
      lastBar = state.bars.slice().reverse().find(function (b) { return b.time === chartTime; }) || lastBar;
    }
    lastBar.high = Math.max(lastBar.high || price, price);
    lastBar.low = Math.min(lastBar.low || price, price);
    lastBar.close = price;
    lastBar.price = price;
    lastBar.delta = (lastBar.delta || 0) + delta;
    lastBar.cvd = f.cvd;
    lastBar.obi = f.obi;
    lastBar.rows = rows;
    while (state.bars.length > Math.max(tfBarCount() * 4, 220)) state.bars.shift();
    if (f.gammaFlip > 0) applyLevel({ symbol: f.symbol, price: f.gammaFlip, typeName: "GammaFlip", strengthScore: 90, timestamp: new Date().toISOString(), description: "Z-GEX gamma flip level", source: "SVI_GEX" });
    f.pins.forEach(function (pin) { applyLevel({ symbol: f.symbol, price: pin, typeName: "OpenInterestPin", strengthScore: 74, timestamp: new Date().toISOString(), description: "Open-interest strike pin", source: "OI_PIN" }); });
    updateSeries(); updatePanels(); ageLevels(); render();
  }

  function volumeDelta(raw) {
    var sym = String(any(raw, ["symbol", "Symbol"], state.symbol)).toUpperCase(); if (sym !== state.symbol) return;
    var cvd = Number(any(raw, ["cumulativeDelta", "CumulativeDelta"], state.lastCvd || 0)) || 0;
    var instant = Number(any(raw, ["instantaneousDelta", "InstantaneousDelta"], 0)) || 0;
    if (el["cvd-value"]) {
      el["cvd-value"].textContent = signed(cvd, 1);
      el["cvd-value"].classList.toggle("negative", cvd < 0);
    }
    if (el["vol-delta"]) el["vol-delta"].innerHTML = fmt(Math.abs(instant), 0) + " (" + compact(instant) + ")";
  }

  function obiUpdate(raw) {
    var sym = String(any(raw, ["symbol", "Symbol"], state.symbol)).toUpperCase(); if (sym !== state.symbol || !state.lastFeature) return;
    state.lastFeature.obi = Number(any(raw, ["obi", "Obi"], state.lastFeature.obi)) || 0; updatePanels();
  }

  function applyLevels(list) { if (Array.isArray(list)) list.forEach(applyLevel); render(); }

  function applyLevel(raw) {
    var level = normLevel(raw || {}); if (level.symbol !== state.symbol || level.price <= 0 || !sanePrice(level.price, level.symbol)) return;
    var tol = p(level.symbol).pip; var found = state.levels.find(function (x) { return x.type === level.type && Math.abs(x.price - level.price) <= tol; });
    if (found) {
      found.price = level.price; found.strength = Math.max(found.strength, level.strength); found.timestamp = level.timestamp; found.description = level.description; found.source = level.source;
    } else {
      state.levels.push(level); pushAlert(level);
    }
    state.levels.sort(function (a, b) { return b.strength - a.strength; });
    if (state.levels.length > 18) state.levels.length = 18;
    render();
  }

  function clearLines() {
    state.levels = [];
    if (state.series && state.microLine) { try { state.series.removePriceLine(state.microLine); } catch (e) {} }
    if (state.series && state.gammaLine) { try { state.series.removePriceLine(state.gammaLine); } catch (e2) {} }
    state.microLine = null; state.gammaLine = null;
    if (el["alerts-feed"]) el["alerts-feed"].innerHTML = '<div class="alert-item"><span class="alert-time">[routing]</span><span class="alert-title">SYMBOL_SWITCH</span><br />Cleared stale orderflow levels.</div>';
  }

  function ageLevels() {
    var now = Date.now(); var prof = p(state.symbol);
    state.levels = state.levels.filter(function (x) {
      if (now - x.timestamp > 24 * 60 * 60 * 1000) return false;
      if (x.type === "GammaFlip" || x.type === "OpenInterestPin") return true;
      return !(Math.abs(x.price - state.price) <= prof.pip * 0.35 && now - x.timestamp > 10 * 60 * 1000);
    });
  }

  function updatePanels() {
    var sym = state.symbol; var prof = p(sym); var bar = state.bars[state.bars.length - 1] || { price: prof.price, delta: 0, cvd: 0, obi: 0, rows: [] };
    var f = state.lastFeature || { netGex: sym === "GBPUSD" ? 18000000 : 45000000, obi: bar.obi, cvd: bar.cvd, depth: null };
    var price = state.price || bar.price || prof.price; var netGex = Number(f.netGex || 0) || (sym === "XAUUSD" || sym === "US30" || sym === "USTEC" ? 38000000 : 12000000);
    var positive = netGex >= 0;

    if (el["instrument-symbol"]) el["instrument-symbol"].textContent = sym;
    if (el["instrument-change"]) {
      var open = state.bars[0] ? (state.bars[0].open || state.bars[0].price) : price; var pct = open ? (price - open) / open * 100 : 0;
      el["instrument-change"].textContent = signed(pct, 2) + "%"; el["instrument-change"].style.color = pct >= 0 ? "var(--neon-2)" : "#ff766d";
    }
    if (el.ohlc) {
      var recent = state.bars.slice(-12); var openValue = recent[0] ? (recent[0].open || recent[0].price) : price;
      var high = Math.max.apply(null, recent.map(function (b) { return b.high || b.price; }).concat([price]));
      var low = Math.min.apply(null, recent.map(function (b) { return b.low || b.price; }).concat([price]));
      el.ohlc.innerHTML = fmt(openValue, prof.decimals) + ' | <span class="up">' + fmt(high, prof.decimals) + '</span> | <span class="down">' + fmt(low, prof.decimals) + '</span> | ' + fmt(price, prof.decimals);
    }
    if (el["vol-delta"]) el["vol-delta"].innerHTML = fmt(Math.abs(bar.delta), 0) + " (" + compact(bar.delta) + ")";
    if (el["microprice-badge"]) el["microprice-badge"].textContent = "MICRO " + fmt(price, prof.decimals);

    if (el["gamma-net"]) el["gamma-net"].textContent = compact(netGex);
    if (el["gamma-regime"]) { el["gamma-regime"].textContent = positive ? "POS GAMMA" : "NEG GAMMA"; el["gamma-regime"].classList.toggle("negative", !positive); }
    if (el["gamma-meter"]) el["gamma-meter"].style.width = clamp(52 + Math.log10(Math.abs(netGex) + 10) * 7, 14, 94) + "%";
    exposure(el["dex-value"], el["dex-bar"], netGex * 0.026, 1800000);
    exposure(el["vex-value"], el["vex-bar"], netGex * (f.obi >= 0 ? 0.010 : -0.008), 850000);
    exposure(el["chex-value"], el["chex-bar"], -netGex * 0.0048, 650000);

    var momentum = clamp(Math.round(50 + f.obi * 28 + Math.tanh((bar.delta || 0) / 800) * 18 + (positive ? 7 : -4)), 5, 98);
    if (el["momentum-score"]) el["momentum-score"].textContent = String(momentum);
    if (el["momentum-label"]) el["momentum-label"].textContent = momentum >= 58 ? "Bullish" : momentum <= 42 ? "Bearish" : "Neutral";
    if (el["momentum-ring"]) el["momentum-ring"].style.background = "radial-gradient(circle at center, rgba(9,20,14,.96) 52%, transparent 54%), conic-gradient(" + (momentum >= 50 ? "var(--neon)" : "var(--red)") + " 0deg " + (momentum * 3.6) + "deg, rgba(216,238,224,.12) " + (momentum * 3.6) + "deg 360deg)";

    var cvdVal = f.cvd !== undefined ? f.cvd : bar.cvd;
    var negCvd = cvdVal < 0;
    if (el["cvd-value"]) {
      el["cvd-value"].textContent = signed(cvdVal, 1);
      el["cvd-value"].classList.toggle("negative", negCvd);
    }
    if (el["cvd-bias"]) {
      var bull = cvdVal >= 0;
      el["cvd-bias"].textContent = bull ? "Bullish" : "Bearish";
      el["cvd-bias"].classList.toggle("bearish", !bull);
    }
    if (el["cvd-bar"]) {
      el["cvd-bar"].style.width = clamp(50 + Math.tanh((cvdVal || 0) / 3000) * 42, 8, 95) + "%";
      el["cvd-bar"].classList.toggle("negative", negCvd);
    }
    book(f.depth, price, prof); priceLines(price);
  }

  function exposure(valueEl, barEl, value, scale) {
    if (!valueEl || !barEl) return; var neg = value < 0;
    valueEl.textContent = compact(value); valueEl.classList.toggle("negative", neg); barEl.classList.toggle("negative", neg); barEl.style.width = clamp(Math.abs(value) / scale * 100, 8, 100) + "%";
  }

  function book(depth, price, prof) {
    var bids = depth ? (any(depth, ["bids", "Bids"], []) || []) : []; var asks = depth ? (any(depth, ["asks", "Asks"], []) || []) : [];
    if (!bids.length || !asks.length) {
      bids = []; asks = [];
      for (var i = 0; i < 5; i += 1) { bids.push({ price: price - (i + 1) * prof.spread * 1.35, volume: 1.1 + (4 - i) * 0.88 }); asks.push({ price: price + (i + 1) * prof.spread * 1.4, volume: 0.8 + i * 0.74 }); }
    }
    var html = [];
    asks.slice(0, 5).reverse().forEach(function (r) { html.push(bookRow(r, "ask", prof)); });
    bids.slice(0, 5).forEach(function (r) { html.push(bookRow(r, "bid", prof)); });
    if (el["book-rows"]) el["book-rows"].innerHTML = html.join("");
    if (el["book-mid"]) el["book-mid"].innerHTML = "<span>" + fmt(price, prof.decimals) + "</span><span>" + ((state.bars[state.bars.length - 1] || {}).delta >= 0 ? "UP" : "DOWN") + "</span>";
    if (el["spread-value"]) el["spread-value"].textContent = fmt(prof.spread, prof.decimals > 3 ? 4 : 2);
  }

  function bookRow(row, side, prof) {
    var px = Number(any(row, ["price", "Price"], 0)) || 0; var vol = Number(any(row, ["volume", "Volume"], 0)) || 0; var width = clamp(vol * 18, 10, 100);
    return '<div class="book-row ' + side + '"><span class="price">' + fmt(px, prof.decimals) + '</span><span class="book-track"><span style="width:' + width + '%"></span></span><span class="size">' + compact(vol * 1000000).replace("+", "") + '</span></div>';
  }

  function priceLines(price) {
    if (!state.series || !window.LightweightCharts) return;
    if (state.microLine) { try { state.series.removePriceLine(state.microLine); } catch (e) {} }
    state.microLine = state.series.createPriceLine({ price: price, color: "#28ff9c", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: "MICROPRICE" });
    var gamma = state.levels.find(function (x) { return x.type === "GammaFlip"; });
    if (gamma) {
      if (state.gammaLine) { try { state.series.removePriceLine(state.gammaLine); } catch (e2) {} }
      state.gammaLine = state.series.createPriceLine({ price: gamma.price, color: "#35e7d2", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: "Z-GEX / GAMMA FLIP LEVEL" });
    }
  }

  function setupCanvas(canvas) {
    if (!canvas) return null; var rect = canvas.getBoundingClientRect(); var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * ratio)); canvas.height = Math.max(1, Math.floor(rect.height * ratio));
    var ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); return { ctx: ctx, w: rect.width, h: rect.height };
  }

  function priceRange(plotW) {
    if (state.manualY && Number.isFinite(state.manualY.min) && Number.isFinite(state.manualY.max) && state.manualY.max > state.manualY.min) return state.manualY;
    var prof = p(state.symbol); var prices = [];
    var visible = visibleFootprintBars(plotW || 800).items.map(function (m) { return m.bar; });
    (visible.length ? visible : state.bars).forEach(function (b) {
      prices.push(b.price);
      (b.rows || []).forEach(function (r) { prices.push(r.price); });
    });
    prices.push(state.price || prof.price);
    var barMin = Math.min.apply(null, prices); var barMax = Math.max.apply(null, prices);
    var barSpan = Math.max(barMax - barMin, prof.step * 8);
    state.levels.filter(function (l) { return sanePrice(l.price, l.symbol) && l.price >= barMin - barSpan * 0.45 && l.price <= barMax + barSpan * 0.45; }).forEach(function (l) { prices.push(l.price); });
    var rawMin = Math.min.apply(null, prices); var rawMax = Math.max.apply(null, prices);
    var span = Math.max(rawMax - rawMin, prof.step * 8);
    var pad = Math.max(prof.pip * 0.40, span * 0.08);
    var min = rawMin - pad; var max = rawMax + pad;
    if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) { min = (state.price || prof.price) - prof.step * 15; max = (state.price || prof.price) + prof.step * 15; }
    return { min: min, max: max };
  }

  function logicalRange() {
    var fallback = { from: Math.max(0, state.bars.length - tfRenderCount()), to: Math.max(1, state.bars.length - 1) };
    if (!state.chart || !state.chart.timeScale || !state.chart.timeScale().getVisibleLogicalRange) return fallback;
    var r = state.chart.timeScale().getVisibleLogicalRange();
    if (!r || !Number.isFinite(r.from) || !Number.isFinite(r.to) || r.to <= r.from) return fallback;
    return { from: r.from, to: r.to };
  }

  function setLogicalRange(range) {
    if (!state.chart || !state.chart.timeScale || !state.chart.timeScale().setVisibleLogicalRange) return;
    var span = Math.max(range.to - range.from, 4);
    var maxSpan = Math.max(state.bars.length * 2.4, 24);
    if (span > maxSpan) {
      var mid = (range.from + range.to) / 2;
      range = { from: mid - maxSpan / 2, to: mid + maxSpan / 2 };
    }
    state.chart.timeScale().setVisibleLogicalRange(range);
    renderSoon();
  }

  function startChartPan(e) {
    if (e.button !== 0 || state.axisDrag || state.xAxisDrag || !el["tv-chart"]) return;
    var bounds = el["tv-chart"].getBoundingClientRect();
    if (e.clientX >= bounds.right - 86 || e.clientY >= bounds.bottom - 38) return;
    var range = priceRange(Math.max(240, bounds.width));
    state.userScaled = true;
    state.chartDrag = {
      pointerId: e.pointerId,
      startY: e.clientY,
      min: range.min,
      max: range.max,
      height: Math.max(180, bounds.height)
    };
    if (el["chart-shell"]) el["chart-shell"].classList.add("is-free-panning");
  }

  function moveChartPan(e) {
    var drag = state.chartDrag;
    if (!drag || e.pointerId !== drag.pointerId) return;
    var span = Math.max(drag.max - drag.min, p(state.symbol).step * 20);
    var priceOffset = (e.clientY - drag.startY) / drag.height * span;
    state.manualY = { min: drag.min + priceOffset, max: drag.max + priceOffset };
    renderSoon();
  }

  function stopChartPan(e) {
    var drag = state.chartDrag;
    if (!drag || (e && e.pointerId !== undefined && e.pointerId !== drag.pointerId)) return;
    state.chartDrag = null;
    if (el["chart-shell"]) el["chart-shell"].classList.remove("is-free-panning");
    renderSoon();
  }

  function startXAxisDrag(e) {
    if (!el["x-axis-hotzone"]) return;
    e.preventDefault();
    state.userScaled = true;
    state.xAxisDrag = { x: e.clientX, range: logicalRange(), pan: e.shiftKey || e.altKey };
    el["x-axis-hotzone"].classList.add("is-dragging");
    if (el["x-axis-hotzone"].setPointerCapture) el["x-axis-hotzone"].setPointerCapture(e.pointerId);
  }

  function moveXAxisDrag(e) {
    if (!state.xAxisDrag) return;
    e.preventDefault();
    var r = state.xAxisDrag.range; var span = Math.max(r.to - r.from, 4); var dx = e.clientX - state.xAxisDrag.x;
    if (state.xAxisDrag.pan || e.shiftKey || e.altKey) {
      var chartW = Math.max(240, (el["footprint-canvas"] && el["footprint-canvas"].getBoundingClientRect().width) || 900);
      var offset = dx / chartW * span;
      setLogicalRange({ from: r.from - offset, to: r.to - offset });
    } else {
      var center = (r.from + r.to) / 2; var factor = Math.exp(dx * 0.006);
      var nextSpan = clamp(span * factor, 6, Math.max(state.bars.length * 2.4, 24));
      setLogicalRange({ from: center - nextSpan / 2, to: center + nextSpan / 2 });
    }
  }

  function stopXAxisDrag(e) {
    if (!state.xAxisDrag) return;
    state.xAxisDrag = null;
    if (el["x-axis-hotzone"]) {
      el["x-axis-hotzone"].classList.remove("is-dragging");
      try { if (e && el["x-axis-hotzone"].releasePointerCapture) el["x-axis-hotzone"].releasePointerCapture(e.pointerId); } catch (ignore) {}
    }
  }

  function wheelXAxis(e) {
    e.preventDefault();
    state.userScaled = true;
    var r = logicalRange(); var span = Math.max(r.to - r.from, 4); var center = (r.from + r.to) / 2;
    var delta = Math.abs(e.deltaX || 0) > Math.abs(e.deltaY || 0) ? e.deltaX : e.deltaY;
    var factor = Math.exp(delta * 0.0018); var nextSpan = clamp(span * factor, 6, Math.max(state.bars.length * 2.4, 24));
    setLogicalRange({ from: center - nextSpan / 2, to: center + nextSpan / 2 });
  }

  function resetXAxis() {
    state.userScaled = false;
    state.chartDrag = null;
    if (el["chart-shell"]) el["chart-shell"].classList.remove("is-free-panning");
    if (state.chart && state.chart.timeScale) state.chart.timeScale().fitContent();
    renderSoon();
  }

  function startYAxisDrag(e) {
    if (!el["y-axis-hotzone"]) return;
    e.preventDefault();
    state.userScaled = true;
    state.axisDrag = { y: e.clientY, range: priceRange(800), pan: e.shiftKey || e.altKey };
    el["y-axis-hotzone"].classList.add("is-dragging");
    if (el["y-axis-hotzone"].setPointerCapture) el["y-axis-hotzone"].setPointerCapture(e.pointerId);
  }

  function moveYAxisDrag(e) {
    if (!state.axisDrag) return;
    e.preventDefault();
    var r = state.axisDrag.range; var span = Math.max(r.max - r.min, p(state.symbol).step * 10); var dy = e.clientY - state.axisDrag.y;
    if (state.axisDrag.pan || e.shiftKey || e.altKey) {
      var chartH = Math.max(240, (el["footprint-canvas"] && el["footprint-canvas"].getBoundingClientRect().height) || 600);
      var offset = dy / chartH * span;
      state.manualY = { min: r.min + offset, max: r.max + offset };
    } else {
      var center = (r.max + r.min) / 2; var factor = Math.exp(dy * 0.006);
      var nextSpan = clamp(span * factor, p(state.symbol).step * 12, span * 20);
      state.manualY = { min: center - nextSpan / 2, max: center + nextSpan / 2 };
    }
    renderSoon();
  }

  function stopYAxisDrag(e) {
    if (!state.axisDrag) return;
    state.axisDrag = null;
    if (el["y-axis-hotzone"]) {
      el["y-axis-hotzone"].classList.remove("is-dragging");
      try { if (e && el["y-axis-hotzone"].releasePointerCapture) el["y-axis-hotzone"].releasePointerCapture(e.pointerId); } catch (ignore) {}
    }
  }

  function wheelYAxis(e) {
    e.preventDefault();
    state.userScaled = true;
    var r = priceRange(800); var span = Math.max(r.max - r.min, p(state.symbol).step * 10); var center = (r.max + r.min) / 2;
    var factor = Math.exp((e.deltaY || 0) * 0.0018); var nextSpan = clamp(span * factor, p(state.symbol).step * 12, span * 20);
    state.manualY = { min: center - nextSpan / 2, max: center + nextSpan / 2 };
    renderSoon();
  }

  function resetYAxis() {
    state.userScaled = false;
    state.manualY = null;
    state.chartDrag = null;
    if (el["chart-shell"]) el["chart-shell"].classList.remove("is-free-panning");
    fitChart();
  }

  function render() { renderFootprint(); renderDelta(); }
  var renderPending = false;
  var renderingLock = false;
  function renderSoon() {
    if (renderPending) return;
    renderPending = true;
    requestAnimationFrame(function () {
      renderPending = false;
      if (renderingLock) return;
      renderingLock = true;
      try {
        render();
      } finally {
        renderingLock = false;
      }
    });
  }

  function renderFootprint() {
    var c = setupCanvas(el["footprint-canvas"]); if (!c) return; var ctx = c.ctx; ctx.clearRect(0, 0, c.w, c.h); if (!state.bars.length) return;
    var prof = p(state.symbol); var axisW = 108; var plotW = Math.max(360, c.w - axisW);
    var range = priceRange(plotW); var min = range.min; var max = range.max;
    function y(px) {
      return 18 + (max - px) / (max - min) * (c.h - 44);
    }
    drawLevels(ctx, plotW, c.h, y);

    var plotted = visibleFootprintBars(plotW);
    var spacing = plotted.spacing;
    var minWidth = spacing < 18 ? 6 : spacing < 34 ? 10 : 18;
    var cw = clamp(spacing * 0.64, minWidth, 84);
    var labels = spacing >= 32;
    plotted.items.forEach(function (m) {
      if (m.x < -cw || m.x > plotW + cw) return;
      drawCandle(ctx, m.x, cw, m.bar, y, { labels: labels, compact: !labels });
    });

    var my = y(state.price); ctx.save(); ctx.strokeStyle = "rgba(40,255,156,.95)"; ctx.lineWidth = 1.5; ctx.shadowColor = "rgba(40,255,156,.7)"; ctx.shadowBlur = 16; ctx.beginPath(); ctx.moveTo(0, my); ctx.lineTo(plotW, my); ctx.stroke(); ctx.restore();
    drawPriceAxis(ctx, plotW, c.w, c.h, y, min, max, prof);
  }

  function visibleFootprintBars(plotW) {
    var fallbackGap = plotW / (state.bars.length + 1.55);
    var mapped = state.bars.map(function (b, i) { return { bar: b, x: xCoord(b, i, fallbackGap), index: i }; }).filter(function (m) { return Number.isFinite(m.x); });
    var visible = mapped.filter(function (m) { return m.x >= -140 && m.x <= plotW + 140; });
    if (!visible.length) visible = mapped;
    var xs = visible.map(function (m) { return m.x; }).sort(function (a, b) { return a - b; });
    var spacing = fallbackGap;
    if (xs.length > 1) {
      var diffs = [];
      for (var i = 1; i < xs.length; i += 1) if (Math.abs(xs[i] - xs[i - 1]) > 0.5) diffs.push(Math.abs(xs[i] - xs[i - 1]));
      if (diffs.length) { diffs.sort(function (a, b) { return a - b; }); spacing = diffs[Math.floor(diffs.length / 2)]; }
    }
    return { items: visible, spacing: clamp(spacing, 4, 140) };
  }

  function xCoord(bar, index, gap) {
    if (state.chart && state.chart.timeScale && state.chart.timeScale().timeToCoordinate) {
      var xx = state.chart.timeScale().timeToCoordinate(bar.time);
      if (Number.isFinite(xx)) return xx;
    }
    return gap + index * gap + gap * 0.5;
  }

  function drawCandle(ctx, x, cw, bar, y, opts) {
    opts = opts || {};
    var rows = (bar.rows && bar.rows.length ? bar.rows : synthRows(bar.price, bar.delta, bar.obi, state.symbol)).slice().sort(function (a, b) { return b.price - a.price; });
    var ys = rows.map(function (r) { return y(r.price); }); var top = Math.min.apply(null, ys) - 22; var bottom = Math.max.apply(null, ys) + 22;
    var maxVol = Math.max.apply(null, rows.map(function (r) { return Math.max(r.bidVolume, r.askVolume, 1); })); var up = bar.delta >= 0;
    ctx.save(); ctx.globalAlpha = opts.compact ? 0.78 : 1; ctx.strokeStyle = up ? "rgba(40,255,156,.36)" : "rgba(239,89,80,.32)"; ctx.fillStyle = up ? "rgba(40,255,156,.045)" : "rgba(239,89,80,.045)"; ctx.beginPath(); ctx.moveTo(x, top - 40); ctx.lineTo(x, bottom + 40); ctx.stroke(); ctx.fillRect(x - cw / 2, top, cw, bottom - top); ctx.strokeRect(x - cw / 2, top, cw, bottom - top);
    rows.forEach(function (r) {
      var yy = y(r.price); var h = clamp((bottom - top) / rows.length * 0.92, 18, 44); var bi = clamp(r.bidVolume / maxVol, 0.12, 1); var ai = clamp(r.askVolume / maxVol, 0.12, 1);
      ctx.fillStyle = "rgba(239,89,80," + (0.13 + bi * 0.48) + ")"; ctx.fillRect(x - cw / 2 * bi, yy - h / 2, cw / 2 * bi, h);
      ctx.fillStyle = "rgba(40,255,156," + (0.13 + ai * 0.48) + ")"; ctx.fillRect(x, yy - h / 2, cw / 2 * ai, h);
      ctx.strokeStyle = "rgba(216,238,224,.08)"; ctx.beginPath(); ctx.moveTo(x, yy - h / 2); ctx.lineTo(x, yy + h / 2); ctx.stroke();
      if (opts.labels) { ctx.font = "700 10px JetBrains Mono, monospace"; ctx.textBaseline = "middle"; ctx.textAlign = "right"; ctx.fillStyle = r.bidVolume > r.askVolume * 2.8 ? "#ffb08b" : "rgba(216,238,224,.48)"; ctx.fillText(Math.round(r.bidVolume), x - 6, yy); ctx.textAlign = "left"; ctx.fillStyle = r.askVolume > r.bidVolume * 2.8 ? "#7dffb9" : "rgba(216,238,224,.48)"; ctx.fillText(Math.round(r.askVolume), x + 6, yy); }
    });
    var peak = rows.reduce(function (best, r) { return Math.abs(r.delta) > Math.abs(best.delta) ? r : best; }, rows[0]);
    if (peak && Math.abs(peak.delta) > maxVol * 0.45) { var cy = y(peak.price); ctx.strokeStyle = peak.delta >= 0 ? "rgba(40,255,156,.9)" : "rgba(248,175,56,.9)"; ctx.shadowColor = peak.delta >= 0 ? "rgba(40,255,156,.55)" : "rgba(248,175,56,.55)"; ctx.shadowBlur = 22; ctx.beginPath(); ctx.arc(x, cy, clamp(cw * 0.22, 11, 20), 0, Math.PI * 2); ctx.stroke(); }
    ctx.restore();
  }

  function drawLevels(ctx, width, height, y) {
    state.levels.forEach(function (l) {
      var yy = y(l.price); if (!Number.isFinite(yy) || yy < -40 || yy > height + 40) return;
      var gamma = l.type === "GammaFlip"; var oi = l.type === "OpenInterestPin"; var demand = l.type === "BuyerImbalance" || (l.type === "AbsorptionNode" && l.description.toLowerCase().indexOf("sell delta") >= 0); var supply = l.type === "SellerImbalance" || (l.type === "AbsorptionNode" && !demand);
      ctx.save();
      if (gamma || oi) { ctx.strokeStyle = gamma ? "rgba(53,231,210,.82)" : "rgba(248,175,56,.58)"; ctx.lineWidth = gamma ? 1.4 : 1; ctx.setLineDash(gamma ? [8, 8] : [3, 8]); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(width, yy); ctx.stroke(); ctx.setLineDash([]); levelLabel(ctx, gamma ? "Z-GEX / GAMMA FLIP LEVEL" : "OI STRIKE PIN", yy, gamma ? "#35e7d2" : "#f8af38", width); }
      else { var fill = demand ? "rgba(40,255,156,.14)" : supply ? "rgba(239,89,80,.14)" : "rgba(53,231,210,.12)"; var stroke = demand ? "rgba(40,255,156,.58)" : supply ? "rgba(239,89,80,.55)" : "rgba(53,231,210,.5)"; ctx.fillStyle = fill; ctx.strokeStyle = stroke; ctx.shadowColor = stroke; ctx.shadowBlur = 18; ctx.fillRect(0, yy - 15, width, 30); ctx.strokeRect(0, yy - 15, width, 30); levelLabel(ctx, l.type + " " + Math.round(l.strength), yy, demand ? "#7dffb9" : "#ff8a83", width); }
      ctx.restore();
    });
  }

  function levelLabel(ctx, text, y, color, plotWidth) {
    var w = plotWidth || ctx.canvas.getBoundingClientRect().width; ctx.font = "800 10px JetBrains Mono, monospace"; ctx.textAlign = "right"; ctx.textBaseline = "middle"; var tw = ctx.measureText(text).width + 16; var x = w - 12; ctx.fillStyle = "rgba(3,10,7,.86)"; ctx.fillRect(x - tw, y - 12, tw, 24); ctx.strokeStyle = color; ctx.strokeRect(x - tw, y - 12, tw, 24); ctx.fillStyle = color; ctx.fillText(text, x - 8, y);
  }

  function drawPriceAxis(ctx, plotW, fullW, height, y, min, max, prof) {
    ctx.save();
    ctx.fillStyle = "rgba(2,6,4,.92)";
    ctx.fillRect(plotW, 0, fullW - plotW, height);
    ctx.strokeStyle = "rgba(40,255,156,.24)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(plotW + 0.5, 0); ctx.lineTo(plotW + 0.5, height); ctx.stroke();

    ctx.font = "800 11px JetBrains Mono, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    var ticks = 8;
    for (var i = 0; i <= ticks; i += 1) {
      var price = max - (max - min) * (i / ticks);
      var yy = y(price);
      if (!Number.isFinite(yy) || yy < 16 || yy > height - 18) continue;
      ctx.strokeStyle = "rgba(216,238,224,.18)";
      ctx.beginPath(); ctx.moveTo(plotW, yy); ctx.lineTo(plotW + 8, yy); ctx.stroke();
      ctx.fillStyle = "rgba(216,238,224,.72)";
      ctx.fillText(fmt(price, prof.decimals), plotW + 13, yy);
    }

    var currentY = y(state.price);
    var label = fmt(state.price, prof.decimals);
    var labelW = Math.min(fullW - plotW - 10, Math.max(82, ctx.measureText(label).width + 20));
    var labelY = clamp(currentY, 22, height - 22);
    ctx.shadowColor = "rgba(40,255,156,.75)";
    ctx.shadowBlur = 18;
    ctx.fillStyle = "rgba(40,255,156,.96)";
    ctx.fillRect(plotW + 6, labelY - 15, labelW, 30);
    ctx.shadowBlur = 0;
    ctx.fillStyle = "#03120a";
    ctx.font = "900 12px JetBrains Mono, monospace";
    ctx.fillText(label, plotW + 16, labelY);
    ctx.strokeStyle = "rgba(40,255,156,.95)";
    ctx.beginPath(); ctx.moveTo(plotW - 10, currentY); ctx.lineTo(plotW + 6, labelY); ctx.stroke();
    ctx.restore();
  }

  function renderDelta() {
    var c = setupCanvas(el["delta-canvas"]); if (!c) return; var ctx = c.ctx; ctx.clearRect(0, 0, c.w, c.h); if (!state.bars.length) return;
    var fallbackGap = c.w / (state.bars.length + 1.55);
    var bars = state.bars.map(function (b, i) { return { bar: b, x: xCoord(b, i, fallbackGap) }; }).filter(function (m) { return Number.isFinite(m.x) && m.x >= -60 && m.x <= c.w + 60; });
    if (!bars.length) bars = state.bars.map(function (b, i) { return { bar: b, x: i * fallbackGap + fallbackGap * 0.5 }; });
    var maxAbs = Math.max.apply(null, bars.map(function (m) { return Math.abs(m.bar.delta); }).concat([1]));
    var spacing = fallbackGap;
    if (bars.length > 1) spacing = Math.abs(bars[1].x - bars[0].x) || fallbackGap;
    ctx.fillStyle = "rgba(216,238,224,.10)"; ctx.fillRect(0, c.h - 10, c.w, 3);
    bars.forEach(function (m) { var b = m.bar; var h = clamp(Math.abs(b.delta) / maxAbs * (c.h - 14), 3, c.h - 14); var x = m.x - Math.max(2, spacing * 0.35); var g = ctx.createLinearGradient(0, c.h - h, 0, c.h); if (b.delta >= 0) { g.addColorStop(0, "rgba(40,255,156,.96)"); g.addColorStop(1, "rgba(40,255,156,.22)"); } else { g.addColorStop(0, "rgba(239,89,80,.90)"); g.addColorStop(1, "rgba(239,89,80,.18)"); } ctx.fillStyle = g; ctx.fillRect(x, c.h - h - 8, Math.max(2, spacing * 0.70), h); });
  }

  function pushAlert(l) {
    if (!el["alerts-feed"]) return; var d = new Date(l.timestamp || Date.now()); var t = "[" + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0") + ":" + String(d.getSeconds()).padStart(2, "0") + "]";
    var cls = l.type === "GammaFlip" || l.type === "OpenInterestPin" ? "gamma" : (l.type === "SellerImbalance" ? "supply" : ""); var title = l.type === "GammaFlip" ? "GAMMA_FLIP" : l.type === "AbsorptionNode" ? "ABSORPTION_NODE" : l.type.toUpperCase();
    var div = document.createElement("div"); div.className = "alert-item " + cls; div.innerHTML = '<span class="alert-time">' + t + '</span><span class="alert-title">' + title + '</span><br />' + fmt(l.price, p(l.symbol).decimals) + ' - strength ' + Math.round(l.strength) + ' - ' + l.source;
    el["alerts-feed"].prepend(div); while (el["alerts-feed"].children.length > 8) el["alerts-feed"].removeChild(el["alerts-feed"].lastChild);
  }

  function startSim() {
    if (state.sim) return;
    state.sim = setInterval(function () {
      var prof = p(state.symbol); var last = state.bars[state.bars.length - 1] || { price: prof.price, cvd: 0 }; var delta = Math.round((Math.random() - 0.48) * 760); var px = Math.max(prof.step, last.price + (Math.random() - 0.47) * prof.step * 8);
      applyFeature({ symbol: state.symbol, cvd: (state.lastCvd || last.cvd || 0) + delta, microprice: px, price: px, obi: clamp(delta / 1000 + (Math.random() - 0.5) * 0.2, -1, 1), net_gex: state.symbol === "GBPUSD" ? 18000000 : 45000000, gamma_flip: px - prof.step * 8, footprint_rows: synthRows(px, delta, clamp(delta / 1000, -1, 1), state.symbol).map(function (r) { return { price: r.price, bid_volume: r.bidVolume, ask_volume: r.askVolume, delta: r.delta }; }), timestamp: Date.now() / 1000 });
      if (Math.random() > 0.80) applyLevel({ symbol: state.symbol, price: px + (Math.random() - 0.5) * prof.pip * 2, typeName: Math.random() > 0.5 ? "AbsorptionNode" : "BuyerImbalance", strengthScore: 78 + Math.random() * 20, timestamp: new Date().toISOString(), description: delta < 0 ? "Sell delta absorbed without continuation" : "Buy imbalance near dealer shelf", source: "SIM_FOOTPRINT" });
    }, 900);
  }
  function stopSim() { if (state.sim) { clearInterval(state.sim); state.sim = null; } }
  function resizeSoon() { clearTimeout(state.resizeTimer); state.resizeTimer = setTimeout(function () { if (state.chart && el["tv-chart"]) state.chart.resize(el["tv-chart"].clientWidth, el["tv-chart"].clientHeight); renderSoon(); }, 120); }

  document.addEventListener("DOMContentLoaded", boot);
})();
