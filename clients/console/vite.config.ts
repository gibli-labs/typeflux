import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: {
      output: {
        // Split rarely-changing dependency code from app code so a release
        // that touches one page doesn't invalidate the whole bundle in every
        // operator's browser cache (#580). react and the tanstack libs are
        // separate groups: a routine tanstack bump shouldn't re-download
        // react. First matching group wins.
        codeSplitting: {
          groups: [
            { name: "react", test: /node_modules\/(react|react-dom|scheduler)\// },
            { name: "vendor", test: /node_modules/ },
          ],
        },
      },
    },
  },
  server: {
    // The dev console proxies the control-plane API so no CORS is needed:
    //   python -m typeflux.controlplane serve <manifest>  (port 8400)
    proxy: {
      "/api": "http://127.0.0.1:8400",
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
