"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import axios from "axios";
import { motion } from "motion/react";
import { fadeUp, stagger } from "@/lib/motion-variants";
import { apiClient } from "@/lib/api";
import { Loader2, MapPin, Search, ExternalLink } from "lucide-react";
import { isComposing } from "@/lib/ime";

type DemoJob = {
  title: string;
  company: string;
  location: string;
  url: string;
  platform: string;
};

type SearchState = "idle" | "loading" | "done" | "limited" | "error";

const DEMO_LIMIT = 2;

export function DemoSection() {
  const [query, setQuery] = useState("Backend Engineer");
  const [location, setLocation] = useState("Remote");
  const [jobs, setJobs] = useState<DemoJob[]>([]);
  const [remaining, setRemaining] = useState<number | null>(null);
  const [state, setState] = useState<SearchState>("idle");
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let cancelled = false;
    apiClient
      .get<{ searches_remaining: number }>("/demo/job-search/quota")
      .then((res) => {
        if (!cancelled) setRemaining(res.data.searches_remaining);
      })
      .catch(() => {
        // Quota display is a nice-to-have — a failed pre-check shouldn't
        // block the user from trying to search.
        if (!cancelled) setRemaining(DEMO_LIMIT);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSearch = async () => {
    if (!query.trim() || state === "loading") return;
    setState("loading");
    setErrorMessage("");
    try {
      const res = await apiClient.post<{
        jobs: DemoJob[];
        searches_used: number;
        searches_remaining: number;
      }>("/demo/job-search", { query: query.trim(), location: location.trim() || "Remote" });
      setJobs(res.data.jobs);
      setRemaining(res.data.searches_remaining);
      setState("done");
    } catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 429) {
        setRemaining(0);
        setState("limited");
        return;
      }
      setErrorMessage(
        axios.isAxiosError(error) && typeof error.response?.data?.detail === "string"
          ? error.response.data.detail
          : "Search failed — please try again.",
      );
      setState("error");
    }
  };

  const outOfSearches = remaining === 0;

  return (
    <section id="demo" className="relative overflow-hidden bg-background py-28">
      <div className="absolute inset-x-0 top-1/2 h-px bg-gradient-to-r from-transparent via-primary/25 to-transparent" />
      <div className="mx-auto max-w-4xl px-6">
        <motion.div
          initial="hidden"
          whileInView="show"
          viewport={{ once: true, margin: "-80px" }}
          variants={stagger}
          className="mb-10 text-center"
        >
          <motion.div
            variants={fadeUp}
            className="mx-auto inline-flex rounded-full border border-border bg-card/70 px-3 py-1 text-xs font-medium uppercase tracking-[0.22em] text-primary"
          >
            Live Demo
          </motion.div>
          <motion.h2 variants={fadeUp} className="mt-2 text-4xl font-medium text-foreground md:text-5xl">
            See real jobs. Right now.
          </motion.h2>
          <motion.p variants={fadeUp} className="mx-auto mt-3 max-w-xl text-muted-foreground">
            Try the job search agent with no sign-up. Real, live listings — {DEMO_LIMIT} free
            searches per visitor.
          </motion.p>
        </motion.div>

        <motion.div
          initial="hidden"
          whileInView="show"
          viewport={{ once: true, margin: "-60px" }}
          variants={fadeUp}
          className="rounded-[2rem] border border-border/80 bg-card/70 p-6 shadow-[0_24px_90px_hsl(var(--background)/0.28)] backdrop-blur-xl md:p-8"
        >
          <div className="flex flex-col gap-3 sm:flex-row">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !isComposing(e) && handleSearch()}
              placeholder="Job title, e.g. Backend Engineer"
              maxLength={100}
              disabled={outOfSearches || state === "loading"}
              className="h-11 flex-1 rounded-full border border-border bg-background px-5 text-sm outline-none ring-primary/30 focus:ring-2 disabled:opacity-50"
            />
            <input
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !isComposing(e) && handleSearch()}
              placeholder="Location"
              maxLength={100}
              disabled={outOfSearches || state === "loading"}
              className="h-11 w-full rounded-full border border-border bg-background px-5 text-sm outline-none ring-primary/30 focus:ring-2 disabled:opacity-50 sm:w-40"
            />
            <button
              onClick={handleSearch}
              disabled={outOfSearches || state === "loading" || !query.trim()}
              className="inline-flex h-11 items-center justify-center gap-2 rounded-full bg-primary px-6 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {state === "loading" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Search className="h-4 w-4" />
              )}
              Search
            </button>
          </div>

          {remaining !== null && !outOfSearches && (
            <p className="mt-3 text-center text-xs text-muted-foreground">
              {remaining} of {DEMO_LIMIT} free searches left
            </p>
          )}

          {outOfSearches && (
            <div className="mt-6 rounded-2xl border border-warning/30 bg-warning/10 px-5 py-4 text-center text-sm text-warning">
              You&apos;ve used your {DEMO_LIMIT} free demo searches.{" "}
              <Link href="/register" className="font-medium underline hover:no-underline">
                Sign up free
              </Link>{" "}
              to search without limits.
            </div>
          )}

          {state === "error" && (
            <div className="mt-6 rounded-2xl border border-danger/30 bg-danger/10 px-5 py-4 text-center text-sm text-danger">
              {errorMessage}
            </div>
          )}

          {state === "done" && (
            <div className="mt-6 space-y-3">
              {jobs.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  No live listings matched that search — try a broader title.
                </p>
              ) : (
                jobs.map((job, i) => (
                  <a
                    key={`${job.url}-${i}`}
                    href={job.url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between gap-4 rounded-2xl border border-border bg-background/60 px-5 py-4 transition hover:border-primary/40 hover:bg-background"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-foreground">{job.title}</div>
                      <div className="mt-0.5 flex items-center gap-1.5 truncate text-xs text-muted-foreground">
                        <span>{job.company}</span>
                        {job.location && (
                          <>
                            <span aria-hidden>·</span>
                            <MapPin className="h-3 w-3 shrink-0" />
                            <span className="truncate">{job.location}</span>
                          </>
                        )}
                      </div>
                    </div>
                    <ExternalLink className="h-4 w-4 shrink-0 text-muted-foreground" />
                  </a>
                ))
              )}
            </div>
          )}
        </motion.div>
      </div>
    </section>
  );
}
