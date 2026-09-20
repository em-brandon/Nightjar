import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the console runs on :5173 and the hub on :8000. Proxying /api means
// the browser only ever talks to one origin, so no CORS config on the hub.
// In production Caddy serves both from one origin, same shape.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
