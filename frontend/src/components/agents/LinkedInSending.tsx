"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { LinkedinLogo, Warning } from "@phosphor-icons/react";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { IslandButton } from "@/components/vanguard";

export type LinkedInAccountStatus = {
  enabled: boolean;
  connected: boolean;
  invites_per_day?: number;
  messages_per_day?: number;
};

export function useLinkedInAccount() {
  return useQuery<LinkedInAccountStatus>({
    queryKey: ["linkedin-account"],
    queryFn: async () => (await apiClient.get("/linkedin/account")).data,
  });
}

/** Beta: send approved outreach from the member's own LinkedIn (via Unipile). */
export function LinkedInSending() {
  const queryClient = useQueryClient();
  const account = useLinkedInAccount();
  if (!account.data?.enabled) return null;
  const { connected, invites_per_day: invites, messages_per_day: messages } = account.data;

  async function connect() {
    try {
      const { url } = (await apiClient.post("/linkedin/account/link")).data as { url: string };
      // Unipile's hosted login does not work inside an iframe; go there directly.
      window.location.href = url;
    } catch (error) {
      toast.error(getApiErrorMessage(error, "Could not start the LinkedIn connection."));
    }
  }

  async function disconnect() {
    try {
      await apiClient.delete("/linkedin/account");
      await queryClient.invalidateQueries({ queryKey: ["linkedin-account"] });
      toast.success("LinkedIn disconnected");
    } catch (error) {
      toast.error(getApiErrorMessage(error, "Could not disconnect LinkedIn."));
    }
  }

  return (
    <div className="rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <div className="flex flex-wrap items-start gap-3">
        <LinkedinLogo size={20} weight="light" className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">Send from your LinkedIn <span className="ml-1 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide">Beta</span></p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {connected
              ? `Connected. Approved drafts go out from your account: connections get a message, others a connection request with your note. Up to ${invites ?? 10} requests and ${messages ?? 25} messages a day.`
              : "Connect your LinkedIn to send approved drafts from here instead of copying them. You sign in on LinkedIn's partner page (Unipile); CareerCraft never sees your password."}
          </p>
          <p className="mt-2 flex items-start gap-1.5 text-xs leading-5 text-amber-700 dark:text-amber-400">
            <Warning size={14} className="mt-0.5 shrink-0" />
            LinkedIn doesn&apos;t allow automated tools and can restrict accounts that use them. Free accounts can send only a few connection notes a month. Only send what you&apos;d send by hand.
          </p>
        </div>
        {connected
          ? <IslandButton size="sm" tone="ghost" onClick={() => void disconnect()}>Disconnect</IslandButton>
          : <IslandButton size="sm" tone="ghost" onClick={() => void connect()}>Connect LinkedIn</IslandButton>}
      </div>
    </div>
  );
}
