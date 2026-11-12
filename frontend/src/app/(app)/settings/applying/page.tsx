"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CircleNotch, FloppyDisk, Robot, ShieldCheck, Trash } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  Bezel,
  Field,
  Input,
  IslandButton,
  Notice,
  PageHero,
  PanelTitle,
  Reveal,
  Screen,
  Skeleton,
  Toggle,
} from "@/components/vanguard";

interface AutoSubmitPrefs {
  auto_submit_enabled?: boolean;
  auto_submit_daily_cap?: number;
}

interface SavedAnswer {
  question_key: string;
  question: string;
  group: "screening" | "voluntary" | "other";
  optional: boolean;
  value: string | boolean | string[] | null;
  updated_at: string | null;
}

// Examples only: the extension matches the answer to each form's own options.
const PLACEHOLDERS: Record<string, string> = {
  "authorization.work_authorized": "e.g. Yes",
  "authorization.requires_sponsorship": "e.g. No",
  "experience.notice_period_days": "e.g. 30 days",
  "compensation.expected_salary": "e.g. 18 LPA or 95,000 USD",
  "location.willing_to_relocate": "e.g. Yes",
  "availability.start_date": "e.g. 2026-12-01 or Immediately",
  "disclosure.gender": "e.g. I do not wish to answer",
  "disclosure.race_ethnicity": "e.g. I do not wish to answer",
  "disclosure.veteran_status": "e.g. I am not a protected veteran",
  "disclosure.disability_status": "e.g. I do not wish to answer",
};

const shown = (value: SavedAnswer["value"]) =>
  value == null ? "" : Array.isArray(value) ? value.join(", ") : typeof value === "boolean" ? (value ? "Yes" : "No") : value;

function AutoSubmitCard() {
  const queryClient = useQueryClient();
  const { data: prefs, isLoading } = useQuery<AutoSubmitPrefs | null>({
    queryKey: ["preferences"],
    queryFn: async () => (await apiClient.get("/users/me/preferences")).data,
  });
  const [enabled, setEnabled] = useState(false);
  const [cap, setCap] = useState("10");
  useEffect(() => {
    if (!prefs) return;
    setEnabled(Boolean(prefs.auto_submit_enabled));
    setCap(String(prefs.auto_submit_daily_cap ?? 10));
  }, [prefs]);

  const save = useMutation({
    mutationFn: async () =>
      (
        await apiClient.patch("/users/me/preferences", {
          auto_submit_enabled: enabled,
          auto_submit_daily_cap: Math.min(50, Math.max(1, parseInt(cap, 10) || 10)),
        })
      ).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["preferences"] });
      toast.success("Auto-submit settings saved");
    },
    onError: () => toast.error("Save failed"),
  });

  const dirty =
    enabled !== Boolean(prefs?.auto_submit_enabled) || cap !== String(prefs?.auto_submit_daily_cap ?? 10);

  return (
    <Bezel coreClassName="space-y-5 p-6 md:p-7">
      <PanelTitle title="Auto-submit" icon={<Robot size={16} weight="light" />} />
      {isLoading ? (
        <Skeleton className="h-10 w-full rounded-xl" />
      ) : (
        <>
          <div className="flex items-start gap-4">
            <div className="min-w-0 flex-1 text-sm leading-6 text-muted-foreground">
              <p className="font-medium text-foreground">Submit without my click when nothing needs me</p>
              <p className="mt-1 text-xs leading-5">
                Off by default. When on, the browser extension submits a form it filled only from your saved answers
                below and your profile. Anything drafted by AI, unanswered, refused by the site, a CAPTCHA, a sign-in or
                a consent box still waits for you. LinkedIn and Naukri applications always wait for your click.
              </p>
            </div>
            <Toggle checked={enabled} onChange={setEnabled} label="Auto-submit applications" />
          </div>
          <label className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            At most
            <Input
              name="auto_submit_daily_cap"
              type="number"
              inputMode="numeric"
              min={1}
              max={50}
              className="tabular-nums"
              trayClassName="w-24"
              value={cap}
              onChange={(e) => setCap(e.target.value)}
            />
            automatic submissions a day (rolling 24 hours); the rest wait for your approval.
          </label>
          <div className="flex justify-end">
            <IslandButton
              tone="primary"
              size="sm"
              disabled={!dirty || save.isPending}
              aria-busy={save.isPending}
              onClick={() => save.mutate()}
              trailing={save.isPending ? <CircleNotch size={14} className="animate-spin" /> : <FloppyDisk size={14} weight="light" />}
            >
              {save.isPending ? "Saving…" : "Save"}
            </IslandButton>
          </div>
        </>
      )}
    </Bezel>
  );
}

