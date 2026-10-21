"use client";

import { forwardRef, useEffect, useImperativeHandle, useRef, type KeyboardEvent } from "react";

export type BrowserSurfaceHandle = { flush: () => Promise<void> };
type Props = {
  screen: { base64: string; width: number; height: number; mime?: string };
  disabled: boolean;
  send: (operation: string, parameters: Record<string, unknown>) => Promise<unknown>;
  refresh: () => Promise<void>;
  onError: (error: unknown) => void;
};

// Native text input handles paste, mobile keyboards and IME. Only the focused
// screen captures input; text is never placed in React state or chat messages.
export const LiveBrowserSurface = forwardRef<BrowserSurfaceHandle, Props>(function LiveBrowserSurface(
  { screen, disabled, send, refresh, onError }, ref,
) {
  const active = useRef(true);
  const failed = useRef(false);
  const queue = useRef(Promise.resolve());
  const pending = useRef(0);
  const text = useRef("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const capture = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      text.current = "";
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  function enqueue(operation: string, parameters: Record<string, unknown>) {
    pending.current += 1;
    queue.current = queue.current.then(async () => {
      if (!active.current || failed.current) return;
      await send(operation, parameters);
      if (active.current && pending.current === 1) await refresh();
    }).catch(error => {
      failed.current = true;
      text.current = "";
      if (active.current) onError(error);
    }).finally(() => { pending.current -= 1; });
  }

  function flushText() {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    const value = text.current;
    text.current = "";
    if (value) enqueue("human/type", { text: value });
  }

  useImperativeHandle(ref, () => ({ flush: async () => { flushText(); await queue.current; } }));

  function receiveText(input: HTMLTextAreaElement) {
    if (!input.value || failed.current) { input.value = ""; return; }
    text.current += input.value;
    input.value = "";
    if (!timer.current) timer.current = setTimeout(flushText, 100);
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.key === "Process" || event.key === "Dead") return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); flushText(); event.currentTarget.blur(); return;
    }
    const modifier = event.ctrlKey || event.metaKey || event.altKey;
    // Paste stays native so the text, including multiline/unicode, uses the
    // same private text route. Browser/OS reserved shortcuts stay local.
    if (modifier && ["v", "c", "x"].includes(event.key.toLowerCase())) return;
    const named = ["Tab", "Enter", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"];
    const selectAll = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a";
    if (!named.includes(event.key) && !selectAll) return;
    event.preventDefault(); event.stopPropagation(); flushText();
    const modifiers = [event.ctrlKey || event.metaKey ? "Control" : "", event.altKey ? "Alt" : "", event.shiftKey ? "Shift" : ""].filter(Boolean);
    enqueue("human/key", { key: [...modifiers, selectAll ? "a" : event.key].join("+") });
  }

  return <div>
    <div className="browser-screen relative overflow-hidden rounded-2xl bg-muted ring-1 ring-foreground/[0.08] focus-within:ring-2 focus-within:ring-primary">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`data:${screen.mime ?? "image/png"};base64,${screen.base64}`} alt="Live view of your private browser" className="block w-full" />
      <textarea ref={capture} aria-label="Live browser: click a field and type" aria-describedby="browser-keyboard-help"
        className="absolute inset-0 h-full w-full resize-none cursor-text opacity-0" disabled={disabled}
        autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false}
        onClick={event => {
          flushText(); failed.current = false;
          const bounds = event.currentTarget.getBoundingClientRect();
          enqueue("human/click", { x: (event.clientX - bounds.left) * screen.width / bounds.width,
            y: (event.clientY - bounds.top) * screen.height / bounds.height });
        }}
        onKeyDown={keyDown}
        onChange={event => { if (!(event.nativeEvent as InputEvent).isComposing) receiveText(event.currentTarget); }}
        onCompositionEnd={event => receiveText(event.currentTarget)}
        onBlur={flushText}
      />
    </div>
    <p id="browser-keyboard-help" className="mt-2 text-xs text-muted-foreground">Click a field in the browser and type or paste. Tab moves between fields; Esc leaves keyboard control. Your input stays private from the bot.</p>
  </div>;
});
