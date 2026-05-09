/**
 * Bob Voice — Push-to-Talk SPA
 *
 * WebSocket protocol:
 *   Client → Server (text):
 *     {"type": "start_recording", "userId": "mike", "sessionKey": "voice-mike", "language": "en", "sessionMode": "chat|portuguese_teacher"}
 *     {"type": "stop_recording"}
 *     {"type": "cancel"}
 *     {"type": "set_language", "language": "pt"}
 *
 *   Client → Server (binary):
 *     Raw audio chunks (MediaRecorder output — MP4/AAC on Safari, WebM/Opus on Chrome)
 *
 *   Server → Client (text):
 *     {"type": "status", "state": "recording|transcribing|thinking|speaking|idle"}
 *     {"type": "transcript", "text": "...", "language": "en", "latency_ms": ...}
 *     {"type": "response_text", "text": "..."}
 *     {"type": "latency", "stt_ms": ..., "openclaw_total_ms": ..., "e2e_ms": ...}
 *     {"type": "audio_done"}
 *     {"type": "error", "message": "..."}
 *
 *   Server → Client (binary):
 *     WAV audio chunks (TTS output, one per sentence — progressive playback)
 */

// ---- Telemetry: POST frontend errors/events to server logs ----
function serverLog(level, message, context) {
    try {
        fetch('log', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level, message, tag: 'frontend', context: context || undefined }),
        }).catch(() => {});
    } catch (_) { /* best effort */ }
}

// Catch unhandled errors
window.onerror = (msg, source, lineno, colno, err) => {
    serverLog('error', `Unhandled: ${msg}`, `${source}:${lineno}:${colno}`);
};
window.onunhandledrejection = (event) => {
    serverLog('error', `Unhandled promise: ${event.reason}`);
};

