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

  constructor() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const base = import.meta.env.DEV ? `${proto}//${location.host}` : "";
    const wsBase = import.meta.env.BASE_URL.replace(/\/$/, "");
    const secret = this.getSecret();
    this.url = `${base}${wsBase}/ws${secret ? `?secret=${encodeURIComponent(secret)}` : ""}`;
  }

  private getSecret(): string {
    const m = document.cookie.match(/cyborg_dashboard_secret=([^;]+)/);
    return m ? m[1] : "";
  }

  connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return;

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
      this.ws?.close();
    };
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }

  send(msg: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  get connected() {
    return this._connected;
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.delay = Math.min(this.delay * 2, 30000);
      this.connect();
    }, this.delay);
  }
}

export const ws = new DashboardWS();
