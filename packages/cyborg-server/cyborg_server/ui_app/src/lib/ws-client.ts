export interface WSEvent {
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

type Listener = (event: WSEvent) => void;

export class DashboardWS {
  private ws: WebSocket | null = null;
  private listeners: Set<Listener> = new Set();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private delay = 1000;
  private url: string;
  private _connected = false;
  private _started = false;

  constructor() {
    const wsBase = import.meta.env.BASE_URL.replace(/\/$/, "");
    const secret = this.getSecret();
    if (import.meta.env.DEV) {
      this.url = `ws://127.0.0.1:8420${wsBase}/ws${secret ? `?secret=${encodeURIComponent(secret)}` : ""}`;
    } else {
      this.url = `${wsBase}/ws${secret ? `?secret=${encodeURIComponent(secret)}` : ""}`;
    }
  }

  private getSecret(): string {
    const m = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/);
    return m ? m[1] : "";
  }

  /** Start the WS connection. Safe to call multiple times — only connects once. */
  start() {
    if (this._started) return;
    this._started = true;
    this.connect();
  }

  private connect() {
    const state = this.ws?.readyState;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;

    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this._connected = true;
      this.delay = 1000;
    };

    this.ws.onmessage = (e) => {
      try {
        const event: WSEvent = JSON.parse(e.data);
        for (const fn of this.listeners) fn(event);
      } catch {
        // ignore malformed
      }
    };

    this.ws.onclose = () => {
      this._connected = false;
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose fires after onerror, so reconnect is handled there
    };
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.delay = Math.min(this.delay * 2, 30000);
      this.connect();
    }, this.delay);
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  get connected() {
    return this._connected;
  }
}

export const ws = new DashboardWS();
