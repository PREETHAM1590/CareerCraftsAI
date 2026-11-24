"use client";
import { useEffect, useRef, type ComponentType } from "react";
import { motion, useReducedMotion } from "motion/react";
import {
  ArrowUpRight,
  CheckCircle,
  CircleNotch,
  Clock,
  Hourglass,
  Monitor,
  Prohibit,
  Terminal,
  TreeStructure,
  WarningCircle,
  type IconProps,
} from "@phosphor-icons/react";
import { useAgentStore } from "@/store/agentStore";
import { cn } from "@/lib/utils";
import { Bezel, EmptyPanel, Hairline, StatusPill, EASE_OUT_EXPO, type StatusTone } from "@/components/vanguard";
import { ApprovalModal } from "./ApprovalModal";

const STATUS_TONE: Record<string, StatusTone> = {
  queued: "neutral",
  running: "primary",
  awaiting_approval: "warning",
  needs_verification: "warning",
  completed: "success",
  failed: "danger",
  cancelled: "neutral",
  expired: "neutral",
};

/** Medallion surface per status — soft tinted disc behind the status icon. */
const MEDALLION: Record<StatusTone, string> = {
  neutral: "bg-foreground/[0.04] text-muted-foreground ring-foreground/[0.08] dark:bg-white/[0.05] dark:ring-white/10",
  primary: "bg-primary/10 text-primary ring-primary/20",
  success: "bg-success/10 text-success ring-success/25",
  warning: "bg-warning/10 text-warning ring-warning/25",
  danger: "bg-danger/10 text-danger ring-danger/25",
};

// Progress events (e.g. from the browser extension) carry a readable message.
function eventText(data: unknown): string {
  if (typeof data === "string") return data;
  const message = (data as { message?: unknown } | null)?.message;
  return typeof message === "string" ? message : JSON.stringify(data);
}

const STATUS_ICON: Record<string, ComponentType<IconProps>> = {
  queued: Hourglass,
  running: CircleNotch,
  awaiting_approval: Clock,
  completed: CheckCircle,
  failed: WarningCircle,
  cancelled: Prohibit,
};

/** Event-type tint inside the log (brand for progress, semantic for outcomes). */
function eventTone(type: string): string {
  if (type === "error" || type === "failed") return "text-danger";
  if (type === "checkpoint") return "text-warning";
  if (type === "complete" || type === "completed") return "text-success";
  if (type === "tool_call" || type === "tool_result") return "text-primary";
  return "text-muted-foreground";
}

interface Props {
  runId: string;
  onApprove?: () => void;
  onCancel?: () => void;
}

