/**
 * Bob-initiated voice session page (persona mode).
 *
 * Opened from a link Bob sends in chat: /voice/session.html?id=<token>.
 * The token resolves server-side to Bob's persona + chat context — no prompt
 * editing here, just connect, talk, hang up. Transcript is summarised and sent
 * back to the chat on hang-up.
 */

const params = new URLSearchParams(location.search);
const sessionId = params.get('id');

const ui = {
  status: document.getElementById('status'),
  transcript: document.getElementById('transcript'),
  connect: document.getElementById('connect'),
  hangup: document.getElementById('hangup'),
};

const state = {
  ws: null,
  audioCtx: null,
  micStream: null,
  micCapture: null,
  playback: window.BobAudio.createPlaybackState(),
  transcriptText: '',
};

function setStatus(text, cls) {
  ui.status.textContent = text;
  ui.status.className = cls || '';
}

if (!sessionId) {
  setStatus('Missing session link — ask Bob to send a new one.', 'error');
  ui.connect.disabled = true;
}

async function connect() {
  ui.connect.disabled = true;
  setStatus('Connecting…');
  try {
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (state.audioCtx.state === 'suspended') await state.audioCtx.resume();
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    state.ws = new WebSocket(BobAudio.wsUrl('/voice/realtime'));
    state.ws.binaryType = 'arraybuffer';
    state.ws.onopen = () => {
      state.ws.send(JSON.stringify({ type: 'start', session_id: sessionId }));
      state.micCapture = BobAudio.startMicCapture(state.audioCtx, state.micStream, (chunk) => {
        if (state.ws.readyState === WebSocket.OPEN) state.ws.send(chunk);
      });
      setStatus('Connected — talking to Bob', 'connected');
      ui.hangup.style.display = 'block';
      ui.connect.style.display = 'none';
      ui.transcript.style.display = 'block';
      ui.transcript.textContent = '…';
    };
    state.ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        BobAudio.playPcm16(event.data, state.audioCtx, state.playback);
      } else {
        handleControl(event.data);
      }
    };
    state.ws.onclose = () => {
      // Don't overwrite an error status (e.g. expired/invalid link) with "Call ended".
      if (state.transcriptText === '' && !ui.status.classList.contains('error')) {
        setStatus('Call ended', '');
      }
      teardown();
    };
    state.ws.onerror = () => setStatus('Connection error', 'error');
  } catch (e) {
    setStatus(`Failed: ${e.message}`, 'error');
    teardown();
    ui.connect.disabled = false;
  }
}

function handleControl(data) {
  let msg;
  try { msg = JSON.parse(data); } catch (_) { return; }
  switch (msg.type) {
    case 'transcript_delta':
      state.transcriptText += msg.text;
      ui.transcript.textContent = state.transcriptText;
      break;
    case 'user_transcript':
      // Server sends the full recognised utterance; separate it from the agent's streaming text.
      state.transcriptText += `\n\nYou: ${msg.text}\n\n`;
      ui.transcript.textContent = state.transcriptText;
      break;
    case 'barge_in':
      BobAudio.stopPlayback(state.playback);
      break;
    case 'done':
      if (msg.transcript) {
        state.transcriptText = msg.transcript;
        ui.transcript.textContent = msg.transcript;
      }
      if (msg.end_reason === 'error') {
        setStatus(`Error: ${msg.error_message || 'unknown'}`, 'error');
      } else {
        setStatus('Call ended — summary sent to chat', '');
      }
      break;
    case 'error':
      setStatus(msg.message || 'Server error', 'error');
      break;
  }
}

function hangup() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'stop' }));
  }
}

function teardown() {
  if (state.micCapture) { state.micCapture.disconnect(); state.micCapture = null; }
  if (state.micStream) { state.micStream.getTracks().forEach(t => t.stop()); state.micStream = null; }
  BobAudio.stopPlayback(state.playback);
  ui.connect.style.display = 'block';
  ui.connect.disabled = false;
  ui.hangup.style.display = 'none';
}

ui.connect.addEventListener('click', connect);
ui.hangup.addEventListener('click', hangup);
