import type { ArticleAction } from "../api/client";
import type { EventTopic } from "../api/types";
import { text } from "../lib/format";
import { ArticleRow } from "./ArticleRow";
import { Badge } from "./ui";

export function EventCard({
  event,
  rank,
  expanded,
  onToggle,
  onOpenArticle,
  onAction,
  busy = false,
}: {
  event: EventTopic;
  rank: number;
  expanded: boolean;
  onToggle: () => void;
  onOpenArticle: (id: number) => void;
  onAction: (id: number, action: ArticleAction) => void;
  busy?: boolean;
}) {
  const lead = rank === 1;
  return (
    <article
      className={`rounded-xl border ${
        lead
          ? "border-cyan-500/40 bg-cyan-500/[0.04]"
          : "border-slate-800 bg-slate-900/40"
      }`}
    >
      <button
        type="button"
        className="flex w-full items-start gap-3 p-3 text-left sm:p-4"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <span
          className={`mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-sm font-semibold ${
            lead ? "bg-cyan-500 text-slate-950" : "bg-slate-800 text-slate-300"
          }`}
        >
          {rank}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold leading-7 text-slate-100 sm:text-lg">
              {event.label}
            </span>
            {event.dimension ? <Badge tone="cyan">{event.dimension}</Badge> : null}
            {event.day_index > 1 ? (
              <Badge tone="amber">第 {event.day_index} 天</Badge>
            ) : null}
            {event.new_count > 0 ? (
              <Badge tone="emerald">+{event.new_count} 新</Badge>
            ) : null}
          </span>
          <span className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-500">
            <span>{event.item_count} 篇</span>
            {event.event_date ? <span>事件日 {event.event_date}</span> : null}
          </span>
          {event.summary ? (
            <span className="mt-2 block text-sm leading-6 text-slate-400">
              {text(event.summary)}
            </span>
          ) : null}
        </span>
        <span className="mt-1 text-sm text-slate-500">{expanded ? "收起" : "展开"}</span>
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-slate-800/80 p-3">
          {event.items.map((item) => (
            <ArticleRow
              key={item.id}
              article={item}
              onOpen={onOpenArticle}
              onAction={onAction}
              busy={busy}
            />
          ))}
        </div>
      ) : null}
    </article>
  );
}
