import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";

import type { AppContext } from "../App";

export const testContext: AppContext = { domain: "ai", domains: [] };

export function renderPage(
  element: ReactElement,
  context: AppContext = testContext,
  options: { path?: string; entry?: string } = {},
) {
  const path = options.path ?? "/";
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[options.entry ?? path]}>
        <Routes>
          <Route element={<Outlet context={context} />}>
            <Route path={path} element={element} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
