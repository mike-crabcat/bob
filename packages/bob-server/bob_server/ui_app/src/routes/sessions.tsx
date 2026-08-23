import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/sessions")({
  beforeLoad: () => {
    throw redirect({ to: "/conversations" });
  },
});
