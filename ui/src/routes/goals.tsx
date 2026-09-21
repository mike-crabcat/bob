import { createFileRoute, Outlet } from "@tanstack/react-router";

// Layout route: the list lives at goals.index.tsx (/goals/), the drill-down
// at goals.$goalId.tsx (/goals/$goalId). Without this Outlet the child
// route matches but renders nowhere (TanStack file-routing convention).
function GoalsLayout() {
  return <Outlet />;
}

export const Route = createFileRoute("/goals")({ component: GoalsLayout });
