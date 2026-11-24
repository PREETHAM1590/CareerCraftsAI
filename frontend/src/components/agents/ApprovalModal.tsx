"use client";
import { useState, type ComponentType, type ReactNode } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { motion, useReducedMotion } from "motion/react";
import {
  Check,
  CircleNotch,
  CurrencyDollar,
  Envelope,
  Eye,
  FileText,
  LinkedinLogo,
  MagnifyingGlass,
  PencilSimple,
  Scroll,
  ShieldCheck,
  Warning,
  X,
  type IconProps,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { approvalContent, approvalEdits } from "@/lib/approval-content";
import { apiClient } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAgentStore } from "@/store/agentStore";
import {
  Bezel,
  Eyebrow,
  Hairline,
  IconButton,
  IslandButton,
  Notice,
  StatusPill,
  Textarea,
  EASE_OUT_EXPO,
  SPRING_PANEL,
} from "@/components/vanguard";

interface Props {
  runId: string;
  action: Record<string, unknown>;
  onApprove: () => void;
  onCancel: () => void;
}

function CharCount({ current, max }: { current: number; max: number }) {
  const pct = Math.min(100, (current / max) * 100);
  return (
    <span
      className={`font-geist-mono text-xs tabular-nums ${pct > 90 ? "text-danger" : pct > 75 ? "text-warning" : "text-muted-foreground"}`}
    >
      {current}/{max}
    </span>
  );
}

const SPECIALISED_TYPES = [
  "computer_action",
  "send_email",
  "resume_ready",
  "linkedin_edits",
  "cover_letter_review",
  "search_confirmation",
  "linkedin_outreach",
  "salary_report_review",
];

const ACTION_ICON: Record<string, ComponentType<IconProps>> = {
  send_email: Envelope,
  resume_ready: FileText,
  linkedin_edits: LinkedinLogo,
  linkedin_outreach: LinkedinLogo,
  cover_letter_review: Scroll,
  salary_report_review: CurrencyDollar,
  search_confirmation: MagnifyingGlass,
};

/** Sentence-case label row for a block inside the review, with an optional trailing control. */
function BlockLabel({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-3">
      <span className="pl-1 text-[12px] font-medium tracking-[-0.005em] text-muted-foreground">{children}</span>
      {action}
    </div>
  );
}

/** Recessed read-only preview tray. */
function Recessed({ children, className, mono = false }: { children: ReactNode; className?: string; mono?: boolean }) {
  return (
    <Bezel
      size="md"
      tone="muted"
      coreClassName={cn(
        "overflow-y-auto overscroll-contain whitespace-pre-wrap break-words p-4 text-sm leading-6 text-foreground",
        mono && "font-geist-mono text-[13px]",
        className,
      )}
    >
      {children}
    </Bezel>
  );
}

/** Edit / Preview toggle — accessible name stays exactly "Edit" or "Preview". */
function EditToggle({ editing, onClick }: { editing: boolean; onClick: () => void }) {
  return (
    <IslandButton
      tone="quiet"
      size="sm"
      icon={editing ? <Eye size={14} weight="light" /> : <PencilSimple size={14} weight="light" />}
      onClick={onClick}
    >
      {editing ? "Preview" : "Edit"}
    </IslandButton>
  );
}

const asStrings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item !== "") : [];

/** Every caution the backend attached to a draft, as plain sentences. */
function collectWarnings(action: Record<string, unknown>): string[] {
  const unvetted = asStrings(action.unvetted_destinations);
  const found = [
    ...asStrings(action.warnings),
    ...(unvetted.length > 0
      ? [`Links or addresses you did not provide: ${unvetted.slice(0, 5).join(", ")}. Check them before approving.`]
      : []),
    ...asStrings([action.context_warning, action.persist_warning]),
  ];
  return Array.from(new Set(found));
}

