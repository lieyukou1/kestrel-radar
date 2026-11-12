import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { DomainSummary, SettingsPayload } from "../api/types";
import { renderPage } from "../test/render";
import { SettingsPage } from "./SettingsPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      domain: vi.fn(),
      settings: vi.fn(),
      saveSettings: vi.fn(),
    },
  };
});

const settings: SettingsPayload = {
  domain: null,
  paused_dimensions: [],
  paused_sources: [],
  preferences: { "dimension:1 模型与架构": 0.9 },
  theme: null,
};

const domainInfo: DomainSummary = {
  id: "ai",
  name: "AI 观察",
  taxonomy: [{ name: "1 模型与架构", description: "模型与架构" }],
  defaults: {},
  sources: [
    { id: "rss", name: "中文媒体", kind: "rss", authority: "A", enabled: true },
  ],
  weights: {},
  db: "data/domains/ai/ai_radar.db",
  db_exists: true,
  last_digest: { date: "2026-11-12", kind: "daily", status: "sent" },
};

describe("SettingsPage", () => {
  it("saves paused dimensions", async () => {
    vi.mocked(api.settings).mockResolvedValue(settings);
    vi.mocked(api.domain).mockResolvedValue(domainInfo);
    vi.mocked(api.saveSettings).mockResolvedValue({
      ...settings,
      paused_dimensions: ["1 模型与架构"],
    });

    renderPage(<SettingsPage />);
    await userEvent.click(
      await screen.findByRole("button", { name: /1 模型与架构/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "保存设置" }));

    await waitFor(() =>
      expect(api.saveSettings).toHaveBeenCalledWith("ai", {
        paused_dimensions: ["1 模型与架构"],
        paused_sources: [],
      }),
    );
    expect(await screen.findByText(/已保存/)).toBeInTheDocument();
  });

  it("links to the domain backup and shows the latest digest", async () => {
    vi.mocked(api.settings).mockResolvedValue(settings);
    vi.mocked(api.domain).mockResolvedValue(domainInfo);

    renderPage(<SettingsPage />);

    const backup = await screen.findByRole("link", {
      name: "下载数据库备份",
    });
    expect(backup).toHaveAttribute("href", "/api/v1/export/backup?domain=ai");
    expect(
      await screen.findByText(/最近日报：2026-11-12/),
    ).toBeInTheDocument();
  });
});
