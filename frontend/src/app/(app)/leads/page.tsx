"use client";
import * as DialogPrimitive from "@radix-ui/react-dialog";

import { useRef, useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence, useReducedMotion, type Variants } from "motion/react";
import {
  AddressBook,
  ArrowSquareOut,
  Buildings,
  CalendarBlank,
  CaretRight,
  CircleNotch,
  EnvelopeSimple,
  FileCsv,
  IdentificationCard,
  LinkedinLogo,
  MagnifyingGlass,
  Plus,
  X,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { parseCsv } from "@/lib/csv";
import { apiClient } from "@/lib/api";
import {
  Bezel,
  EASE_OUT_EXPO,
  EmptyPanel,
  Field,
  Hairline,
  IconButton,
  Input,
  IslandButton,
  PageHero,
  PanelTitle,
  Reveal,
  Screen,
  Section,
  Segmented,
  StatStrip,
  StatusPill,
  Textarea,
  panelSwap,
  type StatusTone,
} from "@/components/vanguard";

interface Lead {
  id: string;
  name: string | null;
  email: string | null;
  company: string | null;
  linkedin_url: string | null;
  status: string;
  last_contact: string | null;
  notes: string | null;
}

type UIStatus = "New" | "Contacted" | "Replied" | "Cold";
type StatusFilter = "all" | UIStatus;

const STATUS_MAP: Record<string, UIStatus> = {
  cold: "Cold",
  warm: "Contacted",
  hot: "Replied",
  contacted: "Contacted",
  replied: "Replied",
  closed: "Cold",
};

const STATUS_TONE: Record<UIStatus, StatusTone> = {
  New: "primary",
  Contacted: "warning",
  Replied: "success",
  Cold: "neutral",
};

const ACTION_LABEL: Record<UIStatus, string> = {
  New: "Reply",
  Contacted: "Follow up",
  Replied: "Schedule",
  Cold: "Reach out",
};

function getInitials(name: string | null): string {
  if (!name) return "?";
  return name.split(" ").map((p) => p[0]).join("").toUpperCase().slice(0, 2);
}

function relativeTime(iso: string | null): string {
  if (!iso) return "Never";
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3600000);
  if (h < 1) return "Just now";
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d === 1) return "Yesterday";
  if (d < 7) return `${d}d ago`;
  return `${Math.floor(d / 7)}w ago`;
}

/** Row entrance: light fade-up, staggered by index (capped so long lists don't lag). */
const rowIn: Variants = {
  hidden: { opacity: 0, y: 14 },
  show: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: 0.6, ease: EASE_OUT_EXPO, delay: Math.min(i, 12) * 0.035 },
  }),
};

/** Inline shimmer that is valid inside <p> (Stat renders its value in a paragraph). */
function InlineShimmer({ className }: { className?: string }) {
  return <span aria-hidden className={cn("shimmer inline-block rounded-xl align-middle", className)} />;
}

/** Squircle monogram avatar. */
function Monogram({ name, size = "md", active = false }: { name: string | null; size?: "md" | "lg"; active?: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "grid shrink-0 place-items-center font-geist font-semibold tracking-[-0.02em] ring-1 transition-colors duration-500 ease-vanguard",
        size === "lg" ? "h-16 w-16 rounded-[1.25rem] text-lg" : "h-11 w-11 rounded-[0.9rem] text-[13px]",
        active
          ? "bg-primary/10 text-primary ring-primary/20"
          : "bg-foreground/[0.04] text-foreground/80 ring-foreground/[0.07] dark:bg-white/[0.05] dark:ring-white/10",
      )}
    >
      {getInitials(name)}
    </span>
  );
}

