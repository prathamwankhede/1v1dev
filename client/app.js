const statusPill = document.getElementById('status-pill');
const playerCount = document.getElementById('player-count');
const lobbyMessage = document.getElementById('lobby-message');

let ws;
const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${protocol}//${window.location.host}/ws`;

function connect() {
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    statusPill.textContent = 'Online';
    statusPill.className = 'status-pill online';
    lobbyMessage.textContent = 'Connected to arena. Waiting for matchmaking...';
    lobbyMessage.classList.remove('pulse');
  };

  ws.onclose = () => {
    statusPill.textContent = 'Offline';
    statusPill.className = 'status-pill offline';
    playerCount.textContent = '0';
    lobbyMessage.textContent = 'Disconnected. Reconnecting...';
    lobbyMessage.classList.add('pulse');
    setTimeout(connect, 3000);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'playerCount') {
        playerCount.textContent = data.count;
      }
    } catch (err) {
      console.error('Error parsing message:', err);
    }
  };

  ws.onerror = (err) => {
    console.error('WebSocket error:', err);
    ws.close();
  };
}

connect();
