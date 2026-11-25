"use client";

import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Bezel, Hairline, IslandButton, Notice, PanelTitle, Select } from "@/components/vanguard";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { apiClient } from "@/lib/api";
import { fetchApplications, type AutoApplyPreferences } from "@/lib/applications-api";
import { startAssistedApply } from "@/lib/assisted-apply";
import { ResumePrefsFields, toResumePrefs, useAutoApplyPrefs, type ResumePrefs } from "@/components/settings/ResumePreferences";

type Mode = "apply" | "outreach" | "both";
type RuleAction = NonNullable<AutoApplyPreferences["auto_rule_action"]>;

const MODES: { id: Mode; label: string }[] = [
  { id: "apply", label: "Apply only" },
  { id: "outreach", label: "Outreach only" },
  { id: "both", label: "Both" },
];
const RULE_ACTIONS: { id: RuleAction; label: string }[] = [
  { id: "apply", label: "Apply" },
  { id: "outreach", label: "Email outreach" },
  { id: "both", label: "Both" },
  { id: "notify", label: "Notify only" },
];
const MATCH_STEPS = [50, 60, 70, 75, 80, 85, 90, 95];
const labelClass = "text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground";

/** Queue outreach drafts for the review queue, 10 jobs per request (the endpoint's cap). */
async function queueOutreach(ids: string[]) {
  let queued = 0;
  for (let i = 0; i < ids.length; i += 10) {
    const { data } = await apiClient.post<{ queued: string[]; skipped: string[] }>("/jobs/applications/outreach", {
      ids: ids.slice(i, i + 10),
    });
    queued += data.queued.length;
  }
  return queued;
}