/** Closes the surrounding surface on Escape. */
/** Fixed overlay shell shared by the add-lead and (mobile) detail dialogs. */
function DialogShell({
  labelledBy,
  onClose,
  className,
  children,
}: {
  labelledBy: string;
  onClose: () => void;
  className?: string;
  children: ReactNode;
}) {
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-background/70 backdrop-blur-md" />
        <DialogPrimitive.Content aria-labelledby={labelledBy} aria-describedby={undefined} className={cn("fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 outline-none", className)}>
          <DialogPrimitive.Title className="sr-only">{labelledBy === "add-lead-title" ? "Add lead" : "Lead details"}</DialogPrimitive.Title>
          <Bezel lifted coreClassName="max-h-[calc(100dvh-3rem)] overflow-y-auto p-6 md:p-8">{children}</Bezel>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

interface AddLeadForm {
  name: string;
  company: string;
  email: string;
  linkedin_url: string;
  notes: string;
}

function AddLeadModal({ onClose, onAdd }: { onClose: () => void; onAdd: (lead: Lead) => void }) {
  const [form, setForm] = useState<AddLeadForm>({
    name: "", company: "", email: "", linkedin_url: "", notes: "",
  });
  const [saving, setSaving] = useState(false);

  const set = (k: keyof AddLeadForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.name.trim()) {
      toast.error("Name is required");
      return;
    }
    setSaving(true);
    try {
      const { data } = await apiClient.post("/leads", {
        name: form.name,
        company: form.company || null,
        email: form.email || null,
        linkedin_url: form.linkedin_url || null,
        notes: form.notes || null,
        status: "cold",
      });
      onAdd(data as Lead);
      toast.success(`Lead "${form.name}" added`);
      onClose();
    } catch {
      toast.error("Could not add lead — backend did not save it");
      // Re-enable the submit button so the user can retry after a failure.
      setSaving(false);
    }
  };

  return (
    <DialogShell labelledBy="add-lead-title" onClose={onClose}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-muted-foreground">New contact</p>
          <h2 id="add-lead-title" className="mt-2 font-geist text-2xl font-semibold tracking-[-0.03em] text-foreground">
            Add lead
          </h2>
        </div>
        <IconButton aria-label="Close" onClick={onClose}>
          <X size={16} weight="light" />
        </IconButton>
      </div>

      <form onSubmit={handleSubmit} className="mt-7 space-y-5" noValidate>
        <Field label="Name *">
          {(id) => (
            <Input
              id={id}
              name="name"
              autoFocus
              autoComplete="off"
              aria-required="true"
              value={form.name}
              onChange={set("name")}
              placeholder="Sarah Chen"
            />
          )}
        </Field>
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
          <Field label="Company">
            {(id) => (
              <Input id={id} name="company" autoComplete="off" value={form.company} onChange={set("company")} placeholder="Acme Corp" />
            )}
          </Field>
          <Field label="Email">
            {(id) => (
              <Input
                id={id}
                name="email"
                type="email"
                autoComplete="off"
                value={form.email}
                onChange={set("email")}
                placeholder="sarah@acme.com"
              />
            )}
          </Field>
        </div>
        <Field label="LinkedIn URL">
          {(id) => (
            <Input
              id={id}
              name="linkedin_url"
              autoComplete="off"
              value={form.linkedin_url}
              onChange={set("linkedin_url")}
              placeholder="linkedin.com/in/sarahchen"
            />
          )}
        </Field>
        <Field label="Notes">
          {(id) => (
            <Textarea
              id={id}
              name="notes"
              value={form.notes}
              onChange={set("notes")}
              placeholder="How you found them, context…"
              rows={2}
              className="min-h-20 resize-none"
            />
          )}
        </Field>

        <Hairline />

        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:items-center sm:justify-end">
          <IslandButton tone="quiet" size="md" onClick={onClose}>
            Cancel
          </IslandButton>
          <IslandButton
            type="submit"
            tone="primary"
            size="md"
            disabled={saving}
            icon={saving ? <CircleNotch size={16} weight="light" className="animate-spin" /> : <Plus size={16} weight="light" />}
          >
            {saving ? "Saving…" : "Add lead"}
          </IslandButton>
        </div>
      </form>
    </DialogShell>
  );
}

