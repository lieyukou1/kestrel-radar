import type { ReactNode } from "react";

const TONES = {
  slate: "border-slate-700 bg-slate-800/60 text-slate-300",
  cyan: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300",
  amber: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  emerald: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  rose: "border-rose-500/40 bg-rose-500/10 text-rose-300",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({
  children,
  tone = "slate",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] leading-4 ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export const buttonClass =
  "inline-flex items-center gap-1 rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 transition hover:border-cyan-500 hover:text-cyan-300 disabled:cursor-not-allowed disabled:opacity-50";

export const primaryButtonClass =
  "inline-flex items-center gap-1 rounded-md bg-cyan-500 px-3 py-1.5 text-xs font-medium text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:opacity-50";

export const inputClass =
  "w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500";

export function Panel({
  title,
  extra,
  children,
  className = "",
}: {
  title: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-slate-800 bg-slate-900/50 p-4 ${className}`}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-800 px-4 py-8 text-center text-sm text-slate-500">
      {children}
    </div>
  );
}

export function Loading({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-6 text-sm text-slate-500">
      <span className="h-3 w-3 animate-pulse rounded-full bg-cyan-500" />
      {label}
    </div>
  );
}

export function ErrorBox({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 px-4 py-3 text-sm text-rose-300">
      <span>加载失败：{message}</span>
      {onRetry ? (
        <button type="button" className={`${buttonClass} ml-3`} onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}
