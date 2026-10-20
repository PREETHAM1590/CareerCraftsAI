"use client";
import { createContext, useCallback, useContext, useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { useQueries, type Query } from "@tanstack/react-query";
import { usePollMs } from "@/store/agentStore";
import { apiClient } from "@/lib/api";
import { companionActivity, conversationRunIds, defaultCompanion, readCompanion, type ActivityMessage, type ActivityRun, type CompanionPreferences, type CompanionActivity } from "@/lib/copilot-activity";

type ChatActivity = { messages: readonly ActivityMessage[]; running: boolean; error: string | null };
type CompanionContextValue = {
  preferences: CompanionPreferences; updatePreferences: (preferences: CompanionPreferences) => void;
  updateChat: Dispatch<SetStateAction<ChatActivity>>; activity: CompanionActivity;
  loaded: boolean; saveError: string; refreshRuns: () => void;
};
const CompanionContext = createContext<CompanionContextValue | null>(null);
export function useCompanion() {
  const context = useContext(CompanionContext);
  if (!context) throw new Error("Companion requires the signed-in app shell");
  return context;
}
export function CompanionProvider({ ownerId, children }: { ownerId: string; children: React.ReactNode }) {
  const [preferences, setPreferences] = useState<CompanionPreferences>(defaultCompanion);
  const [loaded, setLoaded] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [chat, updateChat] = useState<ChatActivity>({ messages: [], running: false, error: null });
  const storageKey = `cc-copilot-companion:${ownerId}`;
  useEffect(() => {
    if (!ownerId) return;
    try { setPreferences(readCompanion(localStorage.getItem(storageKey))); } catch { setSaveError("Preferences cannot be saved in this browser."); }
    setLoaded(true);
    const sync = (event: StorageEvent) => { if (event.key === storageKey) setPreferences(readCompanion(event.newValue)); };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, [ownerId, storageKey]);
  const updatePreferences = useCallback((next: CompanionPreferences) => {
    setPreferences(next);
    try { localStorage.setItem(storageKey, JSON.stringify(next)); setSaveError(""); } catch { setSaveError("Preferences cannot be saved in this browser."); }
  }, [storageKey]);
  const ids = conversationRunIds(chat.messages);
  const pollMs = usePollMs(3000, 15_000);
  const runQuery = (id: string) => ({
    queryKey: ["copilot-run", id],
    queryFn: async (): Promise<ActivityRun> => (await apiClient.get(`/agents/runs/${id}`)).data,
    refetchInterval: (query: Query<ActivityRun>) => ["completed", "failed", "cancelled", "expired"].includes(query.state.data?.status ?? "") ? false as const : pollMs,
  });
  const queries = useQueries({ queries: ids.map(runQuery) });
  const parents = queries.flatMap(query => query.data ? [query.data] : []);
  const childrenIds = [...new Set(parents.flatMap(run => Array.isArray(run.output?.child_run_ids) ? run.output.child_run_ids : []))]
    .filter((id): id is string => typeof id === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id) && !ids.includes(id)).slice(-12);
  const childQueries = useQueries({ queries: childrenIds.map(runQuery) });
  const childRuns = childQueries.flatMap(query => query.data ? [query.data] : []);
  const runs = parents.flatMap(parent => [parent, ...childRuns.filter(child => Array.isArray(parent.output?.child_run_ids) && parent.output.child_run_ids.includes(child.id))]);
  let activity = companionActivity(runs, chat.running, chat.error, chat.messages);
  if ([...queries, ...childQueries].some(query => query.isError)) activity = { state: "failed", title: "Task status unavailable", detail: "Refresh to check task progress. The task may still be running." };
  else if (ids.length && [...queries, ...childQueries].some(query => query.isPending)) activity = { state: "running", title: "Checking task status…", detail: "Restoring progress from your conversation." };
  return <CompanionContext.Provider value={{ preferences, updatePreferences, updateChat, activity, loaded, saveError, refreshRuns: () => [...queries, ...childQueries].forEach(query => void query.refetch()) }}>{children}</CompanionContext.Provider>;
}
