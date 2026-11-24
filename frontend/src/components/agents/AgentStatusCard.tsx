"use client";

import { CheckCircle, CircleNotch, Clock, Hourglass, MagnifyingGlass, Warning, XCircle, type Icon } from "@phosphor-icons/react";
import { cn } from "@/lib/utils";

export type AgentRunStatus =
  | "queued" | "running" | "succeeded" | "completed" | "failed"
  | "awaiting_approval" | "needs_verification" | "expired" | "cancelled";

const MUTED = "bg-foreground/[0.04] text-muted-foreground ring-foreground/[0.08] dark:bg-white/[0.05] dark:ring-white/10";

// Medallion + pill tints per status. Semantic tokens only (success / warning /
// danger / primary) so the card reads identically in light and dark mode.
const STATUS_STYLES: Record<AgentRunStatus, { medallion: string; pill: string }> = {
  queued: { medallion: MUTED, pill: MUTED },
  // Closed without running to completion: neither success nor an error.
  expired: { medallion: MUTED, pill: MUTED },
  cancelled: { medallion: MUTED, pill: MUTED },
  running: { medallion: "bg-primary/10 text-primary ring-primary/20", pill: "bg-primary/10 text-primary ring-primary/20" },
  succeeded: { medallion: "bg-success/10 text-success ring-success/25", pill: "bg-success/10 text-success ring-success/25" },
  completed: { medallion: "bg-success/10 text-success ring-success/25", pill: "bg-success/10 text-success ring-success/25" },
  failed: { medallion: "bg-danger/10 text-danger ring-danger/25", pill: "bg-danger/10 text-danger ring-danger/25" },
  awaiting_approval: { medallion: "bg-warning/10 text-warning ring-warning/25", pill: "bg-warning/10 text-warning ring-warning/25" },
  // Distinct from "failed": an external action (e.g. a submit click) may
  // have gone through with no way to confirm it — never auto-retried, so
  // the color/copy must read as "go check", not "broken".
  needs_verification: { medallion: "bg-warning/10 text-warning ring-warning/25", pill: "bg-warning/10 text-warning ring-warning/25" },
};

const STATUS_ICONS: Record<AgentRunStatus, Icon> = {
  queued: Clock,
  running: CircleNotch,
  succeeded: CheckCircle,
  completed: CheckCircle,
  failed: Warning,
  awaiting_approval: Clock,
  needs_verification: MagnifyingGlass,
  expired: Hourglass,
  cancelled: XCircle,
};

const AGENT_LABELS: Record<string, string> = {
  resume_optimize: "Resume Optimization",
  linkedin_optimize: "LinkedIn Optimization",
  apply_prepare: "Application Preparation",
};

function displayName(value: string) {
  return AGENT_LABELS[value] ?? value.replace(/_/g, " ").replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

type Props = {
  agentType: string;
  status: AgentRunStatus;
  latestMessage?: string;
  startedAt?: string;
};

/**
 * Compact run row: status medallion, agent name, status pill, latest message
 * and start time. Built from phrasing elements (spans) only, so it is valid
 * both standalone (dashboard) and nested inside a <button> (agents history).
 */
export function AgentStatusCard({ agentType, status, latestMessage, startedAt }: Props) {
  const normalizedStatus: AgentRunStatus = STATUS_ICONS[status] ? status : "queued";
  const StatusIcon = STATUS_ICONS[normalizedStatus];
  const styles = STATUS_STYLES[normalizedStatus];
  return (
    <span
      className={cn(
        "block rounded-2xl bg-card px-4 py-3.5 ring-1 ring-foreground/[0.06] shadow-bezel-core dark:ring-white/10 dark:shadow-bezel-core-dark",
        "transition-[background-color,transform] duration-500 ease-vanguard hover:bg-muted/40",
      )}
    >
      <span className="flex items-start gap-3">
        <span aria-hidden className={cn("mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full ring-1", styles.medallion)}>
          <StatusIcon
            size={15}
            weight="light"
            className={normalizedStatus === "running" ? "animate-spin motion-reduce:animate-none" : undefined}
          />
        </span>
        <span className="block min-w-0 flex-1">
          <span className="flex items-center justify-between gap-2">
            <span className="truncate font-geist text-sm font-medium tracking-[-0.01em] text-foreground">{displayName(agentType)}</span>
            <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1", styles.pill)}>
              {displayName(normalizedStatus)}
            </span>
          </span>
          {latestMessage && <span className="mt-1 line-clamp-2 block text-xs leading-5 text-muted-foreground">{latestMessage}</span>}
          {startedAt && (
            <span className="mt-2 flex items-center gap-1.5 text-[11px] tabular-nums text-muted-foreground/80">
              <Clock aria-hidden size={12} weight="light" />
              Started {startedAt}
            </span>
          )}
        </span>
      </span>
    </span>
  );
}
