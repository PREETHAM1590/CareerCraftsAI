"use client";

import { useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  ArrowSquareOut,
  CalendarBlank,
  ClockCountdown,
  EnvelopeSimple,
  FileText,
  Globe,
  Gauge,
  Lightning,
  MapPin,
  Note,
  Pulse,
  Sparkle,
  X,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { setPendingJd } from "@/lib/job-handoff";
import { fetchApplicationJd } from "@/lib/applications-api";
import {
  Bezel,
  EASE_VANGUARD,
  Eyebrow,
  Hairline,
  IconButton,
  IslandButton,
  IslandLink,
  Notice,
  PanelTitle,
  Segmented,
  SPRING_PANEL,
  StatusPill,
  bezelCore,
  bezelShell,
  listItem,
  listStagger,
  type StatusTone,
} from "@/components/vanguard";
import { APP_STAGES, ApplyControls, STAGE_LABELS, STAGE_TONE, type ApplicationItem, type AppStage } from "./ApplicationList";
import { SubmissionEvidence, type Submission } from "./SubmissionEvidence";

type AgentRun = {
  id: string;
  agent_type: string;
  status: string;
  started_at: string;
  output_summary?: string;
};

type Props = {
  application: ApplicationItem | null;
  open: boolean;
  onClose: () => void;
  onStageChange: (stage: AppStage) => void;
  activityRuns: AgentRun[];
  /** Who approved the browser submission and with which answers, if any. */
  submission?: Submission | null;
};

const AI_SUGGESTIONS: Partial<Record<AppStage, { label: string; copy: string; href: string }>> = {
  interview: {
    label: "Prepare for interview",
    copy: "Practice role specific questions and plan your stories.",
    href: "/interview?tab=prep",
  },
  offer: {
    label: "Review compensation",
    copy: "Compare your offer with market data and prepare negotiation points.",
    href: "/salary",
  },
};

const STAGE_OPTIONS = APP_STAGES.map((stage) => ({ value: stage, label: STAGE_LABELS[stage] }));

function safeSource(url: string | null | undefined): { href: string; label: string } | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return null;
    return { href: parsed.href, label: parsed.hostname.replace(/^www\./, "") };
  } catch {
    return null;
  }
}

function dateLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? null
    : date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function runTone(status: string): StatusTone {
  const s = status.toLowerCase();
  if (["completed", "complete", "succeeded", "success", "approved"].includes(s)) return "success";
  if (["failed", "error", "cancelled", "canceled", "rejected"].includes(s)) return "danger";
  if (["awaiting_approval", "pending", "queued"].includes(s)) return "warning";
  if (["running", "in_progress", "started"].includes(s)) return "primary";
  return "neutral";
}

type Fact = { key: string; label: string; icon: ReactNode; value: ReactNode };

export function ApplicationDrawer({ application, open, onClose, onStageChange, activityRuns, submission }: Props) {
  const router = useRouter();
  const reduce = useReducedMotion();
  // Keep the last shown application so the drawer can finish its exit
  // animation after the parent clears the selection.
  const [shown, setShown] = useState<ApplicationItem | null>(application);
  if (application && application !== shown) setShown(application);
  const app = application ?? shown;

  const visible = open && app !== null;

  const [customizing, setCustomizing] = useState(false);

  // The row's jd_text is truncated/often empty, so ask the API (falls back to the shared job catalog).
  async function customizeResume() {
    const current = app;
    if (!current || customizing) return;
    setCustomizing(true);
    try {
      const jd = await fetchApplicationJd(current.id);
      if (!jd.jd_text?.trim()) throw Object.assign(new Error("empty"), { response: { status: 404 } });
      setPendingJd({ jdText: jd.jd_text, role: current.role, company: current.company });
      onClose();
      router.push("/resume");
    } catch (err) {
      const notFound = (err as { response?: { status?: number } }).response?.status === 404;
      toast.error(notFound ? "This saved role has no description to tailor against." : "Could not load the job description. Try again.");
    } finally {
      setCustomizing(false);
    }
  }

  const panelMotion = reduce
    ? { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, transition: { duration: 0.2 } }
    : { initial: { x: "104%" }, animate: { x: 0 }, exit: { x: "104%" }, transition: SPRING_PANEL };

  return (
    <Dialog.Root open={visible} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <AnimatePresence>
        {visible && app ? (
          <Dialog.Portal forceMount key="application-drawer">
            <Dialog.Overlay asChild forceMount>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: reduce ? 0.2 : 0.5, ease: EASE_VANGUARD }}
                className="fixed inset-0 z-40 bg-foreground/15 backdrop-blur-md dark:bg-black/50"
              />
            </Dialog.Overlay>
            <Dialog.Content asChild forceMount aria-describedby="application-details-description">
              <motion.div
                {...panelMotion}
                className="fixed inset-y-0 right-0 z-40 flex w-full p-2 font-geist antialiased focus:outline-none sm:max-w-[35rem] sm:p-3"
              >
                <DrawerBody
                  app={app}
                  reduce={!!reduce}
                  onClose={onClose}
                  onStageChange={onStageChange}
                  onCustomize={customizeResume}
                  customizing={customizing}
                  activityRuns={activityRuns}
                  submission={submission ?? null}
                />
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        ) : null}
      </AnimatePresence>
    </Dialog.Root>
  );
}

