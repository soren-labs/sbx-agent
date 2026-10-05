import "@fontsource-variable/inter";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { I18nProvider } from "./i18n";

const savedTheme = localStorage.getItem("sbx.console.theme");
document.documentElement.dataset.theme =
  savedTheme === "light" || savedTheme === "dark" ? savedTheme : "dark";
document.documentElement.lang = "en";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <BrowserRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
      </BrowserRouter>
    </I18nProvider>
  </StrictMode>,
);
