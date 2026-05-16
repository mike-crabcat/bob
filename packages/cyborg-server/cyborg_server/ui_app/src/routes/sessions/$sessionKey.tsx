import { createFileRoute, Outlet } from "@tanstack/react-router";

function SessionLayout() {
  return <Outlet />;
}

export const Route = createFileRoute("/sessions/$sessionKey")({ component: SessionLayout });
