"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChatCircleDots } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { IslandButton } from "@/components/vanguard";

type Status = { configured: boolean; connected: boolean };

/** Lets Nova ask for one-time codes on Telegram; the reply goes straight to the waiting step. */
export function TelegramConnect() {
  const queryClient = useQueryClient();
  const [waiting, setWaiting] = useState(false);
  const status = useQuery<Status>({
    queryKey: ["telegram"],
    queryFn: async () => (await apiClient.get("/telegram")).data,
    // While the member is finishing the link in Telegram, check every few seconds.
    refetchInterval: query => (waiting && !query.state.data?.connected ? 4000 : false),
  });
  if (!status.data?.configured) return null;
  const connected = status.data.connected;

  async function connect() {
    // Opened before the request so popup blockers allow it. "noopener" in the
    // features string would make window.open return null, so drop the opener here.
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const { url } = (await apiClient.post("/telegram/link")).data as { url: string };
      if (tab) tab.location.href = url; else window.location.href = url;
      setWaiting(true);
    } catch {
      tab?.close();
      toast.error("Could not create a Telegram link. Try again.");
    }
  }

  async function disconnect() {
    try {
      await apiClient.delete("/telegram");
      setWaiting(false);
      await queryClient.invalidateQueries({ queryKey: ["telegram"] });
    } catch {
      toast.error("Could not disconnect Telegram.");
    }
  }

  return (
    <div className="flex items-start gap-3 rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <ChatCircleDots size={18} weight="light" className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">Code requests on Telegram</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {connected
            ? "Connected. When a site texts or emails you a code, Nova asks you on Telegram. Reply with just the code."
            : waiting
              ? "Press Start in Telegram to finish connecting…"
              : "Get a Telegram message when Nova needs a one-time code, and reply there instead of opening CareerCraft."}
        </p>
      </div>
      {connected
        ? <IslandButton size="sm" tone="ghost" onClick={() => void disconnect()}>Disconnect</IslandButton>
        : <IslandButton size="sm" tone="ghost" onClick={() => void connect()}>Connect</IslandButton>}
    </div>
  );
}
