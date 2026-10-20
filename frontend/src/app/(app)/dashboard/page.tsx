"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import axios from "axios";
import {
  ArrowUpRight,
  BellRinging,
  BookmarkSimple,
  Buildings,
  CalendarCheck,
  ClockCounterClockwise,
  Crosshair,
  FileArrowUp,
  FileText,
  Kanban,
  Lightning,
  MagnifyingGlass,
  Microphone,
  Robot,
  SealCheck,
  ShieldCheck,
  X,
} from "@phosphor-icons/react";
import { ResumeScoreCard } from "@/components/ui/ResumeScoreCard";
import { JobMatchCard } from "@/components/ui/JobMatchCard";
import { AgentStatusCard } from "@/components/agents/AgentStatusCard";
import { ApprovalCard } from "@/components/agents/ApprovalCard";
import {
  REVEAL_VIEWPORT,
  Bezel,
  EmptyPanel,
  Hairline,
  IconButton,
  IslandButton,
  IslandLink,
  PageHero,
  PanelTitle,
  Reveal,
  Screen,
  Section,
  SectionHeading,
  StatStrip,
  StatusPill,
  bezelCore,
  bezelShell,
  listItem,
  listStagger,
  type StatusTone,
} from "@/components/vanguard";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAgentStore, usePollMs } from "@/store/agentStore";

interface AgentResults {
  applications: number;
  hands_off_rate: number | null;
  verified_email_rate: number | null;
  bounce_rate: number | null;
  emails_sent: number;
}

function percent(value: number | null | undefined): string {
  return value == null ? "No data yet" : `${Math.round(value * 100)}%`;
}

function timeGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

interface DashboardStats {
  applications_count: number;
  interviews_count: number;
  avg_match_score: number;
  followups_due: number;
  recent_agent_runs: Array<{
    id: string;
    agent_type: string;
    status: string;
    started_at: string | null;
  }>;
}

interface JobApplication {
  id: string;
  company: string;
  role: string;
  match_score: number;
  location: string;
  job_url: string | null;
}

interface PendingApproval {
  id: string;
  agent_type: string;
  status: string;
  output: { type?: string; subject?: string; body?: string } | null;
}

/** Quick-action cards: primary Search Jobs triggers agent; others navigate to dedicated pages. */
const quickActions = [
  { label: "Search Jobs", icon: MagnifyingGlass, taskType: "job_search", ctx: { query: "", location: "Remote" }, description: "Scan the boards and score every match." },
  { label: "Optimize Resume", icon: FileText, taskType: "resume_optimize", ctx: {}, description: "Tailor to a role and rescore it for ATS.", href: "/resume" },
  { label: "Mock Interview", icon: Microphone, taskType: "interview_coach", ctx: { role: "Software Engineer" }, description: "Practice answers with scored feedback.", href: "/interview" },
  { label: "Research Company", icon: Buildings, taskType: "company_research", ctx: {}, description: "Culture, news and interview patterns.", href: "/company" },
] as const;

const [primaryAction] = quickActions;

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/* ------------------------------------------------------------------------ */
/* Local building blocks                                                     */
/* ------------------------------------------------------------------------ */

const MEDALLION: Record<StatusTone, string> = {
  neutral: "bg-foreground/[0.04] text-foreground/75 ring-foreground/[0.07] dark:bg-white/[0.05] dark:ring-white/10",
  primary: "bg-primary/10 text-primary ring-primary/20",
  success: "bg-success/10 text-success ring-success/25",
  warning: "bg-warning/10 text-warning ring-warning/25",
  danger: "bg-danger/10 text-danger ring-danger/25",
};

function Medallion({ tone = "neutral", children, className }: { tone?: StatusTone; children: ReactNode; className?: string }) {
  return (
    <span aria-hidden className={cn("grid h-10 w-10 shrink-0 place-items-center rounded-xl ring-1", MEDALLION[tone], className)}>
      {children}
    </span>
  );
}

interface NextAction {
  id: string;
  tone: StatusTone;
  icon: ReactNode;
  title: string;
  detail: string;
  href: string;
  cta: string;
}

