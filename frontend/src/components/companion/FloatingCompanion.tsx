"use client";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { motion, useDragControls } from "motion/react";
import { createPortal } from "react-dom";
import Link from "next/link";
import { GearSix, X, ChatCircle, Heart, HandWaving, Moon } from "@phosphor-icons/react";
import { useCompanion } from "./CompanionProvider";
import { CompanionPet } from "./CompanionPet";
import { ApprovalModal } from "@/components/agents/ApprovalModal";
import { CompanionChat } from "./CompanionChat";

export function FloatingCompanion() {
  const { preferences, activity, loaded, refreshRuns } = useCompanion();
  const [panel, setPanel] = useState<{ key: string; open: boolean } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [view, setView] = useState<"chat" | "status">("chat");
  const [reaction, setReaction] = useState<{ kind: string; id: number; text: string } | null>(null);
  const [resting, setResting] = useState(false);
  const reactionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const panelId = useId();
  const bounds = useRef<HTMLDivElement>(null);
  const popup = useRef<HTMLElement>(null);
  const [popupShift, setPopupShift] = useState({ x: 0, y: 0 });
  const dragControls = useDragControls();
  const activityKey = `${activity.state}:${activity.run?.id ?? ""}:${activity.title}:${activity.detail}`;
  const open = panel?.open === true || (panel?.key !== activityKey && activity.state === "waiting");
  const asleep = resting && ["idle", "review"].includes(activity.state);
  function respond(kind: string, text: string) {
    if (reactionTimer.current) clearTimeout(reactionTimer.current);
    setReaction(previous => ({ kind, text, id: (previous?.id ?? 0) + 1 }));
    reactionTimer.current = setTimeout(() => setReaction(null), 1800);
  }
  useEffect(() => () => { if (reactionTimer.current) clearTimeout(reactionTimer.current); }, []);
  const run = activity.run;
  const closeReview = () => { setReviewing(false); refreshRuns(); };
  useLayoutEffect(() => {
    if (!open || !loaded || !preferences.visible) return;
    const fit = () => {
      const rect = popup.current?.getBoundingClientRect();
      if (!rect) return;
      const dx = Math.max(8, Math.min(rect.left, window.innerWidth - rect.width - 8)) - rect.left;
      const dy = Math.max(8, Math.min(rect.top, window.innerHeight - rect.height - 8)) - rect.top;
      if (Math.abs(dx) > .5 || Math.abs(dy) > .5) setPopupShift(previous => ({ x: previous.x + dx, y: previous.y + dy }));
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [open, loaded, preferences.visible, preferences.corner, preferences.size, activityKey, view, reaction?.text]);
  if (!loaded || !preferences.visible) return null;
  const label = { idle: "Ready", running: "Working", waiting: "Input needed", failed: "Needs attention", review: "Results ready" }[activity.state];
  return createPortal(<>
    <div ref={bounds} className="floating-companion-boundary">
    <motion.aside key={`${preferences.corner}:${preferences.size}`} drag dragListener={false} dragControls={dragControls} dragConstraints={bounds} dragElastic={0} dragMomentum={false} onDragStart={() => { setPanel({ key: activityKey, open: false }); setPopupShift({ x: 0, y: 0 }); respond("drag", "Whee! A new spot."); }} className={`floating-companion corner-${preferences.corner} size-${preferences.size} state-${activity.state}`} aria-label={`${preferences.name} companion`} onKeyDown={event => { if (event.key === "Escape") setPanel({ key: activityKey, open: false }); }}>
      {open && <section ref={popup} id={panelId} className="floating-companion-panel" style={{ transform: `translate(${popupShift.x}px, ${popupShift.y}px)` }} aria-label="Companion status">
        <div className="flex items-center justify-between gap-3"><span className="text-xs font-medium text-muted-foreground">{preferences.name} · Nova</span><button aria-label="Close companion status" className="rounded-full p-1" onClick={() => setPanel({ key: activityKey, open: false })}><X size={16} /></button></div>
        <div className="companion-bubble-nav"><button aria-pressed={view === "chat"} onClick={() => setView("chat")}>Chat</button><button aria-pressed={view === "status"} onClick={() => setView("status")}>Task status{activity.state === "waiting" ? " · input needed" : ""}</button></div>
        {view === "chat" ? <CompanionChat /> : <div role="status" aria-live="polite"><h2 className="mt-3 text-sm font-semibold">{activity.title}</h2><p className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{activity.detail}</p></div>}
        <div className="mt-4 flex flex-wrap items-center gap-3 text-xs">
          {run?.status === "awaiting_approval" && run.output && <button className="rounded-full bg-primary px-3 py-2 font-medium text-primary-foreground" onClick={() => setReviewing(true)}>Review action</button>}
          {run && <Link href={`/agents?run=${encodeURIComponent(run.id)}`} className="text-primary underline underline-offset-4">View task</Link>}
          <Link href="/nova" className="inline-flex items-center gap-1 text-primary"><ChatCircle size={16} />Open chat</Link>
          <Link href="/settings/companion" className="ml-auto inline-flex items-center gap-1 text-muted-foreground"><GearSix size={16} />Settings</Link>
        </div>
        <div className="companion-play-controls" aria-label="Play with your companion">
          <button onClick={() => { setResting(false); respond("pet", "Happy to be here with you!"); }}><Heart size={15} />Pet</button>
          <button onClick={() => { setResting(false); respond("wave", "Hello, you!"); }}><HandWaving size={15} />Wave</button>
          <button aria-pressed={resting} onClick={() => { setResting(!resting); respond(resting ? "wave" : "sleep", resting ? "I'm back!" : "Resting until you need me."); }}><Moon size={15} />{resting ? "Wake" : "Rest"}</button>
        </div>
      </section>}
      {reaction && <span className="pet-reaction-caption" role="status" aria-label="Pet reaction">{reaction.text}</span>}
      <motion.button className="floating-companion-trigger" aria-label={`${open ? "Close" : "Open"} ${preferences.name} companion status`} aria-expanded={open} aria-controls={panelId} onPointerDown={event => dragControls.start(event)} onTap={() => { respond("wave", "Hello, you!"); setView(activity.state === "waiting" ? "status" : "chat"); setPanel({ key: activityKey, open: !open }); }}>
        <CompanionPet preferences={preferences} state={activity.state} reaction={reaction} resting={asleep} />
        <span className="floating-companion-label" role="status" aria-live="polite"><span className="floating-status-dot" /><span>{label}</span></span>
      </motion.button>
    </motion.aside>
    </div>
    {reviewing && run?.status === "awaiting_approval" && run.output && <ApprovalModal runId={run.id} action={run.output} onApprove={closeReview} onCancel={closeReview} />}
  </>, document.body);
}
