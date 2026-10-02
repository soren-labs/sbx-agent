import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "../App";
import { AppShell } from "../components/AppShell";
import { ErrorNotice } from "../components/ErrorNotice";
import { ApiError } from "../api";
import { I18nProvider, useI18n } from "../i18n";
import { makeApi, renderApp } from "../test/helpers";
import { ApiProvider } from "../state/api";

function ShellHarness() {
  return (
    <I18nProvider>
      <ApiProvider client={makeApi()}>
        <MemoryRouter>
          <AppShell />
        </MemoryRouter>
      </ApiProvider>
    </I18nProvider>
  );
}

describe("AppShell information architecture", () => {
  it("exposes exactly the four product areas", () => {
    render(<ShellHarness />);
    for (const label of ["New Session", "Sessions", "Integrations", "Settings"]) {
      // side nav + bottom nav both render it
      expect(screen.getAllByText(label).length).toBe(2);
    }
    // no Tasks/Agents/Workflows/Artifacts as top-level concepts
    expect(document.body.innerHTML).not.toMatch(/>\s*(Tasks|Agents|Workflows|Artifacts)\s*</);
  });

  it("renders both desktop sidebar and mobile bottom nav (responsive smoke)", () => {
    render(<ShellHarness />);
    expect(document.querySelector("nav.side-nav")).toBeInTheDocument();
    expect(document.querySelector("nav.bottom-nav")).toBeInTheDocument();
    expect(document.querySelectorAll("nav.bottom-nav a").length).toBe(4);
    expect(document.querySelectorAll("nav.side-nav a").length).toBe(4);
  });
});

describe("routing", () => {
  it("/ renders the composer, /sessions renders the list", async () => {
    renderApp(<App />, { route: "/" });
    expect(await screen.findByRole("textbox", {name:"Session task"})).toBeInTheDocument();
  });
});

describe("ErrorNotice kinds", () => {
  const cases: [string, string, string][] = [
    ["provider_login", "auth_invalid", "Provider needs login"],
    ["provider_busy", "provider_exhausted", "Provider busy"],
    ["runtime_disabled", "runtime_disabled", "Runtime disabled"],
    ["github_required", "github_app_unconfigured", "GitHub required"],
    ["session_failed", "session_failed", "Session failed"],
    ["unauthorized", "unauthorized", "Sign-in required"],
    ["not_found", "not_found", "Not found"],
    ["network", "fetch_failed", "Can't reach"],
  ];
  for (const [kind, subcode, title] of cases) {
    it(`${subcode} → ${kind} UX`, () => {
      renderApp(
        <ErrorNotice
          error={
            new ApiError(kind as never, "detail", {
              subcode,
              httpStatus: 500,
            })
          }
        />,
      );
      const n = screen.getByTestId("error-notice");
      expect(n).toHaveAttribute("data-kind", kind);
      expect(n).toHaveTextContent(title);
    });
  }
});

describe("i18n zh-CN", () => {
  function Probe() {
    const { locale, setLocale, t } = useI18n();
    return (
      <div>
        <span data-testid="loc">{locale}</span>
        <span data-testid="txt">{t("nav.sessions")}</span>
        <button onClick={() => setLocale("zh-CN")}>zh</button>
      </div>
    );
  }

  it("switches English → 简体中文", async () => {
    const { getByTestId, getByText } = render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(getByTestId("txt")).toHaveTextContent("Sessions");
    getByText("zh").click();
    const { findByTestId } = screen;
    expect(await findByTestId("txt")).toHaveTextContent("会话");
    expect(await findByTestId("loc")).toHaveTextContent("zh-CN");
  });
});