export function AgentStatusStream({ runId, onApprove, onCancel }: Props) {
  const setActiveRun = useAgentStore((s) => s.setActiveRun);
  const reduce = useReducedMotion();
  const logRef = useRef<HTMLDivElement>(null);
  const pinnedToBottom = useRef(true);
  // Register this run as active; the persistent stream in AppShell handles the
  // SSE connection so it survives page navigation.
  useEffect(() => {
    setActiveRun(runId);
  }, [runId, setActiveRun]);
  const run = useAgentStore((s) => s.runs[runId]);
  const eventCount = run?.events.length ?? 0;

  // Follow the tail of the log while the reader is already at the bottom.
  useEffect(() => {
    const el = logRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [eventCount]);

  if (!run) {
    return (
      <Bezel size="lg" coreClassName="grid min-h-52 place-items-center">
        <div role="status" aria-live="polite">
          <EmptyPanel
            compact
            icon={<CircleNotch size={22} weight="light" className="animate-spin text-primary motion-reduce:animate-none" />}
            title="Connecting to agent..."
            description={<span className="font-geist-mono text-xs tabular-nums">{runId.slice(0, 8)}</span>}
          />
        </div>
      </Bezel>
    );
  }
  const tone = STATUS_TONE[run.status] ?? "neutral";
  const StatusIcon = STATUS_ICON[run.status] ?? Clock;
  const isLive = run.status === "running" || run.status === "awaiting_approval";
  const lastEvent = run.events[run.events.length - 1];

  return (
    <div className="space-y-4">
      <Bezel size="lg" coreClassName="overflow-hidden">
        {/* Run header: status medallion, status + id, live-log meta */}
        <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between md:p-5">
          <div className="flex min-w-0 items-center gap-3.5">
            <span aria-hidden className={cn("grid h-11 w-11 shrink-0 place-items-center rounded-full ring-1", MEDALLION[tone])}>
              <StatusIcon size={20} weight="light" className={run.status === "running" ? "animate-spin motion-reduce:animate-none" : undefined} />
            </span>
            <div className="min-w-0 space-y-1.5">
              <StatusPill tone={tone} live={isLive}>{run.status.replace("_", " ")}</StatusPill>
              <span className="block font-geist-mono text-xs tabular-nums text-muted-foreground">{runId.slice(0, 8)}</span>
            </div>
          </div>
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            {lastEvent ? (
              <span className="hidden max-w-[22ch] truncate font-geist-mono md:inline" title={lastEvent.type}>
                {lastEvent.type}
              </span>
            ) : null}
            <span className="inline-flex items-center gap-2 rounded-full bg-foreground/[0.035] px-3 py-1.5 ring-1 ring-foreground/[0.06] dark:bg-white/[0.04] dark:ring-white/10">
              <Terminal aria-hidden size={14} weight="light" />
              Live log
              <span className="font-geist-mono tabular-nums text-foreground/70">{eventCount}</span>
            </span>
          </div>
        </div>

        <Hairline />

        {/* Live browser frame (server-browser mode) */}
        {run.lastFrame?.screenshot_b64 && (
          <div className="p-1.5 pb-0">
            <figure className="overflow-hidden rounded-[calc(2rem-0.75rem)] bg-muted/40 ring-1 ring-foreground/[0.06] dark:ring-white/10">
              <figcaption className="flex items-center justify-between gap-3 px-4 py-2 text-xs text-muted-foreground">
                <span className="flex min-w-0 items-center gap-2">
                  <Monitor aria-hidden size={14} weight="light" className="shrink-0" />
                  <span className="truncate">{run.lastFrame.title || run.lastFrame.url}</span>
                </span>
                <span className="shrink-0 font-geist-mono tabular-nums">live · step {run.lastFrame.step}</span>
              </figcaption>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={`data:${run.lastFrame.mime ?? "image/png"};base64,${run.lastFrame.screenshot_b64}`}
                alt="Live browser view"
                className="block w-full"
              />
            </figure>
          </div>
        )}

        {/* Log: recessed tray, mono, polite live region */}
        <div className="p-1.5">
          <Bezel size="md" tone="muted">
            <div
              ref={logRef}
              role="log"
              aria-live="polite"
              aria-label="Agent run log"
              tabIndex={0}
              onScroll={(e) => {
                const el = e.currentTarget;
                pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
              }}
              className="h-64 overflow-y-auto overscroll-contain rounded-[calc(1.5rem-0.25rem)] px-4 py-3.5 font-geist-mono text-xs text-card-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              {run.events.length === 0 ? (
                <p className="text-muted-foreground">Waiting for the first event…</p>
              ) : null}
              <ol className="space-y-1.5">
                {run.events.map((e, i) => (
                  <motion.li
                    key={i}
                    initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: reduce ? 0.2 : 0.45, ease: EASE_OUT_EXPO }}
                    className="grid grid-cols-[2.25rem_minmax(0,1fr)] gap-2 break-words leading-relaxed"
                  >
                    <span aria-hidden className="select-none text-right tabular-nums text-muted-foreground/50">
                      {String(i + 1).padStart(3, "0")}
                    </span>
                    <span>
                      <span className={eventTone(e.type)}>[{e.type}]</span>{" "}
                      {eventText(e.data)}
                    </span>
                  </motion.li>
                ))}
              </ol>
              {run.status === "running" && (
                <span aria-hidden className="ml-[2.75rem] mt-1.5 inline-block h-3.5 w-1.5 animate-pulse rounded-[1px] bg-primary/70 motion-reduce:animate-none" />
              )}
            </div>
          </Bezel>
        </div>
      </Bezel>

      {Array.isArray(run.result?.child_run_ids) && (
        <Bezel size="md" coreClassName="p-4 md:p-5">
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="grid h-8 w-8 place-items-center rounded-full bg-foreground/[0.04] text-foreground/80 ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10">
              <TreeStructure size={15} weight="light" />
            </span>
            <p className="font-geist text-sm font-semibold tracking-[-0.015em] text-foreground">Application workflows</p>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {(run.result.child_run_ids as string[]).map(childId => (
              <a
                key={childId}
                href={`/agents?run=${encodeURIComponent(childId)}`}
                className={cn(
                  "group inline-flex h-9 items-center gap-2 rounded-full bg-card pl-4 pr-1 text-[13px] font-medium text-primary",
                  "ring-1 ring-foreground/10 shadow-bezel-core dark:ring-white/10 dark:shadow-bezel-core-dark",
                  "transition-[background-color,transform] duration-500 ease-vanguard hover:bg-muted/60 active:scale-[0.98]",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                )}
              >
                <span>
                  Open <span className="font-geist-mono tabular-nums">{childId.slice(0, 8)}</span>
                </span>
                <span aria-hidden className="grid h-7 w-7 place-items-center rounded-full bg-primary/10 transition-transform duration-500 ease-vanguard group-hover:-translate-y-[1px] group-hover:translate-x-0.5">
                  <ArrowUpRight size={13} weight="light" />
                </span>
              </a>
            ))}
          </div>
        </Bezel>
      )}
      {typeof run.result?.notifications_scanned === "number" && (
        <div role="status" className="rounded-2xl bg-muted/40 px-4 py-3 text-sm text-foreground ring-1 ring-foreground/10">
          <p>
            Scanned {run.result.notifications_scanned} email{run.result.notifications_scanned === 1 ? "" : "s"}
            {Array.isArray(run.result.changes) ? `; ${run.result.changes.length} application status change${run.result.changes.length === 1 ? "" : "s"}` : ""}.
          </p>
          {typeof run.result.unclassified === "number" && run.result.unclassified > 0 && (
            <p className="mt-1 text-warning">
              {run.result.unclassified} email{run.result.unclassified === 1 ? "" : "s"} could not be classified and{" "}
              {run.result.unclassified === 1 ? "was" : "were"} skipped. Check your AI model in Settings and scan again.
            </p>
          )}
        </div>
      )}
      {run.status === "awaiting_approval" && run.pendingAction && (
        <ApprovalModal
          runId={runId}
          action={run.pendingAction}
          onApprove={onApprove ?? (() => {})}
          onCancel={onCancel ?? (() => {})}
        />
      )}
    </div>
  );
}
