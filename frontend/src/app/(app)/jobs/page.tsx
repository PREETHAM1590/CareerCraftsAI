"use client";
import * as DialogPrimitive from "@radix-ui/react-dialog";

import { useState, useEffect, useId, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence, useReducedMotion, type Variants } from "motion/react";
import {
  MagnifyingGlass,
  SlidersHorizontal,
  MapPin,
  Clock,
  BookmarkSimple,
  ArrowSquareOut,
  Lightning,
  Globe,
  Copy,
  Check,
  CaretDown,
  CircleNotch,
  X,
  Brain,
  FloppyDisk,
  WarningCircle,
  Briefcase,
  Sparkle,
  Broadcast,
  ArrowRight,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { setPendingJd } from "@/lib/job-handoff";
import { fetchApplications, fetchApplicationJd, type ApplicationFilters, type ApplicationPage } from "@/lib/applications-api";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { JobSearchBasis } from "@/components/jobs/JobSearchBasis";
import { LocationInput } from "@/components/ui/LocationInput";
import { wakeExtension } from "@/lib/extension-bridge";
import { cn } from "@/lib/utils";
import { AgentStatusStream } from "@/components/agents/AgentStatusStream";
import { useAgentStore, usePollMs } from "@/store/agentStore";
import {
  Bezel,
  Chip,
  EmptyPanel,
  Eyebrow,
  Field,
  Hairline,
  IconButton,
  Input,
  IslandButton,
  IslandLink,
  Notice,
  PageHero,
  PanelTitle,
  Reveal,
  Screen,
  Section,
  SectionHeading,
  Select,
  Skeleton,
  StatusPill,
  Textarea,
  Toggle,
  listItem,
  listStagger,
  panelSwap,
  inputControlClass,
  inputTrayClass,
  type StatusTone,
} from "@/components/vanguard";

const XRAY_TEMPLATES = [
  `site:linkedin.com/jobs "Frontend Engineer" "React" "Remote"`,
  `site:greenhouse.io OR site:lever.co "Software Engineer" "Python" "India"`,
  `"careers.stripe.com" OR "jobs.notion.so" "Software Engineer" -intern`,
];

const MODE_FILTERS = ["Remote", "Hybrid", "Onsite"];
const LOCATION_FILTERS = ["Bangalore", "Hyderabad", "Mumbai"];

const JOB_TYPE_FILTERS = ["Full-time", "Part-time", "Contract", "Internship"];
const DATE_FILTERS = ["Today", "Past week", "Past month"];
const EXPERIENCE_LEVELS = ["fresher", "junior", "mid", "senior", "lead", "principal"];
const WORK_MODES = ["remote", "hybrid", "onsite"];
const JOB_TYPES = ["full-time", "part-time", "contract", "internship"];

const FILTER_GROUPS: ReadonlyArray<{ label: string; items: string[] }> = [
  { label: "Location / Mode", items: ["Remote", "Hybrid", "Onsite", "Bangalore", "Hyderabad", "Mumbai"] },
  { label: "Date posted", items: DATE_FILTERS },
];

/** Reduced-motion stand-in for the kit's translate/blur variants. */
const FADE_ONLY: Variants = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.2 } },
  exit: { opacity: 0, transition: { duration: 0.15 } },
};

interface SavedJob {
  id: string;
  company: string;
  role: string;
  location: string | null;
  job_url: string | null;
  jd_text: string | null;
  match_score: number | null;
  status: string;
  applied_at: string | null;
  source?: string | null;
  posted_at?: string | null;
}

interface JobSearchPrefs {
  target_roles?: string[];
  preferred_locations?: string[];
  work_mode?: string;
  job_type?: string;
  experience_level?: string;
  years_experience?: number | null;
  current_title?: string;
  bio?: string | null;
  salary_min?: number | null;
  salary_max?: number | null;
}

interface JobSearchProfile {
  resume_found: boolean;
  resume_filename?: string | null;
  role_suggestions: string[];
  skills: string[];
  inferred_years_experience?: number | null;
  inferred_experience_level?: string | null;
  saved_preferences: JobSearchPrefs;
  search_query_preview: string;
  location_preview: string;
  work_mode_preview?: string | null;
  missing_fields: string[];
  analysis_notes: string[];
}

interface SearchProfileForm {
  target_roles: string;
  preferred_locations: string;
  experience_level: string;
  years_experience: string;
  job_type: string;
  work_mode: string;
  current_title: string;
  salary_min: string;
  salary_max: string;
}

function splitCsv(value: string | null | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function toggleCsvValue(value: string, item: string) {
  const values = splitCsv(value);
  if (values.includes(item)) {
    return values.length > 1 ? values.filter((v) => v !== item).join(", ") : value;
  }
  return [...values, item].join(", ");
}

function primaryCsvValue(value: string | null | undefined, fallback = "") {
  return splitCsv(value)[0] ?? fallback;
}

function titleToValue(value: string) {
  return value.toLowerCase().replace(/\s+/g, "-");
}

function modeValueToChip(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function jobTypeValueToChip(value: string) {
  const labels: Record<string, string> = {
    "full-time": "Full-time",
    "part-time": "Part-time",
    contract: "Contract",
    internship: "Internship",
  };
  return labels[value] ?? labelize(value);
}

function labelize(value: string | null | undefined): string {
  if (!value) return "Not set";
  return value.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3600000);
  if (h < 1) return "Just now";
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return d === 1 ? "Yesterday" : `${d}d ago`;
}

function jobDomain(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace("www.", "");
  } catch {
    return null;
  }
}

function clampScore(score: number | null): number {
  return Math.max(0, Math.min(100, score ?? 0));
}

function matchFill(p: number) {
  return p >= 80 ? "bg-primary" : p >= 60 ? "bg-warning" : "bg-danger";
}

function matchText(p: number) {
  if (p <= 0) return "text-muted-foreground";
  return p >= 80 ? "text-primary" : p >= 60 ? "text-warning" : "text-danger";
}

function statusTone(status: string): StatusTone {
  if (status === "applied") return "success";
  if (status === "saved") return "primary";
  return "neutral";
}

function statusLabel(status: string) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

/* -------------------------------------------------------------------------- */
/*  Small presentational pieces                                               */
/* -------------------------------------------------------------------------- */

function MatchMeter({ percent, className }: { percent: number | null; className?: string }) {
  const p = clampScore(percent);
  return (
    <div
      role="meter"
      aria-label="Match score"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={p}
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-foreground/[0.06] dark:bg-white/10", className)}
    >
      <div
        className={cn("h-full w-full origin-left rounded-full transition-transform duration-700 ease-vanguard", matchFill(p))}
        style={{ transform: `scaleX(${p / 100})` }}
      />
    </div>
  );
}

function JobMeta({ job, className }: { job: SavedJob; className?: string }) {
  const domain = jobDomain(job.job_url);
  return (
    <ul className={cn("flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground", className)}>
      {job.location && (
        <li className="inline-flex min-w-0 items-center gap-1.5">
          <MapPin size={13} weight="light" aria-hidden />
          <span className="truncate">{job.location}</span>
        </li>
      )}
      {domain && (
        <li className="inline-flex min-w-0 items-center gap-1.5">
          <Globe size={13} weight="light" aria-hidden />
          <span className="truncate">{domain}</span>
        </li>
      )}
      <li className="inline-flex items-center gap-1.5 tabular-nums">
        <Clock size={13} weight="light" aria-hidden />
        {relativeTime(job.applied_at)}
      </li>
    </ul>
  );
}