export function AutoApplyPanel() {
  const qc = useQueryClient();
  const { prefs, isLoading: prefsLoading, patch } = useAutoApplyPrefs();
  const [minMatch, setMinMatch] = useState(70);
  const [within, setWithin] = useState<"any" | "7" | "30">("any");
  const [mode, setMode] = useState<Mode>("apply");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Apply walk-through: ids frozen at the first click, `opened` = how many tabs so far.
  const [queue, setQueue] = useState<typeof items>([]);
  const preparing = useRef(false);
  const [opened, setOpened] = useState(0);
  const [busy, setBusy] = useState(false);
  const [askPrefs, setAskPrefs] = useState<"start" | "enable" | null>(null);
  const [draftPrefs, setDraftPrefs] = useState<ResumePrefs | null>(null);

  const foundAfter = useMemo(
    () => (within === "any" ? undefined : new Date(Date.now() - Number(within) * 86_400_000).toISOString()),
    [within],
  );
  const jobsQuery = useQuery({
    queryKey: ["auto-apply-jobs", minMatch, within],
    queryFn: () => fetchApplications({ status: "saved", sort: "match_desc", minMatch, foundAfter, region: "auto" }, { limit: 100 }),
  });
  const items = useMemo(() => jobsQuery.data?.items ?? [], [jobsQuery.data]);
  const chosen = items.filter((a) => selected.has(a.id));
  const walking = queue.length > 0;
  const needsPrefs = !prefsLoading && !prefs?.resume_prefs_set_at;

  const resetWalk = () => {
    if (preparing.current) return;
    setQueue([]);
    setOpened(0);
  };
  const toggle = (id: string) => {
    if (preparing.current) return;
    resetWalk();
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // startAssistedApply opens the tab before its first await.
  const start = async () => {
    if (preparing.current) return;
    if (needsPrefs) {
      setDraftPrefs(toResumePrefs(prefs));
      setAskPrefs("start");
      return;
    }
    if (chosen.length === 0 && !walking) return;
    preparing.current = true;
    setBusy(true);
    try {
      if (mode !== "outreach") {
        const order = walking ? queue : chosen.filter((a) => a.job_url);
        const target = order[walking ? opened : 0];
        if (!target) return;
        if (!walking) setQueue(order);
        if (!await startAssistedApply(target)) return;
        setOpened((n) => n + 1);
        void qc.invalidateQueries({ queryKey: ["auto-apply-jobs"] });
      }
      if (mode !== "apply" && !walking) {
        const n = await queueOutreach(chosen.map((a) => a.id));
        toast.success(n ? `${n} email drafts waiting in Outreach for your review` : "No recruiter contact found for these jobs");
      }
    } catch {
      toast.error("Couldn't queue outreach");
    } finally {
      preparing.current = false;
      setBusy(false);
    }
  };

  const toggleRule = (on: boolean) => {
    if (on && needsPrefs) {
      setDraftPrefs(toResumePrefs(prefs));
      setAskPrefs("enable");
      return;
    }
    patch.mutate({ auto_apply_enabled: on });
  };
  const savePrefs = () => {
    if (!draftPrefs || !askPrefs) return;
    const after = askPrefs;
    patch.mutate(
      {
        ...draftPrefs,
        resume_prefs_set_at: new Date().toISOString(),
        ...(after === "enable" ? { auto_apply_enabled: true } : {}),
      },
      {
        onSuccess: () => {
          setAskPrefs(null);
          if (after === "start") toast.success("Saved. Press Start again to begin.");
        },
      },
    );
  };

  const total = queue.length;
  const startLabel = walking
    ? opened >= total
      ? `Done: opened ${total}`
      : `Open next (${opened + 1} of ${total})`
    : mode === "outreach"
      ? "Queue outreach"
      : "Start";

  return (
    <div className="mt-7 space-y-6" aria-label="Auto apply">
      <Hairline />
      <Bezel size="md" tone="muted" coreClassName="p-4 md:p-5">
        <PanelTitle title="Auto-apply rule" />
        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm text-foreground">
          <span>New job ≥</span>
          <Select
            aria-label="Minimum match for the rule"
            className="w-24"
            value={String(prefs?.auto_rule_min_match ?? 70)}
            onChange={(e) => patch.mutate({ auto_rule_min_match: Number(e.target.value) })}
          >
            {MATCH_STEPS.map((n) => (
              <option key={n} value={n}>
                {n}%
              </option>
            ))}
          </Select>
          <span aria-hidden>→</span>
          <Select
            aria-label="Rule action"
            className="w-44"
            value={prefs?.auto_rule_action ?? "apply"}
            onChange={(e) => patch.mutate({ auto_rule_action: e.target.value as RuleAction })}
          >
            {RULE_ACTIONS.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </Select>
          <div className="ml-auto flex items-center gap-2">
            <span id="rule-switch" className="text-sm text-muted-foreground">
              {prefs?.auto_apply_enabled ? "Running" : "Paused"}
            </span>
            <Switch
              aria-labelledby="rule-switch"
              checked={Boolean(prefs?.auto_apply_enabled)}
              disabled={prefsLoading}
              onCheckedChange={toggleRule}
            />
          </div>
        </div>
        <div className="mt-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-sm text-foreground">
              Review every outreach email
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Outreach and follow-up drafts wait in Outreach until you approve the message and attachments.
            </p>
          </div>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          Applications always open in your browser for you to review and submit. Rule actions are logged.
        </p>
      </Bezel>

      <div>
        <PanelTitle title="Saved jobs to apply to" />
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <label className="space-y-1.5">
            <span className={labelClass}>Min match</span>
            <Select
              value={String(minMatch)}
              onChange={(e) => {
                if (preparing.current) return;
                resetWalk();
                setMinMatch(Number(e.target.value));
              }}
            >
              <option value="0">Any</option>
              {MATCH_STEPS.map((n) => (
                <option key={n} value={n}>
                  {n}%+
                </option>
              ))}
            </Select>
          </label>
          <label className="space-y-1.5">
            <span className={labelClass}>Found</span>
            <Select
              value={within}
              onChange={(e) => {
                if (preparing.current) return;
                resetWalk();
                setWithin(e.target.value as "any" | "7" | "30");
              }}
            >
              <option value="any">Any time</option>
              <option value="7">Last 7 days</option>
              <option value="30">Last 30 days</option>
            </Select>
          </label>
          <label className="space-y-1.5">
            <span className={labelClass}>Mode</span>
            <Select
              value={mode}
              onChange={(e) => {
                if (preparing.current) return;
                resetWalk();
                setMode(e.target.value as Mode);
              }}
            >
              {MODES.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </Select>
          </label>
        </div>

        <div className="mt-4 max-h-72 overflow-y-auto rounded-2xl ring-1 ring-foreground/[0.08] dark:ring-white/10">
          {jobsQuery.isLoading ? (
            <p className="p-4 text-sm text-muted-foreground">Loading saved jobs…</p>
          ) : items.length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No saved jobs match these filters.</p>
          ) : (
            <ul>
              <li className="flex items-center gap-3 border-b border-foreground/[0.06] px-4 py-2 text-xs text-muted-foreground dark:border-white/[0.07]">
                <input
                  type="checkbox"
                  aria-label="Select all jobs"
                  checked={chosen.length === items.length}
                  onChange={(e) => {
                    if (preparing.current) return;
                    resetWalk();
                    setSelected(e.target.checked ? new Set(items.map((a) => a.id)) : new Set());
                  }}
                />
                {chosen.length} of {jobsQuery.data?.total ?? items.length} selected
              </li>
              {items.map((a) => (
                <li key={a.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                  <input
                    type="checkbox"
                    aria-label={`Select ${a.role} at ${a.company}`}
                    checked={selected.has(a.id)}
                    onChange={() => toggle(a.id)}
                  />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="font-medium text-foreground">{a.role}</span>
                    <span className="text-muted-foreground"> · {a.company}</span>
                  </span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">{a.match_score ?? "–"}%</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {mode === "apply"
              ? "Each click opens one job in your browser."
              : "Outreach drafts go to your Outreach review queue."}
          </p>
          <div className="flex items-center gap-2">
            {walking ? (
              <IslandButton tone="ghost" size="sm" onClick={resetWalk}>
                Reset
              </IslandButton>
            ) : null}
            <IslandButton
              tone="primary"
              size="sm"
              disabled={busy || prefsLoading || (!walking && chosen.length === 0) || (walking && opened >= total && mode !== "outreach")}
              onClick={start}
            >
              {busy ? "Working…" : startLabel}
            </IslandButton>
          </div>
        </div>
      </div>

      <Dialog open={askPrefs !== null} onOpenChange={(open) => !open && setAskPrefs(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>How should your resumes look?</DialogTitle>
            <DialogDescription>
              Asked once. Auto-apply uses these for every job; change them any time in Settings, Profile.
            </DialogDescription>
          </DialogHeader>
          {draftPrefs ? <ResumePrefsFields value={draftPrefs} onChange={setDraftPrefs} /> : null}
          <div className="flex justify-end gap-2">
            <IslandButton tone="ghost" size="sm" onClick={() => setAskPrefs(null)}>
              Cancel
            </IslandButton>
            <IslandButton tone="primary" size="sm" disabled={patch.isPending} onClick={savePrefs}>
              {patch.isPending ? "Saving…" : "Save and continue"}
            </IslandButton>
          </div>
        </DialogContent>
      </Dialog>
      {!jobsQuery.isLoading && jobsQuery.isError ? (
        <Notice className="text-[13px]">Couldn&apos;t load saved jobs. Try again in a moment.</Notice>
      ) : null}
    </div>
  );
}
