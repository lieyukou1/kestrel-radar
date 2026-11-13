import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { ArticleDetail } from "../api/types";
import { renderPage, testContext } from "../test/render";
import { ArticlePage } from "./ArticlePage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      article: vi.fn(),
      act: vi.fn(),
    },
  };
});

const detail: ArticleDetail = {
  document: {
    id: 11,
    kind: "blog",
    title: "测试文章",
    abstract: "摘要",
    url: "https://example.com",
    published_at: "2026-11-12T00:00:00+00:00",
    first_seen_at: "",
    authority: "A",
    status: "pushed",
    tag: "1 模型与架构",
    tags: [],
    read: false,
    read_at: null,
    read_count: 0,
    local_path: null,
  },
  summary: null,
  kb: null,
  reading: null,
  sections: [],
  claims: [],
  notes: [],
  sources: [],
  appearances: [],
  saved: false,
  read: false,
};

function renderArticle() {
  return renderPage(<ArticlePage />, testContext, {
    path: "/articles/:documentId",
    entry: "/articles/11",
  });
}

describe("ArticlePage", () => {
  it("renders as a page instead of an overlay", async () => {
    vi.mocked(api.article).mockResolvedValue(detail);

    renderArticle();

    expect(await screen.findByText("测试文章")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /返回/ })).toBeInTheDocument();
    expect(screen.getByText("文档信息")).toBeInTheDocument();
  });

  it("shows immediate feedback after requesting a deep read", async () => {
    vi.mocked(api.article).mockResolvedValue(detail);
    vi.mocked(api.act).mockResolvedValue({
      ok: true,
      action: "deep_read",
      status: "deep_reading",
      saved: false,
      read: false,
    });

    renderArticle();

    await userEvent.click(
      await screen.findByRole("button", { name: "精读这篇" }),
    );

    expect(
      await screen.findByText(/精读进行中：抓取原文/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /精读排队中/ }),
    ).toBeDisabled();
  });

  it("renders the knowledge card sections", async () => {
    vi.mocked(api.article).mockResolvedValue({
      ...detail,
      kb: {
        problem: "为什么要做",
        method: "怎么做",
        evidence: ["证据一", "证据二"],
        conclusions: ["结论一"],
        limitations: ["局限一"],
        l3: "工程解读正文",
      },
    });

    renderArticle();

    expect(await screen.findByText("要解决的问题")).toBeInTheDocument();
    expect(screen.getByText("为什么要做")).toBeInTheDocument();
    expect(screen.getByText("证据二")).toBeInTheDocument();
    expect(screen.getByText("局限一")).toBeInTheDocument();
    expect(screen.getByText("工程解读正文")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "移出知识库" }),
    ).toBeInTheDocument();
  });

  it("renders the new reading view with numbered, clickable citations", async () => {
    vi.mocked(api.article).mockResolvedValue({
      ...detail,
      reading: {
        thesis: "Carbon 用更低成本达到第一梯队",
        narrative: "作者先给出评测结果 [C1]。",
        questions: [],
        outline: [],
        main_claims: [
          {
            claim_id: 101,
            text: "Carbon 在 CursorBench 上排名第一",
            note: "支撑全文",
          },
        ],
        relations: [],
        critique: {},
      },
      claims: [
        {
          id: 101,
          kind: "finding",
          text: "Carbon 在 CursorBench 上排名第一",
          claim_type: "main",
          confidence: "source",
          quote: "CursorBench 53.9%",
          anchor: "评测 · 片段 1",
          question: "CursorBench 表现如何？",
          status: "active",
          section_id: null,
          evidence: [],
        },
      ],
    });

    renderArticle();

    expect(await screen.findByText("核心主张")).toBeInTheDocument();
    expect(screen.getByText("C1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "[C1]" })).toHaveAttribute(
      "href",
      "#claim-C1",
    );
  });
});
