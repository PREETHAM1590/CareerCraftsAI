"use client";

import { useEffect, useId, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence, useReducedMotion, type Variants } from "motion/react";
import {
  Archive,
  ArrowsClockwise,
  ArrowSquareOut,
  CaretRight,
  CircleNotch,
  Clock,
  EnvelopeSimple,
  FloppyDisk,
  GearSix,
  HandPalm,
  NotePencil,
  PaperPlaneTilt,
  Sparkle,
  Tray,
  Trash,
  WarningCircle,
} from "@phosphor-icons/react";
import {
  Bezel,
  EmptyPanel,
  Hairline,
  Input,
  IslandButton,
  IslandLink,
  Notice,
  PageHero,
  PanelTitle,
  RevealGroup,
  Screen,
  Section,
  Select,
  StatusPill,
  Textarea,
  EASE_OUT_EXPO,
  SPRING_SOFT,
  listItem,
  listStagger,
  panelSwap,
  reveal,
} from "@/components/vanguard";
import { cn } from "@/lib/utils";
import { apiClient } from "@/lib/api";
import { connectGmail } from "@/lib/nango-connect";
import { toast } from "sonner";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

interface Draft {
  id: string;
  subject: string;
  company: string;
  timestamp: string;
  initial: string;
  body: string;
  status?: string;
  recipient_email?: string | null;
}

const SUGGESTIONS = [
  {
    id: "1",
    title: "Personalize subject line",
    description: "Mention the specific role and company initiative",
    applied: "Subject: Following up on [Role] at [Company] — excited about [initiative]",
  },
  {
    id: "2",
    title: "Add social proof",
    description: "Reference a recent project or achievement",
    applied: "\n\nP.S. — I recently shipped [project] which reduced latency by 40%. Happy to share details.",
  },
  {
    id: "3",
    title: "CTA optimization",
    description: "Use a specific date/time for the interview request",
    applied: "\n\nAre you available for a 20-minute call this Thursday between 2–4pm, or Friday morning?",
  },
];


interface Template {
  id: string;
  name: string;
  body: string;
}

const TEMPLATES: Template[] = [
  {
    id: "t1",
    name: "Referral ask",
    body: "Hi [Name], I noticed you work at [Company] and I'm applying for [Role]. Would you be open to a 15-minute chat about the team culture?",
  },
  {
    id: "t2",
    name: "Direct recruiter outreach",
    body: "Hi [Name], I came across your profile and wanted to reach out about opportunities at [Company]. My background in [Skill] aligns well with what you're hiring for.",
  },
  {
    id: "t3",
    name: "Warm intro follow-up",
    body: "Following up on my application for [Role] at [Company]. I wanted to share how my work on [Project] directly maps to your needs.",
  },
  {
    id: "t4",
    name: "Coffee chat",
    body: "Hi [Name], I've been following [Company]'s work on [Product] and I'm genuinely excited about what you're building. Would you be open to a quick 20-minute chat?",
  },
  {
    id: "t5",
    name: "Post-rejection keep-warm",
    body: "Thank you for the update. I'd love to stay in touch for future opportunities that might be a better fit.",
  },
];

type EmailTab = "drafts" | "templates" | "inbox";

const TAB_OPTIONS: ReadonlyArray<{ value: EmailTab; label: string }> = [
  { value: "drafts", label: "Drafts" },
  { value: "templates", label: "Templates" },
  { value: "inbox", label: "Inbox" },
];

/** Reduced-motion fallback for the column reveals. */
const fadeOnly: Variants = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.2 } },
};

/* -------------------------------------------------------------------------- */
/* Rail primitives                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Pill switcher for the left rail. Plain buttons (aria-pressed) rather than
 * the kit's tablist so `getByRole("button", { name: "Inbox" })` keeps working.
 */
