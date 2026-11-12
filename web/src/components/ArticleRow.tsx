import type { ArticleAction } from "../api/client";
import type { Article, EventItem, SummaryPayload } from "../api/types";
import { formatDate, text, truncate } from "../lib/format";
import { Badge, buttonClass } from "./ui";

type RowArticle = Article & {
  saved?: boolean;
  summary?: SummaryPayload | null;
};

export function ArticleRow({
  article,
  onOpen,
  onAction,
  busy = false,
  showAbstract = false,
}: {
  article: RowArticle | EventItem;
  onOpen?: (id: number) => void;
  onAction?: (id: number, action: ArticleAction) => void;
  busy?: boolean;
  showAbstract?: boolean;
}) {
  const why = article.summary
    ? text(article.summary.why ?? article.summary.problem ?? article.summary.conclusion)
    : "";
  return (
    <article className="rounded-lg border border-slate-800/80 bg-slate-950/40 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <button
            type="button"
            className="text-left text-sm font-medium text-slate-100 hover:text-cyan-300"
            onClick={() => onOpen?.(article.id)}
          >
            {article.title}
          </button>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            {article.tag ? <Badge tone="cyan">{article.tag}</Badge> : null}
            <span>{article.kind}</span>
            <span>{formatDate(article.published_at)}</span>
            {article.read ? <Badge tone="emerald">已读</Badge> : null}
            {article.saved ? <Badge tone="amber">收藏</Badge> : null}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <a
            className={buttonClass}
            href={article.url}
            target="_blank"
            rel="noreferrer"
          >
            原文
          </a>
          {onAction ? (
            <>
              <button
                type="button"
                className={buttonClass}
                disabled={busy}
                onClick={() => onAction(article.id, "read")}
              >
                已读
              </button>
              <button
                type="button"
                className={buttonClass}
                disabled={busy}
                onClick={() => onAction(article.id, article.saved ? "unsave" : "save")}
              >
                {article.saved ? "取消收藏" : "收藏"}
              </button>
            </>
          ) : null}
        </div>
      </div>
      {why ? (
        <p className="mt-2 text-xs leading-5 text-slate-400">{why}</p>
      ) : showAbstract && article.abstract ? (
        <p className="mt-2 text-xs leading-5 text-slate-400">
          {truncate(article.abstract, 180)}
        </p>
      ) : null}
    </article>
  );
}
