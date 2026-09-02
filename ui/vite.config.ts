import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import path from "path";

export default defineConfig({
  plugins: [TanStackRouterVite(), react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  base: "/dashboard/",
  build: {
    // Emits into the server tree so main.py's Path(__file__).parent / "ui_dist"
    // mount keeps working; gitignored, rebuilt by deploy.sh / the Docker build.
    outDir: "../server/ui_dist",
    emptyOutDir: true,
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["mike-workstation.tail94e30e.ts.net"],
    proxy: {
      "/dashboard/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
      },
      "/dashboard/ws": {
        target: "ws://127.0.0.1:8420",
        ws: true,
        changeOrigin: true,
      },
      "/phone": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
