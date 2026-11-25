"use client";

import { HttpAgent } from "@ag-ui/client";
import { API_BASE_URL, apiClient } from "@/lib/api";
import { getClerkAuthToken } from "@/lib/clerk-token";

export const COPILOT_AGENT_ID = "career-copilot";

// CopilotKit clears the agent before connecting an explicit thread. Restore
// from our history endpoint at that lifecycle boundary, without running a turn.
class CareerCopilotAgent extends HttpAgent {
  private latestMessages: HttpAgent["messages"] = [];
  constructor(config: ConstructorParameters<typeof HttpAgent>[0]) {
    super(config);
    this.latestMessages = [...this.messages];
    this.subscribe({ onMessagesChanged: ({ messages }) => {
      // A newly mounted SDK view clears messages before connect. Retain the
      // streaming snapshot so opening the full page cannot erase a pet reply.
      if (messages.length || !this.isRunning) this.latestMessages = [...messages];
    } });
  }
  async connectAgent() {
    if (this.isRunning) {
      this.setMessages(this.latestMessages);
      return { result: undefined, newMessages: [] };
    }
    try {
      const response = await apiClient.get(`/agents/chat/threads/${encodeURIComponent(this.threadId)}`);
      this.setMessages(response.data.messages);
    } catch (error) {
      if ((error as { response?: { status: number } }).response?.status !== 404) throw error;
      this.setMessages([]);
    }
    return { result: undefined, newMessages: [] };
  }
}

export function createCopilotAgent(threadId: string, messages: HttpAgent["messages"] = []): HttpAgent {
  const agent = new CareerCopilotAgent({
    agentId: COPILOT_AGENT_ID,
    threadId,
    initialMessages: messages,
    url: `${API_BASE_URL}/agents/chat`,
    // Resolve credentials on every request, including the first chat turn.
    fetch: async (url, init) => {
      const token = await getClerkAuthToken();
      if (!token) throw new Error("Please sign in again to use Nova.");
      const headers = new Headers(init.headers);
      headers.set("Authorization", `Bearer ${token}`);
      return fetch(url, { ...init, headers });
    },
  });
  return agent;
}

export function getOrCreateCopilotThreadId(userId: string): string {
  const key = `cc-copilot-thread-id:${userId}`;
  const stored = window.localStorage.getItem(key);
  if (stored) return stored;
  return resetCopilotThreadId(userId);
}

export function resetCopilotThreadId(userId: string): string {
  const threadId = crypto.randomUUID();
  window.localStorage.setItem(`cc-copilot-thread-id:${userId}`, threadId);
  return threadId;
}
