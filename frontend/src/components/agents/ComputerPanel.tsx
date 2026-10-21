"use client";

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import { CopilotRunCard } from "./CopilotRunCard";
import { LiveBrowserSurface, type BrowserSurfaceHandle } from "./LiveBrowserSurface";
import { Bezel, IslandButton, Input, Select, Textarea, Field } from "@/components/vanguard";
import { Desktop, Globe, LockKey, CursorClick, Moon, ArrowRight, CaretDown, ArrowsOut, ArrowsIn } from "@phosphor-icons/react";

import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";

type Screen = { base64: string; width: number; height: number; url: string; mime?: string };
type Snapshot = { snapshotId: number; computer_run: string; elements: { ref: string; role: string; name: string }[] };
type SavedLogin = { id: string; origin: string; label: string };

export function ComputerPanel() {
  const [expanded, setExpanded] = useState(false);
  const surface = useRef<BrowserSurfaceHandle>(null);
  const [running, setRunning] = useState(false);
  // null until the first status answer; false: this site has no sandbox relay.
  const [available, setAvailable] = useState<boolean | null>(null);
  const [screen, setScreen] = useState<Screen | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Polling failures, kept apart from action errors and cleared by the next good poll.
  const [statusError, setStatusError] = useState("");
  const [url, setUrl] = useState("");
  const [controlId, setControlId] = useState<string | null>(null);
  const [privateText, setPrivateText] = useState("");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [logins, setLogins] = useState<SavedLogin[]>([]);
  const [loginId, setLoginId] = useState("");
  const [userRef, setUserRef] = useState("");
  const [passRef, setPassRef] = useState("");
  const [task, setTask] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [resumeId, setResumeId] = useState("");
  const [fileRef, setFileRef] = useState("");
  const { data: resumes = [] } = useQuery<{ id: string; filename: string; is_primary: boolean }[]>({
    queryKey: ["computer-resumes"], queryFn: async () => (await apiClient.get("/rag/documents?doc_type=resume")).data,
  });

  async function action(operation: string, parameters: Record<string, unknown> = {}) {
    return (await apiClient.post("/computer/action", { operation, parameters }, { timeout: 80000 })).data;
  }
  async function perform(work: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await surface.current?.flush(); await work(); } catch (e) { setError(getApiErrorMessage(e, "Computer action failed. Refresh before retrying.")); }
    finally { setBusy(false); }
  }
  async function refreshLogins() { setLogins((await apiClient.get("/computer/credentials")).data); }

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let polling = false; // one poll at a time, even when the tab regains focus mid-poll
    async function poll() {
      polling = true;
      let delay = 4000;
      try {
        const state = (await apiClient.get("/computer")).data;
        if (disposed) return;
        setStatusError("");
        setAvailable(state.available !== false);
        if (state.available === false) {
          // Re-check slowly so the workspace appears once a relay is set up.
          delay = 60000;
        } else if (state.running && document.hidden) {
          // Nobody can see the screen: skip the frame (each one costs the
          // computers host CPU) and check back less often.
          setRunning(true);
          delay = 15000;
        } else if (state.running) {
          setRunning(true);
          const next = await action("screenshot");
          if (!disposed) setScreen(next);
          const control = await action("control");
          if (!disposed) setControlId(control.holder === "human" ? control.request?.id : null);
        } else { setRunning(false); setScreen(null); setControlId(null); }
      } catch (e) {
        // Availability stays as it was: a failed poll neither reveals nor hides controls.
        if (!disposed) setStatusError(getApiErrorMessage(e, "Computer service unavailable."));
      }
      polling = false;
      if (!disposed) timer = setTimeout(poll, delay);
    }
    // Coming back to the tab refreshes the screen at once instead of after the slow interval.
    function onVisible() { if (!document.hidden && !disposed && !polling) { clearTimeout(timer); void poll(); } }
    document.addEventListener("visibilitychange", onVisible);
    void poll();
    void refreshLogins().catch(() => {});
    return () => { disposed = true; clearTimeout(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, []);

  if (available !== true) {
    // No actionable controls until the relay is confirmed; saved logins stay removable.
    return <Bezel className="computer-workspace" coreClassName="overflow-hidden"><section aria-label="Computer workspace" className="space-y-4 p-4 sm:p-5">
      <div className="flex items-center gap-3"><span className="grid h-10 w-10 place-items-center rounded-2xl bg-muted text-muted-foreground"><Desktop size={22} weight="light" /></span>
        <div><h2 className="text-base font-semibold tracking-tight">Your computer</h2>
          <p role="status" className="mt-1 text-xs text-muted-foreground">{available === null ? "Checking..." : "Not available yet. The live browser workspace isn’t switched on for this site."}</p></div></div>
      {available === false && logins.length > 0 && <div className="space-y-2"><p className="text-xs text-muted-foreground">Saved logins</p>
        {logins.map(login => <div key={login.id} className="flex items-center justify-between gap-2 text-sm"><span className="min-w-0 break-all">{login.label}  /  {login.origin}</span>
          <IslandButton size="sm" tone="ghost" disabled={busy} onClick={() => perform(async () => { await apiClient.delete(`/computer/credentials/${login.id}`); await refreshLogins(); })}>Delete</IslandButton></div>)}</div>}
      {(statusError || error) && <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{statusError || error}</p>}
    </section></Bezel>;
  }

  const workspace = <Bezel className={`computer-workspace ${expanded ? "computer-expanded" : ""}`} coreClassName="overflow-hidden"><section aria-label="Computer workspace" className="space-y-4 p-4 sm:p-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-3"><span className="grid h-10 w-10 place-items-center rounded-2xl bg-primary/10 text-primary"><Desktop size={22} weight="light" /></span><div><h2 className="text-base font-semibold tracking-tight">Your computer</h2><p role="status" className="mt-1 text-xs text-muted-foreground">{busy ? "Working..." : running ? "Awake / sleeps when idle" : "Sleeping / ready to resume"}</p></div></div>
      <div className="flex flex-wrap gap-2">
        <IslandButton size="sm" tone="quiet" icon={expanded ? <ArrowsIn size={17} weight="light" /> : <ArrowsOut size={17} weight="light" />} onClick={() => setExpanded(!expanded)}>{expanded ? "Close large view" : "Expand"}</IslandButton>
        <IslandButton size="sm" disabled={busy} onClick={() => perform(async () => {
          await apiClient.post("/computer/start", {}, { timeout: 80000 }); setRunning(true);
        })} tone="primary">{running ? "Keep awake" : "Resume"}</IslandButton>
        <IslandButton size="sm" tone="ghost" disabled={busy || !running} onClick={() => perform(async () => {
          await apiClient.post("/computer/stop"); setRunning(false); setScreen(null); setSnapshot(null);
        })}>Stop</IslandButton>
      </div>
    </div>
    {(error || statusError) && <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{error || statusError}</p>}
    <form className="flex gap-2" onSubmit={e => { e.preventDefault(); void perform(async () => {
      // Manual navigation owns a handoff too: restart recovery and a pending
      // agent action must never race the user's Open button.
      let requestId = controlId;
      if (!requestId) {
        const request = await action("control/request", { reason: "User opened a career page" });
        requestId = request.request.id;
        await action("control/take", { requestId });
      }
      try { await action("human/navigate", { url }); setSnapshot(null); }
      finally { if (!controlId) await action("control/release", { requestId }); }
    }); }}>
      <Input leading={<Globe size={17} weight="light" />} trayClassName="flex-1" aria-label="Career page URL" required placeholder="https://careers.example.com" value={url} onChange={e => setUrl(e.target.value)} />
      <IslandButton size="sm" tone="ghost" disabled={busy || !running} type="submit">Open</IslandButton>
    </form>
    <p className="truncate px-1 text-[11px] text-muted-foreground" title={screen?.url}>{screen?.url || "Resume, then open a career page."}</p>
    <div className="flex flex-wrap items-center gap-2">
      <IslandButton size="sm" tone="ghost" disabled={busy || !running} onClick={() => perform(async () => {
        if (controlId) { await action("control/release", { requestId: controlId }); setControlId(null); }
        else { const request = await action("control/request", { reason: "User takeover for login or browser interaction" });
          await action("control/take", { requestId: request.request.id }); setControlId(request.request.id); setExpanded(true); }
      })} icon={<CursorClick size={17} weight="light" />}>{controlId ? "Return to assistant" : "Take control"}</IslandButton>
      <IslandButton size="sm" tone="ghost" disabled={busy || !running} onClick={() => perform(() => action("privacy/release"))}>Finish private input</IslandButton>
      <IslandButton size="sm" tone="ghost" disabled={busy || !controlId} onClick={() => perform(() => action("human/scroll", { deltaY: 600 }))}>Scroll down</IslandButton>
    </div>
    {screen && expanded && controlId ? <LiveBrowserSurface key={controlId} ref={surface} screen={screen} disabled={busy}
      send={action} refresh={async () => { setScreen(await action("screenshot")); setSnapshot(null); }}
      onError={e => setError(getApiErrorMessage(e, "Browser input failed. Check the page, then click the field again."))}
    /> : screen ? <button type="button" aria-label={!expanded ? "Expand live browser" : controlId ? "Browser screen: click to interact" : "Browser screen: take control to interact"}
      disabled={busy || (expanded && !controlId)} className="browser-screen block w-full overflow-hidden rounded-2xl bg-muted ring-1 ring-foreground/[0.08] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      onClick={e => { if (!expanded) { setExpanded(true); return; } const bounds = e.currentTarget.getBoundingClientRect(); void perform(() => action("human/click", {
        x: (e.clientX - bounds.left) * screen.width / bounds.width,
        y: (e.clientY - bounds.top) * screen.height / bounds.height,
      })); }}>
      {/* The authenticated response stays in memory; no public screenshot URL. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`data:${screen.mime ?? "image/png"};base64,${screen.base64}`} alt="Live view of your private browser" className="block w-full" />
    </button> : <div className="flex aspect-[8/5] flex-col items-center justify-center gap-4 rounded-2xl bg-muted/40 px-6 text-center ring-1 ring-foreground/[0.06]"><span className="grid h-16 w-16 place-items-center rounded-[22px] bg-card shadow-bezel-core text-muted-foreground">{running ? <Desktop size={30} weight="light" /> : <Moon size={30} weight="light" />}</span><div><h3 className="text-sm font-medium">{running ? "Opening your browser" : "Your workspace is resting"}</h3><p className="mx-auto mt-2 max-w-64 text-xs leading-5 text-muted-foreground">{running ? "The live view will appear here shortly." : "Resume when you need it. Your browser session stays saved while it sleeps."}</p></div></div>}

    {controlId && <details className="rounded-2xl bg-primary/[0.04] p-4 ring-1 ring-primary/10"><summary className="cursor-pointer text-xs text-muted-foreground">Alternative private input</summary><form className="mt-3 space-y-3" onSubmit={e => { e.preventDefault(); void perform(async () => {
      const text = privateText; setPrivateText(""); await action("human/type", { text });
    }); }}>
      <Field label="Private input for the focused field">{id => <Input id={id} type="password" autoComplete="off" placeholder="Type securely..." value={privateText} onChange={e => setPrivateText(e.target.value)} />}</Field>
      <div className="flex gap-2"><IslandButton size="sm" tone="ghost" disabled={busy || !privateText} type="submit">Enter privately</IslandButton>
        { ["Tab", "Enter", "Backspace"].map(key => <IslandButton key={key} size="sm" tone="ghost" type="button" disabled={busy} onClick={() => perform(() => action("human/key", { key }))}>{key}</IslandButton>) }</div>
      <p className="text-xs text-muted-foreground">The model is blocked while private input is active. After login, check that no secrets are visible, finish private input, then release control.</p>
    </form></details>}
    <details className="workspace-disclosure rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <summary className="cursor-pointer text-sm font-medium">Upload saved resume to this page</summary>
      <p className="my-3 text-xs text-muted-foreground">Take control, inspect the page, then choose its file input. Upload shares the selected resume with this portal.</p>
      <IslandButton size="sm" tone="quiet" disabled={busy || !running || !controlId} onClick={() => perform(async () => { setSnapshot(await action("snapshot")); setFileRef(""); })}>Inspect upload fields</IslandButton>
      {snapshot && <div className="mt-3 space-y-3">
        <Select aria-label="Resume to upload" value={resumeId} onChange={e => setResumeId(e.target.value)}><option value="">Choose saved resume</option>{resumes.map(doc => <option key={doc.id} value={doc.id}>{doc.filename}{doc.is_primary ? " (active)" : ""}</option>)}</Select>
        <Select aria-label="Portal file input" value={fileRef} onChange={e => setFileRef(e.target.value)}><option value="">Choose file input</option>{snapshot.elements.map(item => <option key={item.ref} value={item.ref}>{item.name || item.role} ({item.ref})</option>)}</Select>
        <IslandButton size="sm" tone="primary" disabled={busy || !controlId || !resumeId || !fileRef} onClick={() => perform(async () => {
          await apiClient.post("/computer/action", { operation: "upload", computer_run: snapshot.computer_run, approved_snapshot: snapshot.snapshotId,
            parameters: { document_id: resumeId, ref: fileRef, snapshotId: snapshot.snapshotId } }, { timeout: 80000 });
          setSnapshot(null); setFileRef("");
        })}>Upload selected resume to portal</IslandButton>
      </div>}
      {!resumes.length && <p className="mt-3 text-xs">Save a resume using Upload resume in Copilot or the Resume page first.</p>}
    </details>
    <details className="workspace-disclosure rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]">
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium"><LockKey size={18} weight="light" /> Saved portal logins <span className="ml-auto text-xs font-normal text-muted-foreground">{logins.length} saved</span><CaretDown className="disclosure-chevron" size={14} weight="light" /></summary>
      <p className="my-2 text-xs text-muted-foreground">Encrypted storage. Passwords cannot be retrieved by the model. The browser must receive them to sign in.</p>
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); const form = e.currentTarget; const data = new FormData(form);
        void perform(async () => { await apiClient.post("/computer/credentials", Object.fromEntries(data)); form.reset(); await refreshLogins(); }); }}>
        <Field label="Portal origin">{id => <Input id={id} name="origin" placeholder="https://jobs.example.com" required />}</Field>
        <Field label="Login label">{id => <Input id={id} name="label" placeholder="Portal name" required maxLength={100} />}</Field>
        <Field label="Portal username">{id => <Input id={id} name="username" placeholder="Username / email" autoComplete="off" required />}</Field>
        <Field label="Portal password">{id => <Input id={id} name="password" placeholder="Password" type="password" autoComplete="new-password" required />}</Field>
        <IslandButton size="sm" tone="ghost" disabled={busy} type="submit">Save login</IslandButton>
      </form>
      <div className="mt-3 space-y-2">
        {logins.map(login => <div key={login.id} className="flex items-center justify-between gap-2 text-sm"><span className="min-w-0 break-all">{login.label}  /  {login.origin}</span>
          <IslandButton size="sm" tone="ghost" disabled={busy} onClick={() => perform(async () => { await apiClient.delete(`/computer/credentials/${login.id}`); await refreshLogins(); })}>Delete</IslandButton></div>)}
        <IslandButton size="sm" tone="ghost" disabled={busy || !running} onClick={() => perform(async () => { setSnapshot(await action("snapshot")); })}>Inspect login fields</IslandButton>
        {snapshot && <><Select aria-label="Saved login" value={loginId} onChange={e => setLoginId(e.target.value)}><option value="">Choose saved login</option>{logins.map(login => <option key={login.id} value={login.id}>{login.label}</option>)}</Select>
          <p className="text-xs">Choose the username and password inputs from the current page.</p>
          {[["Username field", userRef, setUserRef], ["Password field", passRef, setPassRef]].map(([label, value, change]) => <Select key={String(label)} aria-label={String(label)} value={String(value)} onChange={e => (change as (v: string) => void)(e.target.value)}>
            <option value="">{String(label)}</option>{snapshot.elements.filter(item => item.role === "textbox").map(item => <option key={item.ref} value={item.ref}>{item.name || item.ref} ({item.ref})</option>)}</Select>)}
          <IslandButton size="sm" tone="ghost" disabled={busy || !loginId || !userRef || !passRef} onClick={() => perform(async () => {
            await apiClient.post("/computer/credentials/fill", { credential_id: loginId, username_ref: userRef, password_ref: passRef, snapshot_id: snapshot.snapshotId, computer_run: snapshot.computer_run }); setSnapshot(null);
          })}>Fill saved login privately</IslandButton>
        </>}
      </div>
    </details>
    <details className="workspace-disclosure rounded-2xl bg-muted/30 p-4 ring-1 ring-foreground/[0.06]"><summary className="flex cursor-pointer items-center gap-2 text-sm font-medium"><ArrowRight size={18} weight="light" /> Browser task <CaretDown className="disclosure-chevron ml-auto" size={14} weight="light" /></summary><form className="mt-4 space-y-3" onSubmit={e => { e.preventDefault(); void perform(async () => {
      const result = await apiClient.post("/agents/run", { task_type: "computer_task", context: { task } }); setRunId(result.data.run_id);
    }); }}>
      <Field label="Ask the browser assistant">{id => <Textarea id={id} value={task} onChange={e => setTask(e.target.value)} placeholder="Prepare the test application using my details..." required maxLength={8000} />}</Field>
      <IslandButton size="sm" tone="ghost" disabled={busy || !running || !task.trim()} type="submit">Start browser task</IslandButton>
      <p className="text-xs text-muted-foreground">Each browser change needs review. Your active model in Settings powers the assistant.</p>
    </form></details>
    <p className="flex items-center gap-2 px-1 text-[11px] leading-5 text-muted-foreground"><LockKey size={14} weight="light" className="shrink-0" />You review browser changes before the assistant acts.</p>
    {runId && <CopilotRunCard runId={runId} />}
  </section></Bezel>;
  return expanded ? <Dialog open onOpenChange={setExpanded}>
    <DialogContent className="computer-dialog block max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-[2rem] border-0 bg-background p-0 shadow-ambient">
      <DialogTitle className="sr-only">Your computer, expanded view</DialogTitle>
      <DialogDescription className="sr-only">View your browser at a larger size. Take control to interact. Close large view to return to chat.</DialogDescription>
      {workspace}
    </DialogContent>
  </Dialog> : workspace;
}