const app = {
    // ---- State ----
    ws: null,
    mediaStream: null,
    mediaRecorder: null,
    audioContext: null,
    analyser: null,
    vizAnimFrame: null,
    isRecording: false,
    isConnected: false,
    isProcessing: false, // true from stop_recording until idle
    sessionMode: 'chat',
    language: 'en',
    userId: localStorage.getItem('bobvoice-user-id') || 'mike',
    serverUrl: '',
    nextPlaybackTime: 0,
    audioSourceQueue: [],
    settingsVisible: false,
    _isReplaying: false,

    _MODE_LANGUAGE: { portuguese_teacher: 'pt', french_teacher: 'fr', beginner_french: 'auto' },

    _LANGUAGE_NAMES: {
        en: 'English', fr: 'French', pt: 'Portuguese', es: 'Spanish',
        de: 'German', it: 'Italian', nl: 'Dutch', ja: 'Japanese',
        ko: 'Korean', zh: 'Chinese', ru: 'Russian', ar: 'Arabic',
        hi: 'Hindi', pl: 'Polish', tr: 'Turkish', sv: 'Swedish',
        da: 'Danish', fi: 'Finnish', no: 'Norwegian', cs: 'Czech',
        el: 'Greek', he: 'Hebrew', th: 'Thai', vi: 'Vietnamese',
        id: 'Indonesian', uk: 'Ukrainian', ro: 'Romanian', hu: 'Hungarian',
    },
    _MODE_LABELS: { chat: 'Bob Voice', portuguese_teacher: 'Portuguese Teacher', french_teacher: 'French Teacher', beginner_french: 'Beginner French' },

    // ---- DOM refs ----
    get el() {
        const ids = [
            'connect-overlay', 'main-ui', 'server-url', 'user-id', 'connect-btn',
            'pt-teacher-btn', 'fr-teacher-btn', 'beginner-fr-btn', 'connect-error', 'status-indicator', 'status-text', 'connection-badge',
            'messages', 'latency-bar', 'latency-info', 'ptt-btn', 'ptt-icon',
            'ptt-label', 'audio-visualizer', 'viz-canvas', 'settings-panel',
            'settings-server-url', 'language-select',
        ];
        const refs = {};
        for (const id of ids) {
            refs[id] = document.getElementById(id);
        }
        return refs;
    },

    // ---- Connection ----

    _connectBtns() {
        return [this.el['connect-btn'], this.el['pt-teacher-btn'], this.el['fr-teacher-btn'], this.el['beginner-fr-btn']];
    },

    _setConnectBtns(disabled) {
        for (const btn of this._connectBtns()) {
            if (btn) btn.disabled = disabled;
        }
    },

    connect(mode = 'chat') {
        const url = this.el['server-url'].value.trim();
        const userId = this.el['user-id'].value.trim() || 'mike';

        if (!url) {
            this.showConnectError('Enter the bridge server WebSocket URL');
            return;
        }

        // Prevent duplicate connections
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        this.userId = userId;
        this.serverUrl = url;
        this.sessionMode = mode;
        this.language = this._MODE_LANGUAGE[mode] || 'en';
        localStorage.setItem('bobvoice-user-id', userId);
        this._setConnectBtns(true);
        this.el['connect-error'].classList.add('hidden');

        try {
            this.ws = new WebSocket(url);
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = () => {
                this.isConnected = true;
                this.el['connect-overlay'].classList.add('hidden');
                this.el['main-ui'].classList.remove('hidden');
                this.el['ptt-btn'].disabled = false;
                this.updateStatus('idle', 'Ready');
                this.el['settings-server-url'].textContent = url;
                this.el['language-select'].value = this.language;
                this.ensureAudioContext();
                this.addSystemMessage('Connected — ' + (this._MODE_LABELS[this.sessionMode] || 'Bob Voice'));
                this.requestMic();
                // Request session history from server
                this.ws.send(JSON.stringify({
                    type: 'session_history',
                    userId: this.userId,
                    sessionMode: this.sessionMode,
                }));
                serverLog('info', `WebSocket connected, mode=${this.sessionMode}, lang=${this.language}, AudioContext state=${this.audioContext?.state}, sampleRate=${this.audioContext?.sampleRate}`);
            };

            this.ws.onmessage = (event) => {
                if (event.data instanceof ArrayBuffer) {
                    this.handleBinaryMessage(event.data);
                } else {
                    this.handleTextMessage(event.data);
                }
            };

            this.ws.onclose = (event) => {
                this.handleDisconnect(event.code, event.reason);
            };

            this.ws.onerror = () => {
                this.showConnectError('Connection failed — check the URL and that the bridge server is running');
                this._setConnectBtns(false);
            };

            // Timeout for slow connections
            setTimeout(() => {
                if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
                    this.showConnectError('Connection timed out — check the URL');
                    this.ws.close();
                    this._setConnectBtns(false);
                }
            }, 10000);

        } catch (e) {
            this.showConnectError(`Invalid URL: ${e.message}`);
            this._setConnectBtns(false);
        }
    },

    disconnect() {
        this.stopRecording();
        if (this.ws) {
            this.ws.close(1000, 'User disconnected');
            this.ws = null;
        }
        this.isConnected = false;
        this.hideSettings();
        this.releaseMic();
        this.el['main-ui'].classList.add('hidden');
        this.el['connect-overlay'].classList.remove('hidden');
        this._setConnectBtns(false);
    },

    handleDisconnect(code, reason) {
        this.isConnected = false;
        this.isProcessing = false;
        this.stopRecording();
        this.releaseMic();
        this.el['ptt-btn'].disabled = true;
        this.updateStatus('disconnected', 'Disconnected');
        this.addSystemMessage(`Disconnected${reason ? ': ' + reason : ''}`);

        // Auto-reconnect after 3s if not intentional
        if (code !== 1000) {
            this.addSystemMessage('Reconnecting in 3s...');
            setTimeout(() => {
                if (!this.isConnected && this.serverUrl) {
                    this.el['server-url'].value = this.serverUrl;
                    this.el['user-id'].value = this.userId;
                    this.connect(this.sessionMode);
                }
            }, 3000);
        }
    },

    showConnectError(msg) {
        const errEl = this.el['connect-error'];
        errEl.textContent = msg;
        errEl.classList.remove('hidden');
    },

    // ---- Push-to-Talk ----

    async requestMic() {
        try {
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                }
            });
            serverLog('info', 'Mic permission granted (preload)');
        } catch (e) {
            serverLog('error', `Mic preload denied: ${e.name}: ${e.message}`);
            this.mediaStream = null;
        }
    },

    releaseMic() {
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(t => t.stop());
            this.mediaStream = null;
        }
    },

    async pttStart(event) {
        if (event) event.preventDefault();
        if (!this.isConnected || this.isRecording || this.isProcessing) return;

        // Capture pointer so move/release outside button still works
        if (event.target && event.target.setPointerCapture) {
            try { event.target.setPointerCapture(event.pointerId); } catch (e) {}
        }

        // Resume AudioContext (required on iOS after user gesture)
        if (this.audioContext && this.audioContext.state === 'suspended') {
            await this.audioContext.resume();
        }

        try {
            this.isRecording = true;
            this.el['ptt-btn'].classList.add('recording');
            this.el['ptt-icon'].textContent = '⏺';
            this.el['ptt-label'].textContent = 'Recording...';
            this.updateStatus('recording', 'Recording...');
            this.showVisualizer(true);

            // Get mic stream (reuse if already requested during connect)
            if (!this.mediaStream || !this.mediaStream.active) {
                this.mediaStream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                    }
                });
            }

            // Set up analyser for visualizer
            this.setupAnalyser(this.mediaStream);

            // Determine best MIME type for MediaRecorder
            const mimeType = this.getSupportedMimeType();

            // Create MediaRecorder
            this.mediaRecorder = new MediaRecorder(this.mediaStream, {
                mimeType: mimeType,
                // Smaller timeslice for lower latency (100ms chunks)
                // Note: timeslice is passed to start(), not constructor
            });

            this.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0 && this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(e.data);
                }
            };

            this.mediaRecorder.onerror = (e) => {
                console.error('MediaRecorder error:', e);
                this.stopRecording();
                this.addErrorMessage('Microphone error');
            };

            // Send start_recording command
            this.ws.send(JSON.stringify({
                type: 'start_recording',
                userId: this.userId,
                sessionKey: `${this.sessionMode === 'portuguese_teacher' ? 'teacher' : 'voice'}-${this.userId}`,
                language: this.language === 'auto' ? undefined : this.language,
                sessionMode: this.sessionMode,
            }));

            // Start recording with 100ms timeslice for streaming chunks
            this.mediaRecorder.start(100);
            this.recordStartTime = Date.now();

        } catch (e) {
            console.error('PTT start error:', e);
            this.isRecording = false;
            this.el['ptt-btn'].classList.remove('recording');
            this.el['ptt-icon'].textContent = '🎙️';
            this.el['ptt-label'].textContent = 'Hold to Talk';
            this.showVisualizer(false);

            if (e.name === 'NotAllowedError') {
                this.addErrorMessage('Microphone access denied — allow in Settings');
            } else if (e.name === 'NotFoundError') {
                this.addErrorMessage('No microphone found');
            } else {
                this.addErrorMessage(`Mic error: ${e.message}`);
            }
        }
    },

    pttEnd(event) {
        if (event) event.preventDefault();
        if (!this.isRecording) return;
        this.isRecording = false;
        this.showVisualizer(false);
        this.el['ptt-btn'].classList.remove('recording');
        this.el['ptt-icon'].textContent = '🎙️';
        this.el['ptt-label'].textContent = 'Hold to Talk';

        // Stop MediaRecorder and wait for it to flush the final chunk
        const recorder = this.mediaRecorder;
        this.mediaRecorder = null;

        const sendStop = () => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'stop_recording' }));
                this.isProcessing = true;
                this.updateStatus('transcribing', 'Processing...');
            }
        };

        // Ensure at least 200ms of audio so MediaRecorder has time to produce a chunk
        const elapsed = Date.now() - (this.recordStartTime || 0);
        const minDelay = Math.max(0, 200 - elapsed);

        if (recorder && recorder.state !== 'inactive') {
            recorder.onstop = () => setTimeout(sendStop, minDelay);
            try {
                recorder.stop();
            } catch (e) {
                sendStop();
            }
        } else {
            sendStop();
        }
    },

    stopRecording() {
        this.isRecording = false;
        this.showVisualizer(false);

        if (this.mediaRecorder && this.mediaRecorder.state !== 'inactive') {
            try {
                this.mediaRecorder.stop();
            } catch (e) {
                // ignore
            }
        }
        this.mediaRecorder = null;

        this.el['ptt-btn'].classList.remove('recording');
        this.el['ptt-icon'].textContent = '🎙️';
        this.el['ptt-label'].textContent = 'Hold to Talk';
    },

    getSupportedMimeType() {
        // Prefer MP4/AAC for Safari compatibility
        const types = [
            'audio/mp4;codecs=aac',
            'audio/mp4',
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/ogg;codecs=opus',
        ];
        for (const type of types) {
            if (MediaRecorder.isTypeSupported(type)) {
                return type;
            }
        }
        // Fallback — let the browser decide
        return '';
    },

    // ---- Audio Playback (progressive TTS) ----

    ensureAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.nextPlaybackTime = 0;
            serverLog('info', `AudioContext created: state=${this.audioContext.state}, sampleRate=${this.audioContext.sampleRate}`);
        }
        // iOS requires resume on user gesture
        if (this.audioContext.state === 'suspended') {
            this.audioContext.resume();
            serverLog('info', 'AudioContext resumed from suspended');
        }
    },

    handleBinaryMessage(arrayBuffer) {
        // Binary message = WAV audio chunk from TTS
        this.ensureAudioContext();
        serverLog('info', `Audio chunk received: ${arrayBuffer.byteLength} bytes, audioCtx state=${this.audioContext.state}, sampleRate=${this.audioContext.sampleRate}`);

        // Decode the WAV data to AudioBuffer
        this.audioContext.decodeAudioData(
            arrayBuffer,
            (audioBuffer) => {
                serverLog('info', `Audio decoded: ${audioBuffer.duration.toFixed(2)}s, ${audioBuffer.numberOfChannels}ch, ${audioBuffer.sampleRate}Hz`);
                this.playAudioBuffer(audioBuffer);
            },
            (error) => {
                serverLog('error', `Audio decode failed: ${error}`, `bytes=${arrayBuffer.byteLength}`);
                console.error('Audio decode error:', error);
                // Fallback: try playing via Audio element
                this.playFallback(arrayBuffer);
            }
        );
    },

    playAudioBuffer(audioBuffer) {
        try {
            const source = this.audioContext.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(this.audioContext.destination);

            const now = this.audioContext.currentTime;

            if (this.audioContext.state !== 'running') {
                serverLog('error', `AudioContext not running during playback: state=${this.audioContext.state}`);
            }

            // Schedule playback — chain after previous chunk for gapless playback
            if (this.nextPlaybackTime <= now) {
                // Start immediately (no previous chunk playing or gap)
                this.nextPlaybackTime = now;
            }

            const startTime = this.nextPlaybackTime;
            source.start(startTime);
            this.nextPlaybackTime = startTime + audioBuffer.duration;
            serverLog('info', `Scheduled audio: start=${startTime.toFixed(2)}s, duration=${audioBuffer.duration.toFixed(2)}s, queue=${this.audioSourceQueue.length}, ctxState=${this.audioContext.state}`);

            // Track source for potential cancellation
            this.audioSourceQueue.push(source);
            source.onended = () => {
                const idx = this.audioSourceQueue.indexOf(source);
                if (idx >= 0) this.audioSourceQueue.splice(idx, 1);
                serverLog('info', `Audio source ended: remaining=${this.audioSourceQueue.length}`);
            };
        } catch (e) {
            serverLog('error', `playAudioBuffer error: ${e.message}`, `ctxState=${this.audioContext.state}`);
        }
    },

    playFallback(arrayBuffer) {
        try {
            const blob = new Blob([arrayBuffer], { type: 'audio/wav' });
            const url = URL.createObjectURL(blob);
            const audio = new Audio();
            audio.src = url;
            audio.play().then(() => {
                serverLog('info', `Fallback audio playing: ${arrayBuffer.byteLength} bytes`);
            }).catch(e => {
                serverLog('error', `Fallback audio play rejected: ${e.name}: ${e.message}`);
            });
            audio.onended = () => {
                serverLog('info', 'Fallback audio ended');
                URL.revokeObjectURL(url);
            };
        } catch (e) {
            serverLog('error', `Fallback playback failed: ${e.message}`);
        }
    },

    stopPlayback() {
        // Stop all queued audio sources
        for (const source of this.audioSourceQueue) {
            try { source.stop(); } catch (e) { /* already stopped */ }
        }
        this.audioSourceQueue = [];
        this.nextPlaybackTime = 0;
    },

    // ---- Visualizer ----

    setupAnalyser(stream) {
        if (!this.audioContext) return;
        const source = this.audioContext.createMediaStreamSource(stream);
        this.analyser = this.audioContext.createAnalyser();
        this.analyser.fftSize = 256;
        source.connect(this.analyser);
        this.drawVisualizer();
    },

    showVisualizer(show) {
        const el = this.el['audio-visualizer'];
        if (show) {
            el.classList.remove('hidden');
        } else {
            el.classList.add('hidden');
            if (this.vizAnimFrame) {
                cancelAnimationFrame(this.vizAnimFrame);
                this.vizAnimFrame = null;
            }
            this.analyser = null;
        }
    },

    drawVisualizer() {
        const canvas = this.el['viz-canvas'];
        if (!canvas || !this.analyser) return;

        const ctx = canvas.getContext('2d');
        const bufferLength = this.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        const draw = () => {
            if (!this.isRecording) return;
            this.vizAnimFrame = requestAnimationFrame(draw);

            this.analyser.getByteFrequencyData(dataArray);

            ctx.clearRect(0, 0, canvas.width, canvas.height);

            const barCount = 30;
            const barWidth = canvas.width / barCount;
            const step = Math.floor(bufferLength / barCount);

            for (let i = 0; i < barCount; i++) {
                const value = dataArray[i * step];
                const percent = value / 255;
                const barHeight = percent * canvas.height;

                const x = i * barWidth;
                const y = canvas.height - barHeight;

                ctx.fillStyle = `rgba(233, 69, 96, ${0.4 + percent * 0.6})`;
                ctx.fillRect(x, y, barWidth - 1, barHeight);
            }
        };

        draw();
    },

    // ---- Message handling ----

    handleTextMessage(data) {
        let msg;
        try {
            msg = JSON.parse(data);
        } catch (e) {
            console.error('Invalid JSON from server:', data);
            return;
        }

        switch (msg.type) {
            case 'status':
                this.handleStatus(msg.state);
                break;
            case 'transcript':
                this.handleTranscript(msg);
                break;
            case 'partial_response':
                this.handlePartialResponse(msg);
                break;
            case 'response_text':
                this.handleResponseText(msg);
                break;
            case 'latency':
                this.handleLatency(msg);
                break;
            case 'audio_done':
                this.handleAudioDone();
                break;
            case 'error':
                this.handleError(msg);
                break;
            case 'history':
                this.handleHistory(msg);
                break;
            case 'lesson_progress':
                this.handleLessonProgress(msg);
                break;
            default:
                console.log('Unknown message type:', msg.type, msg);
        }
    },

    handleStatus(state) {
        if (this._isReplaying) return;
        switch (state) {
            case 'recording':
                // New interaction — stop any leftover audio from previous response
                this._streamingBubble = null;
                this.stopPlayback();
                this.updateStatus('recording', 'Recording...');
                break;
            case 'transcribing':
                this.updateStatus('transcribing', 'Transcribing...');
                break;
            case 'thinking':
                this.updateStatus('thinking', 'Thinking...');
                break;
            case 'speaking':
                this.updateStatus('speaking', 'Speaking...');
                break;
            case 'idle':
                this.isProcessing = false;
                this.updateStatus('idle', 'Ready');
                this.el['ptt-btn'].disabled = false;
                break;
        }
    },

    handleTranscript(msg) {
        this.addUserMessage(msg.text, msg.language);
    },

    handlePartialResponse(msg) {
        if (!this._streamingBubble) {
            this._streamingBubble = this.addAssistantMessage(msg.text);
        } else {
            this._streamingBubble.textContent = msg.text;
        }
    },

    handleResponseText(msg) {
        if (this._streamingBubble) {
            this._streamingBubble.textContent = msg.text;
            this._streamingBubble = null;
        } else {
            this.addAssistantMessage(msg.text);
        }
    },

    handleLatency(msg) {
        const parts = [];
        if (msg.stt_ms) parts.push(`STT: ${(msg.stt_ms / 1000).toFixed(1)}s`);
        if (msg.openclaw_total_ms) parts.push(`LLM: ${(msg.openclaw_total_ms / 1000).toFixed(1)}s`);
        if (msg.tts_first_chunk_ms) parts.push(`TTS: ${(msg.tts_first_chunk_ms / 1000).toFixed(1)}s`);
        if (msg.e2e_ms) parts.push(`Total: ${(msg.e2e_ms / 1000).toFixed(1)}s`);

        if (parts.length) {
            this.el['latency-info'].textContent = parts.join(' · ');
            this.el['latency-bar'].classList.remove('hidden');
            // Auto-hide after 10s
            setTimeout(() => {
                this.el['latency-bar'].classList.add('hidden');
            }, 10000);
        }
    },

    handleAudioDone() {
        serverLog('info', `Audio done — queue=${this.audioSourceQueue.length}, nextPlayTime=${this.nextPlaybackTime.toFixed(2)}`);
        this._isReplaying = false;
        // All TTS chunks sent — playback will continue until queued audio finishes
        // Reset nextPlaybackTime for next interaction
        setTimeout(() => {
            this.nextPlaybackTime = 0;
        }, 500);
    },

    handleError(msg) {
        this.addErrorMessage(msg.message || 'Unknown error');

        // Reset state on error
        if (this.isProcessing) {
            this.isProcessing = false;
            this.updateStatus('idle', 'Ready');
            this.el['ptt-btn'].disabled = false;
        }
    },

    handleHistory(msg) {
        const container = this.el['messages'];
        container.innerHTML = '';

        if (msg.messages && msg.messages.length > 0) {
            this.addSystemMessage('Restored session (' + msg.messages.length + ' messages)');
            for (const entry of msg.messages) {
                if (entry.role === 'user') {
                    this.addUserMessage(entry.text, entry.language || null);
                } else if (entry.role === 'assistant') {
                    this.addAssistantMessage(entry.text);
                }
            }
        }
    },

    handleLessonProgress(msg) {
        this._lessonInfo = msg;
        const statusText = this.el['status-text'];
        if (msg.lessonNumber && msg.totalLessons) {
            const progress = msg.completedSteps.length > 0
                ? ` (${msg.completedSteps.length}/${msg.totalSteps} steps)`
                : '';
            statusText.textContent = `Lesson ${msg.lessonNumber}/${msg.totalLessons}${progress}`;
        }
    },

    clearHistory() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
                type: 'clear_history',
                userId: this.userId,
                sessionMode: this.sessionMode,
            }));
            this.el['messages'].innerHTML = '';
            this.addSystemMessage('History cleared');
        }
    },

    // ---- UI Helpers ----

    updateStatus(state, text) {
        const indicator = this.el['status-indicator'];
        indicator.className = `status-dot ${state}`;
        this.el['status-text'].textContent = text;

        // Disable PTT during processing (except idle)
        if (state !== 'idle' && state !== 'recording') {
            this.el['ptt-btn'].disabled = true;
        }
    },

    addMessage(content, type, language = null) {
        const container = this.el['messages'];
        const div = document.createElement('div');
        div.className = `message ${type}`;

        if (type === 'user' || type === 'assistant') {
            const label = document.createElement('div');
            label.className = 'label';
            label.textContent = type === 'user' ? 'You' : 'Bob';

            if (type === 'user' && language) {
                const langName = this._LANGUAGE_NAMES[language] || language.toUpperCase();
                const badge = document.createElement('span');
                badge.className = 'lang-badge';
                badge.textContent = langName;
                label.appendChild(badge);
            }

            div.appendChild(label);
        }

        const text = document.createElement('div');
        text.className = 'text-content';
        text.textContent = content;
        div.appendChild(text);

        if (type === 'assistant') {
            div.dataset.rawText = content;
            const replayBtn = document.createElement('button');
            replayBtn.className = 'replay-btn';
            replayBtn.title = 'Replay audio';
            replayBtn.setAttribute('aria-label', 'Replay audio');
            replayBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0014 8.14v7.72A4.5 4.5 0 0016.5 12zM14 3.23v2.06A6.99 6.99 0 0121 12a6.99 6.99 0 01-7 6.71v2.06A9 9 0 0023 12 9 9 0 0014 3.23z"/></svg>';
            replayBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.replayTts(div.dataset.rawText);
            });
            div.appendChild(replayBtn);
        }

        container.appendChild(div);

        // Auto-scroll to bottom
        const conv = document.getElementById('conversation');
        conv.scrollTop = conv.scrollHeight;

        return div;
    },

    addUserMessage(text, language = null) {
        this.addMessage(text, 'user', language);
    },

    addAssistantMessage(text) {
        const div = this.addMessage(text, 'assistant');
        // addMessage appends label, text div, then replay btn — return the text div
        return div.querySelector('.text-content');
    },

    addSystemMessage(text) {
        this.addMessage(text, 'system');
    },

    addErrorMessage(text) {
        this.addMessage(text, 'error');
    },

    replayTts(text) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.stopPlayback();
        this._isReplaying = true;
        this.ws.send(JSON.stringify({
            type: 'replay_tts',
            text: text,
            sessionMode: this.sessionMode,
        }));
    },

    // ---- Settings ----

    showSettings() {
        this.el['settings-panel'].classList.remove('hidden');
        this.settingsVisible = true;
    },

    hideSettings() {
        this.el['settings-panel'].classList.add('hidden');
        this.settingsVisible = false;
    },

    setLanguage(lang) {
        this.language = lang;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            if (lang !== 'auto') {
                this.ws.send(JSON.stringify({ type: 'set_language', language: lang }));
            }
        }
    },
};

