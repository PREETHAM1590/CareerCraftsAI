"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import {
  Article,
  BracketsCurly,
  Buildings,
  ChatsCircle,
  CheckCircle,
  CircleNotch,
  Clock,
  ClockCounterClockwise,
  Cpu,
  CurrencyDollar,
  EnvelopeSimple,
  FileText,
  HandPalm,
  LinkedinLogo,
  MagnifyingGlass,
  Play,
  Pulse,
  Robot,
  Sparkle,
  Stack,
  Tray,
  Users,
  Warning,
  type Icon,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { AgentStatusCard } from "@/components/agents/AgentStatusCard";
import { AgentStatusStream } from "@/components/agents/AgentStatusStream";
import { AutoApplyPanel } from "@/components/agents/AutoApplyPanel";
import {
  Bezel,
  EmptyPanel,
  Eyebrow,
  Hairline,
  IslandButton,
  IslandLink,
  Notice,
  PageHero,
  PanelTitle,
  Reveal,
  RevealGroup,
  Screen,
  Select,
  Section,
  Skeleton,
  StatusPill,
  Textarea,
  listItem,
  listStagger,
  SPRING_SOFT,
  type StatusTone,
} from "@/components/vanguard";
import { apiClient } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAgentStore, usePollMs } from "@/store/agentStore";

// The four purpose groups. Colour encodes what an agent *does* — shared tint
// means shared job — but stays inside the brand's neutral + single-green
// system: pipeline carries the brand green, documents a warm neutral,
// outreach a faint green wash, intel an outlined neutral.
type AgentGroup = "pipeline" | "document" | "outreach" | "intel";

const ACCENT: Record<AgentGroup, string> = {
  // Moves an application forward.
  pipeline: "bg-primary/[0.12] text-primary ring-primary/20",
  // Produces something you will read and edit.
  document: "bg-foreground/[0.05] text-foreground/80 ring-foreground/[0.08] dark:bg-white/[0.06] dark:ring-white/10",
  // Talks to a human.
  outreach: "bg-primary/[0.05] text-foreground/80 ring-primary/15 dark:bg-primary/[0.08]",
  // Gathers information you act on later.
  intel: "bg-transparent text-muted-foreground ring-foreground/[0.12] dark:ring-white/15",
};

const GROUPS: ReadonlyArray<{ key: AgentGroup; label: string; blurb: string; span: string; tiles: string }> = [
  { key: "pipeline", label: "Pipeline", blurb: "Moves an application forward", span: "lg:col-span-5", tiles: "sm:grid-cols-2" },
  { key: "document", label: "Documents", blurb: "Drafts you read and edit", span: "lg:col-span-7", tiles: "sm:grid-cols-2" },
  { key: "outreach", label: "Outreach", blurb: "Talks to a human for you", span: "lg:col-span-7", tiles: "sm:grid-cols-2" },
  { key: "intel", label: "Intel", blurb: "Research you act on later", span: "lg:col-span-5", tiles: "grid-cols-1" },
];

const AGENTS: ReadonlyArray<{ key: string; label: string; icon: Icon; group: AgentGroup; note: string; wide?: boolean }> = [
  { key: "auto_apply", label: "Auto Apply", icon: Robot, group: "pipeline", note: "Isolated browser + two reviews", wide: true },
  { key: "resume_optimize", label: "Resume", icon: FileText, group: "document", note: "Tailored resume draft" },
  { key: "job_search", label: "Job Search", icon: MagnifyingGlass, group: "pipeline", note: "Fresh matching roles" },
  { key: "linkedin_optimize", label: "LinkedIn", icon: LinkedinLogo, group: "document", note: "Profile rewrite" },
  { key: "linkedin_outreach", label: "Outreach", icon: Users, group: "outreach", note: "Recruiter drafts" },
  { key: "email", label: "Email", icon: EnvelopeSimple, group: "outreach", note: "Reviewable draft" },
  { key: "email_monitor", label: "Monitor", icon: Tray, group: "outreach", note: "Inbox status scan" },
  { key: "interview_prep", label: "Interview Prep", icon: Sparkle, group: "document", note: "Question set" },
  { key: "interview_coach", label: "Coach", icon: ChatsCircle, group: "outreach", note: "Mock interview session" },
  { key: "cover_letter", label: "Cover Letter", icon: Article, group: "document", note: "Role-specific letter" },
  { key: "salary_intelligence", label: "Salary", icon: CurrencyDollar, group: "intel", note: "Market benchmark" },
  { key: "company_research", label: "Company", icon: Buildings, group: "intel", note: "Interview intel brief" },
];

