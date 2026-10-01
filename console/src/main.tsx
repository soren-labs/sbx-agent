import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { I18nProvider } from "./i18n";
import { ApiProvider } from "./state/api";
import { handleGithubReturn } from "./api/github-return";
import { bootstrapGrant } from "./api/grant";

const savedTheme = localStorage.getItem("sbx.console.theme");
document.documentElement.dataset.theme =
  savedTheme === "light" || savedTheme === "dark" ? savedTheme : "dark";
document.documentElement.lang = "en";

function render() {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <I18nProvider>
        <ApiProvider>
          <BrowserRouter
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <App />
          </BrowserRouter>
        </ApiProvider>
      </I18nProvider>
    </StrictMode>,
  );
}

// SOR-266: redeem a `sbx open` one-time grant before first paint so the
// app never flashes an unauthenticated shell.
void bootstrapGrant().then(handleGithubReturn).finally(render);
