using Microsoft.AspNetCore.SignalR;
using System.Collections.Concurrent;

namespace TBBFX.SignalRFeatureStore.Hubs
{
    /// <summary>
    /// Real-time market-pulse hub. Streams live Volume Delta (CVD) and Order
    /// Block Imbalance (OBI) updates to the web terminal, and manages incoming
    /// connections from the companion .NET MAUI mobile client.
    ///
    /// Server -> client events:
    ///   PulseWelcome        : handshake on connect
    ///   ReceivePulse        : full feature record (CVD, OBI, microprice, GEX...)
    ///   ReceiveVolumeDelta  : { symbol, cumulativeDelta, instantaneousDelta, timestamp }
    ///   ReceiveObiUpdate    : { symbol, obi, timestamp }
    /// </summary>
    public class MarketPulseHub : Hub
    {
        // Connected clients (web + mobile), keyed by connection id, for observability.
        private static readonly ConcurrentDictionary<string, ClientInfo> Connected = new();

        public static int ConnectedCount => Connected.Count;
        public static int MobileCount => Connected.Values.Count(c => c.IsMobile);

        public override async Task OnConnectedAsync()
        {
            Connected[Context.ConnectionId] = new ClientInfo
            {
                ConnectionId = Context.ConnectionId,
                ConnectedAtUtc = DateTimeOffset.UtcNow
            };

            await Clients.Caller.SendAsync("PulseWelcome", new
            {
                message = "Connected to TBBFX MarketPulseHub",
                connectionId = Context.ConnectionId,
                totalClients = Connected.Count
            });

            await base.OnConnectedAsync();
        }

        public override async Task OnDisconnectedAsync(Exception? exception)
        {
            Connected.TryRemove(Context.ConnectionId, out _);
            await base.OnDisconnectedAsync(exception);
        }

        /// <summary>Subscribe this connection to a symbol's pulse stream.</summary>
        public async Task JoinSymbolGroup(string symbol)
        {
            var sym = symbol.ToUpperInvariant();
            await Groups.AddToGroupAsync(Context.ConnectionId, sym);
            if (Connected.TryGetValue(Context.ConnectionId, out var info))
            {
                info.Symbols.Add(sym);
            }
            await Clients.Caller.SendAsync("Subscribed", new { symbol = sym, message = $"Subscribed to {sym} pulse stream." });
        }

        public async Task LeaveSymbolGroup(string symbol)
        {
            var sym = symbol.ToUpperInvariant();
            await Groups.RemoveFromGroupAsync(Context.ConnectionId, sym);
            if (Connected.TryGetValue(Context.ConnectionId, out var info))
            {
                info.Symbols.Remove(sym);
            }
            await Clients.Caller.SendAsync("Unsubscribed", new { symbol = sym });
        }

        /// <summary>
        /// Called by the .NET MAUI mobile client right after connecting so the
        /// server can identify mobile devices and place them in the "mobile"
        /// group for targeted, bandwidth-conscious broadcasts.
        /// </summary>
        public async Task RegisterMobileClient(string deviceId, string platform)
        {
            if (Connected.TryGetValue(Context.ConnectionId, out var info))
            {
                info.IsMobile = true;
                info.DeviceId = deviceId;
                info.Platform = platform;
            }
            await Groups.AddToGroupAsync(Context.ConnectionId, "mobile");
            await Clients.Caller.SendAsync("MobileRegistered", new
            {
                deviceId,
                platform,
                message = "Mobile client registered with TBBFX MarketPulseHub.",
                mobileClients = MobileCount
            });
        }

        /// <summary>Lightweight stats for the terminal's "LIVE STREAM ACTIVE" badge.</summary>
        public Task<object> GetConnectionStats() =>
            Task.FromResult<object>(new { totalClients = ConnectedCount, mobileClients = MobileCount });

        private sealed class ClientInfo
        {
            public string ConnectionId { get; set; } = string.Empty;
            public DateTimeOffset ConnectedAtUtc { get; set; }
            public bool IsMobile { get; set; }
            public string? DeviceId { get; set; }
            public string? Platform { get; set; }
            public HashSet<string> Symbols { get; } = new();
        }
    }
}
