import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, Vite forwards /api/* to the TraceOps backend on port 8000,
// so the browser only ever talks to one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
