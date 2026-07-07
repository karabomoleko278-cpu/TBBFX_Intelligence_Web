using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.AspNetCore.SignalR;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using System.Collections.Concurrent;
using System.Globalization;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.RateLimiting;
using TBBFX.SignalRFeatureStore.Hubs;
using TBBFX.Engine.Backtesting;
using TBBFX.Engine.Validation;
using TBBFX.Engine.Strategies;
using TBBFX.Models;

var builder = WebApplication.CreateBuilder(args);
var configuredPort = int.TryParse(Environment.GetEnvironmentVariable("PORT"), out var azurePort)
    ? azurePort
    : 5000;
var publicReadOnly = IsTruthy(builder.Configuration["TBBFX_PUBLIC_READONLY"]);
var featureUpdateKey = builder.Configuration["TBBFX_FEATURE_UPDATE_KEY"];
var allowedOrigins = ParseAllowedOrigins(builder.Configuration["TBBFX_ALLOWED_ORIGINS"]);

// Configure port to run on 5000 locally; Azure can inject PORT when hosted behind a platform proxy.
builder.WebHost.ConfigureKestrel(options =>
{
    options.ListenAnyIP(configuredPort);
});

// Add services to the container
builder.Services.AddSignalR();
builder.Services.AddSingleton<OrderflowGammaLevelRefinery>();
builder.Services.AddCors(options =>
{
    options.AddPolicy("TerminalCors", policy =>
    {
        if (allowedOrigins.Length > 0)
        {
            policy.WithOrigins(allowedOrigins);
        }
        else
        {
            policy.SetIsOriginAllowed(IsAllowedTerminalOrigin);
        }

        policy.AllowAnyHeader()
              .AllowAnyMethod()
              .AllowCredentials();
    });
});
builder.Services.AddRateLimiter(options =>
{
    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.AddFixedWindowLimiter("public-read", limiter =>
    {
        limiter.PermitLimit = 120;
        limiter.Window = TimeSpan.FromMinutes(1);
        limiter.QueueLimit = 0;
        limiter.QueueProcessingOrder = QueueProcessingOrder.OldestFirst;
    });
    options.AddFixedWindowLimiter("private-write", limiter =>
    {
        // FeatureFactory publishes bursty multi-symbol telemetry. Keep this private/CORS scoped,
        // but allow enough headroom that live cache updates are not starved by 429s.
        limiter.PermitLimit = 600;
        limiter.Window = TimeSpan.FromMinutes(1);
        limiter.QueueLimit = 60;
        limiter.QueueProcessingOrder = QueueProcessingOrder.OldestFirst;
    });
});

var app = builder.Build();

app.Use(async (context, next) =>
{
    context.Response.Headers.TryAdd("X-Content-Type-Options", "nosniff");
    context.Response.Headers.TryAdd("Referrer-Policy", "strict-origin-when-cross-origin");
    context.Response.Headers.TryAdd("X-Frame-Options", "DENY");
    context.Response.Headers.TryAdd("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()");
    await next();
});

app.Use(async (context, next) =>
{
    if (context.Request.Headers.TryGetValue("Access-Control-Request-Private-Network", out var privateNetwork) &&
        string.Equals(privateNetwork.FirstOrDefault(), "true", StringComparison.OrdinalIgnoreCase) &&
        IsAllowedTerminalOrigin(context.Request.Headers.Origin.FirstOrDefault()))
    {
        context.Response.OnStarting(() =>
        {
            context.Response.Headers["Access-Control-Allow-Private-Network"] = "true";
            return Task.CompletedTask;
        });
    }

    await next();
});

app.UseCors("TerminalCors");
app.UseRateLimiter();

// In-Memory online feature cache for clean microstructure features.
var featureCache = new ConcurrentDictionary<string, MarketFeature>();
// Last seen CVD per symbol so we can derive an instantaneous Volume Delta.
var lastCvd = new ConcurrentDictionary<string, double>();
// Live MT5 candle snapshots streamed by TBBFX_DirectBridge.mq5 via the local relay.
var candleCache = new ConcurrentDictionary<string, CandleSnapshot>();

