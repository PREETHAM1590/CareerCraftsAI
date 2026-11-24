"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CaretDown, Copy, EnvelopeSimple, Trash } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { IslandButton } from "@/components/vanguard";

type Address = { configured: boolean; address: string | null };
type InboxMessage = {
  id: string;
  sender: string;
  subject: string;
  received_at: string | null;
  excerpt: string;
  codes: string[];
  links: string[];
  filled_for_nova: boolean;
};

async function copy(value: string, what: string) {
  try {
    await navigator.clipboard.writeText(value);
    toast.success(`${what} copied`);
  } catch {
    toast.error(`Copy failed. Select the ${what.toLowerCase()} and copy it.`);
  }
}

/** The member's application email: where portals send codes and confirmations. */
export function ApplyInbox() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const address = useQuery<Address>({
    queryKey: ["apply-email"],
    queryFn: async () => (await apiClient.get("/apply-email")).data,
    staleTime: 5 * 60_000,
  });
  const messages = useQuery<InboxMessage[]>({
    queryKey: ["apply-email-messages"],
    queryFn: async () => (await apiClient.get("/apply-email/messages")).data,
    enabled: open && Boolean(address.data?.configured),
    // Portal codes arrive within seconds; refresh while the inbox is open.
    refetchInterval: open ? 15_000 : false,
  });

  if (!address.data?.configured) return null;
  const items = messages.data ?? [];

  return (
    <details
      className="workspace-disclosure rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]"
      onToggle={e => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium">
        <EnvelopeSimple size={18} weight="light" /> Application email
        <span className="ml-auto text-xs font-normal text-muted-foreground">{open ? `${items.length} messages` : "Codes and confirmations"}</span>
        <CaretDown className="disclosure-chevron" size={14} weight="light" />
      </summary>
      <p className="my-2 text-xs text-muted-foreground">
        Job portals send sign-up codes and confirmations here. When Nova is waiting for a code from the same site, it fills it in for you.
      </p>
      {address.data.address && <div className="flex items-center gap-2 rounded-lg bg-background px-3 py-2 ring-1 ring-foreground/[0.06]">
        <span className="min-w-0 flex-1 break-all font-geist-mono text-sm">{address.data.address}</span>
        <IslandButton size="sm" tone="ghost" onClick={() => void copy(address.data!.address!, "Address")}><Copy size={14} /> Copy</IslandButton>
      </div>}
      <div className="mt-3 space-y-2">
        {messages.isLoading && <p className="text-xs text-muted-foreground">Loading messages…</p>}
        {open && !messages.isLoading && !items.length && <p className="text-xs text-muted-foreground">No portal mail yet.</p>}
        {items.map(message => <article key={message.id} className="space-y-1.5 rounded-lg bg-background p-3 text-sm ring-1 ring-foreground/[0.06]">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate font-medium">{message.subject || "(no subject)"}</p>
              <p className="truncate text-xs text-muted-foreground">{message.sender}{message.received_at ? ` · ${new Date(message.received_at).toLocaleString()}` : ""}</p>
            </div>
            <button type="button" aria-label="Delete message" className="text-muted-foreground hover:text-foreground"
              onClick={() => void apiClient.delete(`/apply-email/messages/${message.id}`).then(() => queryClient.invalidateQueries({ queryKey: ["apply-email-messages"] })).catch(() => toast.error("Could not delete the message"))}>
              <Trash size={15} />
            </button>
          </div>
          {message.filled_for_nova && <p className="text-xs text-primary">Nova used this code for you.</p>}
          {message.codes.length > 0 && <div className="flex flex-wrap gap-2">{message.codes.map(code =>
            <button key={code} type="button" onClick={() => void copy(code, "Code")} className="rounded-md bg-muted px-2 py-1 font-geist-mono text-sm tracking-[0.2em]">{code}</button>)}</div>}
          {message.links.map(link => <a key={link} href={link} target="_blank" rel="noreferrer noopener" className="block truncate text-xs text-primary underline underline-offset-2">{link}</a>)}
          {!message.codes.length && !message.links.length && <p className="line-clamp-3 text-xs text-muted-foreground">{message.excerpt}</p>}
        </article>)}
      </div>
    </details>
  );
}
