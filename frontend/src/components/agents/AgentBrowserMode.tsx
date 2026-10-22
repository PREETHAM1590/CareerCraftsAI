"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Segmented, StatusPill } from "@/components/vanguard";
import { apiClient, getApiErrorMessage } from "@/lib/api";

export type ComputerMode = "auto" | "browser" | "cloud";
export type ComputerModeState = {
  mode: ComputerMode;
  effective: "browser" | "cloud";
  extension: { paired: boolean; online: boolean };
  cloud: { available: boolean };
};

export const PAIR_EXTENSION_HREF = "/settings/integrations#browser-extension";

const OPTIONS = [
  { value: "auto", label: "Automatic" },
  { value: "browser", label: "My browser" },
  { value: "cloud", label: "Cloud" },
] as const;

/** GET/PUT /computer/mode, shared by Settings and the Copilot computer panel. */
export function useComputerMode() {
  const queryClient = useQueryClient();
  const query = useQuery<ComputerModeState>({
    queryKey: ["computer-mode"],
    queryFn: async () => (await apiClient.get("/computer/mode")).data,
    refetchInterval: 30_000,
  });
  const update = useMutation({
    mutationFn: async (mode: ComputerMode) => (await apiClient.put<ComputerModeState>("/computer/mode", { mode })).data,
    onSuccess: (data) => queryClient.setQueryData(["computer-mode"], data),
  });
  return { query, update };
}

function extensionLabel(state: ComputerModeState) {
  if (!state.extension.paired) return { tone: "warning" as const, text: "Extension not paired" };
  return state.extension.online
    ? { tone: "success" as const, text: "Extension online" }
    : { tone: "neutral" as const, text: "Extension offline" };
}

/** "Agent browser" choice. `compact` is the one-line version for the computer panel header. */
export function AgentBrowserSetting({ compact = false, onChanged }: { compact?: boolean; onChanged?: () => void }) {
  const { query, update } = useComputerMode();
  const state = query.data;
  const select = (
    <Segmented<ComputerMode>
      asTabs={false}
      size="sm"
      ariaLabel="Agent browser"
      value={state?.mode ?? "auto"}
      options={OPTIONS}
      onChange={(mode) => {
        if (!state || update.isPending || mode === state.mode) return;
        update.mutate(mode, { onSuccess: () => onChanged?.() });
      }}
    />
  );
  const error = (query.isError || update.isError) && (
    <p role="alert" className="text-xs text-destructive">
      {getApiErrorMessage(update.error ?? query.error, "Could not load the agent browser setting.")}
    </p>
  );
  const pairLink = state && !state.extension.paired && (
    <Link href={PAIR_EXTENSION_HREF} className="font-medium text-primary underline decoration-primary/40 underline-offset-4 hover:decoration-primary">
      Pair the extension
    </Link>
  );

  if (compact) {
    return (
      <div className="space-y-1.5">
        {select}
        {pairLink && <p className="text-xs text-muted-foreground">To run the agent in your own Chrome: {pairLink}</p>}
        {error}
      </div>
    );
  }

  const status = state && extensionLabel(state);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">Agent browser</h3>
        {status && <StatusPill tone={status.tone}>{status.text}</StatusPill>}
      </div>
      {select}
      <p role="status" className="text-xs leading-5 text-muted-foreground">
        {!state
          ? query.isLoading ? "Checking..." : ""
          : `${state.mode === "auto" ? "Automatic uses your browser when the extension is connected, otherwise the cloud computer. " : ""}The agent currently runs in ${state.effective === "browser" ? "a CareerCraft tab in your browser" : "the cloud computer"}.`}{" "}
        {pairLink}
      </p>
      {error}
    </div>
  );
}
