import { createFileRoute, redirect } from "@tanstack/react-router";

// The goals list merged into the Work page (2026-10-06); detail pages stay
// at /goals/$goalId.
export const Route = createFileRoute("/goals/")({
  beforeLoad: () => {
    throw redirect({ to: "/work" });
  },
});
