"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ArrowSquareOut,
  Browser,
  CalendarCheck,
  CircleNotch,
  EnvelopeSimple,
  FloppyDisk,
  Lightning,
  PaperPlaneTilt,
} from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { cn } from "@/lib/utils";
import { SettingsNav } from "@/components/settings/SettingsNav";
import {
  Bezel,
  Field,
  Input,
  IslandButton,
  IslandLink,
  PageHero,
  Reveal,
  Screen,
  Skeleton,
  Textarea,
  Toggle,
} from "@/components/vanguard";

// Job preferences (roles, locations, seniority, salary) live on the Jobs page's
// search profile, and resume preferences on the Resume page. This page owns
// only the automation switches, so it saves only those fields.
interface AutomationPrefs {
  prefer_live_browser: boolean;
  daily_search_enabled: boolean;
  inbox_tracking_enabled: boolean;
  auto_apply_enabled: boolean;
  outreach_auto_send: boolean;
  outreach_track_opens: boolean;
  outreach_daily_cap: number;
  bio: string | null;
}

interface FormState {
  prefer_live_browser: boolean;
  daily_search_enabled: boolean;
  inbox_tracking_enabled: boolean;
  auto_apply_enabled: boolean;
  outreach_auto_send: boolean;
  outreach_track_opens: boolean;
  outreach_daily_cap: string;
  bio: string;
}

const DEFAULT_FORM: FormState = {
  prefer_live_browser: false,
  daily_search_enabled: false,
  inbox_tracking_enabled: false,
  auto_apply_enabled: false,
  outreach_auto_send: false,
  outreach_track_opens: false,
  outreach_daily_cap: "25",
  bio: "",
};

function prefsToForm(prefs: Partial<AutomationPrefs>): FormState {
  return {
    prefer_live_browser: Boolean(prefs.prefer_live_browser),
    daily_search_enabled: Boolean(prefs.daily_search_enabled),
    inbox_tracking_enabled: Boolean(prefs.inbox_tracking_enabled),
    auto_apply_enabled: Boolean(prefs.auto_apply_enabled),
    outreach_auto_send: Boolean(prefs.outreach_auto_send),
    outreach_track_opens: Boolean(prefs.outreach_track_opens),
    outreach_daily_cap: String(prefs.outreach_daily_cap ?? 25),
    bio: prefs.bio ?? "",
  };
}

/** One switch row: title, one-line description, toggle. */
function SwitchRow({
  icon,
  title,
  description,
  checked,
  onChange,
  children,
}: {
  icon: ReactNode;
  title: string;
  description: ReactNode;
  checked: boolean;
  onChange?: (next: boolean) => void;
  children?: ReactNode;
}) {
  return (
    <li className="py-4 first:pt-0 last:pb-0">
      <div className="flex items-start gap-4">
        <span
          aria-hidden
          className={cn(
            "mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full ring-1 transition-colors duration-500 ease-vanguard",
            checked
              ? "bg-primary/10 text-primary ring-primary/20"
              : "bg-foreground/[0.04] text-foreground/70 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10",
          )}
        >
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium tracking-[-0.01em] text-foreground">{title}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{description}</p>
          {children}
        </div>
        {onChange && <Toggle checked={checked} onChange={onChange} label={title} />}
      </div>
    </li>
  );
}

