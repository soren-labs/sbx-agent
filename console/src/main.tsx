import "@fontsource-variable/inter";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/ui.css";
import "./shell/shell.css";
import "./features/auth/auth.css";
import "./features/home/home.css";
import "./features/composer/composer.css";
import "./features/sessions/sessions.css";
import "./features/pages.css";
import { I18nProvider } from "./i18n";
import { AuthProvider } from "./state/auth";
import { ApiProvider } from "./state/context";
import { ToastProvider } from "./ui/Toast";

const savedTheme = localStorage.getItem("sbx.console.theme");
document.documentElement.dataset.theme =
  savedTheme === "light"
    ? "light"
    : savedTheme === "dark"
      ? "dark"
      : window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <ApiProvider>
        <AuthProvider>
          <ToastProvider>
            <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
              <App />
            </BrowserRouter>
          </ToastProvider>
        </AuthProvider>
      </ApiProvider>
    </I18nProvider>
  </StrictMode>,
);