const DEFAULT_CONTEXT: Record<string, Record<string, unknown>> = {
  auto_apply: { search_query: "software engineer", location: "Remote", max_applications: 1 },
  resume_optimize: { jd_text: "Software Engineer role focused on product delivery, reliability, and measurable impact." },
  job_search: { search_query: "software engineer", location: "Remote", max_results: 10 },
  linkedin_optimize: { target_role: "Software Engineer" },
  linkedin_outreach: { company_name: "Target Company", role_context: "Software Engineer" },
  email: { company: "Target Company", role: "Software Engineer", recipient_email: "recruiter@example.com" },
  email_monitor: {},
  interview_prep: { role: "Software Engineer", company: "Target Company" },
  interview_coach: { role: "Software Engineer", company: "Target Company" },
  cover_letter: { tone: "formal", jd_text: "Software Engineer role focused on product delivery, reliability, and measurable impact." },
  salary_intelligence: { role: "Software Engineer", company: "Target Company", location: "Remote" },
  company_research: { company_name: "Target Company" },
};

// BUG 6: interface matches backend AgentRunResponse (started_at, not created_at)
interface AgentRun {
  id: string;
  agent_type: string;
  status: string;
  started_at: string;
  output: { error?: string } | null;
  duration_ms: number | null;
}

interface RagDocument {
  id: string;
  filename: string;
  doc_type: string;
  is_primary: boolean;
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function runMessage(run: AgentRun): string {
  if (run.status === "queued") return "Queued for a worker";
  if (run.status === "awaiting_approval") return "Waiting for approval";
  if (run.status === "running") return "In progress...";
  if (run.status === "failed") return run.output?.error ?? "Failed";
  if (run.duration_ms !== null) return `Completed in ${(run.duration_ms / 1000).toFixed(1)}s`;
  return run.status === "awaiting_approval" ? "Waiting for approval" : "Completed";
}

function runStatus(run?: AgentRun): { label: string; icon: Icon; tone: StatusTone; live: boolean } {
  if (!run) return { label: "Ready", icon: Clock, tone: "neutral", live: false };
  if (run.status === "completed") return { label: "Last run succeeded", icon: CheckCircle, tone: "success", live: false };
  if (run.status === "failed") return { label: "Last run failed", icon: Warning, tone: "danger", live: false };
  if (run.status === "awaiting_approval") return { label: "Review pending", icon: Clock, tone: "warning", live: true };
  return { label: "Running", icon: CircleNotch, tone: "primary", live: true };
}

function agentLabel(agentType: string): string {
  return AGENTS.find((a) => a.key === agentType)?.label ?? agentType.replace(/_/g, " ");
}

/** Tiny status dot shown on a tile when that agent has a recent run. */
const DOT: Record<StatusTone, string> = {
  neutral: "bg-muted-foreground/60",
  primary: "bg-primary",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

export default function AgentsPage() {
  const [active, setActive] = useState("resume_optimize");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  // "" = use the active resume / model; anything else is sent as a per-run override.
  const [resumeId, setResumeId] = useState("");
  const [modelId, setModelId] = useState("");
  const [contextText, setContextText] = useState(JSON.stringify(DEFAULT_CONTEXT.resume_optimize, null, 2));
  const qc = useQueryClient();
  const initRun = useAgentStore((s) => s.initRun);
  const storeActiveRunId = useAgentStore((s) => s.activeRunId);
  const setActiveRun = useAgentStore((s) => s.setActiveRun);
  const tileIndicatorId = useId();
  const contextFieldId = useId();
  const contextHintId = useId();
  // Persisted run survives navigation + reload, so the panel reappears on return.
  const displayRunId = activeRunId ?? storeActiveRunId;
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("run");
    if (requested && /^[0-9a-f-]{36}$/i.test(requested)) setActiveRun(requested);
  }, [setActiveRun]);

