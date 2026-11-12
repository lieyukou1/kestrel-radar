import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { renderPage } from "../test/render";
import { AskPage } from "./AskPage";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, qa: vi.fn() } };
});

describe("AskPage", () => {
  it("shows the answer and sources", async () => {
    vi.mocked(api.qa).mockResolvedValue({
      supported: true,
      answer: "这是答案",
      sources: [{ document_id: 1, title: "来源一", url: "https://example.com" }],
    });

    renderPage(<AskPage />);
    await userEvent.type(screen.getByLabelText("问题"), "这篇讲了什么？");
    await userEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText("这是答案")).toBeInTheDocument();
    expect(screen.getByText(/来源一/)).toBeInTheDocument();
  });

  it("renders the unsupported state", async () => {
    vi.mocked(api.qa).mockResolvedValue({ supported: false, related: [] });

    renderPage(<AskPage />);
    await userEvent.type(screen.getByLabelText("问题"), "没有依据的问题");
    await userEvent.click(screen.getByRole("button", { name: "提问" }));

    expect(await screen.findByText(/没有足够依据/)).toBeInTheDocument();
  });
});