function RailSwitcher({ value, onChange }: { value: EmailTab; onChange: (next: EmailTab) => void }) {
  const layoutId = useId();
  return (
    <div
      role="group"
      aria-label="Outreach lists"
      className="grid grid-cols-3 gap-1 rounded-full bg-foreground/[0.035] p-1 ring-1 ring-foreground/[0.06] dark:bg-white/[0.04] dark:ring-white/10"
    >
      {TAB_OPTIONS.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              "relative inline-flex h-8 min-w-0 items-center justify-center rounded-full px-2 text-xs font-medium transition-colors duration-500 ease-vanguard",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {active ? (
              <motion.span
                layoutId={layoutId}
                transition={SPRING_SOFT}
                className="absolute inset-0 rounded-full bg-card shadow-[0_1px_2px_hsl(var(--foreground)/0.06),inset_0_1px_0_hsl(0_0%_100%/0.6)] ring-1 ring-foreground/[0.06] dark:bg-white/10 dark:shadow-none dark:ring-white/10"
              />
            ) : null}
            <span className="relative truncate">{opt.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function RailListHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-baseline justify-between px-1">
      <h3 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground">{title}</h3>
      <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground">{String(count).padStart(2, "0")}</span>
    </div>
  );
}

function DraftCard({
  draft,
  active,
  onClick,
}: {
  draft: Draft;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={cn(
        "group flex w-full items-start gap-3 rounded-2xl px-3 py-3 text-left ring-1 transition-[background-color,box-shadow,transform] duration-500 ease-vanguard active:scale-[0.99]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        active
          ? "bg-primary/[0.07] ring-primary/20"
          : "bg-transparent ring-transparent hover:bg-foreground/[0.03] dark:hover:bg-white/[0.04]",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "grid h-9 w-9 shrink-0 place-items-center rounded-[0.8rem] text-xs font-semibold ring-1 transition-colors duration-500 ease-vanguard",
          active
            ? "bg-primary text-primary-foreground ring-primary"
            : "bg-foreground/[0.04] text-foreground/70 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10",
        )}
      >
        {draft.initial}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium tracking-[-0.01em] text-foreground">{draft.subject}</span>
        <span className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <span className="truncate">{draft.company}</span>
          <span aria-hidden>&middot;</span>
          <span className="shrink-0 tabular-nums">{draft.timestamp}</span>
        </span>
      </span>
    </button>
  );
}

function TemplateCard({
  template,
  onUse,
}: {
  template: Template;
  onUse: (body: string) => void;
}) {
  const preview =
    template.body.length > 80 ? template.body.slice(0, 80) + "…" : template.body;

  return (
    <div className="rounded-2xl bg-foreground/[0.025] px-3.5 py-3 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.07]">
      <p className="text-[13px] font-medium tracking-[-0.01em] text-foreground">{template.name}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{preview}</p>
      <button
        type="button"
        onClick={() => onUse(template.body)}
        className={cn(
          "group mt-2.5 inline-flex items-center gap-1 rounded-full py-1 pl-0 pr-1 text-xs font-medium text-primary transition-colors duration-500 ease-vanguard hover:text-primary/80",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        Use template
        <CaretRight
          size={12}
          weight="light"
          aria-hidden
          className="transition-transform duration-500 ease-vanguard group-hover:translate-x-0.5"
        />
      </button>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Inbox cleanup                                                              */
/* -------------------------------------------------------------------------- */

interface InboxCleanupEmail {
  id: string;
  from: string;
  subject: string;
  date: string;
  unsubscribe_url: string | null;
}

function InboxCleanup() {
  const qc = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState(false);
  const reduce = useReducedMotion();

  const {
    data: emails = [],
    isLoading,
    isError,
  } = useQuery<InboxCleanupEmail[]>({
    queryKey: ["inbox-cleanup"],
    queryFn: async () => (await apiClient.get("/email/inbox-cleanup")).data,
  });

  const toggle = (id: string) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });

  const archiveSelected = async () => {
    if (selectedIds.size === 0) return;
    setArchiving(true);
    try {
      const { data } = await apiClient.post<{ archived: number }>("/email/inbox-cleanup/archive", {
        message_ids: Array.from(selectedIds),
      });
      setSelectedIds(new Set());
      qc.invalidateQueries({ queryKey: ["inbox-cleanup"] });
      toast.success(`${data.archived} emails archived`);
    } catch {
      toast.error("Failed to archive selected emails");
    } finally {
      setArchiving(false);
    }
  };

  const stats = { total: emails.length };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 rounded-2xl bg-foreground/[0.025] px-3.5 py-3 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.07]">
        <div className="flex min-w-0 items-center gap-2.5">
          <Tray size={16} weight="light" aria-hidden className="shrink-0 text-muted-foreground" />
          <span className="truncate text-xs text-muted-foreground">Promotions &amp; updates (30d)</span>
        </div>
        <span className="font-geist text-xl font-semibold tabular-nums tracking-[-0.03em] text-foreground">{stats.total}</span>
      </div>

      {selectedIds.size > 0 && (
        <IslandButton
          tone="ghost"
          size="sm"
          onClick={archiveSelected}
          disabled={archiving}
          className="w-full"
          icon={
            archiving ? (
              <ArrowsClockwise size={14} weight="light" className="animate-spin motion-reduce:animate-none" />
            ) : (
              <Archive size={14} weight="light" />
            )
          }
        >
          Archive ({selectedIds.size})
        </IslandButton>
      )}

      <div className="space-y-1.5" aria-live="polite" aria-busy={isLoading}>
        {isLoading && (
          <p className="px-1 py-4 text-center text-xs text-muted-foreground">Loading inbox…</p>
        )}
        {isError && (
          <Notice tone="warning" icon={<WarningCircle size={14} weight="light" />} className="px-3 py-2.5 text-xs leading-5">
            Could not load inbox — connect Gmail in Settings.
          </Notice>
        )}
        <AnimatePresence initial={false}>
          {emails.map((email) => {
            const checked = selectedIds.has(email.id);
            const checkboxId = `inbox-cleanup-${email.id}`;
            return (
              <motion.div
                key={email.id}
                initial={reduce ? { opacity: 0 } : { opacity: 0, y: 8 }}
                animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0 }}
                exit={reduce ? { opacity: 0 } : { opacity: 0, x: -12 }}
                transition={{ duration: 0.35, ease: EASE_OUT_EXPO }}
              >
                <div
                  className={cn(
                    "flex items-start gap-3 rounded-2xl px-3 py-2.5 ring-1 transition-[background-color,box-shadow] duration-500 ease-vanguard",
                    checked
                      ? "bg-primary/[0.06] ring-primary/20"
                      : "bg-transparent ring-foreground/[0.05] hover:bg-foreground/[0.025] dark:ring-white/[0.07] dark:hover:bg-white/[0.03]",
                  )}
                >
                  <input
                    id={checkboxId}
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggle(email.id)}
                    className="mt-0.5 h-4 w-4 shrink-0 rounded accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                  <div className="min-w-0 flex-1">
                    <label htmlFor={checkboxId} className="block cursor-pointer">
                      <span className="block truncate text-xs font-semibold text-foreground">{email.from}</span>
                      <span className="mt-0.5 block truncate text-xs text-muted-foreground">{email.subject}</span>
                    </label>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground">{email.date}</span>
                      {email.unsubscribe_url && (
                        <a
                          href={email.unsubscribe_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className={cn(
                            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] text-muted-foreground transition-colors duration-500 ease-vanguard hover:bg-danger/10 hover:text-danger",
                            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                          )}
                          title="Open unsubscribe page"
                        >
                          Open unsubscribe page
                          <ArrowSquareOut size={11} weight="light" aria-hidden />
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
        {!isLoading && !isError && emails.length === 0 && (
          <EmptyPanel
            compact
            icon={<Tray size={20} weight="light" />}
            title="Inbox clean! Nothing to show."
          />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Right rail                                                                 */
/* -------------------------------------------------------------------------- */

function SuggestionRow({
  suggestion,
  onApply,
}: {
  suggestion: (typeof SUGGESTIONS)[0];
  onApply: () => void;
}) {
  return (
    <li className="rounded-2xl bg-foreground/[0.025] px-3.5 py-3 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.07]">
      <p className="text-[13px] font-medium tracking-[-0.01em] text-foreground">{suggestion.title}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{suggestion.description}</p>
      <p className="mt-2 line-clamp-2 font-geist-mono text-[11px] leading-5 text-muted-foreground/80">
        {suggestion.applied.trim()}
      </p>
      <button
        type="button"
        onClick={onApply}
        className={cn(
          "group mt-2.5 inline-flex items-center gap-1 rounded-full py-1 pr-1 text-xs font-medium text-primary transition-colors duration-500 ease-vanguard hover:text-primary/80",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        Apply
        <CaretRight
          size={12}
          weight="light"
          aria-hidden
          className="transition-transform duration-500 ease-vanguard group-hover:translate-x-0.5"
        />
      </button>
    </li>
  );
}

function HeroMeta({ items }: { items: Array<{ label: string; value: ReactNode }> }) {
  return (
    <dl className="grid grid-cols-3 gap-4 border-y border-border py-4">
      {items.map((item) => (
        <div key={item.label} className="min-w-0">
          <dt className="text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">{item.label}</dt>
          <dd className="mt-1 font-geist text-2xl font-semibold tabular-nums tracking-[-0.04em] text-foreground">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/* -------------------------------------------------------------------------- */
/* Page                                                                       */
/* -------------------------------------------------------------------------- */

export default function EmailPage() {
  const [selectedId, setSelectedId] = useState<string>("");
  const [composeText, setComposeText] = useState<string>("");
  const [recipientEmail, setRecipientEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [emailTab, setEmailTab] = useState<EmailTab>("drafts");
  const [localDrafts, setLocalDrafts] = useState<Draft[]>([]);
  const [deleteTarget, setDeleteTarget] = useState<Draft | "all" | null>(null);
  const [deleting, setDeleting] = useState(false);
  const qc = useQueryClient();
  const reduce = useReducedMotion();
  const columnVariants = reduce ? fadeOnly : reveal;
  const recipientId = useId();
  const subjectId = useId();
  const bodyId = useId();

  const { data: remoteDrafts = [] } = useQuery<Draft[]>({
    queryKey: ["email-drafts"],
    queryFn: async () => {
      const { data } = await apiClient.get("/email/drafts");
      return data;
    },
  });

  const { data: connectedAccounts } = useQuery<{ google: boolean; gmail_send: boolean }>({
    queryKey: ["connected-accounts"],
    queryFn: async () => {
      const { data } = await apiClient.get("/users/me/connected-accounts");
      return data;
    },
  });

  const gmailConnectionFlag = connectedAccounts?.gmail_send ?? false;
  const { data: integrations = [] } = useQuery<
    Array<{ provider: string; status: string; account_email: string | null }>
  >({
    queryKey: ["integrations"],
    queryFn: async () => (await apiClient.get("/integrations")).data,
    enabled: gmailConnectionFlag,
  });
  const gmailConnection = integrations.find((connection) => connection.provider === "gmail");
  const gmailConnected = gmailConnectionFlag && gmailConnection?.status === "connected";
  const gmailAccountEmail = gmailConnection?.account_email;

  const drafts = [...localDrafts, ...remoteDrafts.filter((d) => !localDrafts.find((x) => x.id === d.id))];

  const selected = drafts.find((d) => d.id === selectedId) ?? drafts[0];

  useEffect(() => {
    setComposeText(selected?.body ?? "");
    setRecipientEmail(selected?.recipient_email ?? "");
    setSubject(selected?.subject ?? "");
  }, [selected?.body, selected?.recipient_email, selected?.subject, selectedId]);

  const handleConnectGmail = async () => {
    const { error } = await connectGmail("/email");
    if (error) toast.error(error.message);
  };

  const handleNewDraft = () => {
    const newId = `new-${Date.now()}`;
    const blank = { id: newId, subject: "New draft", company: "Untitled", timestamp: "just now", initial: "N", body: "", status: "draft" };
    setLocalDrafts((prev) => [blank, ...prev]);
    setSelectedId(newId);
    setComposeText("");
    setEmailTab("drafts");
    toast.success("New draft created");
  };

  const handleSaveDraft = async () => {
    if (!gmailConnected) {
      toast.error("Connect Gmail to save drafts in Gmail.");
      return;
    }
    if (!recipientEmail.trim() || !subject.trim()) {
      toast.info("Add a recipient and subject before saving.");
      return;
    }
    if (!composeText.trim()) {
      toast.info("Nothing to save — compose some text first.");
      return;
    }
    try {
      await apiClient.post("/email/gmail-drafts", {
        recipient_email: recipientEmail,
        subject,
        body: composeText,
      });
      setLocalDrafts((previous) => previous.map((draft) => (
        draft.id === selected?.id
          ? { ...draft, subject, body: composeText, recipient_email: recipientEmail, timestamp: "just now" }
          : draft
      )));
      toast.success("Saved to Gmail drafts");
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ? `Could not save: ${detail}` : "Failed to save Gmail draft");
    }
  };

  const [sending, setSending] = useState(false);

  const handleSend = async () => {
    if (!gmailConnected) {
      toast.error("Gmail not connected — connect Gmail to send");
      return;
    }
    if (!composeText.trim()) {
      toast.info("Nothing to send — compose some text first.");
      return;
    }
    if (!recipientEmail.trim() || !subject.trim()) {
      toast.info("Add a recipient and subject before sending.");
      return;
    }
    setSending(true);
    try {
      // Clicking Send IS the human-in-the-loop approval: compose creates a
      // pending email (status awaiting_approval), then approve dispatches it.
      const { data: composed } = await apiClient.post<{ run_id: string; status: string }>(
        "/email/compose",
        {
          company: selected?.company ?? "Unknown",
          role: selected?.subject ?? "Draft",
          recipient_email: recipientEmail,
          subject,
          body: composeText,
        },
      );
      if (composed.status !== "awaiting_approval") {
        toast.error("Draft could not be prepared for sending");
        return;
      }
      await apiClient.post(`/email/approve/${composed.run_id}`);
      qc.invalidateQueries({ queryKey: ["email-drafts"] });
      toast.success("Email sent");
      setComposeText("");
    } catch (err) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detail ? `Send failed: ${detail}` : "Send failed — check Gmail connection");
    } finally {
      setSending(false);
    }
  };

  const handleDeleteDrafts = async () => {
    if (!deleteTarget || deleting) return;
    const target = deleteTarget;
    const isRemote = target === "all" || remoteDrafts.some((draft) => draft.id === target.id);
    setDeleting(true);
    try {
      if (isRemote) {
        await apiClient.delete(target === "all" ? "/email/drafts" : `/email/drafts/${target.id}`);
      }
      const keep = (draft: Draft) => target !== "all" && draft.id !== target.id;
      setLocalDrafts((previous) => previous.filter(keep));
      qc.setQueryData<Draft[]>(["email-drafts"], (previous) => (previous ?? []).filter(keep));
      if (target === "all" || target.id === selected?.id) setSelectedId("");
      setDeleteTarget(null);
      toast.success(target === "all" ? "All workspace drafts deleted" : "Draft deleted");
      void qc.invalidateQueries({ queryKey: ["email-drafts"] });
    } catch {
      toast.error("Could not delete drafts. Please try again.");
    } finally {
      setDeleting(false);
    }
  };

  const handleApplySuggestion = (suggestion: typeof SUGGESTIONS[0]) => {
    setComposeText((prev) => {
      const base = prev || (selected?.body ?? "");
      return base + suggestion.applied;
    });
    toast.success(`Applied: ${suggestion.title}`);
  };

  const wordCount = composeText.trim() ? composeText.trim().split(/\s+/).length : 0;


  return (
    <Screen>
      <PageHero
        eyebrow="Recruiter outreach"
        title="Email workspace"
        description="Write a clear message. Review it. Send when you're ready."
        className="pb-0 md:pb-0"
        actions={
          <>
            {gmailConnected ? (
              <IslandLink
                href="/settings/integrations"
                tone="ghost"
                size="md"
                icon={<GearSix size={16} weight="light" />}
              >
                Manage integrations
              </IslandLink>
            ) : (
              <IslandButton
                tone="primary"
                size="md"
                onClick={handleConnectGmail}
                icon={<EnvelopeSimple size={16} weight="light" />}
                trailing
              >
                Connect Gmail
              </IslandButton>
            )}
            <IslandButton
              tone={gmailConnected ? "primary" : "ghost"}
              size="md"
              onClick={handleNewDraft}
              icon={<NotePencil size={16} weight="light" />}
            >
              New draft
            </IslandButton>
          </>
        }
        aside={
          <HeroMeta
            items={[
              { label: "Drafts", value: drafts.length },
              { label: "Templates", value: TEMPLATES.length },
              { label: "Drafts", value: drafts.length },
            ]}
          />
        }
      />

      <Section aria-label="Outreach workspace">
        <RevealGroup className="grid grid-cols-1 items-start gap-4 lg:grid-cols-12">
          {/* Left rail: connection + list switcher */}
          <motion.aside
            variants={columnVariants}
            aria-label="Mailboxes"
            className="order-2 min-w-0 lg:order-1 lg:col-span-4 2xl:col-span-3"
          >
            <Bezel size="md" coreClassName="flex flex-col gap-4 p-4">
              <div className="space-y-2 px-1 pt-1" aria-live="polite">
                <StatusPill tone={gmailConnected ? "success" : "neutral"} live={gmailConnected}>
                  {gmailConnected ? "Gmail Connected" : "Gmail Not Connected"}
                </StatusPill>
                <p className="break-words text-xs leading-5 text-muted-foreground">
                  {gmailConnected
                    ? `${gmailAccountEmail ?? "Connected account"} · Synced`
                    : "Connect Gmail in Settings → Account"}
                </p>
              </div>

              <Hairline />

              <RailSwitcher value={emailTab} onChange={setEmailTab} />

              <div className="-mx-1 max-h-[20rem] overflow-y-auto overscroll-contain px-1 lg:max-h-[34rem]">
                <AnimatePresence mode="wait" initial={false}>
                  <motion.div
                    key={emailTab}
                    variants={panelSwap}
                    initial="hidden"
                    animate="show"
                    exit="exit"
                    className="space-y-3 pb-1"
                  >
                    {emailTab === "drafts" ? (
                      <>
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <RailListHeader title="Drafts" count={drafts.length} />
                          <IslandButton tone="quiet" size="sm" disabled={!drafts.length || deleting || sending} onClick={() => setDeleteTarget("all")} icon={<Trash size={14} weight="light" />} className="text-danger hover:text-danger">Delete all</IslandButton>
                        </div>
                        {!drafts.length && <EmptyPanel compact icon={<NotePencil size={22} weight="light" />} title="No drafts" description="Create a draft to start your outreach." action={<IslandButton tone="ghost" size="sm" onClick={handleNewDraft}>New draft</IslandButton>} />}
                        <motion.ul variants={listStagger} initial="hidden" animate="show" className="space-y-1">
                          {drafts.map((draft) => (
                            <motion.li key={draft.id} variants={listItem}>
                              <DraftCard
                                draft={draft}
                                active={draft.id === selectedId}
                                onClick={() => setSelectedId(draft.id)}
                              />
                            </motion.li>
                          ))}
                        </motion.ul>
                      </>
                    ) : emailTab === "templates" ? (
                      <>
                        <RailListHeader title="Templates" count={TEMPLATES.length} />
                        <motion.ul variants={listStagger} initial="hidden" animate="show" className="space-y-2">
                          {TEMPLATES.map((tpl) => (
                            <motion.li key={tpl.id} variants={listItem}>
                              <TemplateCard
                                template={tpl}
                                onUse={(body) => {
                                  setComposeText(body);
                                  setEmailTab("drafts");
                                  toast.success(`Template "${tpl.name}" loaded`);
                                }}
                              />
                            </motion.li>
                          ))}
                        </motion.ul>
                      </>
                    ) : (
                      <InboxCleanup />
                    )}
                  </motion.div>
                </AnimatePresence>
              </div>
            </Bezel>
          </motion.aside>

          {/* Composer */}
          <motion.div variants={columnVariants} className="order-1 min-w-0 lg:order-2 lg:col-span-8 2xl:col-span-6">
            <Bezel size="lg" lifted coreClassName="flex flex-col">
              <div className="space-y-2 px-4 pt-4 lg:hidden">
                <label htmlFor={`${bodyId}-draft`} className="block pl-1 text-xs font-medium text-muted-foreground">Open draft</label>
                <Select id={`${bodyId}-draft`} disabled={!drafts.length} value={selected?.id ?? ""} onChange={(event) => setSelectedId(event.target.value)}>
                  {!drafts.length && <option value="">No drafts</option>}
                  {drafts.map((draft) => <option key={draft.id} value={draft.id}>{draft.company} · {draft.subject}</option>)}
                </Select>
              </div>
              {/* Message header */}
              <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-4 sm:px-5">
                {selected ? (
                  <div className="flex min-w-0 items-start gap-3.5">
                    <span
                      aria-hidden
                      className="grid h-11 w-11 shrink-0 place-items-center rounded-[0.95rem] bg-primary/10 text-sm font-semibold text-primary ring-1 ring-primary/20"
                    >
                      {selected.initial}
                    </span>
                    <div className="min-w-0">
                      <h2 className="text-balance font-geist text-lg font-semibold tracking-[-0.03em] text-foreground sm:text-xl">
                        {subject || selected.subject}
                      </h2>
                      <p className="mt-1 flex min-w-0 flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
                        <span className="break-all">To: {recipientEmail || "Add recipient"}</span>
                        <span aria-hidden>&middot;</span>
                        <span>{selected.company}</span>
                      </p>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No drafts yet. Compose your first email to get started.
                  </p>
                )}
                <div className="flex shrink-0 items-center gap-2">
                  {selected ? <StatusPill tone="warning">Draft</StatusPill> : null}
                  <IslandButton tone="quiet" size="sm" disabled={!selected || sending || deleting} onClick={() => selected && setDeleteTarget(selected)} icon={<Trash size={14} weight="light" />} className="text-danger hover:text-danger">Delete draft</IslandButton>
                </div>
              </div>

              <Hairline />

              {/* Envelope fields */}
              <div className="grid gap-3 px-4 py-4 sm:px-5">
                <div className="space-y-2">
                  <label htmlFor={recipientId} className="block pl-1 text-[12px] font-medium text-muted-foreground">
                    To
                  </label>
                  <Input
                    id={recipientId}
                    type="email"
                    autoComplete="email"
                    value={recipientEmail}
                    onChange={(e) => setRecipientEmail(e.target.value)}
                    placeholder="Recipient email"
                  />
                </div>
                <div className="space-y-2">
                  <label htmlFor={subjectId} className="block pl-1 text-[12px] font-medium text-muted-foreground">
                    Subject
                  </label>
                  <Input
                    id={subjectId}
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    placeholder="Subject"
                  />
                </div>
              </div>

              {/* Body */}
              <div className="space-y-2 px-4 sm:px-5">
                <div className="flex flex-wrap items-baseline justify-between gap-2 pl-1">
                  <label htmlFor={bodyId} className="text-[12px] font-medium text-muted-foreground">
                    Message
                  </label>
                  <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground">
                    {wordCount} words · {composeText.length} chars
                  </span>
                </div>
                <Textarea
                  id={bodyId}
                  value={composeText}
                  onChange={(e) => setComposeText(e.target.value)}
                  placeholder="Edit or compose your email here…"
                  rows={10}
                  className="min-h-[14rem] text-[15px] leading-7 sm:min-h-[18rem]"
                />
              </div>

              {/* Send bar */}
              <div className="mt-4 space-y-3 px-4 pb-4 sm:px-5 sm:pb-5">
                <Notice tone="warning" icon={<HandPalm size={16} weight="light" />}>
                  <span className="font-medium">Review before sending.</span>{" "}
                  <span className="opacity-80">Pressing Send approves this email and sends it through Gmail.</span>
                </Notice>
                <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-end">
                  <span className="sr-only" aria-live="polite">
                    {sending ? "Sending email…" : ""}
                  </span>
                  <IslandButton
                    tone="ghost"
                    size="md"
                    onClick={handleSaveDraft}
                    icon={<FloppyDisk size={16} weight="light" />}
                  >
                    Save draft
                  </IslandButton>
                  <IslandButton
                    tone="primary"
                    size="md"
                    onClick={handleSend}
                    disabled={sending}
                    aria-busy={sending}
                    trailing={
                      sending ? (
                        <CircleNotch size={15} weight="light" className="animate-spin motion-reduce:animate-none" />
                      ) : (
                        <PaperPlaneTilt size={15} weight="light" />
                      )
                    }
                  >
                    {sending ? "Sending…" : "Send"}
                  </IslandButton>
                </div>
              </div>
            </Bezel>
          </motion.div>

          {/* Right rail: suggestions + follow-up timeline */}
          <motion.aside
            variants={columnVariants}
            aria-label="Assistant"
            className="order-3 grid min-w-0 grid-cols-1 content-start gap-4 md:grid-cols-2 lg:col-span-12 2xl:col-span-3 2xl:grid-cols-1"
          >
            <Bezel size="md" coreClassName="space-y-4 p-4">
              <PanelTitle
                title="AI Suggestions"
                icon={<Sparkle size={15} weight="light" />}
                meta={<span className="font-geist-mono tabular-nums">{String(SUGGESTIONS.length).padStart(2, "0")}</span>}
              />
              <ul className="space-y-2">
                {SUGGESTIONS.map((s) => (
                  <SuggestionRow key={s.id} suggestion={s} onApply={() => handleApplySuggestion(s)} />
                ))}
              </ul>
            </Bezel>

            <Bezel size="md" coreClassName="space-y-5 p-4">
              <PanelTitle title="Follow-up policy" icon={<Clock size={15} weight="light" />} />
              <p className="text-sm text-muted-foreground">After confirmed outreach, follow-up drafts can be prepared on day 5 and day 12. Each send requires your approval. This is the policy, not a record of sent or queued messages.</p>
              <a href="/applications" className="text-sm text-primary underline">View application follow-ups</a>
            </Bezel>
          </motion.aside>
        </RevealGroup>
      </Section>
      <Dialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleting) setDeleteTarget(null); }}>
        <DialogContent className="w-[calc(100%-2rem)] rounded-3xl border-border bg-card p-5 sm:p-6" onEscapeKeyDown={(event) => { if (deleting) event.preventDefault(); }} onPointerDownOutside={(event) => { if (deleting) event.preventDefault(); }}>
          <DialogTitle>{deleteTarget === "all" ? "Delete all workspace drafts?" : "Delete this draft?"}</DialogTitle>
          <DialogDescription className="break-words leading-6">
            {deleteTarget === "all" ? "This clears your CareerCraft drafts and cancels their pending send approvals." : `“${deleteTarget?.subject ?? "This draft"}” will be removed from CareerCraft and any pending send approval will be cancelled.`} Copies saved in Gmail are kept. This cannot be undone in the workspace.
          </DialogDescription>
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <IslandButton tone="ghost" size="sm" disabled={deleting} onClick={() => setDeleteTarget(null)}>Cancel</IslandButton>
            <IslandButton tone="danger" size="sm" disabled={deleting} aria-busy={deleting} onClick={handleDeleteDrafts} icon={deleting ? <CircleNotch size={14} className="animate-spin" /> : <Trash size={14} />}>{deleting ? "Deleting…" : deleteTarget === "all" ? "Delete all drafts" : "Delete draft"}</IslandButton>
          </div>
        </DialogContent>
      </Dialog>
    </Screen>
  );
}