static string CleanSymbol(string symbol)
{
    var sym = (symbol ?? string.Empty).Trim().ToUpperInvariant();
    return sym.EndsWith("M", StringComparison.OrdinalIgnoreCase) ? sym[..^1] : sym;
}

static string NormalizeTimeframe(string timeframe)
{
    var tf = (timeframe ?? "M5").Trim().ToUpperInvariant();
    return tf switch
    {
        "5M" => "M5",
        "15M" => "M15",
        "1H" or "1HR" => "H1",
        "4H" or "4HR" => "H4",
        "1D" or "D" => "D1",
        "1W" or "W" => "W1",
        _ => tf
    };
}

static int TimeframeSeconds(string tf) => tf switch
{
    "M5" => 300,
    "M15" => 900,
    "H1" => 3600,
    "H4" => 14400,
    "D1" => 86400,
    "W1" => 604800,
    _ => 300
};

static string? ResolveHistoricalCsvPath(string symbol)
{
    var scripts = @"C:\Users\Dineo Lebese\source\repos\TBBFX_Solution\Scripts";
    var sym = CleanSymbol(symbol);
    var candidates = new[]
    {
        Path.Combine(scripts, $"{sym}m_Historical_Data.csv"),
        Path.Combine(scripts, $"{sym}_Historical_Data.csv")
    };

    return candidates.FirstOrDefault(System.IO.File.Exists);
}

static bool IsTruthy(string? value)
{
    return value is not null &&
           (value.Equals("true", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("1", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("yes", StringComparison.OrdinalIgnoreCase));
}

static string[] ParseAllowedOrigins(string? origins)
{
    return string.IsNullOrWhiteSpace(origins)
        ? Array.Empty<string>()
        : origins.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}

static bool IsAllowedTerminalOrigin(string? origin)
{
    if (!Uri.TryCreate(origin, UriKind.Absolute, out var uri))
    {
        return false;
    }

    if (uri.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase) ||
        uri.Host.Equals("127.0.0.1", StringComparison.OrdinalIgnoreCase))
    {
        return uri.Port is 5000 or 8000 or 8010 or 8787;
    }

    return uri.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase) &&
           (uri.Host.Equals("tbbfx-intelligence-web.pages.dev", StringComparison.OrdinalIgnoreCase) ||
            uri.Host.EndsWith(".tbbfx-intelligence-web.pages.dev", StringComparison.OrdinalIgnoreCase));
}

static bool SecureEquals(string? actual, string expected)
{
    if (string.IsNullOrEmpty(actual) || string.IsNullOrEmpty(expected))
    {
        return false;
    }

    var actualBytes = System.Text.Encoding.UTF8.GetBytes(actual);
    var expectedBytes = System.Text.Encoding.UTF8.GetBytes(expected);
    return actualBytes.Length == expectedBytes.Length &&
           CryptographicOperations.FixedTimeEquals(actualBytes, expectedBytes);
}

static List<CandleWire> LoadCsvCandles(string symbol)
{
    var path = ResolveHistoricalCsvPath(symbol);
    var candles = new List<CandleWire>();
    if (path == null) return candles;

    using var reader = new StreamReader(path);
    var first = true;
    while (!reader.EndOfStream)
    {
        var line = reader.ReadLine();
        if (string.IsNullOrWhiteSpace(line)) continue;
        if (first)
        {
            first = false;
            continue;
        }

        var cols = line.Split(',');
        if (cols.Length < 8) continue;
        if (!DateTime.TryParse(cols[0], CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var timestamp)) continue;
        if (!double.TryParse(cols[1], NumberStyles.Any, CultureInfo.InvariantCulture, out var open)) continue;
        if (!double.TryParse(cols[2], NumberStyles.Any, CultureInfo.InvariantCulture, out var high)) continue;
        if (!double.TryParse(cols[3], NumberStyles.Any, CultureInfo.InvariantCulture, out var low)) continue;
        if (!double.TryParse(cols[4], NumberStyles.Any, CultureInfo.InvariantCulture, out var close)) continue;
        _ = double.TryParse(cols[7], NumberStyles.Any, CultureInfo.InvariantCulture, out var volume);

        candles.Add(new CandleWire(
            new DateTimeOffset(DateTime.SpecifyKind(timestamp, DateTimeKind.Utc)).ToUnixTimeSeconds(),
            open,
            high,
            low,
            close,
            volume));
    }

    return candles;
}

