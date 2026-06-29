import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    hmr: {
      overlay: false,
    },
    proxy: {
      "/redemptions": "http://127.0.0.1:8000",
      "/status": "http://127.0.0.1:8000",
      "/freshness": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
});
