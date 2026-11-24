"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { toast } from "sonner";
import {
  ArrowUpRight,
  Buildings,
  ChatCircleText,
  Check,
  CircleNotch,
  PaperPlaneTilt,
  UsersThree,
  X,
} from "@phosphor-icons/react";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { LinkedInSending, useLinkedInAccount } from "@/components/agents/LinkedInSending";
import { cn } from "@/lib/utils";
import {
  Bezel,
  EmptyPanel,
  Field,
  Hairline,
  Input,
  IslandButton,
  listItem,
  Notice,
  PageHero,
  PanelTitle,
  Reveal,
  RevealGroup,
  Screen,
  Section,
  SectionHeading,
  Skeleton,
  StatusPill,
  type StatusTone,
} from "@/components/vanguard";

interface OutreachRun {
  id: string;
  status: string;
  input: { company_name?: string } | null;
  output: { contacts?: Array<{ name: string; title: string; linkedin_url: string }>; messages?: Array<{ contact_name: string; message: string }> } | null;
  started_at: string | null;
}

/* ───────────────────────────── helpers ───────────────────────────── */

function statusTone(status: string): StatusTone {
  if (status === "completed") return "success";
  if (status === "awaiting_approval") return "warning";
  if (status === "failed" || status === "cancelled") return "danger";
  return "primary";
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

function formatStarted(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function isSafeProfileUrl(url: string | undefined): url is string {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

/**
 * Asymmetric bento spans for the queue (lg+ only; everything is one column below md).
 *  1 card  → full width
 *  2 cards → 7 / 5
 *  3+      → featured 8×2 rows beside two stacked 4s, then alternating 7/5 · 5/7 pairs
 */
function spanFor(index: number, total: number): string {
  if (total === 1) return "lg:col-span-12";
  if (total === 2) return index === 0 ? "lg:col-span-7" : "lg:col-span-5";
  if (index === 0) return "lg:col-span-8 lg:row-span-2";
  if (index <= 2) return "lg:col-span-4";
  const cycle = (index - 3) % 4;
  return cycle === 0 || cycle === 3 ? "lg:col-span-7" : "lg:col-span-5";
}

/* ───────────────────────────── queue card ───────────────────────────── */

interface QueueCardProps {
  run: OutreachRun;
  featured: boolean;
  deciding: boolean;
  onDecide: (approved: boolean) => void;
  canSend?: boolean;
}

function QueueCard({ run, featured, deciding, onDecide, canSend }: QueueCardProps) {
  const [expanded, setExpanded] = useState(false);
  const contacts = run.output?.contacts ?? [];
  const messages = run.output?.messages ?? [];
  const inFlight = run.status === "running" || run.status === "pending";
  const started = formatStarted(run.started_at);
  const company = run.input?.company_name ?? "Unknown";
  const visibleMessages = featured ? messages : messages.slice(0, 2);

  return (
    <Bezel
      data-testid="outreach-queue-card"
      size="lg"
      tone={run.status === "awaiting_approval" && featured ? "primary" : "default"}
      className="h-full"
      coreClassName={cn("flex h-full flex-col", featured ? "p-6 md:p-8" : "p-5 md:p-6")}
    >
      <header className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <span
            aria-hidden
            className={cn(
              "grid shrink-0 place-items-center rounded-2xl bg-foreground/[0.04] text-foreground/80 ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10",
              featured ? "h-12 w-12" : "h-10 w-10",
            )}
          >
            <Buildings size={featured ? 20 : 17} weight="light" />
          </span>
          <div className="min-w-0">
            <h3
              className={cn(
                "truncate font-semibold tracking-[-0.03em] text-foreground",
                featured ? "text-2xl md:text-[1.75rem]" : "text-base",
              )}
            >
              {company}
            </h3>
            {started ? (
              <p className="mt-0.5 font-geist-mono text-[11px] tabular-nums text-muted-foreground">{started}</p>
            ) : null}
          </div>
        </div>
        <StatusPill tone={statusTone(run.status)} live={inFlight} className="shrink-0">
          {run.status.replace("_", " ")}
        </StatusPill>
      </header>

      {/* Contacts */}
      {run.output?.contacts && (
        <div className="mt-6">
          <p className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Contacts <span className="font-geist-mono tabular-nums">{Math.min(contacts.length, 5)}</span>
          </p>
          <ul className={cn("mt-3 grid gap-x-6", featured && "md:grid-cols-2")}>
            {contacts.slice(0, 5).map((c, i) => (
              <li key={i} className="flex items-center gap-3 py-2">
                <span
                  aria-hidden
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-primary/10 font-geist-mono text-[11px] text-primary ring-1 ring-primary/15"
                >
                  {initials(c.name)}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-foreground">{c.name}</p>
                  <p className="truncate text-xs text-muted-foreground">{c.title}</p>
                </div>
                {isSafeProfileUrl(c.linkedin_url) ? (
                  <a
                    href={c.linkedin_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    aria-label={`Open ${c.name}'s LinkedIn profile`}
                    className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-muted-foreground ring-1 ring-foreground/[0.06] transition-[background-color,color,transform] duration-500 ease-vanguard hover:bg-foreground/[0.05] hover:text-foreground active:scale-[0.94] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:ring-white/10"
                  >
                    <ArrowUpRight size={13} weight="light" />
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Drafted messages */}
      {visibleMessages.length > 0 && (
        <div className="mt-5 space-y-2.5">
          <Hairline />
          <p className="pt-3 text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Drafted messages <span className="font-geist-mono tabular-nums">{messages.length}</span>
          </p>
          {visibleMessages.map((m, i) => (
            <div
              key={`${m.contact_name}-${i}`}
              className="rounded-[1.1rem] bg-foreground/[0.025] px-4 py-3.5 ring-1 ring-foreground/[0.05] dark:bg-white/[0.025] dark:ring-white/[0.06]"
            >
              <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
                <ChatCircleText size={13} weight="light" aria-hidden className="text-muted-foreground" />
                To {m.contact_name}
              </p>
              <p
                className={cn(
                  "mt-1.5 whitespace-pre-line text-sm leading-6 text-muted-foreground",
                  !expanded && (featured ? "line-clamp-4" : "line-clamp-3"),
                )}
              >
                {m.message}
              </p>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
              className="rounded-full text-xs font-medium text-primary transition-colors duration-500 ease-vanguard hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {expanded ? "Collapse drafts" : "Read full drafts"}
            </button>
            {!featured && messages.length > visibleMessages.length ? (
              <span className="text-xs text-muted-foreground">
                +{messages.length - visibleMessages.length} more
              </span>
            ) : null}
          </div>
        </div>
      )}

      {/* In-flight / empty states inside a card */}
      {inFlight && !run.output?.contacts && (
        <div className="mt-6 space-y-2.5" aria-hidden>
          <Skeleton className="h-4 w-2/3 rounded-full" />
          <Skeleton className="h-4 w-1/2 rounded-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
      {inFlight && !run.output?.contacts ? (
        <p className="mt-3 text-xs text-muted-foreground">Finding decision-makers and drafting messages…</p>
      ) : null}

      {/* HITL decision */}
      {run.status === "awaiting_approval" && (
        <footer className="mt-auto flex flex-wrap gap-2 pt-6">
          {canSend && (
            <IslandButton
              tone="primary"
              size="sm"
              disabled={deciding || !messages.length}
              trailing={deciding ? <CircleNotch size={14} weight="light" className="animate-spin" /> : <PaperPlaneTilt size={14} weight="light" />}
              onClick={() => {
                const count = messages.length;
                if (window.confirm(`Send ${count} ${count === 1 ? "draft" : "drafts"} from your LinkedIn now? Each person gets exactly the text shown.`)) onDecide(true);
              }}
            >
              Send on LinkedIn
            </IslandButton>
          )}
          <IslandButton
            tone={canSend ? "ghost" : "primary"}
            size="sm"
            disabled={deciding}
            trailing={deciding ? <CircleNotch size={14} weight="light" className="animate-spin" /> : <Check size={14} weight="light" />}
            onClick={async () => {
              try { await navigator.clipboard.writeText(messages.map((message) => `To ${message.contact_name}\n${message.message}`).join("\n\n")); toast.success("Drafts copied. Open a contact profile to send in LinkedIn yourself."); } catch { toast.error("Copy failed. Select and copy the draft text."); }
            }}
          >
            Copy drafts
          </IslandButton>
          <IslandButton
            tone="ghost"
            size="sm"
            disabled={deciding}
            icon={<X size={14} weight="light" />}
            onClick={() => onDecide(false)}
          >
            Discard
          </IslandButton>
        </footer>
      )}
    </Bezel>
  );
}

/* ───────────────────────────── page ───────────────────────────── */

export default function LinkedInOutreachPage() {
  const qc = useQueryClient();
  const [company, setCompany] = useState("");
  const [roleContext, setRoleContext] = useState("");

  const identifyMutation = useMutation({
    mutationFn: () => apiClient.post("/linkedin/outreach/identify", { company_name: company, role_context: roleContext || undefined }),
    onSuccess: () => {
      toast.success("Finding contacts — results will appear shortly");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["outreach-queue"] }), 5000);
    },
    onError: () => toast.error("Failed to start outreach — check backend"),
  });

  const { data: queue = [], isLoading } = useQuery<OutreachRun[]>({
    queryKey: ["outreach-queue"],
    queryFn: async () => {
      const { data } = await apiClient.get("/linkedin/outreach/queue");
      return data;
    },
    refetchInterval: 10000,
  });

  const approveMutation = useMutation({
    mutationFn: ({ runId, approved }: { runId: string; approved: boolean }) =>
      apiClient.post(`/linkedin/outreach/${runId}/approve`, { approved }),
    onSuccess: (response, { approved }) => {
      if (!approved) {
        toast.success("Message discarded");
      } else {
        const results = Object.values((response.data?.deliveries ?? {}) as Record<string, string>);
        const sent = results.filter((r) => r === "invited" || r === "messaged").length;
        const waiting = results.filter((r) => r === "limit_reached").length;
        const other = results.length - sent - waiting;
        toast.success(`Sent ${sent} on LinkedIn${waiting ? `, ${waiting} waiting for tomorrow's limit` : ""}${other ? `, ${other} not sent` : ""}.`);
      }
      qc.invalidateQueries({ queryKey: ["outreach-queue"] });
    },
    onError: (error) => toast.error(getApiErrorMessage(error, "Could not process that decision.")),
  });

  const canSearch = company.trim().length > 0 && !identifyMutation.isPending;
  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSearch) return;
    identifyMutation.mutate();
  };

  const linkedin = useLinkedInAccount();
  const awaitingCount = queue.filter((r) => r.status === "awaiting_approval").length;
  const inFlightCount = queue.filter((r) => r.status === "running" || r.status === "pending").length;
  const completedCount = queue.filter((r) => r.status === "completed").length;

  return (
    <Screen>
      <PageHero
        eyebrow="LinkedIn agent · Outreach"
        title="LinkedIn Outreach"
        accent="drafted, never auto-sent."
        description="Find decision-makers, draft messages, and keep every outreach action behind approval."
        aside={
          <Bezel size="lg" lifted coreClassName="p-5 md:p-6">
            <form onSubmit={submitSearch} aria-label="Find contacts" className="space-y-4">
              <PanelTitle title="Find Contacts" icon={<UsersThree size={16} weight="light" />} />
              <Field label="Company">
                {(id) => (
                  <Input
                    id={id}
                    value={company}
                    onChange={(e) => setCompany(e.target.value)}
                    placeholder="Company name"
                    autoComplete="organization"
                    leading={<Buildings size={16} weight="light" />}
                  />
                )}
              </Field>
              <Field label="Role context">
                {(id) => (
                  <Input
                    id={id}
                    value={roleContext}
                    onChange={(e) => setRoleContext(e.target.value)}
                    placeholder="Role context (optional)"
                  />
                )}
              </Field>
              <IslandButton
                type="submit"
                tone="primary"
                className="w-full"
                disabled={!company.trim() || identifyMutation.isPending}
                icon={
                  identifyMutation.isPending ? (
                    <CircleNotch size={16} weight="light" className="animate-spin" />
                  ) : (
                    <UsersThree size={16} weight="light" />
                  )
                }
                trailing
              >
                {identifyMutation.isPending ? "Searching…" : "Find Contacts"}
              </IslandButton>
              <Notice tone="warning" icon={<PaperPlaneTilt size={15} weight="light" />} className="py-3 text-[13px]">
                Every outreach message requires your approval before sending.
              </Notice>
            </form>
          </Bezel>
        }
      />

      <Section aria-label="Outreach Queue">
        <div className="mb-6"><LinkedInSending /></div>
        <Reveal>
          <SectionHeading
            eyebrow="Queue"
            title="Outreach Queue"
            description="Newest first. Approve a draft to dispatch the connection request, or discard it."
            actions={
              <div className="flex flex-wrap items-center gap-2" aria-live="polite">
                <StatusPill tone="warning">
                  <span className="font-geist-mono tabular-nums">{isLoading ? "—" : awaitingCount}</span> awaiting
                </StatusPill>
                <StatusPill tone="primary" live={inFlightCount > 0}>
                  <span className="font-geist-mono tabular-nums">{isLoading ? "—" : inFlightCount}</span> in flight
                </StatusPill>
                <StatusPill tone="success">
                  <span className="font-geist-mono tabular-nums">{isLoading ? "—" : completedCount}</span> done
                </StatusPill>
              </div>
            }
          />
        </Reveal>

        {isLoading ? (
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-12" aria-hidden>
            <Bezel size="lg" className="lg:col-span-8 lg:row-span-2" coreClassName="space-y-4 p-6 md:p-8">
              <Skeleton className="h-8 w-1/2 rounded-full" />
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-24 w-full" />
            </Bezel>
            <Bezel size="lg" className="lg:col-span-4" coreClassName="space-y-3 p-5">
              <Skeleton className="h-5 w-2/3 rounded-full" />
              <Skeleton className="h-16 w-full" />
            </Bezel>
            <Bezel size="lg" className="lg:col-span-4" coreClassName="space-y-3 p-5">
              <Skeleton className="h-5 w-1/2 rounded-full" />
              <Skeleton className="h-16 w-full" />
            </Bezel>
          </div>
        ) : queue.length === 0 ? (
          <Reveal>
            <Bezel size="lg" tone="muted">
              <EmptyPanel
                icon={<UsersThree size={24} weight="light" />}
                title="No outreach runs yet."
                description="Find contacts to get started."
              />
            </Bezel>
          </Reveal>
        ) : (
          <RevealGroup className="grid grid-cols-1 gap-6 lg:auto-rows-min lg:grid-cols-12">
            {queue.map((run, index) => (
              <motion.div key={run.id} variants={listItem} className={cn("min-w-0", spanFor(index, queue.length))}>
                <QueueCard
                  run={run}
                  featured={index === 0}
                  deciding={approveMutation.isPending && approveMutation.variables?.runId === run.id}
                  onDecide={(approved) => approveMutation.mutate({ runId: run.id, approved })}
                  canSend={Boolean(linkedin.data?.enabled && linkedin.data?.connected)}
                />
              </motion.div>
            ))}
          </RevealGroup>
        )}
      </Section>
    </Screen>
  );
}
