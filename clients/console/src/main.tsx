import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";

import { createConsoleQueryClient } from "./queries";
import { router } from "./router";
import "./theme.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("missing #root element");
}
const queryClient = createConsoleQueryClient();
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
