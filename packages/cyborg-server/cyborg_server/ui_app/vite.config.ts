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
    outDir: "../ui_dist",
    emptyOutDir: true,
  },
  server: {
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
    },
  },
});
