/**
 * Bob Realtime Voice Test — browser harness for the OpenAI Realtime bridge.
 *
 * Audio: raw PCM16 little-endian mono at 24 kHz both directions.
 * - Mic:  AudioContext (native rate) → ScriptProcessor → downsample to 24k → Int16 → binary frames
 * - Speaker: binary Int16 24k → Float32 → AudioBuffer @24k (AudioContext resamples on play)
 *
 * Control frames (JSON text):
 *   Client → Server: {type:"start", instructions, voice, max_duration}
 *                    {type:"stop"}
 *   Server → Client: {type:"transcript_delta", text}
 *                    {type:"barge_in"}
 *                    {type:"done", transcript, duration_seconds, tool_calls, end_reason}
 *                    {type:"error", message}
 */

const SAMPLE_RATE = 24000;

const ui = {
  instructions: document.getElementById('instructions'),
  voice: document.getElementById('voice'),
  maxdur: document.getElementById('maxdur'),
  connect: document.getElementById('connect'),
  disconnect: document.getElementById('disconnect'),
  status: document.getElementById('status'),
  transcript: document.getElementById('transcript'),
  tools: document.getElementById('tools'),
  result: document.getElementById('result'),
};

const state = {
  ws: null,
  audioCtx: null,
  micStream: null,
  scriptNode: null,
  micSource: null,
  nextPlayTime: 0,
  sources: [],
  transcriptText: '',
};

function setStatus(text, cls) {
  ui.status.textContent = text;
  ui.status.className = cls || '';
}

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/voice/realtime`;
}

// Float32 [-1,1] → Int16 LE, downsampled from inputRate to SAMPLE_RATE.
function downsampleToPcm16(input, inputRate) {
  if (inputRate === SAMPLE_RATE) {
    return floatToPcm16(input);
  }
  const ratio = inputRate / SAMPLE_RATE;
  const newLen = Math.floor(input.length / ratio);
  const out = new Int16Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const idx = i * ratio;
    const lo = Math.floor(idx);
    const hi = Math.min(lo + 1, input.length - 1);
    const frac = idx - lo;
    const v = input[lo] * (1 - frac) + input[hi] * frac;
    out[i] = Math.max(-32768, Math.min(32767, v * 32767));
  }
  return out;
}

function floatToPcm16(input) {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    out[i] = Math.max(-32768, Math.min(32767, input[i] * 32767));
  }
  return out;
}

function playPcm16(buffer) {
  if (!state.audioCtx) return;
  const view = new Int16Array(buffer);
  const floats = new Float32Array(view.length);
  for (let i = 0; i < view.length; i++) floats[i] = view[i] / 32768;

  const audioBuf = state.audioCtx.createBuffer(1, floats.length, SAMPLE_RATE);
  audioBuf.copyToChannel(floats, 0);

  const src = state.audioCtx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(state.audioCtx.destination);

  const now = state.audioCtx.currentTime;
  if (state.nextPlayTime < now) state.nextPlayTime = now;
  src.start(state.nextPlayTime);
  state.nextPlayTime = state.nextPlayTime + audioBuf.duration;
  state.sources.push(src);
  src.onended = () => {
    const i = state.sources.indexOf(src);
    if (i >= 0) state.sources.splice(i, 1);
  };
}

function stopPlayback() {
  for (const s of state.sources) {
    try { s.stop(); } catch (_) {}
  }
  state.sources = [];
  state.nextPlayTime = 0;
}

async function startSession() {
  const instructions = ui.instructions.value.trim();
  if (!instructions) {
    setStatus('Enter instructions first', 'error');
    return;
  }
  ui.connect.disabled = true;
  setStatus('Connecting…');

  try {
    state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (state.audioCtx.state === 'suspended') await state.audioCtx.resume();

    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    state.ws = new WebSocket(wsUrl());
    state.ws.binaryType = 'arraybuffer';

    state.ws.onopen = () => {
      state.ws.send(JSON.stringify({
        type: 'start',
        instructions,
        voice: ui.voice.value,
        max_duration: parseInt(ui.maxdur.value, 10) || 120,
      }));
      startMicCapture();
      setStatus('Connected — listening', 'connected');
      ui.disconnect.disabled = false;
      state.transcriptText = '';
      ui.transcript.textContent = '…';
      ui.tools.style.display = 'none';
      ui.tools.textContent = '';
      ui.result.style.display = 'none';
    };

    state.ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        playPcm16(event.data);
      } else {
        handleControl(event.data);
      }
    };

    state.ws.onclose = () => {
      setStatus('Disconnected');
      teardown();
    };
    state.ws.onerror = () => setStatus('Connection error', 'error');
  } catch (e) {
    setStatus(`Failed: ${e.message}`, 'error');
    teardown();
    ui.connect.disabled = false;
  }
}

function startMicCapture() {
  const ctxRate = state.audioCtx.sampleRate;
  state.micSource = state.audioCtx.createMediaStreamSource(state.micStream);
  // 2048-sample buffer at ctx rate ≈ 40ms at 48kHz.
  state.scriptNode = state.audioCtx.createScriptProcessor(2048, 1, 1);
  state.scriptNode.onaudioprocess = (event) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm16 = downsampleToPcm16(input, ctxRate);
    if (pcm16.length > 0) state.ws.send(pcm16.buffer);
  };
  state.micSource.connect(state.scriptNode);
  state.scriptNode.connect(state.audioCtx.destination); // required for the node to fire
}

function handleControl(data) {
  let msg;
  try { msg = JSON.parse(data); } catch (_) { return; }
  switch (msg.type) {
    case 'transcript_delta':
      state.transcriptText += msg.text;
      ui.transcript.textContent = state.transcriptText;
      break;
    case 'barge_in':
      stopPlayback();
      break;
    case 'done':
      ui.result.style.display = 'block';
      ui.result.textContent = JSON.stringify({
        end_reason: msg.end_reason,
        error_message: msg.error_message || null,
        duration_seconds: msg.duration_seconds,
        tool_calls: msg.tool_calls,
      }, null, 2);
      if (msg.transcript) {
        state.transcriptText = msg.transcript;
        ui.transcript.textContent = msg.transcript;
      }
      if (msg.end_reason === 'error') {
        setStatus(`Error: ${msg.error_message || 'unknown'}`, 'error');
      } else {
        setStatus(`Done — ${msg.end_reason} (${msg.duration_seconds}s)`);
      }
      break;
    case 'error':
      setStatus(msg.message || 'Server error', 'error');
      break;
  }
}

function endSession() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'stop' }));
  }
}

function teardown() {
  if (state.scriptNode) { try { state.scriptNode.disconnect(); } catch (_) {} state.scriptNode = null; }
  if (state.micSource) { try { state.micSource.disconnect(); } catch (_) {} state.micSource = null; }
  if (state.micStream) { state.micStream.getTracks().forEach(t => t.stop()); state.micStream = null; }
  stopPlayback();
  ui.connect.disabled = false;
  ui.disconnect.disabled = true;
}

ui.connect.addEventListener('click', startSession);
ui.disconnect.addEventListener('click', endSession);