function NextActionRow({ action, onDismiss }: { action: NextAction; onDismiss?: () => void }) {
  return (
    <motion.li variants={listItem} className="flex flex-col gap-4 py-4 first:pt-0 last:pb-0 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-4">
        <Medallion tone={action.tone}>{action.icon}</Medallion>
        <div className="min-w-0">
          <p className="text-[15px] font-medium tracking-[-0.015em] text-foreground">{action.title}</p>
          <p className="mt-0.5 text-[13px] leading-5 text-muted-foreground">{action.detail}</p>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 self-start sm:self-auto">
        <IslandLink href={action.href} tone="ghost" size="sm" trailing>{action.cta}</IslandLink>
        {onDismiss ? <IconButton aria-label="Dismiss keyword gaps recommendation" onClick={onDismiss}><X size={15} weight="light" /></IconButton> : null}
      </div>
    </motion.li>
  );
}

function LaunchTile({
  label,
  description,
  icon,
  href,
  busy,
  disabled,
  onClick,
}: {
  label: string;
  description: string;
  icon: ReactNode;
  href?: string;
  busy?: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  const descId = `launch-${label.toLowerCase().replace(/\s+/g, "-")}-desc`;
  const tileClassName = cn(
    bezelShell("md"),
    "group block h-full w-full text-left transition-[transform,opacity] duration-500 ease-vanguard hover:-translate-y-0.5 active:scale-[0.98]",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-60",
  );

  const content = (
    <span className={cn(bezelCore("md"), "flex h-full flex-col p-4")}>
      <span className="flex items-start justify-between gap-3">
        <Medallion>{icon}</Medallion>
        <span
          aria-hidden
          className="grid h-8 w-8 place-items-center rounded-full bg-foreground/[0.05] text-muted-foreground transition-[transform,color,background-color] duration-500 ease-vanguard group-hover:-translate-y-[1px] group-hover:translate-x-1 group-hover:scale-105 group-hover:bg-primary/10 group-hover:text-primary dark:bg-white/10"
        >
          <ArrowUpRight size={14} weight="light" />
        </span>
      </span>
      <span className="mt-6 block text-sm font-medium tracking-[-0.01em] text-foreground">{label}</span>
      <span id={descId} className="mt-1 block text-xs leading-5 text-muted-foreground">
        {busy ? "Starting agent…" : description}
      </span>
    </span>
  );

  return (
    <motion.li variants={listItem} className="min-w-0">
      {href ? (
        <Link href={href} className={tileClassName} aria-label={label} aria-describedby={descId}>
          {content}
        </Link>
      ) : (
        <button
          type="button"
          onClick={onClick}
          disabled={disabled}
          aria-busy={busy}
          aria-label={label}
          aria-describedby={descId}
          className={tileClassName}
        >
          {content}
        </button>
      )}
    </motion.li>
  );
}

function RowSkeleton({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div aria-hidden className={cn("space-y-3", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-4">
          <div className="shimmer h-10 w-10 shrink-0 rounded-xl" />
          <div className="flex-1 space-y-2">
            <div className="shimmer h-3.5 w-2/5 rounded-full" />
            <div className="shimmer h-3 w-3/5 rounded-full" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Inline (span) skeleton — safe inside the <p> that Stat renders its value into. */
function ValueSkeleton() {
  return <span aria-hidden className="shimmer inline-block h-10 w-16 rounded-xl align-middle md:h-12 md:w-20" />;
}

/* ------------------------------------------------------------------------ */
/* Page                                                                      */
/* ------------------------------------------------------------------------ */

export default function DashboardPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const initRun = useAgentStore((s) => s.initRun);
  const setActiveRun = useAgentStore((s) => s.setActiveRun);
  const [launching, setLaunching] = useState<string | null>(null);
  const [dismissedKeywordGaps, setDismissedKeywordGaps] = useState<string | null>(null);

  const { data: agentResults } = useQuery<AgentResults>({
    queryKey: ["agent-results"],
    queryFn: () => apiClient.get("/metrics/agent").then((r) => r.data as AgentResults),
    staleTime: 60_000,
  });
  const { data: stats, isLoading } = useQuery<DashboardStats>({
    queryKey: ["dashboard-stats"],
    queryFn: async () => {
      const { data } = await apiClient.get("/users/me/stats");
      return data as DashboardStats;
    },
  });

  const { data: jobMatches = [], isSuccess: jobsLoaded } = useQuery<JobApplication[]>({
    queryKey: ["dashboard-jobs"],
    queryFn: async () => {
      const { data } = await apiClient.get("/jobs/applications?status=saved&limit=3");
      return data;
    },
  });

  const approvalsPollMs = usePollMs(10_000, 30_000);
  const { data: pendingApprovals = [] } = useQuery<PendingApproval[]>({
    queryKey: ["pending-approvals"],
    queryFn: async () => {
      const { data } = await apiClient.get("/agents/runs?status=awaiting_approval&limit=100");
      return ((Array.isArray(data) ? data : data.runs ?? []) as PendingApproval[]).filter((r) => r.status === "awaiting_approval");
    },
    refetchInterval: approvalsPollMs,
  });

  const { data: resumeData } = useQuery<{
    ats_score: number | null;
    keyword_score: number | null;
    missing_keywords: string[];
  }>({
    queryKey: ["dashboard-ats"],
    queryFn: async () => {
      const { data } = await apiClient.get("/rag/documents?doc_type=resume");
      const docs = data as Array<{
        is_primary: boolean;
        ats_score: number | null;
        ats_data: { keyword_score?: number | null; missing_keywords?: string[] } | null;
      }>;
      const primary = docs?.find((d) => d.is_primary) ?? docs?.[0];
      return {
        ats_score: primary?.ats_score ?? null,
        keyword_score: primary?.ats_data?.keyword_score ?? null,
        missing_keywords: primary?.ats_data?.missing_keywords ?? [],
      };
    },
  });

  const triggerAgent = async (taskType: string, ctx: Record<string, unknown>) => {
    if (launching) return;
    setLaunching(taskType);
    try {
      const { data } = await apiClient.post("/agents/run", { task_type: taskType, context: ctx });
      const runId = (data as { run_id: string }).run_id;
      initRun(runId);
      setActiveRun(runId);
      router.push("/agents");
    } catch (err) {
      // 429s are already surfaced once by the api client interceptor.
      if (!(axios.isAxiosError(err) && err.response?.status === 429)) {
        toast.error(getApiErrorMessage(err, "Couldn't start the agent. Try again."));
      }
    } finally {
      setLaunching(null);
    }
  };

  const decideApproval = async (runId: string, approved: boolean) => {
    try {
      await apiClient.post(`/agents/${runId}/approve`, { approved });
      qc.invalidateQueries({ queryKey: ["pending-approvals"] });
    } catch (err) {
      toast.error(getApiErrorMessage(err, approved ? "Couldn't approve that action." : "Couldn't reject that action."));
    }
  };

  /* ---- derived view state ------------------------------------------------ */

  const greeting = timeGreeting();
  const dateLabel = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  const approvalsCount = pendingApprovals.length;
  const recentRuns = stats?.recent_agent_runs ?? [];
  const runningCount = recentRuns.filter((r) => r.status === "running" || r.status === "queued").length;
  const lastRunAt = recentRuns
    .map((r) => (r.started_at ? new Date(r.started_at) : null))
    .filter((d): d is Date => d !== null && !Number.isNaN(d.getTime()))
    .sort((a, b) => b.getTime() - a.getTime())[0];
  const lastRunLabel = lastRunAt
    ? lastRunAt.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : null;
  const followupsDue = stats?.followups_due ?? 0;
  const interviews = stats?.interviews_count ?? 0;

  const nextActions: NextAction[] = [];
  if (approvalsCount > 0) {
    nextActions.push({
      id: "approvals",
      tone: "warning",
      icon: <ShieldCheck size={18} weight="light" />,
      title: `${plural(approvalsCount, "action")} waiting for your approval`,
      detail: "Agents paused before sending or submitting anything. Nothing goes out until you decide.",
      href: "#approvals",
      cta: "Review",
    });
  }
  if (followupsDue > 0) {
    nextActions.push({
      id: "followups",
      tone: "primary",
      icon: <BellRinging size={18} weight="light" />,
      title: `${plural(followupsDue, "follow-up")} due`,
      detail: "Day-5 and day-12 drafts are ready for the applications that haven't replied.",
      href: "/applications",
      cta: "Open pipeline",
    });
  }
  if (resumeData && resumeData.ats_score === null) {
    nextActions.push({
      id: "resume",
      tone: "neutral",
      icon: <FileArrowUp size={18} weight="light" />,
      title: "Upload your resume",
      detail: "Every agent tailors from your primary resume, and scoring starts there.",
      href: "/resume",
      cta: "Upload",
    });
  } else if (resumeData && resumeData.keyword_score !== null && resumeData.missing_keywords.length > 0) {
    nextActions.push({
      id: "keywords",
      tone: "neutral",
      icon: <Crosshair size={18} weight="light" />,
      title: `Address ${plural(resumeData.missing_keywords.length, "keyword gap")}`,
      detail: "Your saved jobs ask for terms your resume doesn't mention yet.",
      href: "/resume",
      cta: "Tailor resume",
    });
  }
  if (jobsLoaded && jobMatches.length === 0) {
    nextActions.push({
      id: "jobs",
      tone: "neutral",
      icon: <BookmarkSimple size={18} weight="light" />,
      title: "Save roles worth pursuing",
      detail: "Saved jobs drive match scores, keyword gaps and auto-apply.",
      href: "/jobs",
      cta: "Browse jobs",
    });
  }
  if (interviews > 0) {
    nextActions.push({
      id: "interviews",
      tone: "success",
      icon: <CalendarCheck size={18} weight="light" />,
      title: `Prepare for ${plural(interviews, "interview")}`,
      detail: "Generate a role-specific question bank and study guide.",
      href: "/interview?tab=prep",
      cta: "Prep",
    });
  }
  const keywordGapKey = JSON.stringify([...(resumeData?.missing_keywords ?? [])].sort());
  const visibleActions = nextActions.filter((action) => action.id !== "keywords" || dismissedKeywordGaps !== keywordGapKey).slice(0, 4);

  const heroSummary =
    approvalsCount > 0
      ? `${plural(approvalsCount, "action")} waiting on your approval.`
      : runningCount > 0
        ? `${plural(runningCount, "agent")} working for you right now.`
        : "Your agents are standing by.";

  return (
    <Screen>
      <PageHero
        eyebrow="Command center"
        title="Dashboard"
        accent={<span suppressHydrationWarning>{greeting}.</span>}
        description={
          <>
            <span suppressHydrationWarning>{dateLabel}</span>. {heroSummary}
          </>
        }
        actions={
          <>
            <IslandButton
              size="lg"
              tone="primary"
              trailing
              icon={<MagnifyingGlass size={18} weight="light" />}
              disabled={launching !== null}
              aria-busy={launching === primaryAction.taskType}
              onClick={() => triggerAgent(primaryAction.taskType, { ...primaryAction.ctx })}
            >
              {launching === primaryAction.taskType ? "Starting…" : primaryAction.label}
            </IslandButton>
            <IslandLink href="/applications" size="lg" tone="quiet" icon={<Kanban size={18} weight="light" />}>
              View pipeline
            </IslandLink>
          </>
        }
        aside={
          <Bezel size="lg" lifted coreClassName="p-6">
            <div className="flex items-center justify-between gap-3">
              <p className="flex items-center gap-2 text-[13px] font-medium text-foreground">
                <Robot size={16} weight="light" aria-hidden className="text-muted-foreground" />
                Agent harness
              </p>
              <span aria-live="polite">
                {isLoading ? (
                  <StatusPill tone="neutral">Checking…</StatusPill>
                ) : runningCount > 0 ? (
                  <StatusPill tone="primary" live>
                    {runningCount} running
                  </StatusPill>
                ) : (
                  <StatusPill tone="success">Idle</StatusPill>
                )}
              </span>
            </div>
            <Hairline className="my-5" />
            <dl className="grid grid-cols-2 gap-5">
              <div className="min-w-0">
                <dt className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Awaiting you</dt>
                <dd
                  className={cn(
                    "mt-2 font-geist text-3xl font-semibold tabular-nums tracking-[-0.04em]",
                    approvalsCount > 0 ? "text-warning" : "text-foreground",
                  )}
                >
                  {approvalsCount}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Last run</dt>
                <dd className="mt-2 truncate text-sm font-medium tabular-nums text-foreground">
                  {isLoading ? <span aria-hidden className="shimmer inline-block h-4 w-24 rounded-full align-middle" /> : (lastRunLabel ?? "No runs yet")}
                </dd>
              </div>
            </dl>
          </Bezel>
        }
      />

      {/* ---- Command bento ------------------------------------------------ */}
      <Section aria-label="Command center">
        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-12 lg:items-stretch">
          {/* Next actions — the anchor panel. On lg it stretches to the right column's
              height and pins the launchers to the bottom, so no blank strip is left under it. */}
          <Reveal className="min-w-0 lg:col-span-8">
            <Bezel size="lg" tone="primary" className="h-full" coreClassName="flex flex-col p-4 sm:p-6">
              <PanelTitle
                title="Next actions"
                icon={<Lightning size={16} weight="light" />}
                meta={
                  isLoading ? null : visibleActions.length > 0 ? (
                    <StatusPill tone={approvalsCount > 0 ? "warning" : "primary"} live={approvalsCount > 0}>
                      {plural(visibleActions.length, "open item")}
                    </StatusPill>
                  ) : (
                    <StatusPill tone="success">All clear</StatusPill>
                  )
                }
              />

              <div className="mt-4 lg:flex-1" aria-live="polite">
                {isLoading ? (
                  <RowSkeleton rows={3} />
                ) : visibleActions.length > 0 ? (
                  <motion.ul
                    className="divide-y divide-foreground/[0.06] dark:divide-white/[0.07]"
                    initial="hidden"
                    animate="show"
                    variants={listStagger}
                  >
                    {visibleActions.map((action) => (
                      <NextActionRow key={action.id} action={action} onDismiss={action.id === "keywords" ? () => setDismissedKeywordGaps(keywordGapKey) : undefined} />
                    ))}
                  </motion.ul>
                ) : (
                  <EmptyPanel
                    compact
                    icon={<SealCheck size={22} weight="light" />}
                    title="You're caught up"
                    description="Nothing needs you right now. Start an agent below and it will report back here."
                  />
                )}
              </div>

              <Hairline className="my-5" />

              <div>
                <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Launch an agent</p>
                <motion.ul
                  className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
                  initial="hidden"
                  whileInView="show"
                  viewport={REVEAL_VIEWPORT}
                  variants={listStagger}
                >
                  {quickActions.map((action) => (
                    <LaunchTile
                      key={action.taskType}
                      label={action.label}
                      description={action.description}
                      icon={<action.icon size={18} weight="light" />}
                      href={"href" in action ? action.href : undefined}
                      busy={launching === action.taskType}
                      disabled={launching !== null}
                      onClick={!("href" in action) ? () => triggerAgent(action.taskType, { ...action.ctx }) : undefined}
                    />
                  ))}
                </motion.ul>
              </div>
            </Bezel>
          </Reveal>

          {/* Stacked right column: resume score + approvals */}
          <div className="grid min-w-0 grid-cols-1 gap-6 md:grid-cols-2 lg:col-span-4 lg:grid-cols-1">
            <Reveal delay={0.08} className="min-w-0">
              <ResumeScoreCard
                atsScore={resumeData?.ats_score ?? null}
                keywordCoverage={resumeData?.keyword_score ?? null}
                missingKeywords={resumeData?.missing_keywords ?? []}
              />
            </Reveal>

            {/* Only shown when an agent is actually waiting; an empty card just padded the column. */}
            {approvalsCount > 0 ? (
            <Reveal delay={0.14} className="min-w-0">
              <Bezel
                id="approvals"
                size="lg"
                className={cn(
                  "h-full scroll-mt-24 bg-warning/[0.05] ring-warning/20 dark:bg-warning/[0.06] dark:ring-warning/25",
                )}
                coreClassName="flex flex-col p-6 md:p-7"
              >
                <PanelTitle
                  title="Approvals"
                  icon={<ShieldCheck size={16} weight="light" />}
                  meta={
                    <StatusPill tone="warning" live>
                      {approvalsCount} pending
                    </StatusPill>
                  }
                />
                <div className="mt-5 flex-1" aria-live="polite">
                      <p className="text-[13px] leading-5 text-muted-foreground">
                        {approvalsCount} action{approvalsCount > 1 ? "s" : ""} require approval. Nothing is sent or submitted until you approve.
                      </p>
                      <motion.ul
                        className="mt-4 max-h-[26rem] space-y-3 overflow-y-auto overscroll-contain pr-1"
                        initial="hidden"
                        animate="show"
                        variants={listStagger}
                      >
                        {pendingApprovals.map((run) => (
                          <motion.li key={run.id} variants={listItem}>
                            <ApprovalCard
                              title={`${run.agent_type.replace(/_/g, " ")} action pending`}
                              summary={run.output?.subject ?? run.output?.type ?? "Review required"}
                              onApprove={() => decideApproval(run.id, true)}
                              onReject={() => decideApproval(run.id, false)}
                            />
                          </motion.li>
                        ))}
                      </motion.ul>
                </div>
              </Bezel>
            </Reveal>
            ) : null}
          </div>

          {/* Key metrics */}
          <Reveal delay={0.1} className="min-w-0 lg:col-span-12">
            <StatStrip
              items={[
                {
                  label: "Applications",
                  value: isLoading ? <ValueSkeleton /> : (stats?.applications_count ?? 0),
                  hint: "Tracked in your pipeline",
                },
                {
                  label: "Interviews",
                  value: isLoading ? <ValueSkeleton /> : interviews,
                  hint: "At the interview stage",
                },
                {
                  label: "Avg match",
                  value: isLoading ? <ValueSkeleton /> : `${Math.round(stats?.avg_match_score ?? 0)}%`,
                  hint: "Across tracked roles",
                },
                {
                  label: "Follow-ups due",
                  value: isLoading ? (
                    <ValueSkeleton />
                  ) : (
                    <span className={followupsDue > 0 ? "text-primary" : undefined}>{followupsDue}</span>
                  ),
                  hint: followupsDue > 0 ? "Drafts ready to review" : "Nothing due",
                },
              ]}
            />
          </Reveal>

          {/* How the agent is doing against its targets, last 30 days */}
          <Reveal delay={0.12} className="min-w-0 lg:col-span-12">
            <StatStrip
              items={[
                {
                  label: "Applied without you",
                  value: percent(agentResults?.hands_off_rate),
                  hint: `Target 90%, of ${agentResults?.applications ?? 0} submitted in 30 days`,
                },
                {
                  label: "Verified recruiter email",
                  value: percent(agentResults?.verified_email_rate),
                  hint: "Target 50% of applications",
                },
                {
                  label: "Email bounce rate",
                  value: percent(agentResults?.bounce_rate),
                  hint: "Target under 3%",
                },
                {
                  label: "Recruiter emails sent",
                  value: agentResults?.emails_sent ?? 0,
                  hint: "Last 30 days",
                },
              ]}
            />
          </Reveal>
        </div>
      </Section>

      {/* ---- Activity + matches ------------------------------------------- */}
      <Section aria-label="Activity">
        <Reveal>
          <SectionHeading
            eyebrow="Activity"
            title="Recent work and best fits"
            description="The latest runs from your agent harness, next to the saved roles that match you best."
            actions={
              <IslandLink href="/agents" tone="ghost" size="md" trailing>
                Agent console
              </IslandLink>
            }
          />
        </Reveal>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <Reveal className="min-w-0 lg:col-span-7">
            <Bezel size="lg" className="h-full" coreClassName="flex flex-col p-6 md:p-7">
              <PanelTitle
                title="Recent agent runs"
                icon={<ClockCounterClockwise size={16} weight="light" />}
                meta={recentRuns.length ? <span className="tabular-nums">{plural(recentRuns.length, "run")}</span> : null}
              />
              <div className="mt-5 flex-1">
                {isLoading ? (
                  <RowSkeleton rows={3} />
                ) : recentRuns.length ? (
                  <motion.ul
                    className="space-y-2"
                    initial="hidden"
                    whileInView="show"
                    viewport={REVEAL_VIEWPORT}
                    variants={listStagger}
                  >
                    {recentRuns.map((run) => (
                      <motion.li key={run.id} variants={listItem}>
                        <AgentStatusCard
                          agentType={run.agent_type}
                          status={
                            run.status === "completed"
                              ? "succeeded"
                              : (run.status as "running" | "succeeded" | "failed" | "awaiting_approval")
                          }
                          latestMessage=""
                          startedAt={run.started_at ? new Date(run.started_at).toLocaleString() : ""}
                        />
                      </motion.li>
                    ))}
                  </motion.ul>
                ) : (
                  <EmptyPanel
                    compact
                    icon={<Robot size={22} weight="light" />}
                    title="No recent agent activity"
                    description="Run your first agent to see results here."
                  />
                )}
              </div>
            </Bezel>
          </Reveal>

          <Reveal delay={0.08} className="min-w-0 lg:col-span-5">
            <JobMatchCard
              jobs={jobMatches.map((j) => ({
                id: j.id,
                company: j.company,
                role: j.role,
                matchPercent: j.match_score,
                location: j.location,
                jobUrl: j.job_url,
              }))}
            />
          </Reveal>
        </div>
      </Section>
    </Screen>
  );
}
