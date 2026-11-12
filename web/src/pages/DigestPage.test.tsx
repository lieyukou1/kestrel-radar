import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { DigestDetail, DigestSummary, EventTopic } from "../api/types";
import { renderPage } from "../test/render";
import { DigestPage } from "./DigestPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      digests: vi.fn(),
      digest: vi.fn(),
      act: vi.fn(),
    },
  };
});

const digest: DigestSummary = {
  date: "2026-11-12",
  kind: "daily",
  status: "sent",
  created_at: "2026-11-12T03:00:00+00:00",
  sent_at: "2026-11-12T03:10:00+00:00",
  count: 2,
  events_count: 1,
  theme: "主线事件",
  stats: {},
};

const event: EventTopic = {
  id: 7,
  label: "测试事件",
  summary: "值得关注",
  entity: "nvidia",
  dimension: "1 模型与架构",
  score: 0.9,
  item_count: 1,
  event_date: null,
  event_key: null,
  first_date: null,
  day_index: 1,
  new_count: 0,
  items: [
    {
      id: 11,
      kind: "blog",
      title: "文章标题",
      abstract: null,
      url: "https://example.com",
      published_at: null,
      first_seen_at: "",
      authority: "A",
      status: "pushed",
      tag: "1 模型与架构",
      tags: [],
      read: false,
      read_at: null,
      read_count: 0,
      local_path: null,
      angle: null,
      item_rank: 1,
      summary: { why: "理由" },
      saved: false,
    },
  ],
};

const detail: DigestDetail = { digest, events: [event], briefs: [] };

describe("DigestPage", () => {
  it("renders the event list with the first event expanded", async () => {
    vi.mocked(api.digests).mockResolvedValue([digest]);
    vi.mocked(api.digest).mockResolvedValue(detail);

    renderPage(<DigestPage />);

    expect(await screen.findByText("测试事件")).toBeInTheDocument();
    expect(screen.getByText("主线：主线事件")).toBeInTheDocument();
    expect(screen.getByText("文章标题")).toBeInTheDocument();
  });
});
