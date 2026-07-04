using System.Collections.Concurrent;
using System.Text.Json.Serialization;

public sealed class OrderflowGammaLevelRefinery
{
    private readonly ConcurrentDictionary<string, List<InstitutionalConfluenceLevel>> _levels = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, MarketFeature> _lastFeature = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, double> _lastCvd = new(StringComparer.OrdinalIgnoreCase);
    private static readonly TimeSpan LevelTtl = TimeSpan.FromHours(24);

    public IReadOnlyCollection<InstitutionalConfluenceLevel> Process(MarketFeature feature)
    {
        var symbol = feature.Symbol.ToUpperInvariant();
        var now = DateTime.UtcNow;
        var currentPrice = feature.Microprice > 0 ? feature.Microprice : feature.Price;
        var last = _lastFeature.GetValueOrDefault(symbol);
        var previousCvd = _lastCvd.GetValueOrDefault(symbol, feature.Cvd);
        var cvdDelta = feature.Cvd - previousCvd;
        _lastCvd[symbol] = feature.Cvd;
        _lastFeature[symbol] = feature;

        var active = _levels.GetOrAdd(symbol, _ => new List<InstitutionalConfluenceLevel>());
        lock (active)
        {
            active.RemoveAll(level =>
                now - level.Timestamp > LevelTtl ||
                IsMitigated(level, currentPrice, GetFivePipWindow(symbol)) ||
                !IsSaneLevelPrice(symbol, level.Price, currentPrice));

            var emitted = new List<InstitutionalConfluenceLevel>();

            if (feature.GammaFlip > 0 && IsSaneLevelPrice(symbol, feature.GammaFlip, currentPrice))
            {
                emitted.Add(Upsert(active, new InstitutionalConfluenceLevel
                {
                    Symbol = symbol,
                    Price = feature.GammaFlip,
                    Type = LevelType.GammaFlip,
                    StrengthScore = Math.Clamp(62 + Math.Log10(Math.Abs(feature.NetGex) + 10) * 4, 65, 92),
                    Timestamp = now,
                    Description = "Zero-gamma threshold; volatility regime boundary",
                    Source = "SVI_GEX"
                }));
            }

            foreach (var pin in ResolveOpenInterestPins(feature, currentPrice))
            {
                emitted.Add(Upsert(active, new InstitutionalConfluenceLevel
                {
                    Symbol = symbol,
                    Price = pin,
                    Type = LevelType.OpenInterestPin,
                    StrengthScore = IsNear(pin, feature.GammaFlip, GetFivePipWindow(symbol) * 2) ? 88 : 72,
                    Timestamp = now,
                    Description = "Open-interest strike magnet / dealer hedge shelf",
                    Source = "OI_PIN"
                }));
            }

            foreach (var row in ResolveFootprintRows(feature, currentPrice, cvdDelta))
            {
                var rowLevels = AnalyzeFootprintRow(symbol, row, feature, last, cvdDelta, now, active);
                emitted.AddRange(rowLevels);
            }

            return emitted
                .Where(level => level.StrengthScore >= 70)
                .OrderByDescending(level => level.StrengthScore)
                .Take(8)
                .ToList();
        }
    }

    public IReadOnlyCollection<InstitutionalConfluenceLevel> GetActiveLevels(string symbol)
    {
        var sym = symbol.ToUpperInvariant();
        if (!_levels.TryGetValue(sym, out var levels))
        {
            return Array.Empty<InstitutionalConfluenceLevel>();
        }

        var now = DateTime.UtcNow;
        lock (levels)
        {
            levels.RemoveAll(level => now - level.Timestamp > LevelTtl);
            return levels
                .OrderByDescending(level => level.StrengthScore)
                .ThenByDescending(level => level.Timestamp)
                .ToList();
        }
    }