export function ApprovalModal({ runId, action, onApprove, onCancel }: Props) {
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editedText, setEditedText] = useState<string | null>(null);
  const reduce = useReducedMotion();
  const actionType = (action.type as string) || "unknown";
  const warnings = collectWarnings(action);
  const ActionIcon = ACTION_ICON[actionType] ?? ShieldCheck;

  const decide = async (approved: boolean) => {
    if (approved && actionType === "linkedin_outreach") {
      try { await navigator.clipboard.writeText(JSON.stringify(action.messages ?? [], null, 2)); toast.success("Drafts copied. Send them manually in LinkedIn."); } catch { toast.error("Copy failed. Copy the draft text manually."); }
      return;
    }
    if (approved && editedText !== null && !editedText.trim()) {
      toast.error("The approved content cannot be empty");
      return;
    }
    setLoading(true);
    try {
      const edits = approvalEdits(approved, editedText);
      await apiClient.post(`/agents/${runId}/approve`, {
        approved,
        edits,
      });
      if (approved) {
        useAgentStore.getState().clearCheckpoint(runId);
        useAgentStore.getState().setRunStatus(runId, "queued");
        toast.success("Action approved");
        onApprove();
      } else {
        useAgentStore.getState().setRunStatus(runId, "cancelled");
        toast.info("Action cancelled");
        onCancel();
      }
    } catch {
      toast.error("Failed to process approval");
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape" && !editing) onCancel();
  };

  const approveLabel = loading ? "Processing..." : actionType === "linkedin_outreach" ? "Copy drafts" : "Approve & Execute";

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && onCancel()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay asChild>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.45, ease: EASE_OUT_EXPO }}
            className="fixed inset-0 z-40 grid place-items-center bg-background/60 p-3 backdrop-blur-md dark:bg-black/60 sm:p-6"
          >
            <DialogPrimitive.Content asChild onKeyDown={handleKeyDown}>
              <motion.div
                initial={reduce ? { opacity: 0 } : { opacity: 0, y: 32, scale: 0.97 }}
                animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
                transition={reduce ? { duration: 0.2 } : SPRING_PANEL}
                data-testid="approval-modal"
                className="approval-modal w-full max-w-3xl font-geist outline-none"
              >
                <Bezel
                  size="lg"
                  lifted
                  coreClassName="flex max-h-[calc(100dvh-1.5rem)] flex-col overflow-hidden sm:max-h-[90dvh]"
                >
                  {/* Header — what is being gated */}
                  <header className="flex items-start gap-4 px-5 pb-5 pt-5 md:px-7 md:pt-7">
                    <span
                      aria-hidden
                      className="mt-1 hidden h-12 w-12 shrink-0 place-items-center rounded-2xl bg-warning/10 text-warning ring-1 ring-warning/25 sm:grid"
                    >
                      <ActionIcon size={22} weight="light" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <Eyebrow>Human in the loop</Eyebrow>
                      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                        <DialogPrimitive.Title className="font-geist text-2xl font-semibold tracking-[-0.03em] text-foreground md:text-[1.75rem]">
                          Review Required
                        </DialogPrimitive.Title>
                        <StatusPill tone="warning" live className="capitalize">
                          {actionType.replace(/_/g, " ")}
                        </StatusPill>
                      </div>
                      <DialogPrimitive.Description className="mt-2 text-sm leading-6 text-muted-foreground">
                        Review before executing this action.
                      </DialogPrimitive.Description>
                    </div>
                    <DialogPrimitive.Close asChild>
                      <IconButton aria-label="Close without deciding" disabled={loading}>
                        <X size={16} weight="light" />
                      </IconButton>
                    </DialogPrimitive.Close>
                  </header>

                  <Hairline />

                  {/* Body — the exact payload that will execute */}
                  <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-5 py-5 md:px-7 md:py-6">
                    {warnings.length > 0 && (
                      <div role="alert">
                        <Notice tone="warning" icon={<Warning size={16} weight="light" />}>
                          <p className="font-medium">Warnings</p>
                          <ul className="mt-1.5 list-disc space-y-1 pl-5">
                            {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
                          </ul>
                        </Notice>
                      </div>
                    )}

                    {/* Email preview */}
                    {actionType === "send_email" && (
                      <div className="space-y-3">
                        <Bezel size="md" coreClassName="text-sm">
                          <dl className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-baseline gap-x-3 px-4 py-3">
                            <dt className="font-medium text-muted-foreground">To:</dt>
                            <dd className="break-words text-foreground">{String(action.recipient ?? "—")}</dd>
                          </dl>
                          <Hairline />
                          <dl className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-baseline gap-x-3 px-4 py-3">
                            <dt className="font-medium text-muted-foreground">Subject:</dt>
                            <dd className="break-words font-medium text-foreground">{String(action.subject ?? "—")}</dd>
                          </dl>
                        </Bezel>
                        <BlockLabel
                          action={
                            <EditToggle
                              editing={editing}
                              onClick={() => {
                                if (!editing && editedText === null) setEditedText(String(action.body ?? ""));
                                setEditing(!editing);
                              }}
                            />
                          }
                        >
                          Email Body
                        </BlockLabel>
                        {editing ? (
                          <div className="space-y-1.5">
                            <Textarea
                              aria-label="Edit the email body"
                              value={editedText ?? ""}
                              onChange={(e) => setEditedText(e.target.value)}
                              className="min-h-48 font-geist-mono text-sm"
                              placeholder="Edit the email body..."
                            />
                            <div className="flex justify-end pr-1">
                              <CharCount current={(editedText ?? "").length} max={1500} />
                            </div>
                          </div>
                        ) : (
                          <Recessed className="max-h-48">{approvalContent(String(action.body ?? "—"), editedText)}</Recessed>
                        )}
                      </div>
                    )}

                    {/* Resume preview */}
                    {actionType === "resume_ready" && (() => {
                      const ats = (action.ats_breakdown ??
                        (typeof action.ats_score === "object" ? action.ats_score : null)) as
                        {
                          composite_score?: number;
                          keyword_score?: number;
                          readability_score?: number;
                          format_score?: number;
                          missing_keywords?: string[];
                        } | null;
                      return (
                        <div className="space-y-4">
                          {/* ats_score is the composite int; ats_breakdown carries the
                              sub-scores. Older runs stored only the int, so fall back to
                              treating ats_score as an object if that is what arrived. */}
                          {ats ? (
                            <Bezel size="md" tone="primary" coreClassName="grid grid-cols-1 gap-4 p-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center md:p-5">
                              <div className="flex items-center gap-4">
                                <div className="grid h-16 w-16 shrink-0 place-items-center rounded-full bg-primary/10 ring-1 ring-primary/20">
                                  <span className="font-geist text-2xl font-semibold tabular-nums tracking-[-0.04em] text-primary">
                                    {ats.composite_score ?? "—"}
                                  </span>
                                </div>
                                <p className="font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground">ATS Score</p>
                              </div>
                              <dl className="grid grid-cols-3 gap-2 sm:justify-self-end">
                                {([
                                  ["Keyword", ats.keyword_score],
                                  ["Readability", ats.readability_score],
                                  ["Format", ats.format_score],
                                ] as const).map(([label, value]) => (
                                  <div key={label} className="rounded-xl bg-card px-3 py-2 text-center ring-1 ring-foreground/[0.06] dark:bg-white/[0.03] dark:ring-white/10">
                                    <dt className="text-[11px] text-muted-foreground">{label}</dt>
                                    <dd className="mt-0.5 font-geist text-base font-semibold tabular-nums text-foreground">{value ?? "—"}</dd>
                                  </div>
                                ))}
                              </dl>
                            </Bezel>
                          ) : null}
                          {ats && ats.missing_keywords ? (
                            <ul className="flex flex-wrap gap-1.5" aria-label="Missing keywords">
                              {ats.missing_keywords?.slice(0, 8).map((kw: string) => (
                                <li
                                  key={kw}
                                  className="rounded-full bg-foreground/[0.04] px-2.5 py-1 font-geist-mono text-[11px] text-muted-foreground ring-1 ring-foreground/[0.08] dark:bg-white/[0.04] dark:ring-white/10"
                                >
                                  + {kw}
                                </li>
                              ))}
                            </ul>
                          ) : null}
                          <BlockLabel
                            action={
                              <EditToggle
                                editing={editing}
                                onClick={() => {
                                  if (!editing && editedText === null) setEditedText(String(action.resume_markdown ?? action.resume_text ?? ""));
                                  setEditing(!editing);
                                }}
                              />
                            }
                          >
                            Resume Draft
                          </BlockLabel>
                          {editing ? (
                            <div className="space-y-1.5">
                              <Textarea
                                aria-label="Edit the resume draft"
                                value={editedText ?? ""}
                                onChange={(e) => setEditedText(e.target.value)}
                                className="min-h-48 font-geist-mono text-sm"
                                placeholder="Edit the resume draft..."
                              />
                              <div className="flex justify-end pr-1">
                                <CharCount current={(editedText ?? "").length} max={5000} />
                              </div>
                            </div>
                          ) : (
                            <Recessed mono className="max-h-80">
                              {approvalContent(String(action.resume_markdown ?? action.resume_text ?? "—"), editedText)}
                            </Recessed>
                          )}
                        </div>
                      );
                    })()}

                    {/* LinkedIn edits */}
                    {actionType === "linkedin_edits" && (
                      <div className="grid grid-cols-1 gap-4">
                        <div className="space-y-1.5">
                          <BlockLabel>Headline</BlockLabel>
                          <Recessed className="font-medium">{String(action.headline ?? "—")}</Recessed>
                        </div>
                        <div className="space-y-1.5">
                          <BlockLabel>About</BlockLabel>
                          <Recessed className="max-h-72">{String(action.about ?? "—")}</Recessed>
                        </div>
                      </div>
                    )}

                    {/* Cover letter */}
                    {actionType === "cover_letter_review" && (
                      <div className="space-y-3">
                        <BlockLabel
                          action={
                            <EditToggle
                              editing={editing}
                              onClick={() => {
                                if (!editing && editedText === null) setEditedText(String(action.body ?? ""));
                                setEditing(!editing);
                              }}
                            />
                          }
                        >
                          Cover Letter
                        </BlockLabel>
                        {editing ? (
                          <div className="space-y-1.5">
                            <Textarea
                              aria-label="Edit the cover letter"
                              value={editedText ?? ""}
                              onChange={(e) => setEditedText(e.target.value)}
                              className="min-h-48 font-geist-mono text-sm"
                            />
                            <div className="flex justify-end pr-1">
                              <CharCount current={(editedText ?? "").length} max={1500} />
                            </div>
                          </div>
                        ) : (
                          <Recessed className="max-h-64">{approvalContent(String(action.body ?? "—"), editedText)}</Recessed>
                        )}
                      </div>
                    )}

                    {/* LinkedIn outreach drafts */}
                    {actionType === "linkedin_outreach" && (
                      <ul className="space-y-3">
                        {(Array.isArray(action.messages) ? action.messages : []).map((draft, index) => {
                          const item = draft as Record<string, unknown>;
                          const name = String(item.contact_name ?? "Unknown contact");
                          return (
                            <li key={`${String(item.contact_name ?? "contact")}-${index}`}>
                              <Bezel size="md" coreClassName="space-y-3 p-4">
                                <div className="flex items-center gap-3">
                                  <span
                                    aria-hidden
                                    className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-primary/10 font-geist text-sm font-semibold text-primary ring-1 ring-primary/20"
                                  >
                                    {name.trim().charAt(0).toUpperCase() || "?"}
                                  </span>
                                  <div className="min-w-0">
                                    <p className="truncate font-medium text-foreground">{name}</p>
                                    <p className="truncate text-xs text-muted-foreground">{String(item.contact_title ?? "")}</p>
                                  </div>
                                </div>
                                <p className="whitespace-pre-wrap text-sm leading-6 text-foreground">{String(item.message ?? "—")}</p>
                              </Bezel>
                            </li>
                          );
                        })}
                      </ul>
                    )}

                    {/* Salary report */}
                    {actionType === "salary_report_review" && (() => {
                      const report = (action.report ?? {}) as Record<string, unknown>;
                      const script = (action.script ?? {}) as Record<string, unknown>;
                      return (
                        <div className="space-y-4">
                          <div className="grid grid-cols-3 gap-2 md:gap-3">
                            {["p25", "p50", "p75"].map((key) => (
                              <Bezel
                                key={key}
                                size="md"
                                tone={key === "p50" ? "primary" : "default"}
                                coreClassName="px-3 py-4 text-center"
                              >
                                <p className="font-geist-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground">{key}</p>
                                <p className="mt-1.5 break-words font-geist text-lg font-semibold tabular-nums tracking-[-0.03em] text-foreground md:text-xl">
                                  {String(report[key] ?? "—")}
                                </p>
                              </Bezel>
                            ))}
                          </div>
                          <div className="space-y-1.5">
                            <BlockLabel>Negotiation Script</BlockLabel>
                            <Recessed className="max-h-72 space-y-2">
                              {Object.entries(script).map(([key, value]) => (
                                <p key={key} className="whitespace-pre-wrap"><span className="font-medium capitalize">{key.replace(/_/g, " ")}:</span> {typeof value === "object" ? JSON.stringify(value) : String(value)}</p>
                              ))}
                            </Recessed>
                          </div>
                        </div>
                      );
                    })()}

                    {/* Search confirmation (NL Search) */}
                    {actionType === "search_confirmation" && (
                      <div className="space-y-3">
                        <p className="text-sm leading-6 text-muted-foreground">
                          We interpreted your search as:
                          <span className="ml-2 font-medium text-foreground">
                            {String(action.original_query ?? "—")}
                          </span>
                        </p>
                        {action.interpretation ? (
                          <Recessed mono className="max-h-72">
                            <pre className="whitespace-pre-wrap font-geist-mono text-xs">
                              {JSON.stringify(action.interpretation, null, 2)}
                            </pre>
                          </Recessed>
                        ) : null}
                      </div>
                    )}

                    {/* Generic fallback for unknown action types */}
                    {actionType === "computer_action" && (() => {
                      const step = action.action as { operation?: string; parameters?: { url?: string; text?: string; deltaY?: number } } | undefined;
                      return <div className="space-y-3">
                        <p className="font-medium">{String(action.summary ?? "Review browser action")}</p>
                        <p className="break-all text-sm text-muted-foreground">Page: {String(action.page_url ?? "Current browser page")}</p>
                        <Recessed>
                          <p>Action: {step?.operation}</p>
                          {action.target ? <p>Target: {String(action.target)}</p> : null}
                          {step?.parameters?.url ? <p>Open: {step.parameters.url}</p> : null}
                          {step?.parameters?.text !== undefined ? <p>Enter: {step.parameters.text}</p> : null}
                          {step?.parameters?.deltaY !== undefined ? <p>Scroll: {step.parameters.deltaY} pixels</p> : null}
                        </Recessed>
                        <p className="text-sm text-muted-foreground">Check the live computer view before approving.</p>
                      </div>;
                    })()}
                    {!SPECIALISED_TYPES.includes(actionType) && (
                      <Recessed mono className="max-h-64">
                        <pre className="whitespace-pre-wrap font-geist-mono text-xs text-foreground">
                          {JSON.stringify(action, null, 2)}
                        </pre>
                      </Recessed>
                    )}
                  </div>

                  <Hairline />

                  {/* Decision bar — the only path to executing the action */}
                  <footer className="flex flex-col gap-4 px-5 py-4 md:flex-row md:items-center md:px-7 md:py-5">
                    <p className="flex items-center gap-2 text-xs leading-5 text-muted-foreground md:mr-auto">
                      <ShieldCheck aria-hidden size={15} weight="light" className="shrink-0 text-primary" />
                      Nothing runs until you approve.
                    </p>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_2fr] md:flex md:items-center">
                      <IslandButton
                        tone="ghost"
                        onClick={() => decide(false)}
                        disabled={loading}
                        className="order-2 w-full sm:order-1 md:w-auto"
                      >
                        Cancel
                      </IslandButton>
                      <IslandButton
                        tone="primary"
                        onClick={() => decide(true)}
                        disabled={loading}
                        aria-busy={loading}
                        className="order-1 w-full sm:order-2 md:w-auto"
                        trailing={
                          loading
                            ? <CircleNotch size={15} weight="light" className="animate-spin motion-reduce:animate-none" />
                            : <Check size={15} weight="light" />
                        }
                      >
                        {approveLabel}
                      </IslandButton>
                    </div>
                  </footer>
                </Bezel>
              </motion.div>
            </DialogPrimitive.Content>
          </motion.div>
        </DialogPrimitive.Overlay>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
