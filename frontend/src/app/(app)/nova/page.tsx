"use client";

import { useState } from "react";
import { useUser } from "@clerk/nextjs";
import { z } from "zod";

import {
  CopilotChat,
  CopilotKitProvider,
  useConfigureSuggestions,
  useRenderTool,
  useAgent,
} from "@copilotkit/react-core/v2";
import "./copilot-v2.css";
import "./workspace.css";
import { IslandButton } from "@/components/vanguard";
import { Sparkle, Plus, ClockCounterClockwise, Desktop, ChatCircle, ArrowUpRight } from "@phosphor-icons/react";
import Link from "next/link";
import { modelSetupRequired } from "@/lib/copilot-activity";

import { COPILOT_AGENT_ID } from "@/lib/copilot";
import { CopilotRunCard } from "@/components/agents/CopilotRunCard";
import { ComputerPanel } from "@/components/agents/ComputerPanel";
import { ResumeUpload } from "@/components/agents/ResumeUpload";
import { useCopilotSession } from "@/components/agents/CopilotSessionProvider";



const SUGGESTIONS = [
  {
    title: "Find me jobs",
    message: "Search for Python developer jobs in Remote and show me the top matches.",
  },
  {
    title: "Apply to a job",
    message: "Help me start an auto-apply run. Ask me for my target role and location first.",
  },
  {
    title: "Tailor my resume",
    message: "Tailor my resume to a job description I'll paste.",
  },
  {
    title: "Interview prep",
    message: "Prep me for a senior backend interview at a product company.",
  },
];

function CopilotChatPanel() {
  const [showHistory, setShowHistory] = useState(false);
  const { threadId, threads, selectThread: onSelect, newChat: onNewChat, messages, running, error, reportError: handleChatError } = useCopilotSession();
  const hasMessages = messages.length > 0;
  const { agent } = useAgent({ agentId: COPILOT_AGENT_ID });
  useRenderTool({
    name: "*",
    agentId: COPILOT_AGENT_ID,
    render: ({ name, result }: { name: string; result?: string }) => {
      let parsed: unknown;
      try { parsed = result ? JSON.parse(result) : null; } catch { parsed = null; }
      const run = z.object({ run_id: z.string().uuid() }).safeParse(parsed);
      if (run.success) return <CopilotRunCard runId={run.data.run_id} />;
      return <div className="my-2 rounded-2xl border border-border p-3 text-sm">
        <p className="font-medium">{name.replaceAll("_", " ")}</p>
        <p className="whitespace-pre-wrap text-muted-foreground">{result ?? "Working..."}</p>
      </div>;
    },
  }, []);

  useConfigureSuggestions(
    {
      suggestions: SUGGESTIONS,
      available: "before-first-message",
      consumerAgentId: COPILOT_AGENT_ID,
    },
    [threadId],
  );

  return (
    <section className={`conversation-workspace ${hasMessages ? "has-messages" : "is-empty"}`} aria-label="Nova conversation">
      <div className="conversation-toolbar">
        <span className="conversation-state"><span className={running ? "is-working" : ""} />{running ? "Working on your request" : "Your conversation"}</span>
        <div className="flex items-center gap-2">
        <IslandButton tone="quiet" size="sm" icon={<ClockCounterClockwise size={16} weight="light" />} onClick={() => setShowHistory(!showHistory)} aria-expanded={showHistory}>Chat history</IslandButton>
        <IslandButton tone="quiet" size="sm" icon={<Plus size={16} weight="light" />} onClick={onNewChat} disabled={running || agent.isRunning} aria-label="New chat">New chat</IslandButton>
        </div>
      </div>
      {showHistory && <nav aria-label="Chat history" className="mx-5 mb-3 max-h-40 overflow-y-auto rounded-xl bg-muted/40 p-2">
        {threads.length ? threads.map(thread => <button key={thread.thread_id} disabled={running || agent.isRunning}
          aria-current={thread.thread_id === threadId ? "page" : undefined}
          className="block w-full rounded-lg px-3 py-2 text-left text-xs hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50 aria-[current=page]:bg-primary/10"
          onClick={() => { onSelect(thread.thread_id); setShowHistory(false); }}>
          <span className="block truncate font-medium">{thread.title}</span><span className="text-[10px] text-muted-foreground">{new Date(thread.updated_at).toLocaleDateString()}</span>
        </button>) : <p className="p-3 text-xs text-muted-foreground">Your conversations appear here after your first message.</p>}
      </nav>}
      {error && (
        <div className="mx-6 mt-4 rounded-2xl border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {modelSetupRequired(messages) && <div className="copilot-model-notice"><span>Connect a model to start getting AI answers.</span><Link href="/settings/models">Set up model <ArrowUpRight size={14} /></Link></div>}
      {!hasMessages && <div className="copilot-welcome"><span className="welcome-mark"><Sparkle size={28} weight="duotone" /></span><p className="welcome-eyebrow">A little direction for your next chapter</p><h2>Where do you want<br />to go next?</h2><p className="welcome-description">Find the right roles, tailor your resume, or work through an application. Start with a question or a job link.</p></div>}
      <div className="copilot-chat-host min-h-0 flex-1">
        <CopilotChat
          agentId={COPILOT_AGENT_ID}
          threadId={threadId ?? undefined}
          labels={{ chatInputPlaceholder: "Paste a job link, or ask for your next step…" }}
          onError={handleChatError}
          attachments={{ enabled: false }}
        />
      </div>
      <div className="conversation-footer"><ResumeUpload /><span className="conversation-save">Private conversation · Saved to your account</span></div>
    </section>
  );
}

export default function CopilotPage() {
  const { user } = useUser();
  const { threadId, agent, historyError } = useCopilotSession();
  const [showComputer, setShowComputer] = useState(false);

  return (
    <div className={`copilot-workspace ${showComputer ? "with-computer" : ""}`}>
      <header className="copilot-page-header">
        <div><p className="copilot-page-eyebrow">YOUR CAREER WORKSPACE</p><h1>Nova<span>.</span></h1></div>
        <div className="workspace-view-switch" aria-label="Workspace view">
          <button type="button" aria-pressed={!showComputer} onClick={() => setShowComputer(false)}><ChatCircle size={17} />Chat</button>
          <button type="button" aria-pressed={showComputer} aria-controls="copilot-computer" onClick={() => setShowComputer(true)}><Desktop size={17} />Computer</button>
        </div>
      </header>
      <div className="copilot-panels">
      {historyError && <p role="alert" className="col-span-full rounded-xl bg-destructive/10 p-3 text-sm text-destructive">{historyError}</p>}
      {threadId && agent && user ? (
        <CopilotKitProvider
          key={`${user.id}:${threadId}`}
          agentId={COPILOT_AGENT_ID}
          selfManagedAgents={{ [COPILOT_AGENT_ID]: agent }}
        >
          <CopilotChatPanel />
        </CopilotKitProvider>
      ) : (
        <div className="glass-panel h-[calc(100vh-9rem)] animate-pulse rounded-[32px]" />
      )}
      {user && <aside id="copilot-computer" className="copilot-computer-panel" aria-label="Browser workspace" hidden={!showComputer}><ComputerPanel key={user.id} /></aside>}
      </div>
    </div>
  );
}
