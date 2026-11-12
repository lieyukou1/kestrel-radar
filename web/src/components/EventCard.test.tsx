import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EventTopic } from "../api/types";
import { EventCard } from "./EventCard";

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
  day_index: 2,
  new_count: 3,
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

describe("EventCard", () => {
  it("renders badges and items when expanded", () => {
    render(
      <EventCard
        event={event}
        rank={1}
        expanded
        onToggle={vi.fn()}
        onOpenArticle={vi.fn()}
        onAction={vi.fn()}
      />,
    );
    expect(screen.getByText("测试事件")).toBeInTheDocument();
    expect(screen.getByText("第 2 天")).toBeInTheDocument();
    expect(screen.getByText("+3 新")).toBeInTheDocument();
    expect(screen.getByText("文章标题")).toBeInTheDocument();
  });

  it("calls onToggle when the header is clicked", async () => {
    const onToggle = vi.fn();
    render(
      <EventCard
        event={event}
        rank={1}
        expanded={false}
        onToggle={onToggle}
        onOpenArticle={vi.fn()}
        onAction={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /测试事件/ }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});
