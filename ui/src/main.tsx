import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import "./main.css";

// Frontend error reporting to backend log
function reportError(payload: Record<string, unknown>) {
  try {
    const base = import.meta.env.BASE_URL.replace(/\/$/, "");
    const secret = document.cookie.match(/bob_dashboard_secret=([^;]+)/)?.[1] ?? "";
    fetch(`${base}/api/frontend-errors?secret=${encodeURIComponent(secret)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: location.href, ...payload }),
    }).catch(() => {});
  } catch {}
}

window.addEventListener("error", (e) => {
  reportError({
    message: e.message,
    source: e.filename,
    lineno: e.lineno,
    colno: e.colno,
    stack: e.error?.stack ?? "",
  });
});

window.addEventListener("unhandledrejection", (e) => {
  reportError({
    message: `Unhandled promise rejection: ${e.reason}`,
    stack: e.reason?.stack ?? "",
  });
});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: Infinity,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
    },
  },
});

const basepath = import.meta.env.BASE_URL.replace(/\/$/, "");

const router = createRouter({
  routeTree,
  context: { queryClient },
  basepath,
});

declare module "@tanstack/react-router" {
  interface RouterContext {
    queryClient: QueryClient;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
