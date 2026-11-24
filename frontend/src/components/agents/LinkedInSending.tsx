"use client";

import { useQuery } from "@tanstack/react-query";
import { LinkedinLogo, Warning } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";

export type LinkedInAccountStatus = {
  enabled: boolean;
  ready: boolean;
  invites_per_day?: number;
  messages_per_day?: number;
};

export function useLinkedInAccount() {
  return useQuery<LinkedInAccountStatus>({
    queryKey: ["linkedin-account"],
    queryFn: async () => (await apiClient.get("/linkedin/account")).data,
    // The extension comes and goes with Chrome; keep the button honest.
    refetchInterval: 30_000,
  });
}

/** Beta: send approved outreach from the member's own LinkedIn, via their extension. */
export function LinkedInSending() {
  const account = useLinkedInAccount();
  if (!account.data?.enabled) return null;
  const { ready, invites_per_day: invites, messages_per_day: messages } = account.data;

  return (
    <div className="rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <div className="flex items-start gap-3">
        <LinkedinLogo size={20} weight="light" className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">
            Send from your LinkedIn{" "}
            <span className="ml-1 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide">Beta</span>
            <span className={`ml-2 text-xs font-normal ${ready ? "text-primary" : "text-muted-foreground"}`}>
              {ready ? "Chrome extension online" : "Chrome extension offline"}
            </span>
          </p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {ready
              ? `Approved drafts are sent by your CareerCraft extension, in your own Chrome, from the LinkedIn you're signed into there: connections get a message, others a connection request with your note. Up to ${invites ?? 10} requests and ${messages ?? 25} messages a day. Keep Chrome open while they send.`
              : "Open Chrome with the CareerCraft extension and sign in to LinkedIn there to send approved drafts from here. Nothing about your LinkedIn leaves your computer."}
          </p>
          <p className="mt-2 flex items-start gap-1.5 text-xs leading-5 text-amber-700 dark:text-amber-400">
            <Warning size={14} className="mt-0.5 shrink-0" />
            LinkedIn doesn&apos;t allow automated tools and can restrict accounts that use them. Free accounts can send only a few connection notes a month. Only send what you&apos;d send by hand.
          </p>
        </div>
      </div>
    </div>
  );
}
