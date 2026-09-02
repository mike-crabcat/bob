/**
 * Shared PCM16 24kHz audio helpers for the realtime voice pages.
 *
 * Used by realtime.html (manual test harness) and session.html (Bob-initiated
 * persona calls). Attached to window.BobAudio so both pages can use it via a
 * plain <script> tag — no module/bundler setup needed.
 */
(function () {
  const SAMPLE_RATE = 24000;

  function wsUrl(path) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}${path}`;
  }

  function floatToPcm16(input) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      out[i] = Math.max(-32768, Math.min(32767, input[i] * 32767));
    }
    return out;
  }

  /** Float32 [-1,1] → Int16 LE, downsampled from inputRate to SAMPLE_RATE (24kHz). */
  function downsampleToPcm16(input, inputRate) {
    if (inputRate === SAMPLE_RATE) return floatToPcm16(input);
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

  function createPlaybackState() {
    return { sources: [], nextPlayTime: 0 };
  }

  /** Play a PCM16 LE 24kHz mono buffer through the AudioContext (gapless, scheduled). */
  function playPcm16(buffer, audioCtx, state) {
    if (!audioCtx) return;
    const view = new Int16Array(buffer);
    const floats = new Float32Array(view.length);
    for (let i = 0; i < view.length; i++) floats[i] = view[i] / 32768;

    const audioBuf = audioCtx.createBuffer(1, floats.length, SAMPLE_RATE);
    audioBuf.copyToChannel(floats, 0);

    const src = audioCtx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(audioCtx.destination);

    const now = audioCtx.currentTime;
    if (state.nextPlayTime < now) state.nextPlayTime = now;
    src.start(state.nextPlayTime);
    state.nextPlayTime = state.nextPlayTime + audioBuf.duration;
    state.sources.push(src);
    src.onended = () => {
      const i = state.sources.indexOf(src);
      if (i >= 0) state.sources.splice(i, 1);
    };
  }

  function stopPlayback(state) {
    for (const s of state.sources) {
      try { s.stop(); } catch (_) {}
    }
    state.sources = [];
    state.nextPlayTime = 0;
  }

  /**
   * Start mic capture, calling onChunk(pcm16Buffer) for each ~40ms frame.
   * Returns { disconnect }. Requires a running AudioContext + getUserMedia stream.
   */
  function startMicCapture(audioCtx, micStream, onChunk) {
    const ctxRate = audioCtx.sampleRate;
    const micSource = audioCtx.createMediaStreamSource(micStream);
    const scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
    scriptNode.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const pcm16 = downsampleToPcm16(input, ctxRate);
      if (pcm16.length > 0) onChunk(pcm16.buffer);
    };
    micSource.connect(scriptNode);
    scriptNode.connect(audioCtx.destination); // required for the node to fire
    return {
      disconnect() {
        try { scriptNode.disconnect(); } catch (_) {}
        try { micSource.disconnect(); } catch (_) {}
      },
    };
  }

  window.BobAudio = {
    SAMPLE_RATE,
    wsUrl,
    downsampleToPcm16,
    floatToPcm16,
    createPlaybackState,
    playPcm16,
    stopPlayback,
    startMicCapture,
  };
})();
