using Microsoft.AspNetCore.SignalR;
using System.Threading.Tasks;

namespace TBBFX.SignalRFeatureStore.Hubs
{
    public class FeatureHub : Hub
    {
        public async Task JoinSymbolGroup(string symbol)
        {
            await Groups.AddToGroupAsync(Context.ConnectionId, symbol.ToUpper());
            await Clients.Caller.SendAsync("Subscribed", $"Successfully subscribed to real-time streams for {symbol.ToUpper()}");
        }

        public async Task LeaveSymbolGroup(string symbol)
        {
            await Groups.RemoveFromGroupAsync(Context.ConnectionId, symbol.ToUpper());
            await Clients.Caller.SendAsync("Unsubscribed", $"Successfully unsubscribed from streams for {symbol.ToUpper()}");
        }
    }
}
