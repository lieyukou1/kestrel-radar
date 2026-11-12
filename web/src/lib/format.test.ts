import { describe, expect, it } from "vitest";

import { formatCny, text, truncate } from "./format";

describe("format helpers", () => {
  it("formats cny with four decimals", () => {
    expect(formatCny(0.12345)).toBe("¥0.1235");
    expect(formatCny(null)).toBe("¥0.0000");
  });

  it("truncates long text and keeps short text", () => {
    expect(truncate("abcdef", 4)).toBe("abc…");
    expect(truncate("abc", 10)).toBe("abc");
  });

  it("joins list values", () => {
    expect(text(["a", 2])).toBe("a；2");
    expect(text(null)).toBe("");
  });
});