function AnswerRow({ answer }: { answer: SavedAnswer }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(shown(answer.value));
  useEffect(() => setDraft(shown(answer.value)), [answer.value]);
  const path = `/users/me/saved-answers/${encodeURIComponent(answer.question_key)}`;
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["saved-answers"] });
  const save = useMutation({
    mutationFn: async () => (await apiClient.put(path, { value: draft.trim() })).data,
    onSuccess: () => {
      refresh();
      toast.success("Answer saved");
    },
    onError: () => toast.error("Could not save this answer"),
  });
  const clear = useMutation({
    mutationFn: async () => apiClient.delete(path),
    onSuccess: () => {
      setDraft("");
      refresh();
      toast.success("Answer removed");
    },
    onError: () => toast.error("Could not remove this answer"),
  });
  const stored = shown(answer.value);
  const busy = save.isPending || clear.isPending;
  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <Field label={answer.question} hint={answer.optional && answer.group === "voluntary" ? "Optional. Leave empty and the extension never answers it." : undefined}>
        {(id) => (
          <div className="flex flex-wrap items-center gap-2">
            <Input
              id={id}
              trayClassName="min-w-0 flex-1"
              maxLength={2000}
              value={draft}
              placeholder={PLACEHOLDERS[answer.question_key] ?? ""}
              onChange={(e) => setDraft(e.target.value)}
            />
            <IslandButton tone="ghost" size="sm" disabled={busy || !draft.trim() || draft.trim() === stored} onClick={() => save.mutate()}>
              Save
            </IslandButton>
            {answer.value != null ? (
              <IslandButton tone="ghost" size="sm" disabled={busy} onClick={() => clear.mutate()} icon={<Trash size={14} />} aria-label={`Remove the answer to ${answer.question}`}>
                Remove
              </IslandButton>
            ) : null}
          </div>
        )}
      </Field>
    </li>
  );
}

function SavedAnswersCard() {
  const { data, isLoading, isError, refetch } = useQuery<SavedAnswer[]>({
    queryKey: ["saved-answers"],
    queryFn: async () => (await apiClient.get("/users/me/saved-answers")).data.answers,
  });
  const groups: Array<[SavedAnswer["group"], string, string]> = [
    ["screening", "Screening questions", "Asked by most applications. Only these answers and your profile can be submitted without your review."],
    ["voluntary", "Voluntary disclosures", "Used only if you answer them here; otherwise the extension leaves them for you."],
    ["other", "Other answers you saved", "Questions you answered in the extension with “Remember my answers” on."],
  ];
  return (
    <Bezel coreClassName="space-y-6 p-6 md:p-7">
      <PanelTitle title="Saved answers" icon={<ShieldCheck size={16} weight="light" />} />
      {isLoading ? (
        <div className="space-y-3" aria-busy="true">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-11 w-full rounded-xl" />
          ))}
        </div>
      ) : isError ? (
        <Notice tone="danger">
          Could not load your saved answers.{" "}
          <button type="button" className="underline" onClick={() => refetch()}>
            Try again
          </button>
        </Notice>
      ) : (
        groups.map(([group, title, description]) => {
          const items = (data ?? []).filter((a) => a.group === group);
          if (!items.length) return null;
          return (
            <section key={group} className="space-y-3">
              <div>
                <h3 className="text-sm font-medium text-foreground">{title}</h3>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
              </div>
              <ul className="divide-y divide-foreground/[0.06] dark:divide-white/[0.07]">
                {items.map((answer) => (
                  <AnswerRow key={answer.question_key} answer={answer} />
                ))}
              </ul>
            </section>
          );
        })
      )}
    </Bezel>
  );
}

export default function ApplyingSettingsPage() {
  return (
    <Screen>
      <div className="space-y-6 md:space-y-8">
        <PageHero
          className="pb-4 md:pb-6"
          eyebrow="Settings"
          title="Applying"
          accent="Your answers, your rules."
          description="Answers the browser extension reuses on every application, and whether it may submit on its own."
        />
        <Reveal subtle>
          <SettingsNav />
        </Reveal>
        <div className="space-y-6">
          <AutoSubmitCard />
          <SavedAnswersCard />
        </div>
      </div>
    </Screen>
  );
}
