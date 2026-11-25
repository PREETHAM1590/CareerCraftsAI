"use client";

// Read-approve queue, built for the phone: every agent run waiting at its
// approval gate, one card each, with the draft readable and an
// approve/reject decision on it. Desktop users get the same list wider.
import { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/lib/api";

type RunRow = {
  id: string;
  agent_type: string;
  status: string;
  requires_extension: boolean;
  started_at?: string | null;
  output?: Record<string, unknown> | null;
};

// Agents store their draft under different keys (and often inside
// pending_action); show the first readable text.
const DRAFT_KEYS = ["content", "body", "message", "draft", "cover_letter", "about", "headline"];

function draftText(output: RunRow["output"]): string {
  const sources = [output, output?.pending_action, output?.result];
  for (const source of sources) {
    if (!source || typeof source !== "object") continue;
    for (const key of DRAFT_KEYS) {
      const value = (source as Record<string, unknown>)[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return "";
}

const AGENT_LABELS: Record<string, string> = {
  email: "Email draft",
  followup: "Follow-up",
  linkedin_outreach: "LinkedIn outreach",
  auto_apply: "Auto-apply",
  cover_letter: "Cover letter",
  resume: "Resume tailor",
  salary: "Negotiation",
};

function AgentBadge({ type }: { type: string }) {
  return (
    <span className="rounded-full bg-neutral-100 px-2.5 py-0.5 text-xs font-medium text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300">
      {AGENT_LABELS[type] ?? type}
    </span>
  );
}

export default function ApprovalsPage() {
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { data } = await apiClient.get<{ runs?: RunRow[] }>("/agents/runs", {
        params: { status: "awaiting_approval" },
      });
      setRuns(Array.isArray(data?.runs) ? data.runs : []);
      setError(null);
    } catch {
      setError("Could not load the approval queue.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const decide = async (run: RunRow, approved: boolean) => {
    setBusy(run.id);
    try {
      await apiClient.post(`/agents/${run.id}/approve`, { approved });
      setRuns((prev) => (prev ?? []).filter((r) => r.id !== run.id));
    } catch {
      setError("Could not record the decision. Try again.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-2xl px-4 pb-24 pt-6 md:pt-10">
      <header className="mb-5">
        <h1 className="text-xl font-semibold tracking-tight md:text-2xl">Approvals</h1>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
          Review drafts here and submit applications from the browser extension.
        </p>
      </header>

      {error && (
        <p role="alert" className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {runs === null ? (
        <div className="space-y-3" aria-busy>
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-24 animate-pulse rounded-xl bg-neutral-100 dark:bg-neutral-800" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <p className="rounded-xl border border-dashed border-neutral-300 px-4 py-10 text-center text-sm text-neutral-500 dark:border-neutral-700">
          Nothing is waiting for you. When an agent drafts something, it shows up here.
        </p>
      ) : (
        <ul className="space-y-3">
          {runs.map((run) => {
            const draft = draftText(run.output);
            return (
            <li
              key={run.id}
              className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900"
            >
              <div className="flex items-center justify-between gap-3">
                <AgentBadge type={run.agent_type} />
                <time className="text-xs text-neutral-500" dateTime={run.started_at ?? undefined}>
                  {run.started_at ? new Date(run.started_at).toLocaleDateString() : ""}
                </time>
              </div>

              <p className="mt-2 line-clamp-2 text-sm text-neutral-700 dark:text-neutral-300">
                {draft.slice(0, 240) || "Draft ready for your review."}
              </p>

              {draft && (
                <>
                  <button
                    type="button"
                    onClick={() => setOpenId(openId === run.id ? null : run.id)}
                    className="mt-2 text-sm font-medium text-blue-700 underline-offset-2 hover:underline dark:text-blue-400"
                    aria-expanded={openId === run.id}
                  >
                    {openId === run.id ? "Hide" : "Read the draft"}
                  </button>
                  {openId === run.id && (
                    <div className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-lg bg-neutral-50 p-3 text-sm text-neutral-800 dark:bg-neutral-800 dark:text-neutral-200">
                      {draft}
                    </div>
                  )}
                </>
              )}

              <div className="mt-3 flex gap-2">
                {run.requires_extension ? (
                  <p className="flex-1 py-2.5 text-sm text-neutral-700 dark:text-neutral-300">
                    Review and submit this application in the browser extension.
                  </p>
                ) : (
                  <button
                    type="button"
                    disabled={busy === run.id}
                    onClick={() => decide(run, true)}
                    className="flex-1 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
                  >
                    {busy === run.id ? "Working…" : "Approve"}
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy === run.id}
                  onClick={() => decide(run, false)}
                  className="flex-1 rounded-lg border border-neutral-300 px-4 py-2.5 text-sm font-medium disabled:opacity-50 dark:border-neutral-700"
                >
                  Reject
                </button>
              </div>
            </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
