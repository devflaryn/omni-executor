import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// Two pages: the main app and the VNC viewer popup (opened by main.py).
// base "./" keeps asset URLs relative so pywebview's local server can serve
// the build from any path.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, "index.html"),
        viewer: resolve(import.meta.dirname, "viewer.html"),
      },
    },
  },
});