// ---- Keyboard shortcut (desktop) ----
document.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && !e.repeat && !app.settingsVisible) {
        e.preventDefault();
        app.pttStart(e);
    }
});

document.addEventListener('keyup', (e) => {
    if (e.code === 'Space') {
        e.preventDefault();
        app.pttEnd(e);
    }
});

// ---- Prevent default touch behaviors on PTT button ----
document.addEventListener('DOMContentLoaded', () => {
    const pttBtn = document.getElementById('ptt-btn');
    // Prevent iOS long-press context menu on PTT button
    pttBtn.addEventListener('contextmenu', (e) => e.preventDefault());
    // Prevent iOS selection
    pttBtn.style.webkitTouchCallout = 'none';
});

// ---- Restore last-used server URL from localStorage ----
document.addEventListener('DOMContentLoaded', () => {
    const saved = localStorage.getItem('bobvoice-server-url');
    const urlInput = document.getElementById('server-url');
    if (saved) {
        urlInput.value = saved;
    } else if (!urlInput.value.trim()) {
        // Auto-detect: build wss:// or ws:// URL from current page
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        urlInput.value = `${proto}//${location.host}/voice/ws`;
    }
    const savedUser = localStorage.getItem('bobvoice-user-id');
    if (savedUser) {
        document.getElementById('user-id').value = savedUser;
    }

    // Save on connect
    const origConnect = app.connect.bind(app);
    app.connect = function (mode) {
        localStorage.setItem('bobvoice-server-url', app.el['server-url'].value.trim());
        localStorage.setItem('bobvoice-user-id', app.el['user-id'].value.trim());
        origConnect(mode);
    };
});