export default function AutomationSettingsPage() {
  const queryClient = useQueryClient();
  const { data: prefs, isLoading } = useQuery<Partial<AutomationPrefs> | null>({
    queryKey: ["preferences"],
    queryFn: async () => (await apiClient.get("/users/me/preferences")).data,
  });

  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  useEffect(() => {
    if (prefs) setForm(prefsToForm(prefs));
  }, [prefs]);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        prefer_live_browser: form.prefer_live_browser,
        daily_search_enabled: form.daily_search_enabled,
        inbox_tracking_enabled: form.inbox_tracking_enabled,
        auto_apply_enabled: form.auto_apply_enabled,
        outreach_auto_send: form.outreach_auto_send,
        outreach_track_opens: form.outreach_track_opens,
        outreach_daily_cap: Math.min(100, Math.max(1, parseInt(form.outreach_daily_cap, 10) || 25)),
        // null clears a stored value; undefined would be dropped and the old one kept.
        bio: form.bio || null,
      };
      return (await apiClient.patch("/users/me/preferences", payload)).data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["preferences"] });
      toast.success("Settings saved");
    },
    onError: () => toast.error("Save failed"),
  });

  const baseline = prefs ? prefsToForm(prefs) : DEFAULT_FORM;
  const isDirty = JSON.stringify(form) !== JSON.stringify(baseline);

  return (
    <Screen>
      <div className="space-y-6 md:space-y-8">
        <PageHero
          className="pb-4 md:pb-6"
          eyebrow="Settings"
          title="Automation"
          accent="What agents may do on their own."
          description="Everything is off until you turn it on. Job preferences live on the Jobs page, resume preferences on the Resume page."
          actions={
            <div className="flex flex-wrap gap-2">
              <IslandLink href="/jobs#search-profile" tone="ghost" size="sm" icon={<ArrowSquareOut size={14} weight="light" />}>
                Job preferences
              </IslandLink>
              <IslandLink href="/resume" tone="ghost" size="sm" icon={<ArrowSquareOut size={14} weight="light" />}>
                Resume preferences
              </IslandLink>
            </div>
          }
        />

        <Reveal subtle>
          <SettingsNav />
        </Reveal>

        {isLoading ? (
          <Bezel coreClassName="space-y-5 p-6 md:p-7" aria-busy="true">
            <span className="sr-only">Loading settings…</span>
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex items-center gap-4">
                <Skeleton className="h-8 w-8 rounded-full" />
                <Skeleton className="h-4 flex-1 rounded-full" />
                <Skeleton className="h-7 w-12 rounded-full" />
              </div>
            ))}
          </Bezel>
        ) : (
          <div className="space-y-6">
            <Bezel coreClassName="p-6 md:p-7">
              <ul className="divide-y divide-foreground/[0.06] dark:divide-white/[0.07]">
                <SwitchRow
                  icon={<CalendarCheck size={16} weight="light" />}
                  title="Daily job search"
                  description="Searches each morning for your target roles and locations, using your own AI model and key."
                  checked={form.daily_search_enabled}
                  onChange={(v) => set("daily_search_enabled", v)}
                />
                <SwitchRow
                  icon={<Lightning size={16} weight="light" />}
                  title="Queue applications for strong matches"
                  description="Tailors a resume and queues saved jobs that match 70% or more in your browser extension. You review every filled form before anything is submitted, unless you turn on auto-submit under Applying."
                  checked={form.auto_apply_enabled}
                  onChange={(v) => set("auto_apply_enabled", v)}
                />
                <SwitchRow
                  icon={<EnvelopeSimple size={16} weight="light" />}
                  title="Track replies in Gmail"
                  description="Checks your connected Gmail once a day and moves an application forward when a reply clearly matches it. Never sends or changes mail."
                  checked={form.inbox_tracking_enabled}
                  onChange={(v) => set("inbox_tracking_enabled", v)}
                />
                <SwitchRow
                  icon={<Browser size={16} weight="light" />}
                  title="Show the browser during autonomous runs"
                  description="Opens a visible browser window instead of the faster headless job-board APIs."
                  checked={form.prefer_live_browser}
                  onChange={(v) => set("prefer_live_browser", v)}
                />
                <SwitchRow
                  icon={<PaperPlaneTilt size={16} weight="light" />}
                  title="Review every recruiter email"
                  description="Every outreach and follow-up waits for your explicit approval of the message and attachments."
                  checked
                >
                  <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-3">
                    <label className="flex items-center gap-2 text-xs text-muted-foreground">
                      At most
                      <Input
                        name="outreach_daily_cap"
                        type="number"
                        inputMode="numeric"
                        min={1}
                        max={100}
                        className="tabular-nums"
                        trayClassName="w-24"
                        value={form.outreach_daily_cap}
                        onChange={(e) => set("outreach_daily_cap", e.target.value)}
                      />
                      emails a day
                    </label>
                    <label className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Toggle
                        checked={form.outreach_track_opens}
                        onChange={(v) => set("outreach_track_opens", v)}
                        label="Show when emails are opened"
                      />
                      Show when emails are opened
                    </label>
                  </div>
                </SwitchRow>
              </ul>
            </Bezel>

            <Bezel coreClassName="p-6 md:p-7">
              <Field label="About you" hint="Agents quote this in recruiter emails and cover letters.">
                {(id) => (
                  <Textarea
                    id={id}
                    name="bio"
                    rows={3}
                    className="resize-none"
                    value={form.bio}
                    onChange={(e) => set("bio", e.target.value)}
                    placeholder="A short summary of your skills and what you want next…"
                  />
                )}
              </Field>
            </Bezel>


            {/* Sticky save bar */}
            <div className="sticky bottom-4 z-20 md:bottom-6">
              <div className="mx-auto flex max-w-3xl items-center justify-between gap-3 rounded-full bg-card/80 p-1.5 pl-5 shadow-ambient ring-1 ring-foreground/[0.08] backdrop-blur-xl dark:bg-background/70 dark:ring-white/10">
                <p role="status" aria-live="polite" className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                  <span
                    aria-hidden
                    className={cn(
                      "h-1.5 w-1.5 shrink-0 rounded-full transition-colors duration-500 ease-vanguard",
                      isDirty ? "bg-warning" : "bg-success",
                    )}
                  />
                  <span className="truncate">{isDirty ? "Unsaved changes" : "Everything saved"}</span>
                </p>
                <IslandButton
                  tone="primary"
                  size="sm"
                  onClick={() => saveMutation.mutate()}
                  disabled={saveMutation.isPending || !isDirty}
                  aria-busy={saveMutation.isPending}
                  trailing={
                    saveMutation.isPending ? (
                      <CircleNotch size={14} weight="light" className="animate-spin motion-reduce:animate-none" />
                    ) : (
                      <FloppyDisk size={14} weight="light" />
                    )
                  }
                >
                  {saveMutation.isPending ? "Saving…" : "Save"}
                </IslandButton>
              </div>
            </div>
          </div>
        )}
      </div>
    </Screen>
  );
}