/** Detail content shared by the lg side panel and the mobile/tablet dialog. */
function LeadDetailBody({ lead, onClose, titleId }: { lead: Lead; onClose: () => void; titleId: string }) {
  const uiStatus: UIStatus = STATUS_MAP[lead.status] ?? "Cold";
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-start justify-between gap-4">
        <Monogram name={lead.name} size="lg" active />
        <IconButton aria-label="Close details" onClick={onClose}>
          <X size={16} weight="light" />
        </IconButton>
      </div>

      <div className="mt-6 min-w-0">
        <h2 id={titleId} className="truncate font-geist text-2xl font-semibold tracking-[-0.03em] text-foreground">
          {lead.name ?? "Unknown"}
        </h2>
        <p className="mt-1.5 flex items-center gap-1.5 truncate text-sm text-muted-foreground">
          <Buildings size={14} weight="light" aria-hidden />
          {lead.company ?? "No company"}
        </p>
      </div>

      <dl className="mt-7 divide-y divide-foreground/[0.07] rounded-[1.25rem] bg-foreground/[0.02] px-4 ring-1 ring-foreground/[0.06] dark:divide-white/[0.07] dark:bg-white/[0.02] dark:ring-white/10">
        <div className="flex items-center justify-between gap-4 py-3.5">
          <dt className="text-xs text-muted-foreground">Status</dt>
          <dd>
            <StatusPill tone={STATUS_TONE[uiStatus]}>{uiStatus}</StatusPill>
          </dd>
        </div>
        <div className="flex items-center justify-between gap-4 py-3.5">
          <dt className="text-xs text-muted-foreground">Last contact</dt>
          <dd className="font-geist-mono text-[13px] tabular-nums text-foreground">{relativeTime(lead.last_contact)}</dd>
        </div>
        {lead.email && (
          <div className="flex items-center justify-between gap-4 py-3.5">
            <dt className="shrink-0 text-xs text-muted-foreground">Email</dt>
            <dd className="min-w-0">
              <a
                href={`mailto:${lead.email}`}
                className="block truncate text-sm text-primary underline-offset-4 transition-colors duration-500 ease-vanguard hover:underline focus-visible:rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={(e) => e.stopPropagation()}
              >
                {lead.email}
              </a>
            </dd>
          </div>
        )}
        {lead.linkedin_url && (
          <div className="flex items-center justify-between gap-4 py-3.5">
            <dt className="text-xs text-muted-foreground">LinkedIn</dt>
            <dd>
              <a
                href={lead.linkedin_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm text-primary underline-offset-4 transition-colors duration-500 ease-vanguard hover:underline focus-visible:rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={(e) => e.stopPropagation()}
              >
                View profile <ArrowSquareOut size={13} weight="light" aria-hidden />
              </a>
            </dd>
          </div>
        )}
      </dl>

      {lead.notes && (
        <div className="mt-5">
          <p className="pl-1 text-xs text-muted-foreground">Notes</p>
          <p className="mt-2 max-w-[60ch] whitespace-pre-line text-pretty rounded-[1.25rem] bg-foreground/[0.02] px-4 py-3.5 text-sm leading-6 text-foreground ring-1 ring-foreground/[0.06] dark:bg-white/[0.02] dark:ring-white/10">
            {lead.notes}
          </p>
        </div>
      )}

      <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:items-center">
        <IslandButton
          tone="primary"
          size="md"
          className="sm:flex-1"
          icon={<EnvelopeSimple size={16} weight="light" />}
          trailing
          onClick={() => {
            toast.info("Open Email page to draft a follow-up");
            onClose();
          }}
        >
          Send email
        </IslandButton>
        <IslandButton tone="ghost" size="md" onClick={onClose}>
          Close
        </IslandButton>
      </div>
    </div>
  );
}

/** Mobile/tablet detail dialog. Hidden on lg+, where the side panel shows the same content. */
function LeadDetailModal({ lead, onClose }: { lead: Lead; onClose: () => void }) {
  return (
    <DialogShell labelledBy="lead-detail-dialog-title" onClose={onClose} className="lg:hidden">
      <LeadDetailBody lead={lead} onClose={onClose} titleId="lead-detail-dialog-title" />
    </DialogShell>
  );
}