    private IEnumerable<InstitutionalConfluenceLevel> AnalyzeFootprintRow(
        string symbol,
        FootprintRow row,
        MarketFeature feature,
        MarketFeature? previous,
        double cvdDelta,
        DateTime now,
        List<InstitutionalConfluenceLevel> active)
    {
        var results = new List<InstitutionalConfluenceLevel>();
        var bid = Math.Max(0, row.BidVolume);
        var ask = Math.Max(0, row.AskVolume);
        var maxSide = Math.Max(bid, ask);
        var minSide = Math.Max(Math.Min(bid, ask), 1);
        var imbalanceRatio = maxSide / minSide;
        var pipWindow = GetFivePipWindow(symbol);
        var nearGamma = feature.GammaFlip > 0 && IsNear(row.Price, feature.GammaFlip, pipWindow);
        var nearPin = ResolveOpenInterestPins(feature, feature.Price).Any(pin => IsNear(row.Price, pin, pipWindow));

        if (imbalanceRatio >= 3.0)
        {
            var isAskDominant = ask > bid;
            var levelType = isAskDominant ? LevelType.BuyerImbalance : LevelType.SellerImbalance;
            var confluenceBoost = nearGamma || nearPin ? 26 : 0;
            results.Add(Upsert(active, new InstitutionalConfluenceLevel
            {
                Symbol = symbol,
                Price = row.Price,
                Type = levelType,
                StrengthScore = Math.Clamp(58 + imbalanceRatio * 7 + confluenceBoost, 70, 96),
                Timestamp = now,
                Description = isAskDominant
                    ? "Aggressive buyers lifting offers; monitor for failed continuation"
                    : "Aggressive sellers hitting bids; monitor for trapped shorts",
                Source = "FOOTPRINT_DELTA"
            }));
        }

        var price = feature.Microprice > 0 ? feature.Microprice : feature.Price;
        var previousPrice = previous?.Microprice > 0 ? previous.Microprice : previous?.Price ?? price;
        var sellAbsorption = bid > ask * 2.5 && cvdDelta < 0 && price >= previousPrice - pipWindow && feature.Obi >= -0.15;
        var buyAbsorption = ask > bid * 2.5 && cvdDelta > 0 && price <= previousPrice + pipWindow && feature.Obi <= 0.15;

        if (sellAbsorption || buyAbsorption)
        {
            var maxConfluence = nearGamma || nearPin;
            results.Add(Upsert(active, new InstitutionalConfluenceLevel
            {
                Symbol = symbol,
                Price = row.Price,
                Type = LevelType.AbsorptionNode,
                StrengthScore = maxConfluence ? 100 : Math.Clamp(76 + Math.Abs(feature.Obi) * 18 + imbalanceRatio * 3, 78, 94),
                Timestamp = now,
                Description = sellAbsorption
                    ? "Sell delta absorbed without continuation; demand floor candidate"
                    : "Buy delta absorbed without continuation; supply cap candidate",
                Source = maxConfluence ? "GEX_OI_FOOTPRINT_CONFLUENCE" : "FOOTPRINT_ABSORPTION"
            }));
        }

        return results;
    }

    private static InstitutionalConfluenceLevel Upsert(List<InstitutionalConfluenceLevel> active, InstitutionalConfluenceLevel next)
    {
        var existing = active.FirstOrDefault(level =>
            level.Symbol.Equals(next.Symbol, StringComparison.OrdinalIgnoreCase) &&
            level.Type == next.Type &&
            Math.Abs(level.Price - next.Price) <= GetFivePipWindow(next.Symbol));

        if (existing == null)
        {
            active.Add(next);
            return next;
        }

        existing.Price = next.Price;
        existing.StrengthScore = Math.Max(existing.StrengthScore * 0.72 + next.StrengthScore * 0.28, next.StrengthScore);
        existing.Timestamp = next.Timestamp;
        existing.Description = next.Description;
        existing.Source = next.Source;
        return existing;
    }

    private static IEnumerable<FootprintRow> ResolveFootprintRows(MarketFeature feature, double currentPrice, double cvdDelta)
    {
        if (feature.FootprintRows is { Count: > 0 })
        {
            return feature.FootprintRows
                .Where(row => row.Price > 0)
                .OrderBy(row => Math.Abs(row.Price - currentPrice))
                .Take(20)
                .ToList();
        }

        var absDelta = Math.Abs(cvdDelta);
        if (currentPrice <= 0 || absDelta < 1)
        {
            return Array.Empty<FootprintRow>();
        }

        var buyPressure = cvdDelta > 0 || feature.Obi > 0;
        return new[]
        {
            new FootprintRow
            {
                Price = currentPrice,
                BidVolume = buyPressure ? Math.Max(1, absDelta * 0.28) : Math.Max(1, absDelta),
                AskVolume = buyPressure ? Math.Max(1, absDelta) : Math.Max(1, absDelta * 0.28),
                Delta = cvdDelta
            }
        };
    }