function DrawerBody({
  app,
  reduce,
  onClose,
  onStageChange,
  onCustomize,
  customizing,
  activityRuns,
  submission,
}: {
  app: ApplicationItem;
  reduce: boolean;
  onClose: () => void;
  onStageChange: (stage: AppStage) => void;
  onCustomize: () => void;
  customizing: boolean;
  activityRuns: AgentRun[];
  submission: Submission | null;
}) {
  const source = safeSource(app.jobUrl);
  const suggestion = AI_SUGGESTIONS[app.stage];
  const appliedDate = dateLabel(app.appliedAt);
  const hasDescription = !!app.jobDescription?.trim();
  const match = app.matchPercent != null ? Math.max(0, Math.min(100, app.matchPercent)) : null;

  const facts = ([
    app.location ? { key: "location", label: "Location", icon: <MapPin size={14} weight="light" />, value: app.location } : null,
    appliedDate ? { key: "applied", label: "Applied", icon: <CalendarBlank size={14} weight="light" />, value: appliedDate } : null,
    app.nextFollowUp
      ? { key: "followup", label: "Next follow-up", icon: <ClockCountdown size={14} weight="light" />, value: app.nextFollowUp }
      : null,
    app.source ? { key: "source", label: "Found on", icon: <MapPin size={14} weight="light" />, value: app.source } : null,
    app.resumeLabel ? { key: "resume", label: "Resume sent", icon: <CalendarBlank size={14} weight="light" />, value: app.resumeLabel } : null,
    app.outreachStatus
      ? {
          key: "outreach",
          label: "Recruiter email",
          icon: <ClockCountdown size={14} weight="light" />,
          value: `${app.outreachStatus}${app.outreachTo ? ` · ${app.outreachTo}` : ""}`,
        }
      : null,
    match != null
      ? {
          key: "match",
          label: "Match",
          icon: <Gauge size={14} weight="light" />,
          value: (
            <span className="flex items-center gap-3">
              <span className="tabular-nums">{app.matchPercent}%</span>
              <span aria-hidden className="h-[3px] w-16 overflow-hidden rounded-full bg-foreground/[0.07] dark:bg-white/10">
                <span className="block h-full origin-left rounded-full bg-primary" style={{ transform: `scaleX(${match / 100})` }} />
              </span>
            </span>
          ),
        }
      : null,
  ] as Array<Fact | null>).filter((fact): fact is Fact => fact !== null);

  const stagger = reduce
    ? {}
    : { initial: "hidden" as const, animate: "show" as const, variants: listStagger };
  const item = reduce ? {} : { variants: listItem };

  return (
    <div className={cn(bezelShell("lg"), "flex h-full w-full bg-background/60 shadow-ambient backdrop-blur-xl dark:bg-white/[0.04]")}>
      <div className={cn(bezelCore("lg"), "flex h-full w-full flex-col overflow-hidden")}>
        <header className="px-6 pb-6 pt-6 sm:px-8 sm:pt-8">
          <div className="flex items-start justify-between gap-4">
            <div className="flex flex-wrap items-center gap-2">
              <Eyebrow>Application details</Eyebrow>
              <StatusPill tone={STAGE_TONE[app.stage]} live={app.stage === "interview"}>
                {STAGE_LABELS[app.stage]}
              </StatusPill>
            </div>
            <Dialog.Close asChild>
              <IconButton aria-label="Close application details" className="-mr-1 -mt-1">
                <X size={16} weight="light" />
              </IconButton>
            </Dialog.Close>
          </div>
          <Dialog.Title className="mt-6 text-balance font-geist text-[1.85rem] font-semibold leading-[1.04] tracking-[-0.035em] text-foreground sm:text-[2.35rem]">
            {app.role}
          </Dialog.Title>
          <Dialog.Description id="application-details-description" className="mt-2 text-[15px] text-muted-foreground">
            {app.company}
            {app.matchPercent != null && (
              <>
                {" "}
                <span aria-hidden="true">·</span> {app.matchPercent}% match
              </>
            )}
          </Dialog.Description>
        </header>

        <Hairline />

        <motion.div {...stagger} className="flex-1 space-y-8 overflow-y-auto overscroll-contain px-6 py-7 sm:px-8">
          {facts.length > 0 && (
            <motion.dl
              {...item}
              className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl bg-foreground/[0.06] ring-1 ring-foreground/[0.06] dark:bg-white/[0.06] dark:ring-white/10"
            >
              {facts.map((fact, index) => (
                <div
                  key={fact.key}
                  className={cn("min-w-0 bg-card px-4 py-3.5", facts.length % 2 === 1 && index === facts.length - 1 && "col-span-2")}
                >
                  <dt className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                    <span aria-hidden>{fact.icon}</span>
                    {fact.label}
                  </dt>
                  <dd className="mt-1.5 truncate text-sm font-medium text-foreground">{fact.value}</dd>
                </div>
              ))}
            </motion.dl>
          )}

          <motion.section {...item} aria-labelledby="application-stage-label" className="space-y-3">
            <p id="application-stage-label" className="pl-1 text-[12px] font-medium text-muted-foreground">
              Application stage
            </p>
            <Segmented
              value={app.stage}
              onChange={(stage) => {
                if (stage !== app.stage) onStageChange(stage);
              }}
              options={STAGE_OPTIONS}
              asTabs={false}
              size="sm"
              ariaLabel="Application stage"
            />
          </motion.section>

          <motion.div {...item}>
            <Bezel tone="primary" size="md" coreClassName="p-5 sm:p-6">
              <div className="flex items-start gap-4">
                <span aria-hidden className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-primary/10 text-primary ring-1 ring-primary/20">
                  <Sparkle size={18} weight="light" />
                </span>
                <div className="min-w-0 flex-1">
                  <h3 className="font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground">Make this resume fit the role</h3>
                  <p className="mt-1.5 text-sm leading-6 text-muted-foreground">
                    Use this job description to tailor your resume and check its match.
                  </p>
                  <IslandButton
                    type="button"
                    tone="primary"
                    size="sm"
                    className="mt-4"
                    disabled={customizing}
                    aria-busy={customizing}
                    onClick={onCustomize}
                    trailing
                  >
                    {customizing ? "Loading description…" : "Customize resume for this job"}
                  </IslandButton>
                </div>
              </div>
            </Bezel>
          </motion.div>

          <motion.section {...item} className="space-y-3">
            <PanelTitle title="Apply in your browser" icon={<Lightning size={15} weight="light" />} />
            <p className="text-sm leading-6 text-muted-foreground">Opens the job in your own browser. You review and click Submit yourself.</p>
            <ApplyControls id={app.id} jobUrl={app.jobUrl} state={app.applyState} />
          </motion.section>

          <motion.section {...item} className="space-y-3">
            <PanelTitle title="Original source" icon={<Globe size={15} weight="light" />} />
            {source ? (
              <a
                href={source.href}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex items-center justify-between gap-3 rounded-2xl bg-foreground/[0.02] p-4 ring-1 ring-foreground/[0.06] transition-[background-color,box-shadow] duration-500 ease-vanguard hover:bg-foreground/[0.04] hover:ring-primary/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 dark:bg-white/[0.02] dark:ring-white/10 dark:hover:bg-white/[0.05]"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-foreground">{source.label}</span>
                  <span className="mt-0.5 block truncate font-geist-mono text-[11px] text-muted-foreground">{source.href}</span>
                </span>
                <span
                  aria-hidden
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-foreground/[0.05] text-muted-foreground transition-[transform,color] duration-500 ease-vanguard group-hover:-translate-y-[1px] group-hover:translate-x-0.5 group-hover:text-primary dark:bg-white/10"
                >
                  <ArrowSquareOut size={14} weight="light" />
                </span>
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            ) : (
              <Notice>Original posting link was not saved for this role.</Notice>
            )}
          </motion.section>

          <motion.section {...item} className="space-y-3">
            <PanelTitle title="Job description" icon={<FileText size={15} weight="light" />} />
            {hasDescription ? (
              <Bezel tone="muted" size="md" coreClassName="max-h-[26rem] overflow-y-auto overscroll-contain p-5">
                <p className="whitespace-pre-wrap text-sm leading-6 text-foreground/90">{app.jobDescription}</p>
              </Bezel>
            ) : (
              <Notice>This listing has no saved description. Open the original source to review the details.</Notice>
            )}
          </motion.section>

          {app.notes && (
            <motion.section {...item} className="space-y-3">
              <PanelTitle title="Notes" icon={<Note size={15} weight="light" />} />
              <Bezel tone="muted" size="md" coreClassName="p-5">
                <p className="whitespace-pre-wrap text-sm leading-6 text-muted-foreground">{app.notes}</p>
              </Bezel>
            </motion.section>
          )}

          {suggestion && (
            <motion.section
              {...item}
              className="flex flex-col gap-4 rounded-2xl bg-foreground/[0.02] p-5 ring-1 ring-foreground/[0.06] dark:bg-white/[0.02] dark:ring-white/10 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex min-w-0 items-start gap-3">
                <span aria-hidden className="mt-0.5 text-warning">
                  <Lightning size={16} weight="light" />
                </span>
                <div className="min-w-0">
                  <p className="text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Next step</p>
                  <p className="mt-1 text-sm leading-6 text-foreground">{suggestion.copy}</p>
                </div>
              </div>
              <IslandLink href={suggestion.href} onClick={onClose} tone="ghost" size="sm" trailing className="shrink-0">
                {suggestion.label}
              </IslandLink>
            </motion.section>
          )}

          {submission ? (
            <motion.div {...item}>
              <SubmissionEvidence applicationId={app.id} submission={submission} />
            </motion.div>
          ) : null}

          <motion.section {...item} className="space-y-4 pb-2">
            <PanelTitle
              title="Recent activity"
              icon={<Pulse size={15} weight="light" />}
              meta={activityRuns.length ? <span className="tabular-nums">{activityRuns.length} runs</span> : null}
            />
            {activityRuns.length ? (
              <ol className="relative space-y-5 pl-6 before:absolute before:bottom-2 before:left-[7px] before:top-2 before:w-px before:bg-foreground/[0.08] dark:before:bg-white/10">
                {activityRuns.map((run) => (
                  <li key={run.id} className="relative text-sm">
                    <span aria-hidden className="absolute -left-6 top-1.5 grid h-[15px] w-[15px] place-items-center rounded-full bg-card ring-1 ring-foreground/10 dark:ring-white/15">
                      <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    </span>
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium capitalize text-foreground">{run.agent_type.replace(/_/g, " ")}</p>
                      <StatusPill tone={runTone(run.status)} live={runTone(run.status) === "primary"}>
                        {run.status.replace(/_/g, " ")}
                      </StatusPill>
                    </div>
                    <p className="mt-1 font-geist-mono text-[11px] text-muted-foreground">{new Date(run.started_at).toLocaleString()}</p>
                    {run.output_summary && <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{run.output_summary}</p>}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="text-sm text-muted-foreground">No agent activity is linked to this role yet.</p>
            )}
          </motion.section>
        </motion.div>

        <Hairline />
        <footer className="flex items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <IslandLink href="/email" onClick={onClose} tone="quiet" size="sm" icon={<EnvelopeSimple size={15} weight="light" />}>
            Draft a follow-up email
          </IslandLink>
          <Dialog.Close asChild>
            <IslandButton tone="ghost" size="sm">
              Done
            </IslandButton>
          </Dialog.Close>
        </footer>
      </div>
    </div>
  );
}
