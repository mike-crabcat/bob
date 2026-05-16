import { useEffect, useRef, useCallback, useSyncExternalStore } from "react";
import { ws, type WSEvent } from "@/lib/ws-client";

export function useWSEvents(): WSEvent[] {
  const eventsRef = useRef<WSEvent[]>([]);
  const subscribersRef = useRef(new Set<() => void>());

  const subscribe = useCallback((fn: () => void) => {
    subscribersRef.current.add(fn);
    return () => subscribersRef.current.delete(fn);
  }, []);

  const getSnapshot = useCallback(() => eventsRef.current, []);

  useSyncExternalStore(subscribe, getSnapshot);

  useEffect(() => {
    return ws.subscribe((event) => {
      eventsRef.current = [event, ...eventsRef.current].slice(0, 100);
      for (const fn of subscribersRef.current) fn();
    });
  }, []);

  return eventsRef.current;
}

export function useWSConnected(): boolean {
  const subscribe = useCallback((fn: () => void) => {
    const interval = setInterval(fn, 1000);
    return () => clearInterval(interval);
  }, []);

  const getSnapshot = useCallback(() => ws.connected, []);

  return useSyncExternalStore(subscribe, getSnapshot);
}