    private static IEnumerable<double> ResolveOpenInterestPins(MarketFeature feature, double currentPrice)
    {
        if (feature.OpenInterestPins is { Count: > 0 })
        {
            return feature.OpenInterestPins.Where(pin => pin > 0).Take(8);
        }

        if (currentPrice <= 0)
        {
            return Array.Empty<double>();
        }

        var step = GetOpenInterestStep(feature.Symbol, currentPrice);
        var anchor = Math.Round(currentPrice / step) * step;
        return new[] { anchor - step, anchor, anchor + step }.Where(pin => pin > 0);
    }

    private static double GetOpenInterestStep(string symbol, double price)
    {
        if (symbol.Contains("US30", StringComparison.OrdinalIgnoreCase)) return 100;
        if (symbol.Contains("USTEC", StringComparison.OrdinalIgnoreCase)) return 50;
        if (symbol.Contains("XAU", StringComparison.OrdinalIgnoreCase)) return 5;
        if (symbol.Contains("JPY", StringComparison.OrdinalIgnoreCase)) return 0.25;
        return price > 10 ? 1 : 0.005;
    }

    private static double GetFivePipWindow(string symbol)
    {
        if (symbol.Contains("US30", StringComparison.OrdinalIgnoreCase)) return 5.0;
        if (symbol.Contains("USTEC", StringComparison.OrdinalIgnoreCase)) return 2.5;
        if (symbol.Contains("XAU", StringComparison.OrdinalIgnoreCase)) return 0.50;
        if (symbol.Contains("JPY", StringComparison.OrdinalIgnoreCase)) return 0.05;
        return 0.00050;
    }

    private static bool IsNear(double a, double b, double tolerance) => b > 0 && Math.Abs(a - b) <= tolerance;

    private static bool IsSaneLevelPrice(string symbol, double levelPrice, double currentPrice)
    {
        if (levelPrice <= 0 || currentPrice <= 0)
        {
            return false;
        }

        var maxDistance = GetFivePipWindow(symbol) * 160;
        var maxPercentDistance = currentPrice * 0.08;
        if (symbol.Contains("US30", StringComparison.OrdinalIgnoreCase) ||
            symbol.Contains("USTEC", StringComparison.OrdinalIgnoreCase) ||
            symbol.Contains("XAU", StringComparison.OrdinalIgnoreCase))
        {
            maxPercentDistance = currentPrice * 0.15;
        }

        return Math.Abs(levelPrice - currentPrice) <= Math.Max(maxDistance, maxPercentDistance);
    }

    private static bool IsMitigated(InstitutionalConfluenceLevel level, double currentPrice, double window)
    {
        if (currentPrice <= 0) return false;
        if (level.Type is LevelType.GammaFlip or LevelType.OpenInterestPin) return false;
        return Math.Abs(currentPrice - level.Price) <= window * 0.35 && DateTime.UtcNow - level.Timestamp > TimeSpan.FromMinutes(10);
    }
}

[JsonConverter(typeof(JsonStringEnumConverter))]
public enum LevelType
{
    GammaFlip,
    OpenInterestPin,
    BuyerImbalance,
    SellerImbalance,
    AbsorptionNode
}

public class InstitutionalConfluenceLevel
{
    public string Symbol { get; set; } = string.Empty;
    public double Price { get; set; }
    public LevelType Type { get; set; }
    public string TypeName => Type.ToString();
    public double StrengthScore { get; set; }
    public DateTime Timestamp { get; set; }
    public string Description { get; set; } = string.Empty;
    public string Source { get; set; } = string.Empty;
}

public sealed class FootprintRow
{
    public double Price { get; set; }

    [JsonPropertyName("bid_volume")]
    public double BidVolume { get; set; }

    [JsonPropertyName("ask_volume")]
    public double AskVolume { get; set; }

    public double Delta { get; set; }
}
