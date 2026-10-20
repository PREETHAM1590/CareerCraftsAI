import { apiClient } from "@/lib/api";

/**
 * Shared client for long-running agent work (Company Research, Salary,
 * Interview Coach, LinkedIn Outreach, etc). These agents can run up to 120s
 * server-side, well past the 30s Axios timeout used for ordinary requests —
 * so instead of holding one long HTTP request open, we start the run with a
 * fast POST and poll a short GET until it reaches a terminal status.
 */
export type AgentRunDetail = {
  id: string;
  status: "queued" | "running" | "awaiting_approval" | "completed" | "failed" | "expired" | "cancelled";
  output: Record<string, unknown> | null;
  error: string | null;
};

const TERMINAL_STATUSES: ReadonlyArray<AgentRunDetail["status"]> = [
  "completed",
  "failed",
  "awaiting_approval",
  "expired",
  "cancelled",
];

export async function startAgentRun(taskType: string, context: Record<string, unknown>): Promise<string> {
  const { data } = await apiClient.post<{ run_id: string }>("/agents/run", { task_type: taskType, context });
  return data.run_id;
}

export async function waitForAgentRun(runId: string, timeoutMs = 300_000): Promise<AgentRunDetail> {
  const deadline = Date.now() + timeoutMs;
  let delay = 1_500;
  let lastStatus: AgentRunDetail["status"] | undefined;
  while (Date.now() < deadline) {
    const { data } = await apiClient.get<AgentRunDetail>(`/agents/runs/${runId}`);
    if (TERMINAL_STATUSES.includes(data.status)) return data;
    delay = data.status === lastStatus ? Math.min(8_000, delay * 1.5) : 1_500;
    lastStatus = data.status;
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await new Promise((resolve) => window.setTimeout(resolve, Math.min(remaining, delay * (0.8 + Math.random() * 0.4))));
  }
  throw new Error(`Agent run did not finish within ${timeoutMs / 1000} seconds`);
}

export type CoverLetterResult = { runId: string; content: string | null; warnings: string[] };

/**
 * Queue a cover letter run and wait for its draft. The route validates the
 * job description (400 when missing) and returns at once; the agent itself
 * runs as a durable workflow.
 */
export type CoverLetterTone = "formal" | "casual" | "bold" | "concise" | "story";

export async function generateCoverLetter(
  tone: CoverLetterTone,
  jdText: string,
): Promise<CoverLetterResult> {
  const { data } = await apiClient.post<{ run_id: string }>("/cover-letter/generate", {
    tone,
    jd_text: jdText,
  });
  const run = await waitForAgentRun(data.run_id);
  const output = (run.output ?? {}) as Record<string, unknown>;
  const content = output.cover_letter_markdown ?? output.content;
  const warnings = Array.isArray(output.warnings) ? (output.warnings as string[]) : [];
  return {
    runId: data.run_id,
    content: run.status === "failed" || typeof content !== "string" ? null : content,
    warnings,
  };
}