  const runMutation = useMutation({
    mutationFn: async () => {
      const context = JSON.parse(contextText);
      if (!context || Array.isArray(context) || typeof context !== "object") throw new Error("Context must be a JSON object");
      return apiClient.post<{ run_id: string }>("/agents/run", {
        task_type: active,
        context: {
          ...context,
          ...(resumeId ? { resume_document_id: resumeId } : {}),
          ...(modelId ? { model_setting_id: modelId } : {}),
        },
      });
    },
    onSuccess: (res) => {
      toast.success(`${AGENTS.find((a) => a.key === active)?.label} Agent started`);
      initRun(res.data.run_id);
      setActiveRunId(res.data.run_id);
      setTimeout(() => qc.invalidateQueries({ queryKey: ["agent-runs"] }), 3000);
    },
    onError: () => toast.error("Could not queue the agent. Check your context, model settings, and active-run limit."),
  });

  const runsPollMs = usePollMs(5000, 60_000);
  const { data: runs = [], isLoading: runsLoading } = useQuery<AgentRun[]>({
    queryKey: ["agent-runs"],
    queryFn: async () => {
      const { data } = await apiClient.get("/agents/runs?limit=10");
      return Array.isArray(data) ? data : data.runs ?? [];
    },
    refetchInterval: runsPollMs,
  });

  const { data: ragDocs = [] } = useQuery<RagDocument[]>({
    queryKey: ["rag-documents"],
    queryFn: async () => {
      const { data } = await apiClient.get("/rag/documents");
      return data;
    },
  });

  // BUG 19: fetch active model from /users/me/models
  const { data: userModels = [] } = useQuery<{ id: string; provider: string; model_name: string | null; is_active: boolean }[]>({
    queryKey: ["user-models"],
    queryFn: async () => {
      const { data } = await apiClient.get("/users/me/models");
      return data;
    },
  });

  const activeModel = userModels.find((m) => m.is_active);
  const resumes = ragDocs.filter((d) => d.doc_type === "resume");
  const primaryResume = resumes.find((d) => d.is_primary);
  const filteredRuns = runs.filter((r) => r.agent_type === active);
  const activeAgent = AGENTS.find((a) => a.key === active) ?? AGENTS[0];
  const activeGroup = GROUPS.find((g) => g.key === activeAgent.group) ?? GROUPS[0];
  const ActiveIcon = activeAgent.icon;
  const latestRun = filteredRuns[0];
  const latestStatus = runStatus(latestRun);
  const LatestStatusIcon = latestStatus.icon;

  // BUG 7: find the awaiting run to pass run_id to approval
  const awaitingRun = runs.find((r) => r.status === "awaiting_approval");
  const showPendingCta = Boolean(awaitingRun && awaitingRun.id !== displayRunId);

  // Latest run per agent (runs arrive newest-first) — drives the tile dots.
  const latestByAgent = useMemo(() => {
    const map = new Map<string, AgentRun>();
    for (const run of runs) if (!map.has(run.agent_type)) map.set(run.agent_type, run);
    return map;
  }, [runs]);

  const inFlight = runs.filter((r) => r.status === "queued" || r.status === "running").length;
  const awaitingCount = runs.filter((r) => r.status === "awaiting_approval").length;

