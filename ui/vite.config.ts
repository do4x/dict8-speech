import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Built into the Python package, loaded from file:// by WKWebView — so relative asset paths
// and no code splitting across pages (two tiny self-contained bundles beat a shared chunk
// graph over file://).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../dict8/ui/web",
    emptyOutDir: true,
    target: "safari17",
    rollupOptions: {
      input: {
        overlay: resolve(__dirname, "overlay.html"),
        window: resolve(__dirname, "window.html"),
      },
    },
  },
});
