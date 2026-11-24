"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId, useState } from "react";
import { motion } from "motion/react";
import { Brain, Lightning, ListChecks, Notebook, Plugs, UserCircle, PawPrint, type Icon } from "@phosphor-icons/react";
import { cn } from "@/lib/utils";
import { SPRING_SOFT } from "@/components/vanguard/motion";

const sections: ReadonlyArray<{ href: string; label: string; icon: Icon }> = [
  { href: "/settings/account", label: "Account", icon: UserCircle },
  { href: "/settings/profile", label: "Automation", icon: Lightning },
  { href: "/settings/applying", label: "Applying", icon: ListChecks },
  { href: "/settings/integrations", label: "Integrations", icon: Plugs },
  { href: "/settings/models", label: "AI models & keys", icon: Brain },
  { href: "/settings/memory", label: "Memory", icon: Notebook },
  { href: "/settings/companion", label: "Companion", icon: PawPrint },
];

/**
 * Settings section switcher: a segmented pill rail of real links with a
 * spring-animated active indicator.
 *
 * Horizontal (scrollable) by default. Any ancestor carrying the
 * `settings-nav-vertical` class turns it into a full-width vertical rail on
 * lg+ (used by the account page's sticky left column).
 */
export function SettingsNav() {
  const pathname = usePathname();
  const layoutId = useId();
  // Optimistic indicator: move the pill the moment a link is pressed instead
  // of waiting for the route to resolve. Ignored once the pathname changes.
  const [optimistic, setOptimistic] = useState<{ href: string; from: string } | null>(null);
  const pendingHref = optimistic && optimistic.from === pathname ? optimistic.href : null;

  return (
    <nav
      aria-label="Settings sections"
      className={cn(
        "flex w-full max-w-full items-center gap-1 overflow-x-auto rounded-full bg-foreground/[0.035] p-1 ring-1 ring-foreground/[0.06] dark:bg-white/[0.04] dark:ring-white/10 sm:w-fit",
        "lg:[.settings-nav-vertical_&]:w-full lg:[.settings-nav-vertical_&]:flex-col lg:[.settings-nav-vertical_&]:items-stretch lg:[.settings-nav-vertical_&]:rounded-[1.5rem] lg:[.settings-nav-vertical_&]:p-1.5",
      )}
    >
      {sections.map(({ href, label, icon: SectionIcon }) => {
        const current = pathname === href || pathname.startsWith(`${href}/`);
        const highlighted = pendingHref ? pendingHref === href : current;
        return (
          <Link
            key={href}
            href={href}
            aria-current={current ? "page" : undefined}
            onClick={() => {
              if (!current) setOptimistic({ href, from: pathname });
            }}
            className={cn(
              "relative inline-flex h-10 shrink-0 items-center gap-2 rounded-full px-4 text-[13px] font-medium tracking-[-0.01em]",
              "transition-colors duration-500 ease-vanguard focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              "lg:[.settings-nav-vertical_&]:h-11 lg:[.settings-nav-vertical_&]:justify-start lg:[.settings-nav-vertical_&]:px-4",
              highlighted ? "text-foreground" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {highlighted ? (
              <motion.span
                layoutId={layoutId}
                transition={SPRING_SOFT}
                aria-hidden
                className="absolute inset-0 rounded-full bg-card shadow-[0_1px_2px_hsl(var(--foreground)/0.06),inset_0_1px_0_hsl(0_0%_100%/0.6)] ring-1 ring-foreground/[0.06] dark:bg-white/10 dark:shadow-none dark:ring-white/10"
              />
            ) : null}
            <SectionIcon aria-hidden size={16} weight="light" className="relative shrink-0" />
            <span className="relative whitespace-nowrap">{label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
