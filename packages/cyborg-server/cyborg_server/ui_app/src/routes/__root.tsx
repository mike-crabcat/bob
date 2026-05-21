import { Outlet, Link, useRouterState, createRootRoute, useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ws, type WSEvent } from "@/lib/ws-client";
import { useWSConnected } from "@/hooks/use-live-data";

const NAV_ITEMS = [
  { to: "/" as const, label: "Home", icon: "home" },
  { to: "/sessions" as const, label: "Sessions", icon: "chat" },
  { to: "/contacts" as const, label: "Contacts", icon: "user" },
  { to: "/skills" as const, label: "Skills", icon: "zap" },
  { to: "/workspace" as const, label: "Workspace", icon: "folder" },
] as const;

const OVERFLOW_ITEMS = [
  { to: "/phone" as const, label: "Phone", icon: "phone" },
] as const;

function NavIcon({ name, size = 18 }: { name: string; size?: number }) {
  const s = size;
  switch (name) {
    case "home":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>;
    case "chat":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>;
    case "user":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>;
    case "folder":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>;
    case "zap":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>;
    case "phone":
      return <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>;
    default:
      return null;
  }
}

function RootLayout() {
  const queryClient = useQueryClient();
  const routerState = useRouterState();
  const currentPath = routerState.location.pathname;
  const connected = useWSConnected();
  const [overflowOpen, setOverflowOpen] = useState(false);
  const overflowRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!overflowOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (overflowRef.current && !overflowRef.current.contains(e.target as Node)) {
        setOverflowOpen(false);
      }
    };
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [overflowOpen]);

  useEffect(() => {
    ws.start();
    const unsub = ws.subscribe((event: WSEvent) => {
      if (event.type.startsWith("llm.call.")) {
        queryClient.invalidateQueries({ queryKey: ["home"] });
        queryClient.invalidateQueries({ queryKey: ["sessions"] });
        queryClient.invalidateQueries({ queryKey: ["session-detail"] });
      } else if (event.type === "whatsapp.message.received" || event.type === "email.message.received") {
        queryClient.invalidateQueries({ queryKey: ["home"] });
        queryClient.invalidateQueries({ queryKey: ["sessions"] });
        queryClient.invalidateQueries({ queryKey: ["session-detail"] });
      } else if (event.type === "session.summary.created") {
        queryClient.invalidateQueries({ queryKey: ["home"] });
        queryClient.invalidateQueries({ queryKey: ["session-detail"] });
      } else if (event.type === "skill.delegation.updated") {
        queryClient.invalidateQueries({ queryKey: ["skills-delegations"] });
        queryClient.invalidateQueries({ queryKey: ["skills-delegation"] });
        queryClient.invalidateQueries({ queryKey: ["skills-installed"] });
      } else if (event.type.startsWith("phone.call.")) {
        queryClient.invalidateQueries({ queryKey: ["phone-calls"] });
        const payload = (event as any).payload || {};
        if (payload.call_id) {
          queryClient.invalidateQueries({ queryKey: ["phone-call", payload.call_id] });
        }
      }
    });
    return unsub;
  }, [queryClient]);

  return (
    <div className="flex flex-col h-dvh">
      <header className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium font-sans tracking-tight text-text">cyborg</span>
          <span className={`w-1.5 h-1.5 rounded-full ${connected ? "bg-success animate-pulse" : "bg-muted"}`} />
        </div>
        <span className="text-xs text-muted">{connected ? "live" : "connecting..."}</span>
      </header>

      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>

      <nav className="flex border-t border-border shrink-0">
        {NAV_ITEMS.map((item) => {
          const active = item.to === "/" ? currentPath === "/" : currentPath.startsWith(item.to);
          return (
            <Link
              key={item.to}
              to={item.to}
              className={`flex-1 flex flex-col items-center justify-center py-2.5 text-[10px] font-sans gap-0.5 transition-colors ${
                active ? "text-accent" : "text-muted hover:text-text"
              }`}
            >
              <NavIcon name={item.icon} />
              <span>{item.label}</span>
            </Link>
          );
        })}
        <div ref={overflowRef} className="flex-1 relative">
          {overflowOpen && (
            <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 bg-surface border border-border shadow-lg py-1 min-w-[120px] z-50">
              {OVERFLOW_ITEMS.map((item) => (
                <button
                  key={item.to}
                  onClick={() => { setOverflowOpen(false); navigate({ to: item.to }); }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-xs text-text hover:bg-accent/10 transition-colors"
                >
                  <NavIcon name={item.icon} size={14} />
                  <span>{item.label}</span>
                </button>
              ))}
            </div>
          )}
          <button
            onClick={() => setOverflowOpen(!overflowOpen)}
            className={`w-full flex flex-col items-center justify-center py-2.5 text-[10px] font-sans gap-0.5 transition-colors ${
              OVERFLOW_ITEMS.some((item) => currentPath.startsWith(item.to)) ? "text-accent" : "text-muted hover:text-text"
            }`}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></svg>
            <span>more</span>
          </button>
        </div>
      </nav>
    </div>
  );
}

export const Route = createRootRoute({ component: RootLayout });
