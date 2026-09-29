import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { I18nProvider } from "./i18n";
import { ApiProvider } from "./state/api";
import { bootstrapGrant } from "./api/grant";
import "./styles.css";

const savedTheme = localStorage.getItem("sbx.console.theme");
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
document.documentElement.dataset.theme =
  savedTheme === "light" || savedTheme === "dark"
    ? savedTheme
    : prefersDark
      ? "dark"
      : "light";
const savedLocale = localStorage.getItem("sbx.console.locale");
document.documentElement.lang =
  savedLocale ??
  (navigator.language.startsWith("zh") ? "zh-CN" : "en");

function render() {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <I18nProvider>
        <ApiProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ApiProvider>
      </I18nProvider>
    </StrictMode>,
  );
}

// SOR-266: redeem a `sbx open` one-time grant before first paint so the
// app never flashes an unauthenticated shell.
void bootstrapGrant().finally(render);
