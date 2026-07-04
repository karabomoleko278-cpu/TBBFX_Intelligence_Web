// =============================================================================
// TBBFX MarketPulseClient  —  companion .NET MAUI mobile client
// =============================================================================
// Drop-in service for the TBBFX.App MAUI project. It does two things:
//
//   1. Connects to the MarketPulseHub SignalR endpoint and receives live
//      Volume Delta + OBI pushes (so the phone mirrors the web terminal).
//   2. Queries the online feature cache on demand (GET /features/latest/{sym})
//      so the device's *local* ML model can leverage the web extension's
//      analytical features (CVD, OBI, Microprice, GEX) to optimize execution.
//
// Requires NuGet package in the MAUI project:
//   <PackageReference Include="Microsoft.AspNetCore.SignalR.Client" Version="10.*" />
//
// Register in MauiProgram.cs:
//   builder.Services.AddSingleton(new MarketPulseClient("http://10.0.2.2:5000"));
//   // 10.0.2.2 = host loopback from the Android emulator; use the LAN IP on device.
// =============================================================================

using System.Net.Http.Json;
using Microsoft.AspNetCore.SignalR.Client;

namespace TBBFX.Mobile.Services;

public sealed class MarketPulseClient : IAsyncDisposable
{
    private readonly string _baseUrl;
    private readonly HttpClient _http;
    private HubConnection? _hub;

    /// <summary>Raised with the running + instantaneous Volume Delta for a symbol.</summary>
    public event Action<VolumeDelta>? VolumeDeltaReceived;

    /// <summary>Raised with each Order Block Imbalance update.</summary>
    public event Action<ObiUpdate>? ObiReceived;

    /// <summary>Raised with the full feature record (feeds the on-device ML model).</summary>
    public event Action<MarketFeature>? PulseReceived;

    public event Action<bool>? ConnectionStateChanged;

    public HubConnectionState State => _hub?.State ?? HubConnectionState.Disconnected;

    public MarketPulseClient(string baseUrl, HttpClient? http = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        _http = http ?? new HttpClient { BaseAddress = new Uri(_baseUrl) };
    }

    public async Task ConnectAsync(string deviceId, string platform, CancellationToken ct = default)
    {
        _hub = new HubConnectionBuilder()
            .WithUrl($"{_baseUrl}/hub/marketpulse")
            .WithAutomaticReconnect()
            .Build();

        _hub.On<VolumeDelta>("ReceiveVolumeDelta", d => VolumeDeltaReceived?.Invoke(d));
        _hub.On<ObiUpdate>("ReceiveObiUpdate", o => ObiReceived?.Invoke(o));
        _hub.On<MarketFeature>("ReceivePulse", f => PulseReceived?.Invoke(f));
        _hub.On<MarketFeature>("ReceiveAnyPulse", f => PulseReceived?.Invoke(f));

        _hub.Reconnected += _ => { ConnectionStateChanged?.Invoke(true); return Task.CompletedTask; };
        _hub.Closed += _ => { ConnectionStateChanged?.Invoke(false); return Task.CompletedTask; };

        await _hub.StartAsync(ct);
        ConnectionStateChanged?.Invoke(true);

        // Identify ourselves so the server can target the "mobile" group.
        await _hub.InvokeAsync("RegisterMobileClient", deviceId, platform, ct);
    }

    /// <summary>Subscribe to a symbol's live pulse stream.</summary>
    public Task SubscribeAsync(string symbol, CancellationToken ct = default) =>
        _hub is null
            ? Task.CompletedTask
            : _hub.InvokeAsync("JoinSymbolGroup", symbol.ToUpperInvariant(), ct);

    public Task UnsubscribeAsync(string symbol, CancellationToken ct = default) =>
        _hub is null
            ? Task.CompletedTask
            : _hub.InvokeAsync("LeaveSymbolGroup", symbol.ToUpperInvariant(), ct);

    /// <summary>
    /// Pull the latest cached features for a symbol so the on-device ML model
    /// can score an entry immediately, without waiting for the next push.
    /// </summary>
    public async Task<MarketFeature?> GetLatestFeatureAsync(string symbol, CancellationToken ct = default)
    {
        try
        {
            return await _http.GetFromJsonAsync<MarketFeature>(
                $"{_baseUrl}/features/latest/{symbol.ToUpperInvariant()}", ct);
        }
        catch (HttpRequestException)
        {
            return null; // not yet in cache
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_hub is not null)
        {
            await _hub.DisposeAsync();
        }
        _http.Dispose();
    }
}

// --- DTOs (match the JSON emitted by Program.cs / the Python factory) --------

public sealed record MarketFeature
{
    public string Symbol { get; init; } = string.Empty;
    public double Cvd { get; init; }
    public double Microprice { get; init; }
    public double Obi { get; init; }
    public double Price { get; init; }
    public double Timestamp { get; init; }
    public double Net_Gex { get; init; }
    public double Gamma_Flip { get; init; }
}

public sealed record VolumeDelta
{
    public string Symbol { get; init; } = string.Empty;
    public double CumulativeDelta { get; init; }
    public double InstantaneousDelta { get; init; }
    public double Timestamp { get; init; }
}

public sealed record ObiUpdate
{
    public string Symbol { get; init; } = string.Empty;
    public double Obi { get; init; }
    public double Timestamp { get; init; }
}
