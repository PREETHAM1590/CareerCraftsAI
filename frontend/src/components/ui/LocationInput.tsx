"use client";

import { useId, useRef, useState, type InputHTMLAttributes, type KeyboardEvent } from "react";
import { MapPin } from "@phosphor-icons/react";
import { cn } from "@/lib/utils";
import { suggestLocations } from "@/lib/locations";
import { isComposing } from "@/lib/ime";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
  /** Comma-separated list: suggestions complete the last entry and append ", ". */
  multiple?: boolean;
  /** Classes for the <input>. Callers supply the field styling (bare or tray). */
  inputClassName?: string;
  /** Classes for the wrapper (positioning context for the dropdown). */
  className?: string;
};

/**
 * Location field with a dropdown of matching cities (ARIA combobox). Free text
 * is always accepted; the list only helps. Works single or comma-separated.
 */
export function LocationInput({ value, onChange, multiple = false, inputClassName, className, onKeyDown, ...rest }: Props) {
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const blurTimer = useRef<number | null>(null);

  const parts = multiple ? value.split(",") : [value];
  const current = parts[parts.length - 1] ?? "";
  const picked = multiple ? parts.slice(0, -1).map((p) => p.trim()).filter(Boolean) : [];
  const options = suggestLocations(current, picked);
  const showList = open && options.length > 0;

  function choose(option: string) {
    onChange(multiple ? [...picked, option].join(", ") + ", " : option);
    setActive(0);
    if (!multiple) setOpen(false);
  }

  function handleKey(e: KeyboardEvent<HTMLInputElement>) {
    if (showList && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setActive((i) => (i + (e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
      return;
    }
    if (showList && e.key === "Enter" && !isComposing(e) && current.trim() && options[active]) {
      // Pick the highlighted city instead of submitting a half-typed one.
      e.preventDefault();
      choose(options[active]);
      return;
    }
    if (e.key === "Escape" && showList) {
      e.preventDefault();
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") setOpen(true);
    onKeyDown?.(e);
  }

  return (
    <div className={cn("relative", className)}>
      <input
        {...rest}
        type="text"
        role="combobox"
        autoComplete="off"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList ? `${listId}-${active}` : undefined}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          blurTimer.current = window.setTimeout(() => setOpen(false), 120);
        }}
        onKeyDown={handleKey}
        className={inputClassName}
      />
      {showList && (
        <ul
          id={listId}
          role="listbox"
          className="absolute left-0 right-0 top-[calc(100%+0.5rem)] z-50 max-h-72 min-w-[14rem] overflow-y-auto overscroll-contain rounded-2xl bg-card p-1.5 shadow-ambient ring-1 ring-foreground/[0.08] dark:bg-background dark:ring-white/10"
          onMouseDown={(e) => e.preventDefault()}
        >
          {options.map((option, i) => (
            <li
              key={option}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                if (blurTimer.current) window.clearTimeout(blurTimer.current);
                choose(option);
              }}
              className={cn(
                "flex cursor-pointer items-center gap-2.5 rounded-xl px-3 py-2 text-sm text-foreground",
                i === active ? "bg-primary/10 text-primary" : "hover:bg-foreground/[0.04] dark:hover:bg-white/[0.05]",
              )}
            >
              <MapPin size={14} weight="light" aria-hidden className="shrink-0 opacity-70" />
              <span className="truncate">{option}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
