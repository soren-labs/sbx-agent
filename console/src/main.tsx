import "@fontsource-variable/inter";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import "./styles.css";
import "./features/features.css";
import "./restoration.css";
import { I18nProvider } from "./i18n";
import { AuthProvider } from "./state/auth";
import { ApiProvider } from "./state/context";

const savedTheme = localStorage.getItem("sbx.console.theme");
document.documentElement.dataset.theme =
  savedTheme === "light"
    ? "light"
    : savedTheme === "dark"
      ? "dark"
      : window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <ApiProvider>
        <AuthProvider>
          <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <App />
          </BrowserRouter>
        </AuthProvider>
      </ApiProvider>
    </I18nProvider>
  </StrictMode>,
);