/** One directory row. On mobile it reads as a stacked card; from md it is a single aligned row. */
function LeadRow({
  lead,
  index,
  selected,
  mutating,
  onSelect,
  onAction,
}: {
  lead: Lead;
  index: number;
  selected: boolean;
  mutating: boolean;
  onSelect: () => void;
  onAction: () => void;
}) {
  const reduce = useReducedMotion();
  const uiStatus: UIStatus = STATUS_MAP[lead.status] ?? "Cold";
  const ActionIcon = uiStatus === "Replied" ? CalendarBlank : EnvelopeSimple;

  return (
    <motion.li custom={index} variants={rowIn} initial={reduce ? false : "hidden"} animate="show" className="lead-row-container min-w-0">
      <Bezel
        size="md"
        tone={selected ? "primary" : "default"}
        data-testid="lead-row"
        className="transition-[background-color,box-shadow] duration-500 ease-vanguard hover:ring-foreground/[0.12] dark:hover:ring-white/[0.14]"
        coreClassName={cn(
          "lead-row-grid grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-3 p-3",
        )}
      >
        {/* Identity — opens the detail panel */}
        <button
          type="button"
          onClick={onSelect}
          aria-pressed={selected}
          className="group/id -m-1 flex min-w-0 items-center gap-3.5 rounded-[1rem] p-1 text-left transition-colors duration-500 ease-vanguard focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Monogram name={lead.name} active={selected} />
          <span className="min-w-0">
            <span className="block truncate font-medium tracking-[-0.01em] text-foreground transition-colors duration-500 ease-vanguard group-hover/id:text-primary">
              {lead.name ?? "Unknown"}
            </span>
            <span className="mt-0.5 flex items-center gap-1.5 truncate text-[13px] text-muted-foreground">
              <Buildings size={13} weight="light" aria-hidden className="shrink-0" />
              <span className="truncate">{lead.company ?? "No company"}</span>
            </span>
          </span>
        </button>

        {/* Status — sits beside the name on mobile, before the action on md+ */}
        <div className="lead-row-status justify-self-end">
          <StatusPill tone={STATUS_TONE[uiStatus]}>{uiStatus}</StatusPill>
        </div>

        {/* Contact meta */}
        <div className="lead-row-contact col-span-2 flex min-w-0 flex-wrap items-center justify-between gap-2 rounded-[0.9rem] bg-foreground/[0.025] px-3 py-2.5 ring-1 ring-foreground/[0.05] dark:bg-white/[0.03] dark:ring-white/[0.08]">
          <div className="flex min-w-0 items-center gap-1.5 text-[13px]">
            <EnvelopeSimple size={13} weight="light" aria-hidden className="shrink-0 text-muted-foreground" />
            {lead.email ? (
              <a
                href={`mailto:${lead.email}`}
                className="truncate text-foreground/85 underline-offset-4 transition-colors duration-500 ease-vanguard hover:text-primary hover:underline focus-visible:rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {lead.email}
              </a>
            ) : (
              <span className="truncate text-muted-foreground/70">No email</span>
            )}
            {lead.linkedin_url ? (
              <a
                href={lead.linkedin_url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`LinkedIn profile for ${lead.name ?? "this contact"}`}
                className="ml-1 grid h-6 w-6 shrink-0 place-items-center rounded-full text-muted-foreground transition-colors duration-500 ease-vanguard hover:bg-foreground/[0.05] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:hover:bg-white/[0.06]"
              >
                <LinkedinLogo size={14} weight="light" />
              </a>
            ) : null}
          </div>
          <p className="lead-row-time shrink-0 font-geist-mono text-[11px] tabular-nums text-muted-foreground">
            <span className="sr-only">Last contact: </span>
            {relativeTime(lead.last_contact)}
          </p>
        </div>

        {/* Next-step action (status update) */}
        <IslandButton
          tone="ghost"
          size="sm"
          className="lead-row-action col-span-2 w-full"
          disabled={mutating}
          icon={
            mutating ? (
              <CircleNotch size={14} weight="light" className="animate-spin" />
            ) : (
              <ActionIcon size={14} weight="light" />
            )
          }
          trailing={<CaretRight size={12} weight="light" />}
          onClick={(e) => {
            e.stopPropagation();
            onAction();
          }}
        >
          {ACTION_LABEL[uiStatus]}
        </IslandButton>
      </Bezel>
    </motion.li>
  );
}