/** Save + Apply pair shared by the featured card and the dense rows. */
function JobActions({
  job,
  onPrepareApply,
  size = "sm",
  className,
}: {
  job: SavedJob;
  onPrepareApply: (job: SavedJob) => void;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div className={cn("relative flex min-w-0 flex-wrap items-center gap-2", className)}>
      <IslandButton
        tone="ghost"
        size={size}
        icon={<BookmarkSimple size={size === "sm" ? 14 : 16} weight="light" />}
        onClick={(e) => e.stopPropagation()}
      >
        Save
      </IslandButton>
      {job.job_url ? (
        <IslandButton
          tone="primary"
          size={size}
          trailing={<ArrowSquareOut size={size === "sm" ? 13 : 15} weight="light" />}
          onClick={(e) => {
            e.stopPropagation();
            // Open the real job posting so the user can apply directly...
            if (job.job_url) window.open(job.job_url, "_blank", "noopener,noreferrer");
            // ...and kick off the human-in-the-loop auto-apply prep.
            onPrepareApply(job);
          }}
        >
          Apply
        </IslandButton>
      ) : (
        <IslandButton tone="primary" size={size} disabled trailing={<ArrowSquareOut size={size === "sm" ? 13 : 15} weight="light" />}>
          Apply
        </IslandButton>
      )}
    </div>
  );
}

