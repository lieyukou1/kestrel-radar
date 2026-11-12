import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ArticleAction } from "../api/client";
import type { SummaryPayload } from "../api/types";
import { formatDateTime, text } from "../lib/format";
import { ArticleRow } from "./ArticleRow";
import { Badge, buttonClass, Empty, ErrorBox, Loading } from "./ui";

function listText(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => text(item)).filter(Boolean);
}

function KbCard({ card }: { card: SummaryPayload }) {
  const rows: [string, unknown][] = [
    ["问题", card.problem],
    ["方法", card.method],
    ["证据", card.evidence],
    ["结论", card.conclusions],
    ["局限", card.limitations],
  ];
  return (
    <div className="space-y-3">
      {rows.map(([label, value]) => {
        const items = listText(value);
        const body = items.length > 0 ? items.join("；") : text(value);
        if (!body) return null;
        return (
          <div key={label}>
            <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
            <p className="mt-1 text-sm leading-6 text-slate-300">{body}</p>
          </div>
        );
      })}
      {card.l3 ? (
        <div>
          <div className="text-[11px] uppercase tracking-wide text-slate-500">
            工程解读
          </div>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-300">
            {text(card.l3)}
          </p>
        </div>
      ) : null}
    </div>
  );
}

export function ArticleDrawer({
  domain,
  documentId,
  onClose,
  onAction,
}: {
  domain: string;
  documentId: number | null;
  onClose: () => void;
  onAction?: (id: number, action: ArticleAction) => void;
}) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["article", domain, documentId],
    queryFn: () => api.article(domain, documentId as number),
    enabled: documentId !== null,
  });
  const action = useMutation({
    mutationFn: ({ id, type }: { id: number; type: ArticleAction }) =>
      api.act(domain, id, type),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["article", domain, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["digest"] });
      void queryClient.invalidateQueries({ queryKey: ["archive"] });
      void queryClient.invalidateQueries({ queryKey: ["kb"] });
    },
  });

  if (documentId === null) return null;
  const detail = query.data;

  const run = (id: number, type: ArticleAction) => {
    if (type === "kb_remove" && !window.confirm("移出知识库会删除这篇的精读卡片，确定吗？")) {
      return;
    }
    action.mutate({ id, type });
    onAction?.(id, type);
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button
        type="button"
        aria-label="关闭详情"
        className="absolute inset-0 bg-slate-950/70 backdrop-blur-sm"
        onClick={onClose}
      />
      <aside className="radar-scroll relative h-full w-full max-w-2xl overflow-y-auto border-l border-slate-800 bg-slate-950 p-4 shadow-2xl sm:p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <span className="text-xs text-slate-500">文章详情 #{documentId}</span>
          <button type="button" className={buttonClass} onClick={onClose}>
            关闭
          </button>
        </div>

        {query.isLoading ? <Loading /> : null}
        {query.isError ? <ErrorBox error={query.error} onRetry={() => void query.refetch()} /> : null}

        {detail ? (
          <div className="space-y-5">
            <div>
              <h2 className="text-lg font-semibold leading-7 text-slate-100">
                {detail.document.title}
              </h2>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                {detail.document.tag ? <Badge tone="cyan">{detail.document.tag}</Badge> : null}
                <span>{detail.document.kind}</span>
                <span>{detail.document.authority} 级</span>
                <span>{formatDateTime(detail.document.published_at)}</span>
                {detail.read ? <Badge tone="emerald">已读</Badge> : null}
                {detail.saved ? <Badge tone="amber">收藏</Badge> : null}
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <a className={buttonClass} href={detail.document.url} target="_blank" rel="noreferrer">
                  打开原文
                </a>
                <button type="button" className={buttonClass} disabled={action.isPending} onClick={() => run(documentId, "read")}>
                  标记已读
                </button>
                <button type="button" className={buttonClass} disabled={action.isPending} onClick={() => run(documentId, detail.saved ? "unsave" : "save")}>
                  {detail.saved ? "取消收藏" : "加入收藏"}
                </button>
                {detail.kb ? (
                  <button type="button" className={buttonClass} disabled={action.isPending} onClick={() => run(documentId, "kb_remove")}>
                    移出知识库
                  </button>
                ) : (
                  <button type="button" className={buttonClass} disabled={action.isPending} onClick={() => run(documentId, "deep_read")}>
                    精读这篇
                  </button>
                )}
              </div>
            </div>

            {detail.document.abstract ? (
              <p className="text-sm leading-6 text-slate-400">{detail.document.abstract}</p>
            ) : null}

            {detail.kb ? (
              <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
                <h3 className="mb-3 text-sm font-semibold text-slate-200">知识卡片</h3>
                <KbCard card={detail.kb} />
              </section>
            ) : null}

            {detail.claims.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold text-slate-200">要点</h3>
                <ul className="list-inside list-disc space-y-1 text-sm leading-6 text-slate-400">
                  {detail.claims.map((claim, index) => (
                    <li key={`${claim.kind}-${index}`}>{claim.text}</li>
                  ))}
                </ul>
              </section>
            ) : null}

            {detail.notes.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold text-slate-200">批注</h3>
                <ul className="space-y-1 text-sm text-slate-400">
                  {detail.notes.map((note, index) => (
                    <li key={index}>{note.content}</li>
                  ))}
                </ul>
              </section>
            ) : null}

            {detail.sources.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold text-slate-200">来源</h3>
                <div className="flex flex-wrap gap-2 text-xs text-slate-400">
                  {detail.sources.map((source) => (
                    <Badge key={source.source_id}>{source.name}</Badge>
                  ))}
                </div>
              </section>
            ) : null}

            {detail.appearances.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold text-slate-200">出现在</h3>
                <ul className="space-y-1 text-xs text-slate-500">
                  {detail.appearances.map((item, index) => (
                    <li key={index}>
                      {item.digest_date} · {item.kind} · {item.label || item.section}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </div>
        ) : null}
        {!query.isLoading && !query.isError && !detail ? <Empty>没有这篇的详情。</Empty> : null}
      </aside>
    </div>
  );
}
