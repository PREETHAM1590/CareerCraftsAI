"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { HttpAgent } from "@ag-ui/client";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { createCopilotAgent, getOrCreateCopilotThreadId, resetCopilotThreadId } from "@/lib/copilot";
import { useCompanion } from "@/components/companion/CompanionProvider";

type ChatThread = { thread_id: string; title: string; updated_at: string };
type Session = {
  agent: HttpAgent | null; threadId: string | null; messages: HttpAgent["messages"];
  running: boolean; error: string | null; historyError: string; threads: ChatThread[];
  selectThread: (id: string) => void; newChat: () => void; sendMessage: (text: string) => Promise<boolean>;
  reportError: (error: unknown) => void;
};
const SessionContext = createContext<Session | null>(null);
export function useCopilotSession() {
  const session = useContext(SessionContext);
  if (!session) throw new Error("Copilot session requires the app shell");
  return session;
}

export function CopilotSessionProvider({ ownerId, children }: { ownerId: string; children: React.ReactNode }) {
  const { updateChat } = useCompanion();
  const [threadId, setThreadId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [restored, setRestored] = useState<HttpAgent["messages"] | null>(null);
  const [messages, setMessages] = useState<HttpAgent["messages"]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState("");
  const sendLock = useRef(false);
  const refreshHistory = useCallback(async () => {
    try { setThreads((await apiClient.get("/agents/chat/threads")).data); }
    catch (e) { setHistoryError(getApiErrorMessage(e, "Could not load chat history.")); }
  }, []);
  useEffect(() => {
    if (!ownerId) return;
    setThreadId(getOrCreateCopilotThreadId(ownerId));
    void refreshHistory();
  }, [ownerId, refreshHistory]);
  useEffect(() => {
    if (!threadId) return;
    let disposed = false;
    setHistoryError("");
    apiClient.get(`/agents/chat/threads/${encodeURIComponent(threadId)}`).then(response => {
      if (!disposed) setRestored(response.data.messages);
    }).catch(e => {
      if (disposed) return;
      if (e?.response?.status === 404) setRestored([]);
      else setHistoryError(getApiErrorMessage(e, "Could not restore this chat. Refresh to retry."));
    });
    return () => { disposed = true; };
  }, [threadId]);
  const agent = useMemo(() => threadId && restored ? createCopilotAgent(threadId, restored) : null, [threadId, restored]);
  useEffect(() => {
    setMessages(agent ? [...agent.messages] : []);
    setError(null);
    if (!agent) return;
    const subscription = agent.subscribe({
      onMessagesChanged: ({ messages }) => setMessages([...messages]),
      onRunInitialized: () => { setRunning(true); setError(null); },
      onRunFailed: ({ error }) => setError(error.message),
      onRunFinalized: async () => { setRunning(false); await refreshHistory(); },
    });
    return () => { subscription.unsubscribe(); agent.abortRun(); };
  }, [agent, refreshHistory]);
  useEffect(() => { updateChat({ messages, running, error }); }, [messages, running, error, updateChat]);
  function selectThread(id: string) {
    if (!ownerId || id === threadId || agent?.isRunning || sendLock.current) return;
    localStorage.setItem(`cc-copilot-thread-id:${ownerId}`, id);
    setRestored(null); setMessages([]); setThreadId(id);
  }
  function newChat() { if (ownerId && !agent?.isRunning && !sendLock.current) selectThread(resetCopilotThreadId(ownerId)); }
  async function sendMessage(text: string) {
    const content = text.trim();
    if (!agent || sendLock.current || agent.isRunning || !content || content.length > 8000) return false;
    sendLock.current = true;
    setRunning(true); setError(null);
    agent.addMessage({ id: crypto.randomUUID(), role: "user", content });
    try { await agent.runAgent(); return true; }
    catch (e) { setError(getApiErrorMessage(e, "Could not finish the reply. Review the task outcome before retrying.")); return false; }
    finally { sendLock.current = false; setRunning(false); }
  }
  function reportError(event: unknown) {
    const message = typeof event === "object" && event !== null && "error" in event ? (event as { error: Error }).error?.message : undefined;
    setError(message || "Nova hit an error. Try again.");
  }
  return <SessionContext.Provider value={{ agent, threadId, messages, running, error, historyError, threads, selectThread, newChat, sendMessage, reportError }}>{children}</SessionContext.Provider>;
}