  // Display-only validity hint; the mutation still performs the real check.
  const contextIsObject = useMemo(() => {
    try {
      const parsed: unknown = JSON.parse(contextText);
      return Boolean(parsed) && typeof parsed === "object" && !Array.isArray(parsed);
    } catch {
      return false;
    }
  }, [contextText]);

  const selectAgent = (key: string) => {
    setActive(key);
    setContextText(JSON.stringify(DEFAULT_CONTEXT[key] ?? {}, null, 2));
  };

  const openRun = (id: string) => {
    setActiveRunId(id);
    setActiveRun(id);
  };

  return (
    <Screen>
      <PageHero
        className="md:pb-8 md:pt-10"
        eyebrow="Agent harness"
        title="Agent cockpit."
        description="Pick an agent, shape its context and run it. Nothing sends or submits until you approve it."
        actions={
          showPendingCta && awaitingRun ? (
            <>
              <IslandButton tone="primary" size="lg" trailing onClick={() => openRun(awaitingRun.id)}>
                Open pending review
              </IslandButton>
              <StatusPill tone="warning" live>
                {agentLabel(awaitingRun.agent_type)} is waiting on you
              </StatusPill>
            </>
          ) : (
            <StatusPill tone={latestStatus.tone} live={latestStatus.live} icon={<LatestStatusIcon size={13} weight="light" />}>
              {activeAgent.label} · {latestStatus.label}
            </StatusPill>
          )
        }
        aside={
          <Bezel size="md" coreClassName="grid grid-cols-3 divide-x divide-foreground/[0.06] dark:divide-white/[0.07]">
            {[
              { label: "In flight", value: inFlight },
              { label: "Awaiting you", value: awaitingCount },
              { label: "Recent runs", value: runs.length },
            ].map((stat) => (
              <div key={stat.label} className="min-w-0 px-4 py-5 md:px-5">
                <p className="truncate text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">{stat.label}</p>
                <p
                  className={cn(
                    "mt-3 font-geist text-3xl font-semibold tabular-nums tracking-[-0.04em] text-foreground",
                    stat.label === "Awaiting you" && stat.value > 0 && "text-warning",
                  )}
                >
                  {runsLoading ? "—" : stat.value}
                </p>
              </div>
            ))}
          </Bezel>
        }
      />

      {/* Agent bento — four purpose groups with asymmetric spans. */}
      <Section aria-label="Choose an agent">
        <h2 className="sr-only">Choose an agent</h2>
        <RevealGroup className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-12">
          {GROUPS.map((group) => {
            const members = AGENTS.filter((a) => a.group === group.key);
            const headingId = `${tileIndicatorId}-${group.key}`;
            return (
              <motion.div key={group.key} variants={listItem} className={cn("min-w-0", group.span)}>
                <Bezel coreClassName="p-3 md:p-4" role="group" aria-labelledby={headingId}>
                  <div className="flex items-baseline justify-between gap-3 px-2 pb-3 pt-1">
                    <div className="flex min-w-0 items-center gap-2.5">
                      <span aria-hidden className={cn("h-2 w-2 shrink-0 rounded-full ring-1", ACCENT[group.key])} />
                      <h3 id={headingId} className="font-geist text-[13px] font-semibold tracking-[-0.01em] text-foreground">
                        {group.label}
                      </h3>
                      <span className="truncate text-xs text-muted-foreground">{group.blurb}</span>
                    </div>
                    <span className="shrink-0 font-geist-mono text-[11px] tabular-nums text-muted-foreground/80">
                      {String(members.length).padStart(2, "0")}
                    </span>
                  </div>
                  <div className={cn("grid grid-cols-1 gap-1.5", group.tiles)}>
                    {members.map((a) => {
                      const Icon = a.icon;
                      const isActive = active === a.key;
                      const last = latestByAgent.get(a.key);
                      const lastStatus = last ? runStatus(last) : null;
                      return (
                        <button
                          key={a.key}
                          type="button"
                          aria-pressed={isActive}
                          onClick={() => selectAgent(a.key)}
                          className={cn(
                            "group relative flex min-h-[68px] w-full items-center gap-3 rounded-[1.1rem] px-3 py-3 text-left",
                            "transition-[background-color,transform] duration-500 ease-vanguard active:scale-[0.985]",
                            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                            !isActive && "hover:bg-foreground/[0.035] dark:hover:bg-white/[0.04]",
                            a.wide && "sm:col-span-2",
                          )}
                        >
                          {isActive ? (
                            <motion.span
                              layoutId={tileIndicatorId}
                              transition={SPRING_SOFT}
                              aria-hidden
                              className="absolute inset-0 rounded-[1.1rem] bg-primary/[0.07] ring-1 ring-primary/25 shadow-bezel-core dark:bg-primary/[0.12] dark:shadow-bezel-core-dark"
                            />
                          ) : null}
                          <span
                            aria-hidden
                            className={cn(
                              "relative grid h-9 w-9 shrink-0 place-items-center rounded-full ring-1 transition-transform duration-500 ease-vanguard group-hover:scale-105",
                              ACCENT[a.group],
                            )}
                          >
                            <Icon size={17} weight="light" />
                          </span>
                          <span className="relative block min-w-0 flex-1">
                            <span className={cn("block truncate text-sm font-medium tracking-[-0.01em]", isActive ? "text-foreground" : "text-foreground/85")}>
                              {a.label}
                            </span>
                            <span className="mt-0.5 block truncate text-xs text-muted-foreground">{a.note}</span>
                          </span>
                          {lastStatus ? (
                            <span className="relative flex h-1.5 w-1.5 shrink-0" title={lastStatus.label}>
                              {lastStatus.live ? (
                                <span aria-hidden className={cn("absolute inset-0 animate-ping rounded-full opacity-60 motion-reduce:hidden", DOT[lastStatus.tone])} />
                              ) : null}
                              <span aria-hidden className={cn("relative h-1.5 w-1.5 rounded-full", DOT[lastStatus.tone])} />
                              <span className="sr-only">, {lastStatus.label.toLowerCase()}</span>
                            </span>
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                </Bezel>
              </motion.div>
            );
          })}
        </RevealGroup>
      </Section>

      {/* Cockpit — span-8 run console + span-4 context rail. */}
      <Section aria-label="Run console" id="run-console">
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <Reveal className="min-w-0 lg:col-span-8">
            <Bezel lifted coreClassName="p-5 md:p-7">
              <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
                <div className="flex min-w-0 items-start gap-4">
                  <span aria-hidden className={cn("grid h-12 w-12 shrink-0 place-items-center rounded-full ring-1", ACCENT[activeAgent.group])}>
                    <ActiveIcon size={22} weight="light" />
                  </span>
                  <div className="min-w-0">
                    <Eyebrow>{activeGroup.label}</Eyebrow>
                    <h2 className="mt-3 font-geist text-2xl font-semibold tracking-[-0.035em] text-foreground md:text-3xl">
                      {activeAgent.label} Agent
                    </h2>
                    <p className="mt-1.5 text-sm text-muted-foreground">{activeAgent.note}</p>
                  </div>
                </div>
                <div className="flex flex-col items-stretch gap-3 sm:items-end">
                  <IslandButton
                    tone="primary"
                    size="md"
                    disabled={runMutation.isPending}
                    aria-busy={runMutation.isPending}
                    onClick={() => runMutation.mutate()}
                    trailing={
                      runMutation.isPending ? (
                        <CircleNotch size={15} weight="light" className="animate-spin motion-reduce:animate-none" />
                      ) : (
                        <Play size={15} weight="light" />
                      )
                    }
                    className="w-full sm:w-auto"
                  >
                    {runMutation.isPending ? "Running..." : "Run agent"}
                  </IslandButton>
                  <p aria-live="polite" className="flex items-center gap-1.5 text-xs text-muted-foreground sm:justify-end">
                    <LatestStatusIcon aria-hidden size={13} weight="light" />
                    {latestRun ? `${latestStatus.label} · ${relativeTime(latestRun.started_at)}` : "No runs yet for this agent"}
                  </p>
                </div>
              </div>

              {active === "auto_apply" ? <AutoApplyPanel /> : null}

              <div className="mt-7 space-y-2">
                <div className="flex items-center justify-between gap-3 pl-1">
                  <label htmlFor={contextFieldId} className="flex items-center gap-2 text-[12px] font-medium tracking-[-0.005em] text-muted-foreground">
                    <BracketsCurly aria-hidden size={14} weight="light" />
                    Task context (JSON)
                  </label>
                  <StatusPill tone={contextIsObject ? "success" : "danger"}>{contextIsObject ? "Valid object" : "Not a JSON object"}</StatusPill>
                </div>
                <Textarea
                  id={contextFieldId}
                  name="task_context"
                  aria-describedby={contextHintId}
                  aria-invalid={!contextIsObject}
                  value={contextText}
                  onChange={(event) => setContextText(event.target.value)}
                  spellCheck={false}
                  autoComplete="off"
                  className="min-h-36 font-geist-mono text-[12.5px] leading-6"
                />
                <p id={contextHintId} className="pl-1 text-xs leading-5 text-muted-foreground">
                  Sent as the run&apos;s context. Switching agents resets it to that agent&apos;s defaults.
                </p>
              </div>

              <Hairline className="my-7" />

              <div className="flex items-center justify-between gap-3 pb-4">
                <div className="flex items-center gap-2 text-[13px] font-medium text-foreground">
                  <Pulse aria-hidden size={16} weight="light" className="text-muted-foreground" />
                  Live run
                </div>
                <StatusPill tone={displayRunId ? "primary" : "neutral"} live={Boolean(displayRunId)}>
                  {displayRunId ? "Stream attached" : "Idle"}
                </StatusPill>
              </div>
              <Bezel size="md" tone="muted" coreClassName="min-h-[260px] p-4 md:p-5">
                {/* BUG 12: mount AgentStatusStream when a run is active */}
                {displayRunId ? (
                  <AgentStatusStream
                    runId={displayRunId}
                    onApprove={() => {
                      qc.invalidateQueries({ queryKey: ["agent-runs"] });
                    }}
                    onCancel={() => {
                      qc.invalidateQueries({ queryKey: ["agent-runs"] });
                      setActiveRunId(null);
                      setActiveRun(null);
                    }}
                  />
                ) : (
                  <EmptyPanel
                    compact
                    className="min-h-[228px] justify-center"
                    icon={<ActiveIcon size={24} weight="light" />}
                    title={latestStatus.label}
                    description={latestRun ? runMessage(latestRun) : activeAgent.note}
                  />
                )}
              </Bezel>
            </Bezel>
          </Reveal>

          <aside className="min-w-0 space-y-6 lg:col-span-4" aria-label="Run context and history">
            {/* BUG 19: dynamic context sidebar */}
            <Reveal delay={0.06}>
              <Bezel coreClassName="p-5">
                <PanelTitle title="Context" icon={<Stack size={15} weight="light" />} />
                <dl className="mt-5 space-y-1">
                  <div className="flex items-center gap-3 rounded-2xl px-2 py-2.5">
                    <FileText aria-hidden size={16} weight="light" className="shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <dt className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Resume</dt>
                      <dd className="mt-1">
                        <Select
                          aria-label="Resume for this run"
                          value={resumeId}
                          onChange={(e) => setResumeId(e.target.value)}
                          disabled={resumes.length === 0}
                        >
                          <option value="">{primaryResume ? `Active: ${primaryResume.filename}` : "No resume uploaded"}</option>
                          {resumes
                            .filter((d) => !d.is_primary)
                            .map((d) => (
                              <option key={d.id} value={d.id}>
                                {d.filename}
                              </option>
                            ))}
                        </Select>
                      </dd>
                    </div>
                    {!primaryResume ? (
                      <IslandLink href="/resume" tone="ghost" size="sm">
                        Upload
                      </IslandLink>
                    ) : null}
                  </div>
                  <Hairline />
                  <div className="flex items-center gap-3 rounded-2xl px-2 py-2.5">
                    <Cpu aria-hidden size={16} weight="light" className="shrink-0 text-muted-foreground" />
                    <div className="min-w-0 flex-1">
                      <dt className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">Model</dt>
                      <dd className="mt-1">
                        <Select
                          aria-label="Model for this run"
                          value={modelId}
                          onChange={(e) => setModelId(e.target.value)}
                          disabled={userModels.length === 0}
                        >
                          <option value="">
                            {activeModel ? `Active: ${activeModel.model_name ?? activeModel.provider}` : "Not configured"}
                          </option>
                          {userModels
                            .filter((m) => !m.is_active)
                            .map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.model_name ?? m.provider}
                              </option>
                            ))}
                        </Select>
                      </dd>
                    </div>
                    {!activeModel ? (
                      <IslandLink href="/settings" tone="ghost" size="sm">
                        Configure
                      </IslandLink>
                    ) : null}
                  </div>
                </dl>
                <Notice className="mt-5 text-[13px] leading-5" icon={<HandPalm size={16} weight="light" />}>
                  Agents prepare, you decide. Every send and submission waits at an approval gate.
                </Notice>
              </Bezel>
            </Reveal>

            <Reveal delay={0.12}>
              <Bezel coreClassName="p-5">
                <PanelTitle
                  title="Run history"
                  icon={<ClockCounterClockwise size={15} weight="light" />}
                  meta={<span className="tabular-nums">{activeAgent.label} · {filteredRuns.length}</span>}
                />
                <div className="mt-5" aria-busy={runsLoading}>
                  {runsLoading ? (
                    <div className="space-y-2">
                      <Skeleton className="h-[76px] rounded-2xl" />
                      <Skeleton className="h-[76px] rounded-2xl" />
                    </div>
                  ) : filteredRuns.length === 0 ? (
                    <EmptyPanel
                      compact
                      icon={<ClockCounterClockwise size={22} weight="light" />}
                      title="No runs yet."
                      description={`Runs of the ${activeAgent.label} agent will collect here.`}
                    />
                  ) : (
                    <motion.ul
                      initial="hidden"
                      animate="show"
                      variants={listStagger}
                      className="-m-1 space-y-2 p-1 lg:max-h-[34rem] lg:overflow-y-auto lg:overscroll-contain"
                    >
                      {filteredRuns.map((run) => {
                        const selected = run.id === displayRunId;
                        return (
                          <motion.li key={run.id} variants={listItem}>
                            <button
                              type="button"
                              aria-current={selected ? "true" : undefined}
                              className={cn(
                                "block w-full rounded-2xl text-left transition-[transform,box-shadow] duration-500 ease-vanguard active:scale-[0.99]",
                                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                                selected && "ring-2 ring-primary/35 ring-offset-2 ring-offset-card",
                              )}
                              onClick={() => openRun(run.id)}
                            >
                              <AgentStatusCard
                                agentType={run.agent_type}
                                status={
                                  run.status === "completed"
                                    ? "succeeded"
                                    : run.status === "failed"
                                      ? "failed"
                                      : run.status === "awaiting_approval"
                                        ? "awaiting_approval"
                                        : "running"
                                }
                                latestMessage={runMessage(run)}
                                startedAt={relativeTime(run.started_at)}
                              />
                            </button>
                          </motion.li>
                        );
                      })}
                    </motion.ul>
                  )}
                </div>
              </Bezel>
            </Reveal>
          </aside>
        </div>
      </Section>
    </Screen>
  );
}
