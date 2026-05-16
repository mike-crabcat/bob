import { Outlet, Link, useRouterState, createRootRoute } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { ws, type WSEvent } from "@/lib/ws-client";
import { useWSConnected } from "@/hooks/use-live-data";

const NAV_ITEMS = [
  { to: "/" as const, label: "Home" },
  { to: "/sessions" as const, label: "Sessions" },
  { to: "/contacts" as const, label: "Contacts" },
];

function RootLayout() {
  const queryClient = useQueryClient();
  const routerState = useRouterState();
  const currentPath = routerState.location.pathname;
  const connected = useWSConnected();

  useEffect(() => {
    ws.connect();
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
      }
    });
    return () => {
      unsub();
      ws.disconnect();
    };
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
              className={`flex-1 text-center py-2 text-xs font-sans transition-colors ${
                active ? "text-accent" : "text-muted hover:text-text"
              }`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}

export const Route = createRootRoute({ component: RootLayout });
