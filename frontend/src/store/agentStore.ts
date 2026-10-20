import { create } from "zustand";

interface AgentEvent {
  type: string;
  data: unknown;
  ts?: number;
}

interface BrowserFrame {
  step: number;
  url: string;
  title: string;
  screenshot_b64: string;
  mime?: string;
}

interface AgentRun {
  runId: string;
  status:
    | "queued"
    | "running"
    | "awaiting_approval"
    | "completed"
    | "failed"
    | "needs_verification"
    | "cancelled"
    | "expired";
  events: AgentEvent[];
  pendingAction: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error: string | null;
  lastFrame?: BrowserFrame;
}

interface AgentStore {
  generation: number;
  owner: string | null;
  setOwner: (owner: string | null) => void;
  runs: Record<string, AgentRun>;
  activeRunId: string | null;
  // True while the active run's SSE stream is open; polling backs off then.
  streamLive: boolean;
  setStreamLive: (live: boolean) => void;

  setActiveRun: (runId: string | null) => void;
  initRun: (runId: string) => void;
  addEvent: (runId: string, eventType: string, data: unknown) => void;
  setCheckpoint: (runId: string, pendingAction: Record<string, unknown>) => void;
  clearCheckpoint: (runId: string) => void;
  setComplete: (runId: string, result: Record<string, unknown>) => void;
  setError: (runId: string, message: string) => void;
  // An external side effect (e.g. a submit click) may have gone through with
  // no way to safely confirm it — this is deliberately not "failed": the
  // system must never auto-retry it, and the copy must tell the user to
  // check the portal themselves rather than implying the agent is broken.
  setNeedsVerification: (runId: string, message: string) => void;
  setRunStatus: (runId: string, status: AgentRun["status"]) => void;
  clearRun: (runId: string) => void;
}

let activeOwner: string | null = null;
function persistActiveRun(runId: string | null) {
  if (typeof window === "undefined" || !activeOwner) return;
  const key = `cc_active_run_id:${activeOwner}`;
  if (runId) localStorage.setItem(key, runId);
  else localStorage.removeItem(key);
}
export const useAgentStore = create<AgentStore>((set) => ({
  generation: 0,
  owner: null,
  runs: {},
  activeRunId: null,
  streamLive: false,
  setStreamLive: (streamLive) => set({ streamLive }),
  setOwner: (owner) => {
    activeOwner = owner;
    if (typeof window !== "undefined") localStorage.removeItem("cc_active_run_id");
    set((s) => ({ generation: s.generation + 1, owner, runs: {}, activeRunId: owner && typeof window !== "undefined" ? localStorage.getItem(`cc_active_run_id:${owner}`) : null }));
  },

  setActiveRun: (runId) => {
    persistActiveRun(runId);
    set({ activeRunId: runId });
  },

  initRun: (runId) => {
    persistActiveRun(runId);
    set((s) => ({
      activeRunId: runId,
      runs: {
        ...s.runs,
        [runId]: {
          runId,
          status: "running",
          events: [],
          pendingAction: null,
          result: null,
          error: null,
        },
      },
    }));
  },

  addEvent: (runId, eventType, data) =>
    set((s) => {
      if (!s.runs[runId]) return s;
      const run = s.runs[runId] ?? {
        runId,
        status: "running",
        events: [],
        pendingAction: null,
        result: null,
        error: null,
      };
      if (eventType === "browser_frame") {
        return {
          runs: { ...s.runs, [runId]: { ...run, lastFrame: data as BrowserFrame } },
        };
      }
      return {
        runs: {
          ...s.runs,
          [runId]: {
            ...run,
            events: [...run.events.slice(-499), { type: eventType, data, ts: Date.now() }],
          },
        },
      };
    }),

  setCheckpoint: (runId, pendingAction) =>
    set((s) => !s.runs[runId] ? s : ({
      runs: {
        ...s.runs,
        [runId]: {
          ...s.runs[runId],
          status: "awaiting_approval",
          pendingAction: pendingAction || null,
        },
      },
    })),

  clearCheckpoint: (runId) =>
    set((s) => !s.runs[runId] ? s : ({
      runs: {
        ...s.runs,
        [runId]: {
          ...s.runs[runId],
          status: "running",
          pendingAction: null,
        },
      },
    })),

  setComplete: (runId, result) => {
    set((s) => {
      if (!s.runs[runId]) return s;
      if (s.activeRunId === runId) persistActiveRun(null);
      return ({
      activeRunId: s.activeRunId === runId ? null : s.activeRunId,
      runs: {
        ...s.runs,
        [runId]: {
          ...s.runs[runId],
          status: "completed",
          pendingAction: null,
          result,
        },
      },
    }); });
  },

  setError: (runId, message) => {
    set((s) => {
      if (!s.runs[runId]) return s;
      if (s.activeRunId === runId) persistActiveRun(null);
      return ({
      activeRunId: s.activeRunId === runId ? null : s.activeRunId,
      runs: {
        ...s.runs,
        [runId]: {
          ...s.runs[runId],
          status: "failed",
          pendingAction: null,
          error: message,
        },
      },
    }); });
  },

  setNeedsVerification: (runId, message) => {
    set((s) => {
      if (!s.runs[runId]) return s;
      if (s.activeRunId === runId) persistActiveRun(null);
      return ({
      activeRunId: s.activeRunId === runId ? null : s.activeRunId,
      runs: {
        ...s.runs,
        [runId]: {
          ...s.runs[runId],
          status: "needs_verification",
          pendingAction: null,
          error: message,
        },
      },
    }); });
  },

  setRunStatus: (runId, status) =>
    set((s) => {
      if (!s.runs[runId]) return s;
      const terminal = ["completed", "failed", "needs_verification", "cancelled", "expired"].includes(status);
      if (terminal && s.activeRunId === runId) persistActiveRun(null);
      return {
        activeRunId: terminal && s.activeRunId === runId ? null : s.activeRunId,
        runs: { ...s.runs, [runId]: { ...s.runs[runId], status, ...(terminal ? { pendingAction: null } : {}) } },
      };
    }),

  clearRun: (runId) =>
    set((s) => {
      const rest = { ...s.runs };
      delete rest[runId];
      return { runs: rest };
    }),
}));

/** Poll interval: `fast` when no SSE stream is open (fallback), else the slow safety-net `slow`. */
export const usePollMs = (fast: number, slow: number) => useAgentStore((s) => (s.streamLive ? slow : fast));