/** Pill disclosure toggle (Filters / Advanced search). */
function DisclosurePill({
  open,
  onToggle,
  controls,
  icon,
  count,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  controls: string;
  icon: ReactNode;
  count?: number;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={open ? controls : undefined}
      onClick={onToggle}
      className={cn(
        "inline-flex h-9 items-center gap-2 rounded-full pl-3.5 pr-3 text-[13px] font-medium ring-1",
        "transition-[background-color,color,box-shadow,transform] duration-500 ease-vanguard active:scale-[0.97]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        open
          ? "bg-foreground text-background ring-foreground dark:bg-white dark:text-black dark:ring-white"
          : "bg-card text-foreground ring-foreground/[0.08] hover:ring-foreground/20 dark:bg-white/[0.03] dark:ring-white/10",
      )}
    >
      <span aria-hidden className="grid place-items-center">{icon}</span>
      {children}
      {count ? (
        <span
          className={cn(
            "rounded-full px-1.5 text-[10px] font-semibold tabular-nums",
            open ? "bg-background/20" : "bg-foreground/[0.07] dark:bg-white/10",
          )}
        >
          {count}
        </span>
      ) : null}
      <CaretDown
        size={13}
        weight="light"
        aria-hidden
        className={cn("transition-transform duration-500 ease-vanguard", open && "rotate-180")}
      />
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/*  Hero: search island + agent console                                       */
/* -------------------------------------------------------------------------- */

function SearchIsland({
  query,
  onQueryChange,
  location,
  onLocationChange,
  onSubmit,
  busy,
  running,
}: {
  query: string;
  onQueryChange: (value: string) => void;
  location: string;
  onLocationChange: (value: string) => void;
  onSubmit: () => void;
  busy: boolean;
  running: boolean;
}) {
  const queryId = useId();
  const locationId = useId();
  const segment =
    "flex min-w-0 flex-1 items-center gap-3 rounded-[1.25rem] px-4 py-2.5 transition-[background-color,box-shadow] duration-500 ease-vanguard " +
    "hover:bg-foreground/[0.025] focus-within:bg-foreground/[0.03] focus-within:ring-2 focus-within:ring-primary/35 dark:hover:bg-white/[0.03] dark:focus-within:bg-white/[0.04]";
  const control =
    "w-full min-w-0 text-ellipsis bg-transparent text-[15px] text-foreground outline-none placeholder:text-muted-foreground/70";

  return (
    <Bezel
      role="search"
      aria-label="Search jobs"
      lifted
      coreClassName="flex flex-col gap-1 p-1.5 md:flex-row md:items-stretch"
    >
      <div className={segment}>
        <MagnifyingGlass size={18} weight="light" aria-hidden className="shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <label htmlFor={queryId} className="block text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Role or keywords
          </label>
          <input
            id={queryId}
            name="query"
            type="text"
            autoComplete="off"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onSubmit()}
            placeholder="Leave blank to use resume + saved preferences, or type custom role…"
            className={control}
          />
        </div>
      </div>

      <Hairline vertical className="my-3 hidden md:block" />

      <div className={cn(segment, "md:max-w-[15rem]")}>
        <MapPin size={18} weight="light" aria-hidden className="shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <label htmlFor={locationId} className="block text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Where
          </label>
          <LocationInput
            id={locationId}
            name="location"
            value={location}
            onChange={onLocationChange}
            onKeyDown={(e) => e.key === "Enter" && onSubmit()}
            placeholder="City or Remote"
            inputClassName={control}
          />
        </div>
      </div>

      <IslandButton
        size="lg"
        onClick={onSubmit}
        disabled={busy}
        aria-label={running ? "Searching — Job Agent running" : "Search — Run Job Agent"}
        icon={
          busy ? (
            <CircleNotch size={17} weight="light" className="animate-spin motion-reduce:animate-none" />
          ) : (
            <Lightning size={17} weight="light" />
          )
        }
        trailing={<MagnifyingGlass size={16} weight="light" />}
        className="w-full md:w-auto md:self-center"
      >
        {running ? "Searching…" : "Search"}
      </IslandButton>
    </Bezel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Expandable panels                                                         */
/* -------------------------------------------------------------------------- */

function FilterPanel({
  activeFilters,
  onToggle,
  onClear,
}: {
  activeFilters: Set<string>;
  onToggle: (chip: string) => void;
  onClear: () => void;
}) {
  const baseId = useId();
  return (
    <Bezel size="md" coreClassName="space-y-5 p-5 md:p-6">
      <PanelTitle
        title="Filters"
        icon={<SlidersHorizontal size={15} weight="light" />}
        meta={
          activeFilters.size > 0 ? (
            <IslandButton tone="quiet" size="sm" onClick={onClear} icon={<X size={13} weight="light" />}>
              Clear all
            </IslandButton>
          ) : (
            "None active"
          )
        }
      />
      <div className="grid gap-5 sm:grid-cols-2">
        {FILTER_GROUPS.map((group, i) => (
          <div key={group.label} role="group" aria-labelledby={`${baseId}-g${i}`} className="space-y-2.5">
            <p id={`${baseId}-g${i}`} className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              {group.label}
            </p>
            <div className="flex flex-wrap gap-2">
              {group.items.map((chip) => (
                <Chip key={chip} active={activeFilters.has(chip)} onClick={() => onToggle(chip)}>
                  {chip}
                </Chip>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Bezel>
  );
}

function XraySearchPanel({
  query,
  setQuery,
}: {
  query: string;
  setQuery: (q: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(query);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <Bezel size="md" coreClassName="space-y-5 p-5 md:p-6">
      <div className="space-y-1.5">
        <PanelTitle title="X-ray search" icon={<Globe size={15} weight="light" />} />
        <p className="pl-[2.625rem] text-xs leading-5 text-muted-foreground">
          Build Boolean search queries to find jobs directly via Google
        </p>
      </div>

      <div role="group" aria-label="Query templates" className="flex flex-wrap gap-2">
        {XRAY_TEMPLATES.map((tpl) => (
          <button
            key={tpl}
            type="button"
            title={tpl}
            aria-pressed={query === tpl}
            onClick={() => setQuery(tpl)}
            className={cn(
              "max-w-full truncate rounded-full px-3 py-1.5 font-geist-mono text-[11px] ring-1",
              "transition-[background-color,color,box-shadow] duration-500 ease-vanguard focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              query === tpl
                ? "bg-primary/10 text-primary ring-primary/25"
                : "bg-card text-muted-foreground ring-foreground/[0.08] hover:text-foreground hover:ring-foreground/20 dark:bg-white/[0.03] dark:ring-white/10",
            )}
          >
            {tpl.length > 52 ? tpl.slice(0, 52) + "…" : tpl}
          </button>
        ))}
      </div>

      <Field label="Boolean query" hint="X-ray searches bypass job board algorithms and find hidden openings">
        {(id) => (
          <Textarea
            id={id}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={3}
            className="min-h-24 resize-none font-geist-mono text-xs"
          />
        )}
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <IslandButton
          size="sm"
          trailing
          onClick={() =>
            window.open(
              `https://www.google.com/search?q=${encodeURIComponent(query)}`,
              "_blank",
            )
          }
        >
          Search on Google
        </IslandButton>
        <IslandButton
          tone="ghost"
          size="sm"
          onClick={handleCopy}
          icon={copied ? <Check size={14} weight="light" /> : <Copy size={14} weight="light" />}
        >
          <span aria-live="polite">{copied ? "Copied" : "Copy query"}</span>
        </IslandButton>
      </div>
    </Bezel>
  );
}

/* -------------------------------------------------------------------------- */
/*  Results: featured match, dense rows, detail panel                         */
/* -------------------------------------------------------------------------- */

function FeaturedJobCard({
  job,
  selected,
  onOpenDetails,
  onPrepareApply,
}: {
  job: SavedJob;
  selected: boolean;
  onOpenDetails: (job: SavedJob) => void;
  onPrepareApply: (job: SavedJob) => void;
}) {
  const p = clampScore(job.match_score);
  const excerpt = job.jd_text?.replace(/\s+/g, " ").trim();

  return (
    <article data-testid="job-card" className="job-card job-row-container group relative min-w-0">
      <Bezel
        tone="primary"
        lifted
        className={cn("transition-[box-shadow] duration-500 ease-vanguard", selected && "ring-primary/40")}
        coreClassName="flex flex-col gap-7 p-6 md:p-8"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Eyebrow tone="primary">
            <Sparkle size={11} weight="light" aria-hidden />
            Top match
          </Eyebrow>
          <StatusPill tone={statusTone(job.status)}>{statusLabel(job.status)}</StatusPill>
          <span className="text-xs text-muted-foreground">{job.source || "Source unavailable"} · {job.posted_at ? new Date(job.posted_at).toLocaleDateString() : "Posted date unknown"}</span>
        </div>

        <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
          <div className="min-w-0 space-y-3">
            <p className="text-sm font-medium text-muted-foreground">{job.company}</p>
            <h3 className="text-balance font-geist text-3xl font-semibold leading-[1.05] tracking-[-0.035em] text-foreground md:text-4xl">
              <button
                type="button"
                onClick={() => onOpenDetails(job)}
                aria-current={selected ? "true" : undefined}
                className={cn(
                  "text-left focus-visible:outline-none",
                  "after:absolute after:inset-0 after:rounded-[calc(2rem-0.375rem)] after:content-['']",
                  "focus-visible:after:ring-2 focus-visible:after:ring-primary/50",
                )}
              >
                {job.role}
              </button>
            </h3>
            <JobMeta job={job} />
          </div>
          <div className="flex items-baseline gap-1 md:flex-col md:items-end md:gap-0">
            <span className={cn("font-geist text-6xl font-semibold leading-none tabular-nums tracking-[-0.05em]", matchText(p))}>
              {p > 0 ? p : "—"}
            </span>
            <span className="text-xs text-muted-foreground">{p > 0 ? "% match" : "No score yet"}</span>
          </div>
        </div>

        <MatchMeter percent={job.match_score} />

        {excerpt ? (
          <p className="line-clamp-3 max-w-[70ch] text-sm leading-6 text-muted-foreground">{excerpt}</p>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-3">
          <JobActions job={job} onPrepareApply={onPrepareApply} size="md" />
          <span aria-hidden className="hidden text-xs text-muted-foreground sm:inline">
            Select to read the full description
          </span>
        </div>
      </Bezel>
    </article>
  );
}

function JobRow({
  job,
  selected,
  onOpenDetails,
  onPrepareApply,
}: {
  job: SavedJob;
  selected: boolean;
  onOpenDetails: (job: SavedJob) => void;
  onPrepareApply: (job: SavedJob) => void;
}) {
  const p = clampScore(job.match_score);
  return (
    <article data-testid="job-card" className="job-card group relative">
      <Bezel
        size="md"
        className={cn("transition-[box-shadow] duration-500 ease-vanguard", selected && "ring-primary/35 dark:ring-primary/40")}
        coreClassName={cn(
          "job-row-grid grid grid-cols-[3.5rem_minmax(0,1fr)] items-center gap-x-3 gap-y-3 p-3 sm:p-4",
          "transition-colors duration-500 ease-vanguard group-hover:bg-muted/40 dark:group-hover:bg-white/[0.04]",
        )}
      >
        <div className="grid h-14 w-14 place-items-center rounded-2xl bg-foreground/[0.03] ring-1 ring-foreground/[0.06] dark:bg-white/[0.04] dark:ring-white/10">
          <div className="text-center leading-none">
            <span className={cn("font-geist text-lg font-semibold tabular-nums tracking-[-0.03em]", matchText(p))}>
              {p > 0 ? p : "—"}
            </span>
            <span className="mt-1 block text-[9px] uppercase tracking-[0.14em] text-muted-foreground">match</span>
          </div>
        </div>

        <div className="min-w-0 space-y-1.5">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-xs font-medium text-muted-foreground">{job.company}</span>
            <StatusPill tone={statusTone(job.status)} className="shrink-0 px-2 py-0.5 text-[10px]">
              {statusLabel(job.status)}
            </StatusPill>
          </div>
          <h3 className="truncate font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground md:text-base">
            <button
              type="button"
              onClick={() => onOpenDetails(job)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "text-left focus-visible:outline-none",
                "after:absolute after:inset-0 after:rounded-[calc(1.5rem-0.25rem)] after:content-['']",
                "focus-visible:after:ring-2 focus-visible:after:ring-primary/50",
              )}
            >
              {job.role}
            </button>
          </h3>
          <JobMeta job={job} />
        </div>

        <JobActions job={job} onPrepareApply={onPrepareApply} className="job-row-action col-span-2" />
      </Bezel>
    </article>
  );
}

function JobDetailBody({ job, onClose, titleId }: { job: SavedJob; onClose?: () => void; titleId?: string }) {
  const router = useRouter();
  const p = clampScore(job.match_score);

  const [tailoring, setTailoring] = useState(false);

  // The row's jd_text is truncated/often empty, so ask the API (falls back to the shared job catalog).
  const handleTailorResume = async () => {
    if (tailoring) return;
    setTailoring(true);
    try {
      const jd = await fetchApplicationJd(job.id);
      if (!jd.jd_text?.trim()) throw Object.assign(new Error("empty"), { response: { status: 404 } });
      setPendingJd({ jdText: jd.jd_text, role: job.role, company: job.company });
      router.push("/resume");
    } catch (err) {
      const notFound = (err as { response?: { status?: number } }).response?.status === 404;
      toast.error(notFound ? "No job description saved for this listing yet." : "Could not load the job description. Try again.");
    } finally {
      setTailoring(false);
    }
  };

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-muted-foreground">{job.company}</p>
          <h3 id={titleId} className="mt-1 text-balance font-geist text-2xl font-semibold leading-tight tracking-[-0.03em] text-foreground">
            {job.role}
          </h3>
        </div>
        {onClose ? (
          <IconButton aria-label="Close" onClick={onClose}>
            <X size={16} weight="light" />
          </IconButton>
        ) : (
          <StatusPill tone={statusTone(job.status)} className="shrink-0">
            {statusLabel(job.status)}
          </StatusPill>
        )}
      </div>

      <JobMeta job={job} className="mt-3" />

      <div className="mt-6 space-y-2.5">
        <div className="flex items-baseline justify-between">
          <span className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Match</span>
          <span className={cn("font-geist text-2xl font-semibold tabular-nums tracking-[-0.03em]", matchText(p))}>
            {job.match_score != null ? `${job.match_score}%` : "—"}
          </span>
        </div>
        <MatchMeter percent={job.match_score} />
      </div>

      <div
        role="region"
        aria-label="Job description"
        tabIndex={0}
        className="mt-6 min-h-0 flex-1 overflow-y-auto rounded-[1.25rem] bg-foreground/[0.025] p-4 ring-1 ring-foreground/[0.05] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:bg-white/[0.03] dark:ring-white/[0.06] md:p-5"
      >
        {job.jd_text ? (
          <p className="whitespace-pre-wrap text-sm leading-6 text-foreground/90">{job.jd_text}</p>
        ) : (
          <p className="text-sm leading-6 text-muted-foreground">
            No description was saved for this listing. Open the original posting to read the full details.
          </p>
        )}
      </div>

      <div className="mt-6 flex flex-wrap gap-2">
        <IslandButton
          className="flex-1"
          onClick={handleTailorResume}
          disabled={tailoring}
          icon={tailoring ? <CircleNotch size={16} weight="light" className="animate-spin" /> : <Lightning size={16} weight="light" />}
        >
          Tailor resume for this job
        </IslandButton>
        {job.job_url && (
          <IslandButton
            tone="ghost"
            icon={<ArrowSquareOut size={16} weight="light" />}
            onClick={() => window.open(job.job_url!, "_blank", "noopener,noreferrer")}
          >
            Open original posting
          </IslandButton>
        )}
      </div>
    </>
  );
}

/** Below lg the detail panel becomes a bottom sheet / centered dialog. */
function JobDetailModal({ job, onClose }: { job: SavedJob; onClose: () => void }) {
  const titleId = useId();
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-foreground/20 backdrop-blur-sm dark:bg-black/60" />
        <DialogPrimitive.Content aria-labelledby={titleId} aria-describedby={undefined} className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-2xl -translate-x-1/2 -translate-y-1/2 outline-none">
          <DialogPrimitive.Title className="sr-only">Job details</DialogPrimitive.Title>
          <Bezel lifted coreClassName="flex max-h-[85dvh] flex-col p-5 sm:p-6"><JobDetailBody job={job} onClose={onClose} titleId={titleId} /></Bezel>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** Compact read-out of the saved job preferences, shown beside the search. */
function PreferencesSummary({ form, onEdit }: { form: SearchProfileForm; onEdit: () => void }) {
  const roles = splitCsv(form.target_roles);
  const places = splitCsv(form.preferred_locations);
  const money = (v: string) => (v ? `$${Math.round(Number(v) / 1000)}k` : "");
  const salary = form.salary_min || form.salary_max ? [money(form.salary_min), money(form.salary_max)].filter(Boolean).join(" – ") : "";
  const rows: { label: string; value: string }[] = [
    { label: "Roles", value: roles.join(", ") },
    { label: "Locations", value: places.join(", ") },
    { label: "Experience", value: [labelize(form.experience_level), form.years_experience ? `${form.years_experience} yrs` : ""].filter(Boolean).join(" · ") },
    { label: "Work", value: [splitCsv(form.work_mode).map(labelize).join(", "), splitCsv(form.job_type).map(labelize).join(", ")].filter(Boolean).join(" · ") },
    { label: "Salary", value: salary },
  ];
  return (
    <Bezel lifted coreClassName="flex h-full flex-col p-5 md:p-6">
      <PanelTitle
        title="Job preferences"
        icon={<SlidersHorizontal size={15} weight="light" />}
        meta={
          <IslandButton tone="ghost" size="sm" onClick={onEdit}>
            Edit
          </IslandButton>
        }
      />
      <dl className="mt-4 space-y-3">
        {rows.map((row) => (
          <div key={row.label} className="grid grid-cols-[6rem_minmax(0,1fr)] items-baseline gap-3">
            <dt className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">{row.label}</dt>
            <dd className={cn("truncate text-sm", row.value ? "text-foreground" : "text-muted-foreground/70")} title={row.value || undefined}>
              {row.value || "Not set"}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-auto pt-4 text-xs leading-5 text-muted-foreground">Blank search uses these with your resume.</p>
    </Bezel>
  );
}

function ResultsSkeleton() {
  return (
    <div aria-hidden className="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <div className="space-y-4 lg:col-span-7">
        <Bezel tone="primary" coreClassName="space-y-6 p-6 md:p-8">
          <Skeleton className="h-6 w-28 rounded-full" />
          <Skeleton className="h-10 w-3/4 rounded-xl" />
          <Skeleton className="h-4 w-1/2 rounded-lg" />
          <Skeleton className="h-1.5 w-full rounded-full" />
          <div className="flex gap-2">
            <Skeleton className="h-11 w-24 rounded-full" />
            <Skeleton className="h-11 w-28 rounded-full" />
          </div>
        </Bezel>
        {Array.from({ length: 3 }).map((_, i) => (
          <Bezel key={i} size="md" coreClassName="flex items-center gap-4 p-4 md:p-5">
            <Skeleton className="h-14 w-14 shrink-0 rounded-2xl" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3 w-24 rounded-md" />
              <Skeleton className="h-4 w-2/3 rounded-md" />
            </div>
            <Skeleton className="hidden h-9 w-40 rounded-full md:block" />
          </Bezel>
        ))}
      </div>
      <div className="hidden lg:col-span-5 lg:block">
        <Bezel coreClassName="space-y-5 p-6">
          <Skeleton className="h-4 w-24 rounded-md" />
          <Skeleton className="h-8 w-3/4 rounded-xl" />
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-11 w-full rounded-full" />
        </Bezel>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Search profile                                                            */
/* -------------------------------------------------------------------------- */

function ProfileSearchPanel({
  profile,
  form,
  saving,
  onChange,
  onSave,
  onRun,
}: {
  profile: JobSearchProfile | undefined;
  form: SearchProfileForm;
  saving: boolean;
  onChange: (key: keyof SearchProfileForm, value: string) => void;
  onSave: () => void;
  onRun: () => void;
}) {
  const suggestions = profile?.role_suggestions ?? [];
  const skills = profile?.skills ?? [];
  const missing = profile?.missing_fields ?? [];
  const groupId = useId();

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
      <Bezel className="lg:col-span-7" coreClassName="p-6 md:p-8">
        <PanelTitle
          title="Resume-first search profile"
          icon={<Brain size={15} weight="light" />}
          meta={
            <StatusPill tone={profile?.resume_found ? "success" : "warning"}>
              <span className="max-w-[14rem] truncate">
                {profile?.resume_found ? profile.resume_filename ?? "Resume found" : "No resume yet"}
              </span>
            </StatusPill>
          }
        />
        <p className="mt-2 max-w-[60ch] pl-[2.625rem] text-xs leading-5 text-muted-foreground">
          Agents analyze resume, then you confirm fresher/years, role, location, and work mode.
        </p>

        <div className="mt-7 grid gap-5 md:grid-cols-2">
          <Field label="Target roles">
            {(id) => (
              <Input
                id={id}
                value={form.target_roles}
                onChange={(e) => onChange("target_roles", e.target.value)}
                placeholder="Frontend Engineer, React Developer"
              />
            )}
          </Field>
          <Field label="Preferred locations">
            {(id) => (
              <div className={inputTrayClass}>
                <LocationInput
                  id={id}
                  multiple
                  value={form.preferred_locations}
                  onChange={(v) => onChange("preferred_locations", v)}
                  placeholder="Remote, Bengaluru, Hyderabad"
                  inputClassName={cn(inputControlClass, "h-11")}
                />
              </div>
            )}
          </Field>
          <Field label="Are you fresher or experienced?">
            {(id) => (
              <Select id={id} value={form.experience_level} onChange={(e) => onChange("experience_level", e.target.value)}>
                {EXPERIENCE_LEVELS.map((level) => (
                  <option key={level} value={level}>
                    {labelize(level)}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label="Exact years">
            {(id) => (
              <Input
                id={id}
                type="number"
                min={0}
                max={60}
                value={form.years_experience}
                onChange={(e) => onChange("years_experience", e.target.value)}
                placeholder="0 for fresher"
                className="tabular-nums"
              />
            )}
          </Field>
          <Field label="Salary minimum (USD / year)">
            {(id) => (
              <Input
                id={id}
                type="number"
                inputMode="numeric"
                min={0}
                leading={<span className="text-sm">$</span>}
                value={form.salary_min}
                onChange={(e) => onChange("salary_min", e.target.value)}
                placeholder="80000"
                className="tabular-nums"
              />
            )}
          </Field>
          <Field label="Salary maximum (USD / year)">
            {(id) => (
              <Input
                id={id}
                type="number"
                inputMode="numeric"
                min={0}
                leading={<span className="text-sm">$</span>}
                value={form.salary_max}
                onChange={(e) => onChange("salary_max", e.target.value)}
                placeholder="150000"
                className="tabular-nums"
              />
            )}
          </Field>
          <div role="group" aria-labelledby={`${groupId}-type`} className="space-y-2.5">
            <p id={`${groupId}-type`} className="pl-1 text-[12px] font-medium tracking-[-0.005em] text-muted-foreground">
              Job type
            </p>
            <div className="flex flex-wrap gap-2">
              {JOB_TYPES.map((type) => (
                <Chip
                  key={type}
                  active={splitCsv(form.job_type).includes(type)}
                  onClick={() => onChange("job_type", toggleCsvValue(form.job_type, type))}
                >
                  {labelize(type)}
                </Chip>
              ))}
            </div>
          </div>
          <div role="group" aria-labelledby={`${groupId}-mode`} className="space-y-2.5">
            <p id={`${groupId}-mode`} className="pl-1 text-[12px] font-medium tracking-[-0.005em] text-muted-foreground">
              Work mode
            </p>
            <div className="flex flex-wrap gap-2">
              {WORK_MODES.map((mode) => (
                <Chip
                  key={mode}
                  active={splitCsv(form.work_mode).includes(mode)}
                  onClick={() => onChange("work_mode", toggleCsvValue(form.work_mode, mode))}
                >
                  {labelize(mode)}
                </Chip>
              ))}
            </div>
          </div>
        </div>

        {suggestions.length > 0 && (
          <div className="mt-6 space-y-2.5">
            <p className="pl-1 text-[12px] font-medium text-muted-foreground">Resume role suggestions</p>
            <div className="flex flex-wrap gap-2">
              {suggestions.map((role) => (
                <button
                  key={role}
                  type="button"
                  onClick={() => onChange("target_roles", role)}
                  className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-3.5 py-1.5 text-xs font-medium text-primary ring-1 ring-primary/20 transition-[background-color,transform] duration-500 ease-vanguard hover:bg-primary/15 active:scale-[0.97] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Sparkle size={12} weight="light" aria-hidden />
                  {role}
                </button>
              ))}
            </div>
          </div>
        )}

        <Hairline className="my-7" />

        <div className="flex flex-wrap gap-2">
          <IslandButton onClick={onRun} icon={<MagnifyingGlass size={16} weight="light" />} trailing>
            Search with this profile
          </IslandButton>
          <IslandButton
            tone="ghost"
            onClick={onSave}
            disabled={saving}
            icon={
              saving ? (
                <CircleNotch size={16} weight="light" className="animate-spin motion-reduce:animate-none" />
              ) : (
                <FloppyDisk size={16} weight="light" />
              )
            }
          >
            Save preferences
          </IslandButton>
        </div>
      </Bezel>

      <Bezel tone="muted" className="lg:col-span-5" coreClassName="flex flex-col gap-6 p-6 md:p-8">
        <PanelTitle title="Search plan" icon={<Briefcase size={15} weight="light" />} />

        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Query preview</p>
          <p className="mt-2 text-balance font-geist text-2xl font-semibold leading-tight tracking-[-0.03em] text-foreground">
            {profile?.search_query_preview || "Set target role"}
          </p>
        </div>

        <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-[1.25rem] bg-foreground/[0.06] ring-1 ring-foreground/[0.06] dark:bg-white/[0.06] dark:ring-white/10">
          <div className="bg-card px-4 py-4">
            <dt className="text-[11px] text-muted-foreground">Location</dt>
            <dd className="mt-1 truncate text-sm font-medium text-foreground">
              {profile?.location_preview || form.preferred_locations || "Any"}
            </dd>
          </div>
          <div className="bg-card px-4 py-4">
            <dt className="text-[11px] text-muted-foreground">Experience</dt>
            <dd className="mt-1 truncate text-sm font-medium tabular-nums text-foreground">
              {form.years_experience ? `${form.years_experience} yr` : "Confirm"} · {labelize(form.experience_level)}
            </dd>
          </div>
        </dl>

        {skills.length > 0 && (
          <div>
            <p className="mb-2.5 text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Skills found in resume</p>
            <ul className="flex flex-wrap gap-1.5">
              {skills.map((skill) => (
                <li
                  key={skill}
                  className="rounded-full bg-card px-2.5 py-1 text-xs text-foreground/85 ring-1 ring-foreground/[0.07] dark:bg-white/[0.04] dark:ring-white/10"
                >
                  {skill}
                </li>
              ))}
            </ul>
          </div>
        )}

        {missing.length > 0 && (
          <Notice tone="warning" icon={<WarningCircle size={16} weight="light" />}>
            <p className="text-xs font-semibold">Needs confirmation</p>
            <p className="text-xs">{missing.join(", ")}</p>
          </Notice>
        )}

        {(profile?.analysis_notes ?? []).length > 0 && (
          <ul className="space-y-1.5">
            {(profile?.analysis_notes ?? []).map((note) => (
              <li key={note} className="text-xs leading-5 text-muted-foreground">
                {note}
              </li>
            ))}
          </ul>
        )}
      </Bezel>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */

const TOP_JOBS = 10;
const BIG_LIST = 50;
const MIN_OPTIONS = [50, 60, 70, 80, 90];
const LIST_CHOICE_KEY = "jobs.listChoice";
const QUERY_LOCATION_FILTERS = ["Remote", "Hybrid", "Onsite", "Bangalore", "Hyderabad", "Mumbai"];
type ListChoice = { choice: "filter"; min: number } | { choice: "all" };

export default function JobsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const reduce = useReducedMotion();
  const itemVariants = reduce ? FADE_ONLY : listItem;
  const swapVariants = reduce ? FADE_ONLY : panelSwap;
  const [detailJob, setDetailJob] = useState<SavedJob | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeFilters, setActiveFilters] = useState<Set<string>>(
    new Set(["Full-time"]),
  );
  const [showFilters, setShowFilters] = useState(false);
  // Job preferences fold away; links to /jobs#search-profile open them.
  const [showProfile, setShowProfile] = useState(false);
  useEffect(() => {
    if (window.location.hash === "#search-profile") setShowProfile(true);
  }, []);
  const openPreferences = () => {
    setShowProfile(true);
    requestAnimationFrame(() => document.getElementById("search-profile")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [xrayQuery, setXrayQuery] = useState(XRAY_TEMPLATES[0]);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchBasis, setSearchBasis] = useState("");
  const [jobSource, setJobSource] = useState("");
  const [postedDays, setPostedDays] = useState(30);
  const [searchLocation, setSearchLocation] = useState("");
  const [agentRunning, setAgentRunning] = useState(false);
  const [liveBrowser, setLiveBrowser] = useState(true);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [profileForm, setProfileForm] = useState<SearchProfileForm>({
    target_roles: "",
    preferred_locations: "",
    experience_level: "fresher",
    years_experience: "0",
    job_type: "full-time",
    work_mode: "remote",
    current_title: "",
    salary_min: "",
    salary_max: "",
  });
  const [profileInitialized, setProfileInitialized] = useState(false);
  const initRun = useAgentStore((s) => s.initRun);
  const addEvent = useAgentStore((s) => s.addEvent);
  const setCheckpoint = useAgentStore((s) => s.setCheckpoint);
  const setRunStatus = useAgentStore((s) => s.setRunStatus);
  const storeActiveRunId = useAgentStore((s) => s.activeRunId);
  const setActiveRun = useAgentStore((s) => s.setActiveRun);
  // Persisted run survives navigation + reload, so the live view reappears on return.
  const displayRunId = activeRunId ?? storeActiveRunId;
  const activeRunStatus = useAgentStore((s) =>
    activeRunId ? s.runs[activeRunId]?.status : undefined,
  );
  const outcomePollMs = usePollMs(5000, 20_000);
  const { data: searchOutcome } = useQuery<{
    status: string;
    output?: { total_found?: number; saved_count?: number; warnings?: string[]; error?: string;
      matches?: { job_id: string; title: string; company: string; url: string; match_score: number }[] };
  }>({
    queryKey: ["job-search-outcome", activeRunId],
    enabled: !!activeRunId,
    queryFn: async () => (await apiClient.get(`/agents/runs/${activeRunId}`)).data,
    refetchInterval: query => ["completed", "failed", "cancelled", "expired"].includes(query.state.data?.status ?? "") ? false : outcomePollMs,
  });
  useEffect(() => {
    if (!activeRunId || !searchOutcome) return;
    if (searchOutcome.status === "completed" || searchOutcome.status === "failed") {
      setAgentRunning(false);
      setRunStatus(activeRunId, searchOutcome.status);
      void qc.invalidateQueries({ queryKey: ["jobs-saved"] });
    }
  }, [searchOutcome, activeRunId, setRunStatus, qc]);

  // Listing choice for large result sets, remembered per browser. undefined = not read yet.
  const [listChoice, setListChoice] = useState<ListChoice | null | undefined>(undefined);
  const [draftMin, setDraftMin] = useState(70);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(LIST_CHOICE_KEY);
      const c = raw ? (JSON.parse(raw) as ListChoice) : null;
      setListChoice(c && (c.choice === "all" || (c.choice === "filter" && MIN_OPTIONS.includes(c.min))) ? c : null);
    } catch {
      setListChoice(null);
    }
  }, []);
  const chooseList = (c: ListChoice | null) => {
    setListChoice(c);
    try {
      if (c) localStorage.setItem(LIST_CHOICE_KEY, JSON.stringify(c));
      else localStorage.removeItem(LIST_CHOICE_KEY);
    } catch {
      /* storage blocked: choice lasts for this visit only */
    }
  };
  const minMatch = listChoice?.choice === "filter" ? listChoice.min : 0;

  const effectivePostedDays = activeFilters.has("Today") ? 1 : activeFilters.has("Past week") ? 7 : activeFilters.has("Past month") ? 30 : postedDays;
  const selectedLocations = Array.from(activeFilters).filter((f) => QUERY_LOCATION_FILTERS.includes(f));

  const { data: jobsPage, isLoading } = useQuery<ApplicationPage>({
    queryKey: ["jobs-saved", Array.from(activeFilters).sort().join(","), jobSource, postedDays, minMatch],
    queryFn: () => {
      const filters: ApplicationFilters = { status: "saved", sort: "match_desc" };
      if (jobSource) filters.source = jobSource;
      if (minMatch > 0) filters.minMatch = minMatch;
      filters.postedWithinDays = effectivePostedDays;
      const locations = Array.from(activeFilters).filter((f) => QUERY_LOCATION_FILTERS.includes(f));
      if (locations.length > 0) filters.location = locations.join(",");
      return fetchApplications(filters, { limit: TOP_JOBS });
    },
  });
  const jobs: SavedJob[] = jobsPage?.items ?? [];
  const totalJobs = jobsPage?.total ?? 0;

  const viewAllParams = new URLSearchParams({ sort: "match_desc", status: "saved", posted: String(effectivePostedDays) });
  if (minMatch > 0) viewAllParams.set("min", String(minMatch));
  if (selectedLocations.length) viewAllParams.set("city", selectedLocations.join(","));
  if (jobSource) viewAllParams.set("source", jobSource);
  const viewAllHref = `/applications?${viewAllParams.toString()}`;
  const showSizePrompt = listChoice === null && totalJobs > BIG_LIST;

  const { data: prefs } = useQuery({
    queryKey: ["preferences"],
    queryFn: async () => {
      const { data } = await apiClient.get("/users/me/preferences");
      return data as {
        target_roles?: string[];
        preferred_locations?: string[];
        work_mode?: string;
        job_type?: string;
        experience_level?: string;
        years_experience?: number | null;
        current_title?: string;
      } | null;
    },
  });

  const { data: searchProfile } = useQuery<JobSearchProfile>({
    queryKey: ["job-search-profile"],
    queryFn: async () => {
      const { data } = await apiClient.get("/users/me/job-search-profile");
      return data;
    },
  });

  useEffect(() => {
    if (!searchProfile || profileInitialized) return;
    const saved = searchProfile.saved_preferences ?? {};
    const inferredYears = saved.years_experience ?? searchProfile.inferred_years_experience;
    setProfileForm({
      target_roles: (saved.target_roles?.length ? saved.target_roles : searchProfile.role_suggestions).join(", "),
      preferred_locations: (saved.preferred_locations ?? []).join(", "),
      experience_level: saved.experience_level ?? searchProfile.inferred_experience_level ?? "fresher",
      years_experience: inferredYears != null ? String(inferredYears) : "",
      job_type: saved.job_type ?? "full-time",
      work_mode: saved.work_mode ?? searchProfile.work_mode_preview ?? "remote",
      current_title: saved.current_title ?? "",
      salary_min: saved.salary_min != null ? String(saved.salary_min) : "",
      salary_max: saved.salary_max != null ? String(saved.salary_max) : "",
    });
    setProfileInitialized(true);
  }, [searchProfile, profileInitialized]);

  // Prefill search form and filters from saved preferences on first load
  useEffect(() => {
    if (!prefs) return;
    setActiveFilters((prev) => {
      const next = new Set(prev);
      splitCsv(prefs.work_mode).forEach((mode) => {
        const chip = modeValueToChip(mode);
        if (MODE_FILTERS.includes(chip)) next.add(chip);
      });
      splitCsv(prefs.job_type).forEach((type) => {
        const chip = jobTypeValueToChip(type);
        if (JOB_TYPE_FILTERS.includes(chip)) next.add(chip);
      });
      if (
        (prefs.experience_level === "fresher" || prefs.experience_level === "junior") &&
        !next.has("Entry-level")
      ) {
        next.add("Entry-level");
      }
      return next;
    });
  }, [prefs]);

  const searchMutation = useMutation({
    mutationFn: (payload: {
      resume_id?: string;
      persona_id?: string;
      platforms?: string[];
      posted_within_days?: number;
      search_query: string;
      location: string;
      max_results: number;
      live_browser: boolean;
      work_mode: string;
      experience_level?: string;
      years_experience?: number;
      job_type?: string;
      target_roles?: string[];
      preferred_locations?: string[];
    }) =>
      apiClient.post("/jobs/search", payload),
    onSuccess: (res) => {
      setAgentRunning(true);
      const runId = res.data?.run_id as string | undefined;
      if (runId) {
        initRun(runId);
        setActiveRunId(runId);
      }
      toast.success("Job Agent started — scanning boards for matching roles");
    },
    onError: (error) => {
      toast.error(getApiErrorMessage(error, "Job Agent unavailable — backend not connected"));
    },
  });

  const saveProfileMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        current_title: profileForm.current_title || undefined,
        experience_level: profileForm.experience_level || undefined,
        years_experience: profileForm.years_experience ? parseInt(profileForm.years_experience, 10) : undefined,
        job_type: profileForm.job_type || undefined,
        work_mode: profileForm.work_mode || undefined,
        target_roles: splitCsv(profileForm.target_roles),
        preferred_locations: splitCsv(profileForm.preferred_locations),
        // null clears a stored value; omitted would keep the old one.
        salary_min: profileForm.salary_min !== "" ? parseInt(profileForm.salary_min, 10) : null,
        salary_max: profileForm.salary_max !== "" ? parseInt(profileForm.salary_max, 10) : null,
      };
      const { data } = await apiClient.patch("/users/me/preferences", payload);
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["preferences"] });
      qc.invalidateQueries({ queryKey: ["job-search-profile"] });
      toast.success("Search preferences saved");
    },
    onError: () => toast.error("Could not save search preferences"),
  });

  useEffect(() => {
    if (!activeRunId || !activeRunStatus) return;
    if (activeRunStatus === "completed" || activeRunStatus === "failed") {
      setAgentRunning(false);
      qc.invalidateQueries({ queryKey: ["jobs-saved"] });
    }
  }, [activeRunId, activeRunStatus, qc]);

  useEffect(() => {
    if (!activeRunId || activeRunStatus !== "running") return;
    let stopped = false;
    const interval = window.setInterval(async () => {
      try {
        const { data } = await apiClient.get("/agents/runs?limit=20");
        const run = ((Array.isArray(data) ? data : data.runs ?? []) as Array<{
          id: string;
          status: "queued" | "completed" | "failed" | "running" | "awaiting_approval" | "cancelled" | "expired";
          output?: Record<string, unknown> | null;
        }>)
          .find((item) => item.id === activeRunId);
        if (
          !stopped &&
          run &&
          (run.status === "completed" ||
            run.status === "failed" ||
            run.status === "cancelled" ||
            run.status === "expired" ||
            run.status === "awaiting_approval")
        ) {
          if (run.status === "awaiting_approval") {
            addEvent(activeRunId, "checkpoint", run.output ?? {});
            setCheckpoint(activeRunId, (run.output ?? {}) as Record<string, unknown>);
          } else {
            setRunStatus(activeRunId, run.status);
          }
        }
      } catch {
        // SSE remains primary; polling only protects against missed terminal events.
      }
    }, 5000);
    return () => {
      stopped = true;
      window.clearInterval(interval);
    };
  }, [activeRunId, activeRunStatus, addEvent, setCheckpoint, setRunStatus]);

  const prepareApplyMutation = useMutation({
    mutationFn: (job: SavedJob) =>
      apiClient.post(`/jobs/applications/${job.id}/prepare-apply`, { live_browser: liveBrowser }),
    onSuccess: (res) => {
      const runId = res.data?.run_id as string | undefined;
      if (runId) {
        initRun(runId);
        setActiveRunId(runId);
      }
      if (res.data?.mode === "extension") {
        // Nudge the extension so the job opens now instead of on its next check.
        wakeExtension();
        toast.success("Opening the job in your browser. Review the form there, then approve the final Submit from the CareerCraft toolbar icon.");
      } else {
        toast.success("Live apply prep started — review before submitting");
      }
    },
    onError: (error) => {
      const status = (error as { response?: { status?: number } })?.response?.status;
      toast.error(getApiErrorMessage(error, "Could not start the application"), {
        action: status === 409 ? { label: "Settings", onClick: () => router.push("/settings/integrations") } : undefined,
      });
    },
  });

  const avgMatch =
    jobs.length > 0
      ? Math.round(jobs.reduce((s, j) => s + (j.match_score ?? 0), 0) / jobs.length)
      : 0;

  const oneDayAgo = Date.now() - 86400000;
  const newToday = jobs.filter(
    (j) => j.applied_at && new Date(j.applied_at).getTime() > oneDayAgo,
  ).length;

  // Asymmetric results: the highest-scoring role is featured, the rest stay in API order.
  const featuredJob =
    jobs.length > 0
      ? jobs.reduce((best, j) => ((j.match_score ?? -1) > (best.match_score ?? -1) ? j : best), jobs[0])
      : null;
  const otherJobs = featuredJob ? jobs.filter((j) => j.id !== featuredJob.id) : [];
  const selectedJob = jobs.find((j) => j.id === selectedId) ?? featuredJob;

  const searchBusy = searchMutation.isPending || agentRunning;

  function toggleFilter(chip: string) {
    setActiveFilters((prev) => {
      const next = new Set(prev);
      if (next.has(chip)) {
        next.delete(chip);
      } else {
        if (LOCATION_FILTERS.includes(chip)) {
          next.delete("Remote");
        }
        next.add(chip);
      }
      return next;
    });
  }

  function updateProfileForm(key: keyof SearchProfileForm, value: string) {
    setProfileForm((prev) => ({
      ...prev,
      [key]: value,
      ...(key === "experience_level" && value === "fresher" && !prev.years_experience
        ? { years_experience: "0" }
        : {}),
    }));
  }

  /** Desktop: show in the side panel. Below lg: open the detail sheet. */
  function openJobDetails(job: SavedJob) {
    if (window.matchMedia("(min-width: 1024px)").matches) {
      setSelectedId(job.id);
    } else {
      setDetailJob(job);
    }
  }

  function handleRunAgent() {
    const query = searchQuery.trim();
    const typedLocation = searchLocation.trim();
    const manualRoles = splitCsv(profileForm.target_roles);
    const manualLocations = splitCsv(profileForm.preferred_locations);
    const selectedLocation = Array.from(activeFilters).find((f) =>
      LOCATION_FILTERS.includes(f)
    );
    const selectedModes = Array.from(activeFilters)
      .filter((f) => MODE_FILTERS.includes(f))
      .map(titleToValue);
    const workMode = selectedModes.length > 0
      ? selectedModes.join(",")
      : profileForm.work_mode || (selectedLocation ? "" : prefs?.work_mode ?? "");
    const locationFromProfile = manualLocations[0] ?? "";
    const primaryWorkMode = primaryCsvValue(workMode);
    searchMutation.mutate({
      resume_id: searchBasis.startsWith("resume:") ? searchBasis.split(":")[1] : undefined,
      persona_id: searchBasis.startsWith("persona:") ? searchBasis.split(":")[1] : undefined,
      platforms: jobSource ? [jobSource] : [],
      posted_within_days: effectivePostedDays,
      search_query: query,
      // An explicitly typed location wins; otherwise fall back to filters/profile as before.
      location:
        typedLocation ||
        (primaryWorkMode === "remote" ? "Remote" : (selectedLocation ?? locationFromProfile) || "Any"),
      max_results: 10,
      live_browser: liveBrowser,
      work_mode: workMode,
      experience_level: profileForm.experience_level,
      years_experience: profileForm.years_experience ? parseInt(profileForm.years_experience, 10) : undefined,
      job_type: profileForm.job_type,
      target_roles: manualRoles,
      preferred_locations: manualLocations,
    });
  }

  const prepareApply = (selected: SavedJob) => prepareApplyMutation.mutate(selected);

  return (
    <>
      <Screen>
        <PageHero
          eyebrow="Job search agent"
          title="Find your next role."
          description="Your resume is read first. Confirm experience, roles, location and work mode, then let the agent scan the boards."
          className="lg:items-start"
          actions={
            <div className="w-full space-y-5">
              <SearchIsland
                query={searchQuery}
                onQueryChange={setSearchQuery}
                location={searchLocation}
                onLocationChange={setSearchLocation}
                onSubmit={handleRunAgent}
                busy={searchBusy}
                running={agentRunning}
              />
              <JobSearchBasis value={searchBasis} onChange={setSearchBasis} source={jobSource} onSource={setJobSource} days={postedDays} onDays={setPostedDays} />

              <div className="flex flex-wrap items-center gap-2">
                <DisclosurePill
                  open={showFilters}
                  onToggle={() => setShowFilters((v) => !v)}
                  controls="jobs-filter-panel"
                  icon={<SlidersHorizontal size={14} weight="light" />}
                  count={activeFilters.size}
                >
                  Filters
                </DisclosurePill>
                <DisclosurePill
                  open={showAdvanced}
                  onToggle={() => setShowAdvanced((v) => !v)}
                  controls="jobs-xray-panel"
                  icon={<Globe size={14} weight="light" />}
                >
                  Advanced search
                </DisclosurePill>
                {Array.from(activeFilters).map((chip) => (
                  <button
                    key={chip}
                    type="button"
                    onClick={() => toggleFilter(chip)}
                    aria-label={`Remove filter ${chip}`}
                    className="inline-flex h-8 items-center gap-1.5 rounded-full bg-primary/10 px-3 text-xs font-medium text-primary ring-1 ring-primary/20 transition-colors duration-500 ease-vanguard hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {chip}
                    <X size={12} weight="light" aria-hidden />
                  </button>
                ))}
                <label className="ml-auto inline-flex cursor-pointer items-center gap-2.5 text-[13px] font-medium text-foreground/85">
                  <Toggle checked={liveBrowser} onChange={setLiveBrowser} label="Watch browser" />
                  Watch browser
                </label>
              </div>

              <AnimatePresence initial={false}>
                {showFilters && (
                  <motion.div key="filters" id="jobs-filter-panel" variants={swapVariants} initial="hidden" animate="show" exit="exit">
                    <FilterPanel
                      activeFilters={activeFilters}
                      onToggle={toggleFilter}
                      onClear={() => setActiveFilters(new Set())}
                    />
                  </motion.div>
                )}
              </AnimatePresence>

              <AnimatePresence initial={false}>
                {showAdvanced && (
                  <motion.div key="xray-panel" id="jobs-xray-panel" variants={swapVariants} initial="hidden" animate="show" exit="exit">
                    <XraySearchPanel query={xrayQuery} setQuery={setXrayQuery} />
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          }
          aside={<PreferencesSummary form={profileForm} onEdit={openPreferences} />}
        />

        <Section aria-label="Matches">
          {displayRunId && (
            <motion.div variants={swapVariants} initial="hidden" animate="show">
              <Bezel lifted coreClassName="space-y-5 p-5 md:p-7">
                <PanelTitle
                  title="Live agent run"
                  icon={<Broadcast size={15} weight="light" />}
                  meta={liveBrowser ? "Browser view on" : "Log only"}
                />
                <AgentStatusStream
                  runId={displayRunId}
                  onApprove={() => {
                    qc.invalidateQueries({ queryKey: ["jobs-saved"] });
                    setActiveRunId(null);
                    setActiveRun(null);
                  }}
                  onCancel={() => {
                    setActiveRunId(null);
                    setActiveRun(null);
                  }}
                />
              </Bezel>
            </motion.div>
          )}

          {searchOutcome?.status === "completed" && <div className="rounded-2xl border border-border p-4" aria-label="Latest search results">
            <p role="status" className="text-sm font-medium">Found {searchOutcome.output?.total_found ?? 0} listings; saved {searchOutcome.output?.saved_count ?? 0} matches scoring at least 50%.</p>
            {searchOutcome.output?.warnings?.map((warning, i) => <p key={i} className="mt-2 text-xs text-muted-foreground">{warning}</p>)}
            {!searchOutcome.output?.matches?.length && <p className="mt-2 text-sm">No listings found. Try a broader role, location, or another source.</p>}
            <ul className="mt-3 space-y-2">{searchOutcome.output?.matches?.slice(0, 10).map(job => <li key={job.job_id} className="flex items-center justify-between gap-3 text-sm">
              <a className="underline underline-offset-4" href={/^https?:\/\//i.test(job.url) ? job.url : undefined} target="_blank" rel="noopener noreferrer">{job.title} · {job.company}</a><span className="shrink-0 text-xs text-muted-foreground">{job.match_score}% match</span>
            </li>)}</ul>
          </div>}
          {searchOutcome?.status === "failed" && <p role="alert" className="rounded-xl bg-destructive/10 p-3 text-sm text-destructive">{searchOutcome.output?.error || "Job search failed. Try again."}</p>}
          <SectionHeading
            eyebrow="Saved matches"
            title="Ranked against your resume"
            description="Select a role to read the full description, tailor your resume, or start a supervised application."
            actions={
              <div className="flex flex-wrap items-center gap-2">
                {agentRunning && (
                  <StatusPill tone="primary" live>
                    Agent searching…
                  </StatusPill>
                )}
                {jobs.length > 0 && (
                  <>
                    <StatusPill tone="primary" className="tabular-nums">
                      {totalJobs} {totalJobs === 1 ? "role" : "roles"}
                    </StatusPill>
                    {avgMatch > 0 && <StatusPill tone="neutral" className="tabular-nums">Avg match {avgMatch}%</StatusPill>}
                    {newToday > 0 && <StatusPill tone="success" className="tabular-nums">{newToday} new today</StatusPill>}
                  </>
                )}
              </div>
            }
          />

          {showSizePrompt && (
            <div className="flex flex-wrap items-center gap-3 rounded-[1.25rem] bg-foreground/[0.025] p-4 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.06]">
              <label htmlFor="jobs-min-match" className="text-sm text-foreground">
                {totalJobs} saved roles. Show only matches ≥
              </label>
              <Select id="jobs-min-match" className="h-9 w-24" trayClassName="w-24" value={draftMin} onChange={(e) => setDraftMin(Number(e.target.value))}>
                {MIN_OPTIONS.map((m) => (
                  <option key={m} value={m}>
                    {m}%
                  </option>
                ))}
              </Select>
              <span className="text-sm text-foreground">?</span>
              <IslandButton size="sm" onClick={() => chooseList({ choice: "filter", min: draftMin })}>
                Filter
              </IslandButton>
              <IslandButton size="sm" tone="ghost" onClick={() => chooseList({ choice: "all" })}>
                Show all
              </IslandButton>
            </div>
          )}

          {listChoice?.choice === "filter" && (
            <p className="text-sm text-muted-foreground">
              Showing matches ≥ {listChoice.min}%.{" "}
              <button type="button" className="underline underline-offset-2 hover:text-foreground" onClick={() => chooseList({ choice: "all" })}>
                Show all
              </button>
            </p>
          )}

          {isLoading ? (
            <ResultsSkeleton />
          ) : jobs.length === 0 || !featuredJob ? (
            <Reveal>
              <Bezel coreClassName="p-2">
                <EmptyPanel
                  icon={<Briefcase size={24} weight="light" />}
                  title="No jobs found yet."
                  description="Search above, or let the agent use your resume and saved preferences to find matching roles."
                  action={
                    <IslandButton onClick={handleRunAgent} disabled={searchBusy} icon={<Lightning size={16} weight="light" />} trailing>
                      Search with my resume
                    </IslandButton>
                  }
                />
              </Bezel>
            </Reveal>
          ) : (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-12 lg:items-start">
              <div className="min-w-0 space-y-6 lg:col-span-7">
                <motion.div key={featuredJob.id} variants={itemVariants} initial="hidden" animate="show">
                  <FeaturedJobCard
                    job={featuredJob}
                    selected={selectedJob?.id === featuredJob.id}
                    onOpenDetails={openJobDetails}
                    onPrepareApply={prepareApply}
                  />
                </motion.div>

                {otherJobs.length > 0 && (
                  <div className="space-y-3">
                    <p className="pl-1 text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                      More matches · <span className="tabular-nums">{otherJobs.length}</span>
                    </p>
                    <motion.ul variants={listStagger} initial="hidden" animate="show" className="space-y-3">
                      {otherJobs.map((job) => (
                        <motion.li key={job.id} variants={itemVariants}>
                          <JobRow
                            job={job}
                            selected={selectedJob?.id === job.id}
                            onOpenDetails={openJobDetails}
                            onPrepareApply={prepareApply}
                          />
                        </motion.li>
                      ))}
                    </motion.ul>
                  </div>
                )}
              </div>

              <aside aria-label="Selected job" className="hidden min-w-0 lg:sticky lg:top-24 lg:col-span-5 lg:block">
                <AnimatePresence mode="wait" initial={false}>
                  {selectedJob && (
                    <motion.div key={selectedJob.id} variants={swapVariants} initial="hidden" animate="show" exit="exit">
                      <Bezel lifted coreClassName="flex max-h-[calc(100dvh-8rem)] flex-col p-6 md:p-7">
                        <JobDetailBody job={selectedJob} />
                      </Bezel>
                    </motion.div>
                  )}
                </AnimatePresence>
              </aside>
            </div>
          )}

          {!isLoading && totalJobs > 0 && (
            <div className="flex justify-end">
              <IslandLink href={viewAllHref} tone="ghost" icon={<ArrowRight size={16} weight="light" />} trailing>
                View all {totalJobs} jobs
              </IslandLink>
            </div>
          )}
        </Section>

        <Section id="search-profile" aria-label="Search profile" className={cn("scroll-mt-24", !showProfile && "hidden")}>
          {showProfile && (
          <div id="jobs-search-profile-panel" className="space-y-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="font-geist text-xl font-semibold tracking-[-0.02em] text-foreground">Edit job preferences</h2>
              <IslandButton tone="ghost" size="sm" onClick={() => setShowProfile(false)} icon={<X size={13} weight="light" />}>
                Close
              </IslandButton>
            </div>
            <ProfileSearchPanel
              profile={searchProfile}
              form={profileForm}
              saving={saveProfileMutation.isPending}
              onChange={updateProfileForm}
              onSave={() => saveProfileMutation.mutate()}
              onRun={handleRunAgent}
            />
          </div>
          )}
        </Section>
      </Screen>

      <AnimatePresence>
        {detailJob && <JobDetailModal job={detailJob} onClose={() => setDetailJob(null)} />}
      </AnimatePresence>
    </>
  );
}
