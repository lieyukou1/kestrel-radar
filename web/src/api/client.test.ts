import { afterEach, describe, expect, it, vi } from "vitest";

import { api, queryString } from "./client";

describe("queryString", () => {
  it("skips empty values but keeps false", () => {
    expect(
      queryString({ domain: "ai", limit: 5, q: "", flag: false }),
    ).toBe("?domain=ai&limit=5&flag=false");
  });

  it("returns empty string without params", () => {
    expect(queryString({})).toBe("");
  });
});

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("raises ApiError with the server detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "没有日报" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    await expect(api.digest("ai", "2026-01-01")).rejects.toMatchObject({
      status: 404,
      message: "没有日报",
    });
  });

  it("builds archive urls with filters", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ total: 0, limit: 20, offset: 0, items: [] }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.archive("finance", {
      q: "降息",
      tag: "2 货币政策",
      saved: true,
      limit: 20,
      offset: 0,
    });

    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toContain("/api/v1/archive?");
    expect(call[0]).toContain("domain=finance");
    expect(call[0]).toContain("q=%E9%99%8D%E6%81%AF");
    expect(call[0]).toContain("saved=true");
  });
});
