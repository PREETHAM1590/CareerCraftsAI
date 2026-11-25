"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { motion } from "motion/react";
import {
  LayoutDashboard,
  FileText,
  Briefcase,
  Bot,
  MessageSquare,
  Mail,
  Send,
  Settings,
  Sparkles,
  Search,
  Users,
  Building2,
  DollarSign,
  Video,
  PenLine,
} from "lucide-react";
import { BrandLinkedin } from "@/components/icons/BrandIcons";
import { X } from "lucide-react";

const ITEMS = [
  { href: "/dashboard", icon: LayoutDashboard, label: "Dashboard" },
  { href: "/resume", icon: FileText, label: "Resume" },
  { href: "/cover-letter", icon: PenLine, label: "Cover Letter" },
  { href: "/jobs", icon: Search, label: "Jobs" },
  { href: "/applications", icon: Briefcase, label: "Applications" },
  { href: "/agents", icon: Bot, label: "Agents" },
  { href: "/nova", icon: MessageSquare, label: "Nova" },
  { href: "/email", icon: Mail, label: "Email" },
  { href: "/outreach", icon: Send, label: "Outreach" },
  { href: "/linkedin", icon: BrandLinkedin, label: "LinkedIn" },
  { href: "/leads", icon: Users, label: "Leads" },
  { href: "/interview", icon: Video, label: "Interview" },
  { href: "/company", icon: Building2, label: "Company" },
  { href: "/salary", icon: DollarSign, label: "Salary" },
  { href: "/settings", icon: Settings, label: "Settings" },
];

interface AppSidebarProps {
  /** Mobile/tablet drawer visibility. Ignored at lg+ where the sidebar is always shown. */
  mobileOpen?: boolean;
  onMobileClose?: () => void;
}

export function AppSidebar({ mobileOpen = false, onMobileClose }: AppSidebarProps) {
  const pathname = usePathname();

  const body = (
    <div className="glass-panel flex min-h-full flex-col rounded-[32px] px-4 py-5">
      <div className="mb-5 flex items-center justify-between px-2">
        <Link href="/" prefetch className="flex items-center gap-3 text-base font-semibold">
          <span className="glow-primary inline-flex h-10 w-10 items-center justify-center rounded-2xl bg-primary text-primary-foreground">
            <Sparkles className="h-4 w-4" />
          </span>
          <span>
            <span className="block font-command text-2xl font-semibold leading-none">CareerCraft</span>
            <span className="block text-[10px] uppercase tracking-[0.28em] text-muted-foreground">Command OS</span>
          </span>
        </Link>
        <button
          type="button"
          onClick={onMobileClose}
          aria-label="Close navigation"
          className="rounded-full p-1.5 text-muted-foreground hover:bg-white/[0.12] hover:text-foreground lg:hidden"
        >
          <X className="h-5 w-5" />
        </button>
      </div>
      <nav className="flex-1 space-y-1">
        {ITEMS.map((item) => {
          const Icon = item.icon;
          const active = pathname === item.href || pathname.startsWith(item.href + "/");
          return (
            <Link
              key={item.href}
              href={item.href}
              prefetch
              onClick={onMobileClose}
              aria-current={active ? "page" : undefined}
              className={`group relative flex items-center gap-3 rounded-2xl px-3 py-2 text-sm transition-colors duration-150 ${
                active
                  ? "text-foreground font-medium"
                  : "text-muted-foreground hover:bg-white/[0.12] hover:text-foreground active:scale-[0.98] dark:hover:bg-white/[0.08]"
              }`}
            >
              {active && (
                <motion.span
                  layoutId="sidebar-active-capsule"
                  transition={{ type: "spring", stiffness: 420, damping: 36 }}
                  aria-hidden
                  className="glow-primary absolute inset-0 -z-10 rounded-2xl border border-primary/40 bg-primary/15"
                />
              )}
              <Icon
                className={`h-4 w-4 shrink-0 transition-colors ${active ? "text-accent" : "text-current"}`}
              />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-5 rounded-3xl border border-white/35 bg-white/[0.10] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.35)] backdrop-blur-[18px] dark:border-white/10 dark:bg-black/[0.12]">
        <div className="text-xs uppercase tracking-[0.24em] text-primary">Human gate</div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">Applications and emails still wait for your approval.</p>
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop/tablet-landscape: persistent sidebar. Sticky (not just
          h-screen) so it stays in view instead of scrolling away once page
          content is taller than one viewport. */}
      <aside className="hidden h-dvh w-64 shrink-0 self-start overflow-y-auto p-3 lg:sticky lg:top-0 lg:flex lg:flex-col">{body}</aside>

      {/* Mobile/tablet-portrait: slide-in drawer + backdrop */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={onMobileClose}
            aria-hidden
          />
          <aside className="relative h-dvh w-72 max-w-[90vw] overflow-y-auto p-3">{body}</aside>
        </div>
      )}
    </>
  );
}
