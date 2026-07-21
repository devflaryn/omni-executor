import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

// base "./" keeps asset URLs relative so pywebview's local server can serve
// the build from any path. (There used to be a second "viewer" page for an
// embedded VNC popup; the engine now owns its own native viewer window, so
// this app is a single page.)
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, "index.html"),
      },
    },
  },
});
