import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Pagination } from "./ui";

describe("Pagination", () => {
  it("shows total pages and supports page size and direct jump", async () => {
    const onPageChange = vi.fn();
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        page={0}
        pageSize={10}
        total={95}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
      />,
    );

    expect(screen.getByText(/共 95 条 · 第 1\/10 页/)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("每页条数"), "50");
    expect(onPageSizeChange).toHaveBeenCalledWith(50);

    await userEvent.type(screen.getByLabelText("跳转页码"), "3");
    await userEvent.click(screen.getByRole("button", { name: "跳转" }));
    expect(onPageChange).toHaveBeenCalledWith(2);
  });
});
