"use client";
import { Fragment, useEffect, useRef, useState } from "react";
import { ArrowUp } from "@phosphor-icons/react";
import { useCopilotSession } from "@/components/agents/CopilotSessionProvider";
import { conversationRunIds, modelSetupRequired } from "@/lib/copilot-activity";
import Link from "next/link";
import { parseInlineMarkdown, type InlineToken } from "@/lib/inline-markdown";
import { CopilotRunCard } from "@/components/agents/CopilotRunCard";

function renderInline(tokens: InlineToken[]): React.ReactNode {
  return tokens.map((token, index) => {
    if (token.kind === "bold") return <strong key={index}>{renderInline(token.children)}</strong>;
    if (token.kind === "code") return <code key={index} className="rounded bg-muted px-1">{token.text}</code>;
    if (token.kind === "link") return <a key={index} href={token.href} target="_blank" rel="noopener noreferrer" className="text-primary underline underline-offset-2">{renderInline(token.children)}</a>;
    return <Fragment key={index}>{token.text}</Fragment>;
  });
}

export function CompanionChat() {
  const { agent, messages, running, error, historyError, sendMessage } = useCopilotSession();
  const [draft, setDraft] = useState("");
  const log = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  useEffect(() => { if (follow.current && log.current) log.current.scrollTop = log.current.scrollHeight; }, [messages, running, error]);
  const visible = messages.filter(message => ["user", "assistant"].includes(message.role) && typeof message.content === "string" && message.content).slice(-8);
  const runId = conversationRunIds(messages).at(-1);
  return <div className="companion-chat">
    <div ref={log} className="companion-chat-log" role="log" aria-label="Conversation with your companion" onScroll={event => { const el = event.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}>
      {!visible.length && <p className="py-6 text-center text-xs leading-5 text-muted-foreground">Tell me what you're working on.<br />This is your saved Nova conversation.</p>}
      {visible.map(message => <div key={message.id} className={`companion-chat-message ${message.role === "user" ? "from-user" : "from-assistant"}`}><p className="mb-1 text-[10px] font-semibold text-muted-foreground">{message.role === "user" ? "You" : "Nova"}</p><p className="whitespace-pre-wrap break-words text-xs leading-5">{message.role === "assistant" ? renderInline(parseInlineMarkdown(String(message.content))) : String(message.content)}</p></div>)}
      {runId && <CopilotRunCard runId={runId} />}
      {running && <p role="status" className="py-2 text-xs text-muted-foreground">Nova is responding…</p>}
    </div>
    {(error || historyError) && <p role="alert" className="my-2 text-xs text-destructive">{error || historyError}</p>}
    {modelSetupRequired(messages) && <Link href="/settings/models" className="mb-3 block text-xs text-primary underline underline-offset-4">Set up an AI model</Link>}
    <form className="companion-chat-composer" onSubmit={event => {
      event.preventDefault();
      follow.current = true;
      void sendMessage(draft).then(sent => { if (sent) setDraft(""); });
    }}>
      <textarea aria-label="Message your companion" placeholder="Ask about jobs or paste a link…" rows={2} maxLength={8000} value={draft} disabled={!agent || running} onChange={event => setDraft(event.target.value)} onKeyDown={event => {
        if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
      }} />
      <button aria-label="Send companion message" type="submit" disabled={!agent || running || !draft.trim()}><ArrowUp size={17} /></button>
    </form>
    <p className="mt-2 text-[10px] text-muted-foreground">{agent ? "Same chat and history as Nova." : "Restoring your conversation…"}</p>
  </div>;
}
