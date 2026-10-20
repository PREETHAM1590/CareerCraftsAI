"use client";
import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getClerkAuthToken } from "@/lib/clerk-token";
import { useAgentStore } from "@/store/agentStore";
import { apiClient, API_BASE_URL } from "@/lib/api";

export function useAgentStream(runId: string | null) {
  const { addEvent, setRunStatus, setCheckpoint, setComplete, setError, setNeedsVerification } = useAgentStore();
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);
  const retryRef = useRef(0);

  useEffect(() => {
    if (!runId) return;
    const id = runId;
    const generation = useAgentStore.getState().generation;
    const currentOwner = () => useAgentStore.getState().generation === generation;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let polling = false;
    let tick = 0;
    // Run events change the runs lists, approvals badge, notifications and per-run queries.
    const refresh = () => {
      for (const key of [["agent-runs"], ["pending-approvals"], ["notifications"], ["job-search-outcome", id], ["copilot-run", id]]) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    };
    let terminal = false;
    const pollController = new AbortController();
    if (!useAgentStore.getState().runs[id]) useAgentStore.getState().initRun(id);

    // DB state is authoritative: restore approvals/results after refresh or lost SSE.
    async function reconcile() {
      if (polling || disposed || terminal || !currentOwner()) return;
      polling = true;
      try {
        const { data } = await apiClient.get(`/agents/runs/${id}`, { signal: pollController.signal });
        if (disposed || !currentOwner()) return;
        if (["completed", "failed", "cancelled", "expired"].includes(data.status)) {
          terminal = true;
          abortRef.current?.abort();
          if (retryTimer) clearTimeout(retryTimer);
        }
        if (data.status === "cancelled" || data.status === "expired") setRunStatus(id, data.status);
        else if (data.status === "awaiting_approval") setCheckpoint(id, data.output || {});
        else if (data.status === "completed") setComplete(id, data.output || {});
        else if (data.status === "failed" && data.output?.outcome === "unknown") {
          setNeedsVerification(id, data.output?.message || "Check the job portal before retrying — this outcome could not be confirmed.");
        }
        else if (data.status === "failed") setError(id, data.output?.error || data.output?.message || "Agent failed");
        else if (data.status === "queued" || data.status === "running") setRunStatus(id, data.status);
      } catch { /* transient transport failure is not a failed agent */ }
      finally { polling = false; }
    }
    void reconcile();
    // DB reconcile every 3s while the stream is down; every 15s (5th tick) while it is healthy.
    const pollTimer = setInterval(() => {
      if (useAgentStore.getState().streamLive && ++tick % 5 !== 0) return;
      void reconcile();
    }, 3000);

    async function connect() {
      if (disposed || terminal || !currentOwner()) return;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const token = await getClerkAuthToken();
        if (disposed || terminal || !currentOwner()) return;
        const apiUrl = API_BASE_URL;
        const res = await fetch(`${apiUrl}/agents/${id}/stream`, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          signal: controller.signal,
        });

        if (!res.ok || !res.body) {
          throw new Error(`SSE ${res.status}`);
        }

        if (disposed || terminal || !currentOwner()) { await res.body.cancel(); return; }
        retryRef.current = 0;
        if (currentOwner()) useAgentStore.getState().setStreamLive(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let eventType = "";
        let dataBuffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (disposed || terminal || !currentOwner()) { await reader.cancel(); return; }
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";

          for (const line of lines) {
            if (line.startsWith("event:")) {
              eventType = line.slice(6).trim();
              continue;
            }
            if (line.startsWith("data:")) {
              const raw = line.slice(5).trim();
              dataBuffer += raw;
              continue;
            }
            if (line.trim() === "") {
              // End of an SSE frame
              if (eventType && dataBuffer) {
                try {
                  const data = JSON.parse(dataBuffer);
                  addEvent(id, eventType, data);
                  if (["checkpoint", "complete", "cancelled", "expired", "approved", "error"].includes(eventType)) refresh();
                  if (eventType === "checkpoint") {
                    setCheckpoint(id, data);
                  } else if (eventType === "complete") {
                    terminal = true;
                    setComplete(id, data);
                    return;
                  } else if (eventType === "cancelled" || eventType === "expired") {
                    terminal = true;
                    setRunStatus(id, eventType);
                    return;
                   } else if (eventType === "error") {
                    // A broken event bus is not evidence of workflow failure.
                    void reconcile();
                  } else if (eventType === "approved") {
                    useAgentStore.getState().clearCheckpoint(id);
                    setRunStatus(id, "queued");
                  }
                } catch { /* malformed JSON — skip */ }
              }
              eventType = "";
              dataBuffer = "";
            }
          }
        }
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        if (retryRef.current < 3) {
          retryRef.current += 1;
           retryTimer = setTimeout(connect, Math.pow(2, retryRef.current) * 1000);
        }
      } finally {
        if (currentOwner()) useAgentStore.getState().setStreamLive(false);
      }
    }

    connect();
    return () => {
      disposed = true;
      useAgentStore.getState().setStreamLive(false);
      pollController.abort();
      clearInterval(pollTimer);
      if (retryTimer) clearTimeout(retryTimer);
      abortRef.current?.abort();
    };
  }, [runId, queryClient, addEvent, setRunStatus, setCheckpoint, setComplete, setError, setNeedsVerification]);
}
