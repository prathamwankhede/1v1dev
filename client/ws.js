/* ═══════════════════════════════════════════════════
   1v1dev — WebSocket transport
   Owns the socket and reconnect; everything the app does on open, close
   and message comes in as handlers from app.js, so this module imports
   nothing and anything may import `ws` without creating a cycle.
   ═══════════════════════════════════════════════════ */

export let ws = null;

export function connect(handlers) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => handlers.onOpen();

  ws.onclose = () => {
    handlers.onClose();
    setTimeout(() => connect(handlers), 3000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handlers.onMessage(data);
    } catch (err) {
      console.error('Error parsing message:', err);
    }
  };

  ws.onerror = () => {
    ws.close();
  };
}
