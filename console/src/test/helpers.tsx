import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { SessionApi } from "../api/client";
import { FixtureSessionApi, type MockScenario } from "../api/mock";
import { I18nProvider } from "../i18n";
import { ApiProvider } from "../state/api";

export function makeApi(scenario: MockScenario = "ok") {
  const api = new FixtureSessionApi();
  api.scenario = scenario;
  api.latencyMs = 1;
  return api;
}

export function renderApp(
  ui: ReactElement,
  opts: {
    api?: SessionApi;
    route?: string;
    state?: unknown;
  } = {},
) {
  const api = opts.api ?? makeApi();
  return {
    api,
    ...render(
      <I18nProvider>
        <ApiProvider client={api}>
          <MemoryRouter
            initialEntries={[
              typeof opts.route === "string"
                ? { pathname: opts.route, state: opts.state }
                : (opts.route ?? "/"),
            ]}
          >
            {ui}
          </MemoryRouter>
        </ApiProvider>
      </I18nProvider>,
    ),
  };
}

/** Render inside a real <Route> so useParams() resolves. */
export function renderRoute(
  route: string,
  path: string,
  ui: ReactElement,
  opts: { api?: SessionApi; state?: unknown } = {},
) {
  const api = opts.api ?? makeApi();
  return {
    api,
    ...render(
      <I18nProvider>
        <ApiProvider client={api}>
          <MemoryRouter
            initialEntries={[{ pathname: route, state: opts.state }]}
          >
            <Routes>
              <Route path={path} element={ui} />
            </Routes>
          </MemoryRouter>
        </ApiProvider>
      </I18nProvider>,
    ),
  };
}
