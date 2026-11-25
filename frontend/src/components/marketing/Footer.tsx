"use client";

import Link from "next/link";
import { LiquidGlassButton } from "@/components/ui/LiquidGlassButton";

const COLUMNS: { title: string; links: { href: string; label: string }[] }[] = [
  {
    title: "Product",
    links: [
      { href: "/#features", label: "Features" },
      { href: "/pricing", label: "Pricing" },
      { href: "/#how", label: "How it Works" },
    ],
  },
  {
    title: "Agents",
    links: [
      { href: "/agents?focus=resume", label: "Resume Agent" },
      { href: "/agents?focus=job", label: "Job Search Agent" },
      { href: "/agents?focus=email", label: "Email Agent" },
      { href: "/agents?focus=followup", label: "Follow-up Agent" },
    ],
  },
  {
    title: "Company",
    links: [
      { href: "/about", label: "About" },
      { href: "/privacy", label: "Privacy" },
      { href: "/terms", label: "Terms" },
      { href: "/cookies", label: "Cookie Policy" },
      { href: "/refund", label: "Refund Policy" },
    ],
  },
  {
    title: "Support",
    links: [
      { href: "/docs", label: "Docs" },
      { href: "/contact", label: "Contact" },
      { href: "/status", label: "Status" },
    ],
  },
];

export function MarketingFooter() {
  return (
    <footer className="relative isolate mt-0 overflow-hidden border-t border-border bg-background text-foreground">
      {/* CTA card */}
      <div className="mx-auto max-w-7xl px-6 pt-20">
        <div className="overflow-hidden rounded-3xl border border-border bg-card px-8 py-14 text-center shadow-[0_18px_70px_hsl(var(--foreground)/0.07)]">
          <div className="mx-auto max-w-xl">
            <div className="text-xs uppercase tracking-[0.24em] text-muted-foreground">Get started</div>
            <h2 className="mt-4 text-3xl font-medium text-foreground md:text-5xl">
              Ready to land your{" "}
              <span className="font-display text-primary">dream role?</span>
            </h2>
            <p className="mt-4 text-base leading-7 text-muted-foreground">
              Use AI to tailor your resume, match jobs, and follow up — automatically.
            </p>
            <div className="mt-8 flex items-center justify-center gap-3">
              <Link href="/register">
                <LiquidGlassButton tone="primary" size="lg">
                  Start free
                </LiquidGlassButton>
              </Link>
              <Link href="/contact">
                <LiquidGlassButton tone="ghost" size="lg">
                  Talk to us
                </LiquidGlassButton>
              </Link>
            </div>
          </div>
        </div>
      </div>

      {/* Link columns */}
      <div className="relative mx-auto max-w-7xl px-6 py-16">
        <div className="grid grid-cols-2 gap-10 md:grid-cols-5">
          <div className="col-span-2">
            <div className="font-display text-3xl text-foreground">CareerCraft AI</div>
            <p className="mt-3 max-w-xs text-sm leading-6 text-muted-foreground">
              Nova, the AI job-search agent for students and freshers. Tailor resumes, match jobs, follow up — automatically.
            </p>
          </div>
          {COLUMNS.map((c) => (
            <div key={c.title}>
              <div className="text-sm font-medium text-foreground">{c.title}</div>
              <ul className="mt-4 space-y-2">
                {c.links.map((l) => (
                  <li key={l.href}>
                    <Link href={l.href} className="text-sm text-muted-foreground transition-colors hover:text-foreground">
                      {l.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="mt-14 flex items-center justify-between border-t border-border pt-6 text-xs text-muted-foreground">
          <span>© {new Date().getFullYear()} CareerCraft AI</span>
          <span>Built with care.</span>
        </div>
      </div>
    </footer>
  );
}
