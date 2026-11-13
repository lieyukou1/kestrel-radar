import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAppContext } from "../App";
import { api } from "../api/client";
import type { QAResult } from "../api/types";
import {
  Badge,
  Empty,
  ErrorBox,
  buttonClass,
  inputClass,
  pageHintClass,
  pageTitleClass,
  primaryButtonClass,
} from "../components/ui";

type Entry = { question: string; result: QAResult };

export function AskPage() {
  const { domain } = useAppContext();
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Entry[]>([]);

  const ask = useMutation({
    mutationFn: (value: string) => api.qa(domain, value),
    onSuccess: (result, value) => {
      setHistory((current) => [{ question: value, result }, ...current]);
      setQuestion("");
    },
  });

  useEffect(() => {
    setHistory([]);
    setQuestion("");
  }, [domain]);

  const submit = () => {
    const value = question.trim();
    if (value.length < 2) return;
    ask.mutate(value);
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className={pageTitleClass}>问答</h1>
        <p className={pageHintClass}>
          只在已精读 / 已收藏的内容里找依据；没有依据会直说。
        </p>
      </div>

      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <textarea
          aria-label="问题"
          className={`${inputClass} min-h-24`}
          placeholder="例如：这篇论文的方法和局限是什么？"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="flex items-center gap-2">
          <button
            type="submit"
            className={primaryButtonClass}
            disabled={ask.isPending || question.trim().length < 2}
          >
            {ask.isPending ? "检索中…" : "提问"}
          </button>
          <span className="text-sm text-slate-600">Ctrl/⌘ + Enter 提交</span>
        </div>
      </form>

      {ask.isError ? <ErrorBox error={ask.error} /> : null}

      <div className="space-y-3">
        {history.length === 0 ? (
          <Empty>还没有提问。先精读几篇，答案才有依据。</Empty>
        ) : null}
        {history.map((entry, index) => (
          <article
            key={`${entry.question}-${index}`}
            className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"
          >
            <div className="text-base font-medium leading-7 text-slate-200">
              {entry.question}
            </div>
            {entry.result.supported ? (
              <div className="mt-3 space-y-3">
                <p className="whitespace-pre-wrap text-base leading-8 text-slate-300">
                  {entry.result.answer}
                </p>
                {entry.result.sources && entry.result.sources.length > 0 ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-slate-500">依据</span>
                    {entry.result.sources.map((source, sourceIndex) => (
                      <a
                        key={source.document_id}
                        className={buttonClass}
                        href={source.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        [{sourceIndex + 1}] {source.title}
                      </a>
                    ))}
                  </div>
                ) : null}
                {entry.result.citations && entry.result.citations.length > 0 ? (
                  <div className="space-y-1.5 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2">
                    <div className="text-sm text-slate-500">引用</div>
                    {entry.result.citations.map((citation) => (
                      <div
                        key={`${citation.label}-${citation.claim_id}-${citation.evidence_id}`}
                        className="flex flex-wrap items-center gap-2 text-sm text-slate-400"
                      >
                        <Link
                          className="rounded border border-slate-700 px-1.5 py-0.5 font-mono text-xs text-cyan-300 hover:border-cyan-500"
                          to={`/articles/${citation.document_id}`}
                        >
                          {citation.label}
                        </Link>
                        {citation.anchor ? (
                          <span className="text-slate-500">{citation.anchor}</span>
                        ) : null}
                        {citation.quote ? (
                          <span className="line-clamp-2 flex-1 text-slate-400">
                            {citation.quote}
                          </span>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : (
              <div className="mt-3 space-y-2">
                <p className="text-base text-amber-300">
                  知识库里没有足够依据回答这个问题。
                </p>
                {entry.result.related && entry.result.related.length > 0 ? (
                  <div className="space-y-1">
                    <div className="text-sm text-slate-500">可能相关</div>
                    {entry.result.related.map((item) => (
                      <div key={item.document_id} className="text-base text-slate-400">
                        <a
                          className="hover:text-cyan-300"
                          href={item.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {item.title}
                        </a>
                        <Badge>{item.reason}</Badge>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
