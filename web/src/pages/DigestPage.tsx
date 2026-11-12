import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useAppContext } from "../App";
import { api, exportDigestUrl, type ArticleAction } from "../api/client";
import { EventCard } from "../components/EventCard";
import { Badge, Empty, ErrorBox, Loading, buttonClass } from "../components/ui";

export function DigestPage() {
  const { domain } = useAppContext();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<"daily" | "weekly">("daily");
  const [date, setDate] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);

  const digests = useQuery({
    queryKey: ["digests", domain],
    queryFn: () => api.digests(domain, 60),
  });
  const options = (digests.data ?? []).filter((item) => item.kind === kind);
  const active = date || options[0]?.date || "";

  const detail = useQuery({
    queryKey: ["digest", domain, active, kind],
    queryFn: () => api.digest(domain, active, kind),
    enabled: Boolean(active),
  });

  const action = useMutation({
    mutationFn: ({ id, type }: { id: number; type: ArticleAction }) =>
      api.act(domain, id, type),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["digest"] });
    },
  });

  useEffect(() => {
    setDate("");
    setExpanded(null);
  }, [domain, kind]);

  useEffect(() => {
    if (!detail.data || detail.data.events.length === 0) return;
    setExpanded((current) =>
      current && detail.data.events.some((event) => event.id === current)
        ? current
        : detail.data.events[0].id,
    );
  }, [detail.data]);

  const digest = detail.data?.digest;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">日报</h1>
          <p className="mt-1 text-xs text-slate-500">
            一级看事件，点开看文章；主线事件置顶。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-md border border-slate-700 p-0.5 text-xs">
            {(["daily", "weekly"] as const).map((item) => (
              <button
                key={item}
                type="button"
                className={`rounded px-2 py-1 ${
                  kind === item ? "bg-cyan-500 text-slate-950" : "text-slate-400"
                }`}
                onClick={() => setKind(item)}
              >
                {item === "daily" ? "日报" : "周报"}
              </button>
            ))}
          </div>
          <select
            aria-label="选择日期"
            value={active}
            onChange={(event) => setDate(event.target.value)}
            className="max-w-full rounded-md border border-slate-700 bg-slate-900 px-2 py-1.5 text-xs text-slate-200 outline-none focus:border-cyan-500"
          >
            {options.length === 0 ? <option value="">暂无记录</option> : null}
            {options.map((item) => (
              <option key={`${item.date}-${item.kind}`} value={item.date}>
                {item.date} · {item.status} · {item.count ?? 0} 条
              </option>
            ))}
          </select>
          <button
            type="button"
            className={buttonClass}
            onClick={() => void detail.refetch()}
            disabled={!active || detail.isFetching}
          >
            {detail.isFetching ? "刷新中…" : "刷新"}
          </button>
          {active ? (
            <a
              className={buttonClass}
              href={exportDigestUrl(domain, active, "markdown", kind)}
              download
            >
              导出 MD
            </a>
          ) : null}
        </div>
      </div>

      {digests.isLoading ? <Loading /> : null}
      {digests.isError ? (
        <ErrorBox error={digests.error} onRetry={() => void digests.refetch()} />
      ) : null}
      {!digests.isLoading && options.length === 0 ? (
        <Empty>这个领域还没有{kind === "daily" ? "日报" : "周报"}。去「状态」页触发一次生成。</Empty>
      ) : null}

      {detail.isLoading ? <Loading label="加载日报…" /> : null}
      {detail.isError ? (
        <ErrorBox error={detail.error} onRetry={() => void detail.refetch()} />
      ) : null}

      {digest ? (
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <Badge tone={digest.status === "sent" ? "emerald" : "amber"}>
            {digest.status}
          </Badge>
          <span>{digest.events_count ?? 0} 个事件</span>
          <span>{digest.count ?? 0} 篇内容</span>
          {digest.theme ? (
            <span className="text-cyan-300">主线：{digest.theme}</span>
          ) : null}
        </div>
      ) : null}

      <div className="space-y-3">
        {(detail.data?.events ?? []).map((event, index) => (
          <EventCard
            key={event.id}
            event={event}
            rank={index + 1}
            expanded={expanded === event.id}
            onToggle={() =>
              setExpanded((current) => (current === event.id ? null : event.id))
            }
            onOpenArticle={(id) => navigate(`/articles/${id}`)}
            onAction={(id, type) => action.mutate({ id, type })}
            busy={action.isPending}
          />
        ))}
      </div>

      {detail.data && detail.data.events.length === 0 ? (
        <Empty>这一天没有事件。</Empty>
      ) : null}

      {detail.data && detail.data.briefs.length > 0 ? (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-slate-200">周报分组</h2>
          {detail.data.briefs.map((item) => (
            <button
              key={`${item.section}-${item.id}`}
              type="button"
              className="block w-full rounded-lg border border-slate-800 bg-slate-900/40 p-3 text-left hover:border-cyan-500/40"
              onClick={() => navigate(`/articles/${item.id}`)}
            >
              <span className="text-sm text-slate-200">{item.title}</span>
              <span className="ml-2 text-[11px] text-slate-500">
                {item.section === "weekly_paper" ? "论文" : "仓库"}
              </span>
            </button>
          ))}
        </section>
      ) : null}
    </div>
  );
}
