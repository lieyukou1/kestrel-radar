const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

type QueryValue = string | number | boolean | null | undefined;

export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // 非 JSON 错误体：保留状态码即可。
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function exportDigestUrl(
  domain: string,
  date: string,
  format: "markdown" | "json" = "markdown",
  kind?: string,
): string {
  return `${BASE}/export/digest/${date}${queryString({ domain, format, kind })}`;
}

export function backupUrl(domain: string): string {
  return `${BASE}/export/backup${queryString({ domain })}`;
}

export type ArticleAction =
  | "read"
  | "save"
  | "unsave"
  | "dislike"
  | "deep_read"
  | "kb_remove";

export const api = {
  domains: () => request<import("./types").DomainSummary[]>("/domains"),

  domain: (domain: string) =>
    request<import("./types").DomainSummary>(`/domains/${domain}`),

  digests: (domain: string, limit = 30) =>
    request<import("./types").DigestSummary[]>(
      `/digests${queryString({ domain, limit })}`,
    ),

  digest: (domain: string, date: string, kind?: string) =>
    request<import("./types").DigestDetail>(
      `/digests/${date}${queryString({ domain, kind })}`,
    ),

  events: (domain: string, date?: string) =>
    request<{ date: string | null; events: import("./types").EventTopic[] }>(
      `/events${queryString({ domain, date })}`,
    ),

  article: (domain: string, id: number) =>
    request<import("./types").ArticleDetail>(
      `/articles/${id}${queryString({ domain })}`,
    ),

  act: (
    domain: string,
    id: number,
    action: ArticleAction,
    extra: { reason?: string; digest_date?: string } = {},
  ) =>
    request<{
      ok: boolean;
      action: string;
      status: string;
      saved: boolean;
      read: boolean;
    }>(
      `/articles/${id}/actions${queryString({ domain })}`,
      { method: "POST", body: JSON.stringify({ action, ...extra }) },
    ),

  archive: (
    domain: string,
    params: {
      q?: string;
      tag?: string;
      kind?: string;
      status?: string;
      saved?: boolean;
      from?: string;
      to?: string;
      limit?: number;
      offset?: number;
    },
  ) =>
    request<import("./types").ArchiveResult>(
      `/archive${queryString({ domain, ...params })}`,
    ),

  kb: (
    domain: string,
    params: { limit?: number; offset?: number; dimension?: string; kind?: string; saved?: boolean },
  ) =>
    request<import("./types").KBResult>(
      `/kb${queryString({ domain, ...params })}`,
    ),

  search: (domain: string, query: string, external = false) =>
    request<import("./types").SearchResult>(
      `/search${queryString({ domain, q: query, external })}`,
    ),

  qa: (domain: string, question: string) =>
    request<import("./types").QAResult>(`/qa${queryString({ domain })}`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  settings: (domain: string) =>
    request<import("./types").SettingsPayload>(
      `/settings${queryString({ domain })}`,
    ),

  saveSettings: (
    domain: string,
    body: { paused_dimensions?: string[]; paused_sources?: string[] },
  ) =>
    request<import("./types").SettingsPayload>(
      `/settings${queryString({ domain })}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  status: (domain: string) =>
    request<import("./types").StatusPayload>(`/status${queryString({ domain })}`),

  jobs: (domain: string, limit = 20) =>
    request<import("./types").RunRecord[]>(
      `/jobs${queryString({ domain, limit })}`,
    ),

  collect: (domain: string) =>
    request<{ status: string; job: string }>(
      `/jobs/collect${queryString({ domain })}`,
      { method: "POST" },
    ),

  digestJob: (domain: string, body: { preview?: boolean; channel?: string } = {}) =>
    request<{ status: string; job: string }>(
      `/jobs/digest${queryString({ domain })}`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};
