export type CompanionPreferences = { name: string; character: "mint" | "blue" | "orange" | "purple"; size: "small" | "medium" | "large"; corner: "left" | "right"; motion: boolean; visible: boolean };
export const defaultCompanion: CompanionPreferences = { name: "Pip", character: "mint", size: "medium", corner: "right", motion: true, visible: true };
export function readCompanion(raw: string | null): CompanionPreferences {
  try {
    const value = JSON.parse(raw ?? "null");
    if (!value || typeof value !== "object") return { ...defaultCompanion };
    return {
      name: typeof value.name === "string" && value.name.trim() ? value.name.trim().slice(0, 24) : defaultCompanion.name,
      character: ["mint", "blue", "orange", "purple"].includes(value.character) ? value.character : value.color === "peach" ? "orange" : value.color === "lilac" ? "purple" : "mint",
      size: ["small", "medium", "large"].includes(value.size) ? value.size : "medium",
      corner: value.corner === "left" ? "left" : "right",
      motion: typeof value.motion === "boolean" ? value.motion : true,
      visible: typeof value.visible === "boolean" ? value.visible : true,
    };
  } catch { return { ...defaultCompanion }; }
}
export type ActivityMessage = { role: string; content?: unknown };
export function modelSetupRequired(messages: readonly ActivityMessage[]): boolean {
  const last = messages.at(-1);
  return last?.role === "assistant" && typeof last.content === "string" && last.content.trim() === "No active model is configured. Set one up in Settings → Models.";
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function conversationRunIds(messages: readonly ActivityMessage[]): string[] {
  const ids: string[] = [];
  for (const message of messages) {
    if (message.role !== "tool" || typeof message.content !== "string") continue;
    try { const value = JSON.parse(message.content); if (typeof value?.run_id === "string" && uuid.test(value.run_id)) ids.push(value.run_id); } catch { /* Plain tool results have no run. */ }
  }
  return [...new Set(ids)].slice(-12);
}
export type ActivityRun = { id: string; status: string; agent_type: string; output: Record<string, unknown> | null; error?: string | null };
export type CompanionActivity = { state: "idle" | "running" | "waiting" | "failed" | "review"; title: string; detail: string; run?: ActivityRun };
/** The panel opens by itself only for a task that cannot go on without the member. A chat reply that ends in a question shows on the pet's label instead. */
export function opensByItself(activity: CompanionActivity): boolean {
  return activity.state === "waiting" && Boolean(activity.run);
}
export function companionActivity(runs: ActivityRun[], running: boolean, error: string | null, messages: readonly ActivityMessage[]): CompanionActivity {
  const waiting = runs.findLast(run => run.status === "awaiting_approval");
  if (waiting) return { state: "waiting", title: "Your review is needed", detail: typeof waiting.output?.summary === "string" ? waiting.output.summary : "Review the proposed action before the assistant continues.", run: waiting };
  const active = runs.findLast(run => ["running", "queued"].includes(run.status));
  if (active) return { state: "running", title: active.status === "queued" ? "Task queued" : `Working on ${active.agent_type.replaceAll("_", " ")}`, detail: "Open the run for its progress and results.", run: active };
  if (running) return { state: "running", title: "Thinking through your request", detail: "Nova is responding to this conversation." };
  if (error) return { state: "failed", title: "The conversation needs attention", detail: error };
  const latest = runs.at(-1);
  if (latest?.output?.input_required === "browser_handoff") return { state: "waiting", title: "Browser input is needed", detail: typeof latest.output.message === "string" ? latest.output.message : "Finish your browser input and return control to the assistant.", run: latest };
  if (latest && ["failed", "expired"].includes(latest.status)) return { state: "failed", title: "Task needs attention", detail: latest.error || (typeof latest.output?.error === "string" ? latest.output.error : "Open the run to review the failure before retrying."), run: latest };
  const last = messages.at(-1);
  if (modelSetupRequired(messages)) return { state: "waiting", title: "Choose a model to chat", detail: "Set up an active model in Settings → Models, then send your message again." };
  if (last?.role === "assistant" && typeof last.content === "string") {
    const question = last.content.split(/\n+/).map(line => line.trim()).filter(Boolean).at(-1);
    if (question?.endsWith("?") && question.length <= 400) return { state: "waiting", title: "Your turn", detail: question };
  }
  if (latest && latest.status === "completed") return { state: "review", title: "Task finished · review the result", detail: typeof latest.output?.message === "string" ? latest.output.message : "Results are ready in the conversation. Check the outcome before your next step.", run: latest };
  return { state: "idle", title: "Ready for your next move", detail: "Share a job link, find a role, or prepare your application." };
}
