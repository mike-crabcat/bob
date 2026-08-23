import { createFileRoute, redirect } from "@tanstack/react-router";

export const Route = createFileRoute("/sessions/$")({
  beforeLoad: ({ params }) => {
    const splat = (params as { _splat?: string })._splat ?? "";
    throw redirect(
      splat
        ? { to: "/conversations/$sessionKey", params: { sessionKey: splat.split("/")[0] } }
        : { to: "/conversations" },
    );
  },
});