function RowSkeleton() {
  return (
    <Bezel size="md" coreClassName="flex items-center gap-4 p-4 md:py-3 md:pl-3">
      <div aria-hidden className="shimmer h-11 w-11 shrink-0 rounded-[0.9rem]" />
      <div className="min-w-0 flex-1 space-y-2">
        <div aria-hidden className="shimmer h-3.5 w-36 rounded-full" />
        <div aria-hidden className="shimmer h-3 w-24 rounded-full" />
      </div>
      <div aria-hidden className="shimmer hidden h-3 w-40 rounded-full md:block" />
      <div aria-hidden className="shimmer hidden h-6 w-20 rounded-full sm:block" />
      <div aria-hidden className="shimmer h-9 w-28 rounded-full" />
    </Bezel>
  );
}

export default function LeadsPage() {
  const qc = useQueryClient();
  const reduce = useReducedMotion();
  const [addOpen, setAddOpen] = useState(false);
  const [mutatingLeadId, setMutatingLeadId] = useState<string | null>(null);
  const [selectedLead, setSelectedLead] = useState<Lead | null>(null);
  const [importing, setImporting] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleImportCsv = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-importing the same file
    if (!file) return;
    setImporting(true);
    try {
      const text = await file.text();
      const rows = parseCsv(text);
      if (rows.length === 0) {
        toast.error("CSV is empty");
        return;
      }
      // Detect + skip a header row (name,email,company,linkedin_url)
      const header = rows[0].map((cell) => cell.toLowerCase());
      const dataRows = header.includes("name") && header.includes("email") ? rows.slice(1) : rows;

      let ok = 0;
      for (const row of dataRows) {
        const [name, email, company, linkedin_url] = row.map((c) => c.trim());
        if (!name) continue;
        try {
          await apiClient.post("/leads", {
            name,
            email: email || null,
            company: company || null,
            linkedin_url: linkedin_url || null,
            status: "cold",
          });
          ok++;
        } catch {
          // skip malformed row, keep going
        }
      }
      qc.invalidateQueries({ queryKey: ["leads"] });
      toast.success(`Imported ${ok} lead${ok === 1 ? "" : "s"} from CSV`);
    } catch {
      toast.error("Could not read CSV file");
    } finally {
      setImporting(false);
    }
  };

  const { data: remoteLeads = [], isLoading } = useQuery<Lead[]>({
    queryKey: ["leads"],
    queryFn: async () => {
      const { data } = await apiClient.get("/leads");
      return data;
    },
  });

  const leads = remoteLeads;

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      apiClient.patch(`/leads/${id}/status`, { status }),
    onMutate: ({ id }) => setMutatingLeadId(id),
    onSuccess: (_response, { id, status }) => {
      qc.setQueryData<Lead[]>(["leads"], (old) => old?.map((lead) => lead.id === id ? { ...lead, status } : lead));
      setSelectedLead((lead) => lead?.id === id ? { ...lead, status } : lead);
      setMutatingLeadId(null);
      toast.success("Lead status updated");
      qc.invalidateQueries({ queryKey: ["leads"] });
    },
    onError: () => {
      setMutatingLeadId(null);
      toast.error("Failed to update status");
    },
  });

  const contactedCount = leads.filter((l) => l.status === "contacted" || l.status === "warm").length;
  const repliedCount = leads.filter((l) => l.status === "replied" || l.status === "hot").length;
  const replyRate = leads.length > 0 ? ((repliedCount / leads.length) * 100).toFixed(1) : "0.0";

  // Presentation-only: client-side search + status filter over the same list.
  const statusCounts: Record<UIStatus, number> = { New: 0, Contacted: 0, Replied: 0, Cold: 0 };
  for (const l of leads) statusCounts[STATUS_MAP[l.status] ?? "Cold"] += 1;

  const query = search.trim().toLowerCase();
  const visibleLeads = leads.filter((l) => {
    if (statusFilter !== "all" && (STATUS_MAP[l.status] ?? "Cold") !== statusFilter) return false;
    if (!query) return true;
    return [l.name, l.company, l.email].some((v) => v?.toLowerCase().includes(query));
  });
  const filtersActive = statusFilter !== "all" || query.length > 0;

  // Keep the open detail in sync with the latest server data (status changes etc.).
  const activeLead = selectedLead ? leads.find((l) => l.id === selectedLead.id) ?? selectedLead : null;

  const filterOptions: ReadonlyArray<{ value: StatusFilter; label: string; count?: number }> = [
    { value: "all", label: "All", count: isLoading ? undefined : leads.length },
    ...(statusCounts.New > 0 ? [{ value: "New" as const, label: "New", count: statusCounts.New }] : []),
    { value: "Cold", label: "Cold", count: isLoading ? undefined : statusCounts.Cold },
    { value: "Contacted", label: "Contacted", count: isLoading ? undefined : statusCounts.Contacted },
    { value: "Replied", label: "Replied", count: isLoading ? undefined : statusCounts.Replied },
  ];

  return (
    <>
      <Screen>
        <PageHero
          eyebrow="Leads CRM"
          title="Recruiter Contacts"
          accent="Every warm lead, one directory."
          description="Add recruiters by hand or import a CSV, then move each contact from first touch to reply. Status changes save straight to your account."
          actions={
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv,text/csv"
                onChange={handleImportCsv}
                className="hidden"
                aria-hidden
                tabIndex={-1}
              />
              <IslandButton
                tone="primary"
                size="lg"
                trailing={<Plus size={17} weight="light" />}
                onClick={() => setAddOpen(true)}
              >
                Add lead
              </IslandButton>
              <IslandButton
                tone="ghost"
                size="lg"
                onClick={() => fileInputRef.current?.click()}
                disabled={importing}
                aria-busy={importing}
                icon={
                  importing ? (
                    <CircleNotch size={17} weight="light" className="animate-spin" />
                  ) : (
                    <FileCsv size={17} weight="light" />
                  )
                }
              >
                {importing ? "Importing…" : "Import CSV"}
              </IslandButton>
            </>
          }
          aside={
            <div aria-live="polite" aria-busy={isLoading}>
              <StatStrip
                items={[
                  {
                    label: "Total leads",
                    value: isLoading ? <InlineShimmer className="h-10 w-12 md:h-12" /> : leads.length,
                  },
                  {
                    label: "Contacted",
                    value: isLoading ? <InlineShimmer className="h-10 w-10 md:h-12" /> : contactedCount,
                  },
                  {
                    label: "Replied",
                    value: isLoading ? <InlineShimmer className="h-10 w-10 md:h-12" /> : repliedCount,
                    hint: isLoading ? "Loading…" : <span className="text-success tabular-nums">{replyRate}% reply rate</span>,
                  },
                ]}
              />
            </div>
          }
        />

        {/* Hero already carries pb-12/md:pb-20; a small top margin keeps the directory in the first laptop viewport. */}
        <Section aria-label="Contact directory" className="!mt-4 md:!mt-6">
          {/* Filter / search island */}
          <Reveal>
            <Bezel size="md" coreClassName="flex flex-col gap-2 p-2 md:flex-row md:items-center md:gap-3">
              <Input
                type="search"
                name="lead-search"
                aria-label="Search contacts"
                placeholder="Search by name, company or email"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                leading={<MagnifyingGlass size={16} weight="light" />}
                trayClassName="md:flex-1 md:max-w-md"
              />
              <div className="flex min-w-0 items-center justify-between gap-3 md:flex-1 md:justify-end">
                <Segmented<StatusFilter>
                  value={statusFilter}
                  onChange={setStatusFilter}
                  options={filterOptions}
                  asTabs={false}
                  ariaLabel="Filter contacts by status"
                  size="sm"
                />
                <p aria-live="polite" className="hidden shrink-0 pr-3 font-geist-mono text-[11px] tabular-nums text-muted-foreground lg:block">
                  {isLoading ? "—" : `${visibleLeads.length} / ${leads.length}`}
                </p>
              </div>
            </Bezel>
          </Reveal>

          {/* Directory + detail panel */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-12 lg:items-start">
            <div className="min-w-0 lg:col-span-8">
              <div className="mb-4 flex items-center justify-between px-1">
                <PanelTitle
                  title="All contacts"
                  icon={<AddressBook size={15} weight="light" />}
                />
                <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground lg:hidden" aria-hidden>
                  {isLoading ? "" : `${visibleLeads.length} / ${leads.length}`}
                </span>
              </div>

              {isLoading ? (
                <div className="space-y-3" aria-busy="true" aria-label="Loading contacts">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <RowSkeleton key={i} />
                  ))}
                </div>
              ) : leads.length === 0 ? (
                <Reveal>
                  <Bezel tone="muted">
                    <EmptyPanel
                      icon={<AddressBook size={24} weight="light" />}
                      title="No leads yet"
                      description="Add contacts manually or import a CSV with name, email, company and linkedin_url columns."
                      action={
                        <IslandButton tone="primary" size="md" icon={<Plus size={15} weight="light" />} onClick={() => setAddOpen(true)}>
                          Add your first contact
                        </IslandButton>
                      }
                    />
                  </Bezel>
                </Reveal>
              ) : visibleLeads.length === 0 ? (
                <Bezel tone="muted">
                  <EmptyPanel
                    compact
                    icon={<MagnifyingGlass size={22} weight="light" />}
                    title="No contacts match"
                    description="Try a different name, company or email, or clear the status filter."
                    action={
                      <IslandButton
                        tone="ghost"
                        size="sm"
                        onClick={() => {
                          setSearch("");
                          setStatusFilter("all");
                        }}
                      >
                        Clear filters
                      </IslandButton>
                    }
                  />
                </Bezel>
              ) : (
                <ul className="space-y-3" aria-label="Recruiter contacts">
                  {visibleLeads.map((lead, i) => {
                    const uiStatus: UIStatus = STATUS_MAP[lead.status] ?? "Cold";
                    return (
                      <LeadRow
                        key={lead.id}
                        lead={lead}
                        index={i}
                        selected={activeLead?.id === lead.id}
                        mutating={mutatingLeadId === lead.id}
                        onSelect={() => setSelectedLead((cur) => (cur?.id === lead.id ? null : lead))}
                        onAction={() =>
                          statusMutation.mutate({
                            id: lead.id,
                            status: uiStatus === "Replied" ? "replied" : "contacted",
                          })
                        }
                      />
                    );
                  })}
                </ul>
              )}

              {filtersActive && !isLoading && visibleLeads.length > 0 ? (
                <p className="mt-4 px-1 text-xs text-muted-foreground">
                  Showing {visibleLeads.length} of {leads.length} contacts.{" "}
                  <button
                    type="button"
                    onClick={() => {
                      setSearch("");
                      setStatusFilter("all");
                    }}
                    className="font-medium text-foreground underline-offset-4 transition-colors duration-500 ease-vanguard hover:text-primary hover:underline focus-visible:rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Clear filters
                  </button>
                </p>
              ) : null}
            </div>

            {/* lg+ detail panel */}
            <aside aria-label="Contact details" className="hidden lg:sticky lg:top-8 lg:col-span-4 lg:block">
              <Reveal delay={0.08}>
                <Bezel lifted tone={activeLead ? "default" : "muted"} coreClassName="min-h-[26rem] p-6 xl:p-7">
                  <AnimatePresence mode="wait" initial={false}>
                    {activeLead ? (
                      <motion.div
                        key={activeLead.id}
                        variants={panelSwap}
                        initial={reduce ? false : "hidden"}
                        animate="show"
                        exit="exit"
                        className="h-full"
                      >
                        <LeadDetailBody lead={activeLead} onClose={() => setSelectedLead(null)} titleId="lead-detail-panel-title" />
                      </motion.div>
                    ) : (
                      <motion.div
                        key="empty"
                        variants={panelSwap}
                        initial={reduce ? false : "hidden"}
                        animate="show"
                        exit="exit"
                      >
                        <EmptyPanel
                          icon={<IdentificationCard size={24} weight="light" />}
                          title="Select a contact"
                          description="Pick anyone in the directory to see their status, last touchpoint, links and notes."
                        />
                      </motion.div>
                    )}
                  </AnimatePresence>
                </Bezel>
              </Reveal>
            </aside>
          </div>
        </Section>
      </Screen>

      <AnimatePresence>
        {addOpen && (
          <AddLeadModal
            key="add-lead"
            onClose={() => setAddOpen(false)}
            onAdd={(lead) => { qc.setQueryData<Lead[]>(["leads"], (old = []) => [lead, ...old]); void qc.invalidateQueries({ queryKey: ["leads"] }); }}
          />
        )}
        {activeLead && (
          <LeadDetailModal key="lead-detail" lead={activeLead} onClose={() => setSelectedLead(null)} />
        )}
      </AnimatePresence>
    </>
  );
}