static List<CandleWire> AggregateCandles(IEnumerable<CandleWire> candles, string timeframe)
{
    if (timeframe == "M5") return candles.OrderBy(c => c.Time).ToList();

    var seconds = TimeframeSeconds(timeframe);
    return candles
        .OrderBy(c => c.Time)
        .GroupBy(c => c.Time / seconds * seconds)
        .Select(g =>
        {
            var ordered = g.OrderBy(c => c.Time).ToList();
            return new CandleWire(
                g.Key,
                ordered.First().Open,
                ordered.Max(c => c.High),
                ordered.Min(c => c.Low),
                ordered.Last().Close,
                ordered.Sum(c => c.Volume));
        })
        .OrderBy(c => c.Time)
        .ToList();
}

static string CandleKey(string symbol, string timeframe) => $"{CleanSymbol(symbol)}|{NormalizeTimeframe(timeframe)}";

static async Task StartMt5CandleRelayListenerAsync(ConcurrentDictionary<string, CandleSnapshot> cache)
{
    while (true)
    {
        try
        {
            using var client = new TcpClient();
            await client.ConnectAsync("127.0.0.1", 77);
            Console.WriteLine("[CandleRelay] Connected to TBBFX.Worker relay on 127.0.0.1:77.");

            using var stream = client.GetStream();
            using var reader = new StreamReader(stream);
            while (client.Connected)
            {
                var line = await reader.ReadLineAsync();
                if (string.IsNullOrWhiteSpace(line)) continue;
                if (!line.Contains("\"action\":\"candles\"", StringComparison.OrdinalIgnoreCase)) continue;

                try
                {
                    using var doc = JsonDocument.Parse(line);
                    var root = doc.RootElement;
                    var symbol = CleanSymbol(root.GetProperty("symbol").GetString() ?? string.Empty);
                    var timeframe = NormalizeTimeframe(root.GetProperty("tf").GetString() ?? "M5");
                    if (!root.TryGetProperty("data", out var data) || data.ValueKind != JsonValueKind.Array) continue;

                    var candles = new List<CandleWire>();
                    foreach (var item in data.EnumerateArray())
                    {
                        candles.Add(new CandleWire(
                            item.GetProperty("t").GetInt64(),
                            item.GetProperty("o").GetDouble(),
                            item.GetProperty("h").GetDouble(),
                            item.GetProperty("l").GetDouble(),
                            item.GetProperty("c").GetDouble(),
                            item.TryGetProperty("v", out var volume) ? volume.GetDouble() : 0));
                    }

                    if (candles.Count > 0)
                    {
                        cache[CandleKey(symbol, timeframe)] = new CandleSnapshot(symbol, timeframe, "mt5_ea_relay", false, candles);
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[CandleRelay] Ignored malformed candle packet: {ex.Message}");
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[CandleRelay] Waiting for TBBFX.Worker/MT5 relay: {ex.Message}");
            await Task.Delay(TimeSpan.FromSeconds(5));
        }
    }
}

app.MapGet("/", () => new
{
    status = "online",
    service = "TBBFX SignalR Feature Store",
    orderflow = "/orderflow",
    confluenceEngine = "Orderflow + Gamma refinery active",
    clients = MarketPulseHub.ConnectedCount,
    mobileClients = MarketPulseHub.MobileCount
});

app.MapGet("/terminal", () =>
{
    var terminalPath = Path.GetFullPath(Path.Combine(
        AppContext.BaseDirectory,
        "..", "..", "..", "..", "terminal", "TBBFX Intelligence Terminal.html"));

    return System.IO.File.Exists(terminalPath)
        ? Results.Content(System.IO.File.ReadAllText(terminalPath), "text/html")
        : Results.NotFound(new { error = $"Terminal file not found at {terminalPath}" });
});

app.MapGet("/orderflow", () =>
{
    var orderflowPath = Path.GetFullPath(Path.Combine(
        AppContext.BaseDirectory,
        "..", "..", "..", "..", "terminal", "orderflow.html"));

    return System.IO.File.Exists(orderflowPath)
        ? Results.Content(System.IO.File.ReadAllText(orderflowPath), "text/html")
        : Results.NotFound(new { error = $"Orderflow workspace file not found at {orderflowPath}" });
});

app.MapGet("/terminal/orderflow_chart_overlays.js", () =>
{
    var scriptPath = Path.GetFullPath(Path.Combine(
        AppContext.BaseDirectory,
        "..", "..", "..", "..", "terminal", "orderflow_chart_overlays.js"));

    return System.IO.File.Exists(scriptPath)
        ? Results.File(scriptPath, "application/javascript")
        : Results.NotFound(new { error = $"Orderflow overlay script not found at {scriptPath}" });
});

app.MapGet("/orderflow_chart_overlays.js", () =>
{
    var scriptPath = Path.GetFullPath(Path.Combine(
        AppContext.BaseDirectory,
        "..", "..", "..", "..", "terminal", "orderflow_chart_overlays.js"));

    return System.IO.File.Exists(scriptPath)
        ? Results.File(scriptPath, "application/javascript")
        : Results.NotFound(new { error = $"Orderflow overlay script not found at {scriptPath}" });
});

app.MapGet("/api/live-scanner/profile", () =>
{
    if (publicReadOnly)
    {
        return Results.NotFound();
    }

    var configPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "TBBFX",
        "live_strategy_config.json");

    var config = new LiveStrategyConfig();
    if (System.IO.File.Exists(configPath))
    {
        try
        {
            var loaded = JsonSerializer.Deserialize<LiveStrategyConfig>(
                System.IO.File.ReadAllText(configPath),
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            if (loaded != null)
            {
                loaded.SymbolOverrides = LiveStrategyConfig.MergeSymbolOverrides(loaded.SymbolOverrides);
                config = loaded;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[LiveScannerProfile] Failed to read live strategy config: {ex.Message}");
        }
    }

    var validationMetrics = new[]
    {
        new { Symbol = "EURUSD", Net = 5917.19, ProfitFactor = 1.83, Trades = 20, Status = "PASS" },
        new { Symbol = "GBPUSD", Net = 8247.77, ProfitFactor = 1.86, Trades = 41, Status = "PASS" },
        new { Symbol = "XAUUSD", Net = 22362.03, ProfitFactor = 2.03, Trades = 51, Status = "PASS" },
        new { Symbol = "US30", Net = 15559.97, ProfitFactor = 2.36, Trades = 54, Status = "PASS" },
        new { Symbol = "USTEC", Net = 8880.55, ProfitFactor = 4.95, Trades = 16, Status = "PASS" },
        new { Symbol = "USDJPY", Net = 13664.42, ProfitFactor = 1.78, Trades = 37, Status = "PASS" }
    };

    var rows = validationMetrics.Select(m => new
    {
        m.Symbol,
        RiskPercent = config.GetRiskPercent(m.Symbol),
        ValidationNet = m.Net,
        ValidationProfitFactor = m.ProfitFactor,
        ValidationTrades = m.Trades,
        m.Status,
        LiveConfig = config.ToWireSnapshot(m.Symbol)
    });

    return Results.Ok(new
    {
        Source = configPath,
        Rows = rows
    });
}).RequireRateLimiting("private-write");

// Endpoint for the Python Centralized Feature Factory to push calculated features.
app.MapPost("/features/update", async (
    HttpContext http,
    MarketFeature feature,
    IHubContext<FeatureHub> featureHub,
    IHubContext<MarketPulseHub> pulseHub,
    OrderflowGammaLevelRefinery levelRefinery) =>
{
    if (publicReadOnly)
    {
        return Results.NotFound();
    }

    if (!string.IsNullOrWhiteSpace(featureUpdateKey) &&
        !SecureEquals(http.Request.Headers["X-TBBFX-FEATURE-KEY"].FirstOrDefault(), featureUpdateKey))
    {
        return Results.Unauthorized();
    }

    if (string.IsNullOrWhiteSpace(feature.Symbol))
    {
        return Results.BadRequest("Symbol is required");
    }

    var sym = feature.Symbol.ToUpperInvariant();
    feature.Symbol = sym;

    // Store in the online cache (this is what the MAUI app queries on demand).
    featureCache[sym] = feature;

    // --- Back-compat broadcasts on the original FeatureHub ---------------
    await featureHub.Clients.Group(sym).SendAsync("ReceiveFeatureUpdate", feature);
    await featureHub.Clients.All.SendAsync("ReceiveAnyUpdate", feature);

    // --- MarketPulseHub: live Volume Delta + OBI -------------------------
    var previous = lastCvd.GetValueOrDefault(sym, feature.Cvd);
    var instantaneousDelta = feature.Cvd - previous;
    lastCvd[sym] = feature.Cvd;

    var volumeDelta = new
    {
        symbol = sym,
        cumulativeDelta = feature.Cvd,
        instantaneousDelta,
        timestamp = feature.Timestamp
    };
    var obiUpdate = new { symbol = sym, obi = feature.Obi, timestamp = feature.Timestamp };

    await pulseHub.Clients.Group(sym).SendAsync("ReceivePulse", feature);
    await pulseHub.Clients.Group(sym).SendAsync("ReceiveVolumeDelta", volumeDelta);
    await pulseHub.Clients.Group(sym).SendAsync("ReceiveObiUpdate", obiUpdate);
    // Also fan out to any client not subscribed to a specific symbol group.
    await pulseHub.Clients.All.SendAsync("ReceiveAnyPulse", feature);

    var confluenceLevels = levelRefinery.Process(feature);
    if (confluenceLevels.Count > 0)
    {
        await pulseHub.Clients.Group(sym).SendAsync("ReceiveInstitutionalConfluenceLevels", confluenceLevels);
        await pulseHub.Clients.All.SendAsync("ReceiveAnyInstitutionalConfluenceLevels", confluenceLevels);

        foreach (var level in confluenceLevels)
        {
            await pulseHub.Clients.Group(sym).SendAsync("ReceiveInstitutionalConfluenceLevel", level);
            await pulseHub.Clients.All.SendAsync("ReceiveAnyInstitutionalConfluenceLevel", level);
        }
    }

    return Results.Ok(new { message = $"Feature updated for {sym} and broadcast to web + mobile clients." });
}).RequireRateLimiting("private-write");

app.MapGet("/api/orderflow/levels/{symbol}", (
    string symbol,
    OrderflowGammaLevelRefinery levelRefinery) =>
{
    return Results.Ok(levelRefinery.GetActiveLevels(symbol));
}).RequireRateLimiting("public-read");

app.MapGet("/api/market/candles/{symbol}/{timeframe}", (string symbol, string timeframe, int count = 240) =>
{
    var sym = CleanSymbol(symbol);
    var tf = NormalizeTimeframe(timeframe);
    if (TimeframeSeconds(tf) == 300 && tf != "M5")
    {
        return Results.BadRequest(new { error = $"Unsupported timeframe: {timeframe}" });
    }

    var limit = Math.Clamp(count, 20, 1200);
    if (candleCache.TryGetValue(CandleKey(sym, tf), out var snapshot) && snapshot.Candles.Count > 0)
    {
        return Results.Ok(new
        {
            symbol = snapshot.Symbol,
            timeframe = snapshot.Timeframe,
            source = snapshot.Source,
            authentic = true,
            stale = false,
            candles = snapshot.Candles.TakeLast(limit)
        });
    }

    var candles = AggregateCandles(LoadCsvCandles(sym), tf).TakeLast(limit).ToList();
    if (candles.Count == 0)
    {
        return Results.NotFound(new { error = $"No candle history available for {sym} {tf}" });
    }

    return Results.Ok(new
    {
        symbol = sym,
        timeframe = tf,
        source = "csv_historical_fallback",
        authentic = true,
        stale = true,
        candles
    });
}).RequireRateLimiting("public-read");

// Endpoint for the MAUI app to query the latest clean features instantly.
app.MapGet("/features/latest/{symbol}", (string symbol) =>
{
    var sym = symbol.ToUpperInvariant();
    return featureCache.TryGetValue(sym, out var feature)
        ? Results.Ok(feature)
        : Results.NotFound(new { error = $"No features found in cache for symbol: {sym}" });
}).RequireRateLimiting("public-read");

// Retrieve all cached features.
app.MapGet("/features/all", () =>
{
    if (publicReadOnly)
    {
        return Results.NotFound();
    }

    return Results.Ok(featureCache.Values);
}).RequireRateLimiting("private-write");

// Map the SignalR hubs.
app.MapHub<FeatureHub>("/features/stream");        // original
app.MapHub<MarketPulseHub>("/hub/marketpulse");     // live Volume Delta + OBI (web + MAUI)

// Endpoint to run the 4 quantitative validation pillars on a background thread and stream progress via SignalR
app.MapPost("/api/validation/run/{symbol}", (
    string symbol,
    IHubContext<MarketPulseHub> pulseHub) =>
{
    if (publicReadOnly)
    {
        return Results.NotFound();
    }

    // Run validation asynchronously in a background thread
    _ = Task.Run(async () =>
    {
        try
        {
            string symbolUpper = symbol.ToUpperInvariant();
            string scriptSymbol = symbolUpper.EndsWith("M") ? symbolUpper : symbolUpper + "m";
            string csvPath = $@"C:\Users\Dineo Lebese\source\repos\TBBFX_Solution\Scripts\{scriptSymbol}_Historical_Data.csv";
            if (!System.IO.File.Exists(csvPath))
            {
                csvPath = $@"C:\Users\Dineo Lebese\source\repos\TBBFX_Solution\Scripts\{symbolUpper}_Historical_Data.csv";
            }

            if (!System.IO.File.Exists(csvPath))
            {
                await pulseHub.Clients.All.SendAsync("ReceiveValidationResult", new { error = $"Historical data file not found at: {csvPath}" });
                return;
            }

            var progress = new Progress<ValidationProgress>(async p =>
            {
                await pulseHub.Clients.All.SendAsync("ReceiveValidationProgress", new {
                    step = p.Step,
                    current = p.Current,
                    total = p.Total,
                    message = p.Message,
                    pct = p.Pct
                });
            });

            var progressReporter = (IProgress<ValidationProgress>)progress;

            const int validationCandles = 25000;
            progressReporter.Report(new ValidationProgress("Setup", 0, 4, $"Loading last {validationCandles:N0} historical candles..."));
            var history = CsvDataFeeder.LoadFromCsv(csvPath, symbolUpper)
                .OrderBy(p => p.Timestamp)
                .TakeLast(validationCandles)
                .ToList();

            if (history.Count == 0)
            {
                await pulseHub.Clients.All.SendAsync("ReceiveValidationResult", new { error = "No historical data loaded." });
                return;
            }

            // Step 1: Run Parameter Stability Sweep
            progressReporter.Report(new ValidationProgress("ParameterStability", 0, 100, "Starting stability grid sweep..."));
            var grid = WalkForwardValidationManager.GetSymbolGrid(symbolUpper);
            var atrGridLabel = string.Join('/', (grid.SlAtrMultiplier ?? Array.Empty<double>()).Select(v => v.ToString("0.00")));
            Console.WriteLine($"[Validation Grid] {symbolUpper} | MA={string.Join('/', grid.EmaSlowPeriod ?? Array.Empty<int>())} | SL ATR={atrGridLabel} | Metric=ProfitFactor");

            var stabilityEngine = new ParameterStabilityEngine();
            var sweepResult = await stabilityEngine.SweepAsync(
                history, grid, initialBalance: 5000, heatmapX: "EmaSlowPeriod", heatmapY: "SlAtrMultiplier", metric: "ProfitFactor", progress: progressReporter);

            // Step 2: Monte Carlo Simulation
            progressReporter.Report(new ValidationProgress("MonteCarlo", 0, 100, "Initializing Monte Carlo simulation paths..."));
            var robustSets = sweepResult.Results.Where(r => r.SharpeRatio > 0).ToList();
            if (robustSets.Count == 0)
            {
                robustSets = sweepResult.Results.OrderByDescending(r => r.SharpeRatio).Take(3).ToList();
            }

            var mcEngine = new MonteCarloSimulationEngine { Iterations = 1000, InitialBalance = 5000, Symbol = symbolUpper };
            var mcResults = await mcEngine.RunAsync(robustSets, progress: progressReporter);

            // Step 3: Cluster Analysis
            progressReporter.Report(new ValidationProgress("ClusterAnalysis", 0, 100, "Running unsupervised parameter clustering..."));
            var clusterService = new ClusterAnalysisService { K = 3 };
            var clusterResult = await clusterService.AnalyzeAsync(sweepResult.Results, progress: progressReporter);

            // Step 4: Walk-Forward Validation Loop
            progressReporter.Report(new ValidationProgress("WalkForward", 0, 100, "Executing walk-forward validation windows..."));
            var wfManager = new WalkForwardValidationManager { InSampleDays = 90, OutSampleDays = 30, InitialBalance = 5000, EnforceWinRateConstraint = false };
            var wfResult = await wfManager.RunAsync(history, grid, progress: progressReporter);
            // Final production gate: walk-forward edge must be backed by a Monte Carlo survivor
            // and a real stability plateau, not a single optimized spike.
            bool mcSurvives = mcResults.Any(r => r.StructurallySurvives);
            int stabilityPlateauCount = sweepResult.Results.Count(r =>
                r.NetProfit > 0 &&
                r.ProfitFactor >= 1.20 &&
                r.Trades >= 15 &&
                r.MaxDrawdownPercent <= 35.0);

            if (wfResult.ProductionApproved && (!mcSurvives || stabilityPlateauCount < 3))
            {
                wfResult.ProductionApproved = false;
                wfResult.ApprovalRationale += $" STRICT PRODUCTION GATE: rejected because Monte Carlo survives={mcSurvives} and stability plateau count={stabilityPlateauCount}/3.";
            }
            else if (wfResult.ProductionApproved)
            {
                wfResult.ApprovalRationale += $" STRICT PRODUCTION GATE: passed with Monte Carlo survival and {stabilityPlateauCount} robust plateau cells.";
            }


            // Candidate Store: Save candidate if approved
            var candidateStore = new ValidationCandidateStore();
            var savedCandidate = candidateStore.SaveIfApproved(wfResult);
            if (savedCandidate != null)
            {
                Console.WriteLine($"[SignalR] Production candidate saved for human sign-off at: {candidateStore.Path}");
            }

            // Deliberately do not auto-promote validation output into live_strategy_config.json.
            // Production deployment must remain a human sign-off step after reviewing
            // walk-forward, Monte Carlo, clustering, and symbol-specific risk context.
            progressReporter.Report(new ValidationProgress("Complete", 4, 4, "Validation complete!"));

            // Send final result
            var payload = new
            {
                symbol = symbolUpper,
                sweepResult = new {
                    results = sweepResult.Results.Select(r => new {
                        parameters = r.Parameters,
                        key = r.Key,
                        netProfit = r.NetProfit,
                        sharpeRatio = r.SharpeRatio,
                        maxDrawdownPercent = r.MaxDrawdownPercent,
                        profitFactor = r.ProfitFactor,
                        winRate = r.WinRate,
                        trades = r.Trades
                    }),
                    heatmap = sweepResult.Heatmap
                },
                mcResults,
                clusterResult,
                wfResult = new {
                    windows = wfResult.Windows,
                    aggregateOosEquity = wfResult.AggregateOosEquity,
                    oosNetProfit = wfResult.OosNetProfit,
                    oosProfitFactor = wfResult.OosProfitFactor,
                    oosExpectancy = wfResult.OosExpectancy,
                    oosWinRate = wfResult.OosWinRate,
                    oosMaxDrawdownPercent = wfResult.OosMaxDrawdownPercent,
                    oosEquitySlope = wfResult.OosEquitySlope,
                    recommendedParameters = wfResult.RecommendedParameters,
                    productionApproved = wfResult.ProductionApproved,
                    approvalRationale = wfResult.ApprovalRationale,
                    savedCandidateStatus = savedCandidate?.Status,
                    monteCarloSurvives = mcSurvives,
                    stabilityPlateauCount = stabilityPlateauCount
                }
            };

            Console.WriteLine($"[Validation Complete] Symbol: {symbolUpper} | Approved: {wfResult.ProductionApproved} | Net: R {wfResult.OosNetProfit:0.00} | Expectancy: R {wfResult.OosExpectancy:0.00}/trade | WR: {wfResult.OosWinRate:0.0}% | DD: {wfResult.OosMaxDrawdownPercent:0.0}% | Rationale: {wfResult.ApprovalRationale}");

            await pulseHub.Clients.All.SendAsync("ReceiveValidationResult", payload);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[Validation Failed] Error: {ex.Message}");
            await pulseHub.Clients.All.SendAsync("ReceiveValidationResult", new { error = $"Validation failed: {ex.Message}" });
        }
    });

    return Results.Ok(new { message = $"Validation suite triggered in background for {symbol}." });
}).RequireRateLimiting("private-write");

_ = Task.Run(() => StartMt5CandleRelayListenerAsync(candleCache));

app.Run();

public record CandleSnapshot(
    string Symbol,
    string Timeframe,
    string Source,
    bool Stale,
    IReadOnlyList<CandleWire> Candles);

public record CandleWire(
    [property: JsonPropertyName("time")] long Time,
    [property: JsonPropertyName("open")] double Open,
    [property: JsonPropertyName("high")] double High,
    [property: JsonPropertyName("low")] double Low,
    [property: JsonPropertyName("close")] double Close,
    [property: JsonPropertyName("volume")] double Volume);

// Data model representing market microstructure features in the online cache.
public record MarketFeature
{
    public string Symbol { get; set; } = string.Empty;
    public double Cvd { get; set; }
    public double Microprice { get; set; }
    public double Obi { get; set; }
    public double Price { get; set; }
    public double Timestamp { get; set; }

    // Optional options-exposure context streamed by the Bytewax pipeline.
    [JsonPropertyName("net_gex")]
    public double NetGex { get; set; }

    [JsonPropertyName("gamma_flip")]
    public double GammaFlip { get; set; }

    [JsonPropertyName("footprint_rows")]
    public List<FootprintRow> FootprintRows { get; set; } = new();

    [JsonPropertyName("open_interest_pins")]
    public List<double> OpenInterestPins { get; set; } = new();

    [JsonPropertyName("depth")]
    public JsonElement? Depth { get; set; }
}
