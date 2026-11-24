"use client";

import Image from "next/image";
import { useEffect, useId, useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "motion/react";
import {
  BookOpen,
  Briefcase,
  Buildings,
  CaretDown,
  ChartBar,
  ChatCircleText,
  CircleNotch,
  Lightbulb,
  Microphone,
  Play,
  Plus,
  Question as QuestionIcon,
  Quotes,
  Sparkle,
  Warning,
  YoutubeLogo,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { apiClient, getApiErrorMessage } from "@/lib/api";
import {
  REVEAL_VIEWPORT,
  Bezel,
  EASE_OUT_EXPO,
  EASE_VANGUARD,
  EmptyPanel,
  Eyebrow,
  Field,
  Input,
  IslandButton,
  Notice,
  PanelTitle,
  Reveal,
  Segmented,
  Skeleton,
  StatusPill,
  Textarea,
  bezelCore,
  bezelShell,
  listItem,
  listStagger,
  type StatusTone,
} from "@/components/vanguard";
import { cn } from "@/lib/utils";
import { ScoreRing } from "./ScoreRing";

type Category = "All" | "Technical" | "Behavioral" | "Company-Specific";
type Difficulty = "Easy" | "Medium" | "Hard";

interface Question {
  id: string;
  category: Exclude<Category, "All">;
  text: string;
  difficulty: Difficulty;
}

interface StarStory {
  id: string;
  title: string;
}

interface VideoResult {
  video_id: string;
  title: string;
  channel: string;
  thumbnail: string;
  watch_url: string;
  description: string;
}

const BASE_QUESTIONS: Question[] = [
  { id: "b1", category: "Technical", difficulty: "Medium", text: "Explain the difference between useEffect and useLayoutEffect in React. When would you use each?" },
  { id: "b2", category: "Behavioral", difficulty: "Easy", text: "Tell me about yourself and your journey as a developer." },
  { id: "b3", category: "Technical", difficulty: "Hard", text: "How would you optimize the performance of a React application with 10,000+ list items?" },
  { id: "b4", category: "Behavioral", difficulty: "Medium", text: "Describe a time you disagreed with your team lead. How did you handle it?" },
  { id: "b5", category: "Technical", difficulty: "Medium", text: "What's the difference between useMemo and useCallback? Give examples." },
  { id: "b6", category: "Behavioral", difficulty: "Easy", text: "Where do you see yourself in 5 years?" },
];

const BASE_STAR_STORIES: StarStory[] = [
  { id: "s1", title: "Led migration from Webpack to Vite — cut build time 73%" },
  { id: "s2", title: "Debugged production race condition affecting 500 users" },
];

const CATEGORY_TABS: Category[] = ["All", "Technical", "Behavioral", "Company-Specific"];

// Difficulty is a real severity signal, so it keeps semantic tones; category
// is a neutral label (the text already differentiates it).
const DIFFICULTY_TONE: Record<Difficulty, StatusTone> = {
  Easy: "success",
  Medium: "warning",
  Hard: "danger",
};

const STAR_STEPS = ["Situation", "Task", "Action", "Result"] as const;

function ScoreBar({ label, value }: { label: string; value: number }) {
  const pct = Math.max(0, Math.min(Number(value) || 0, 100));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-semibold tabular-nums text-foreground">{value > 0 ? `${value}%` : "—"}</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-foreground/[0.06] dark:bg-white/10">
        <motion.div
          initial={{ scaleX: 0 }}
          animate={{ scaleX: pct / 100 }}
          transition={{ duration: 0.9, ease: EASE_OUT_EXPO, delay: 0.3 }}
          className="h-full w-full origin-left rounded-full bg-primary"
        />
      </div>
    </div>
  );
}

function QuestionCard({ question, company, role }: { question: Question; company: string; role: string }) {
  const [open, setOpen] = useState(false);
  const [answer, setAnswer] = useState("");
  const [feedback, setFeedback] = useState<string | null>(null);
  const [gettingFeedback, setGettingFeedback] = useState(false);
  const panelId = useId();

  async function handleFeedback() {
    if (!answer.trim() || gettingFeedback) return;
    setGettingFeedback(true);
    setFeedback(null);
    try {
      const { data } = await apiClient.post("/agents/run", {
        task_type: "interview_prep",
        context: {
          mode: "feedback",
          question: question.text,
          answer,
          company,
          role,
        },
      });
      setFeedback(
        `Run started (ID: ${data.run_id}). Check Agents page for your feedback when complete.`
      );
    } catch {
      setFeedback("Could not connect to agent. Try again.");
    } finally {
      setGettingFeedback(false);
    }
  }

  return (
    <div className="py-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="mb-2.5 flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-medium uppercase tracking-[0.18em] text-muted-foreground">{question.category}</span>
            <span aria-hidden className="h-3 w-px bg-foreground/10 dark:bg-white/10" />
            <StatusPill tone={DIFFICULTY_TONE[question.difficulty]}>{question.difficulty}</StatusPill>
          </div>
          <p data-testid="prep-question-text" className="max-w-[68ch] text-[15px] font-medium leading-7 tracking-[-0.01em] text-foreground">
            {question.text}
          </p>
        </div>
        <IslandButton
          tone="ghost"
          size="sm"
          className="shrink-0 self-start"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
          trailing={
            <CaretDown
              size={13}
              weight="light"
              className={cn("transition-transform duration-500 ease-vanguard", open && "rotate-180")}
            />
          }
        >
          Practice
        </IslandButton>
      </div>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="answer-area"
            id={panelId}
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6, transition: { duration: 0.2, ease: EASE_VANGUARD } }}
            transition={{ duration: 0.45, ease: EASE_OUT_EXPO }}
          >
            <div className="mt-4 space-y-3">
              <Textarea
                aria-label={`Practice answer: ${question.text}`}
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder="Type your answer here — think out loud, structure matters…"
                rows={4}
                className="resize-none"
              />
              <div className="flex items-center justify-between gap-3">
                <span className="text-xs tabular-nums text-muted-foreground">{answer.length} characters</span>
                <IslandButton
                  tone="quiet"
                  size="sm"
                  onClick={handleFeedback}
                  disabled={gettingFeedback || !answer.trim()}
                  className="text-primary hover:text-primary"
                  icon={
                    gettingFeedback ? (
                      <CircleNotch size={14} weight="light" className="animate-spin" />
                    ) : (
                      <Sparkle size={14} weight="light" />
                    )
                  }
                >
                  Get AI feedback
                </IslandButton>
              </div>
              <div aria-live="polite">
                {feedback && (
                  <p className="rounded-2xl bg-primary/[0.06] px-4 py-2.5 text-xs leading-5 text-muted-foreground ring-1 ring-primary/15">{feedback}</p>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/**
 * Interview prep plan: target → agent-generated question bank (auto-approved
 * review run), prep score, STAR stories, pitch/questions-to-ask, YouTube
 * videos and a mock interview drawn from the generated plan's questions.
 * Logic moved intact from the former /interview-prep page.
 */
export function PrepPlanPanel({ onStartMock }: { onStartMock: () => void }) {
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [activeTab, setActiveTab] = useState<Category>("All");
  const [stories, setStories] = useState<StarStory[]>(BASE_STAR_STORIES);
  const [editingStoryId, setEditingStoryId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [reviewRunId, setReviewRunId] = useState<string | null>(null);
  const [approvedRunId, setApprovedRunId] = useState<string | null>(null);

  const { data: lastRun } = useQuery({
    queryKey: ["interview-prep-run", reviewRunId],
    queryFn: async () => {
      if (reviewRunId) {
        const { data } = await apiClient.get(`/agents/runs/${reviewRunId}`);
        return data as { id: string; agent_type: string; status: string; output: (Record<string, unknown> & { warnings?: string[] }) | null };
      }
      const { data } = await apiClient.get("/agents/runs?limit=50");
      const runs = ((Array.isArray(data) ? data : data.runs ?? []) as { id: string; agent_type: string; status: string; output: (Record<string, unknown> & { warnings?: string[] }) | null }[])
        .filter((r) => r.agent_type === "interview_prep" && ["awaiting_approval", "completed"].includes(r.status))
        .filter((r) => !reviewRunId || r.id === reviewRunId);
      return runs[0] ?? null;
    },
    refetchInterval: reviewRunId
      ? (query) => {
          const status = query.state.data?.status;
          return status === "completed" ? false : 2000;
        }
      : false,
    refetchIntervalInBackground: true,
  });

  useEffect(() => {
    if (!reviewRunId || approvedRunId === reviewRunId || lastRun?.id !== reviewRunId || lastRun.status !== "awaiting_approval" || !lastRun.output) return;
    setApprovedRunId(reviewRunId);
    apiClient.post(`/agents/${reviewRunId}/approve`, { approved: true })
      .then(() => toast.success("Interview prep generated"))
      .catch(() => {
        setApprovedRunId(null);
        toast.error("Interview prep approval failed");
      });
  }, [approvedRunId, lastRun, reviewRunId]);

  // Map agent output fields to Question[] — handles both old `questions` and new split fields
  const aiQuestions: Question[] = (() => {
    if (!lastRun?.output) return [];
    const out = lastRun.output as Record<string, unknown>;

    // New output format: behavioral_questions + technical_questions
    const behavioralQs: Question[] = Array.isArray(out.behavioral_questions)
      ? (out.behavioral_questions as string[]).map((q, i) => ({
          id: `b${i}`,
          category: "Behavioral" as const,
          text: q,
          difficulty: "Medium" as Difficulty,
        }))
      : [];

    const technicalQs: Question[] = Array.isArray(out.technical_questions)
      ? (out.technical_questions as string[]).map((q, i) => ({
          id: `t${i}`,
          category: "Technical" as const,
          text: q,
          difficulty: "Medium" as Difficulty,
        }))
      : [];

    if (behavioralQs.length > 0 || technicalQs.length > 0) {
      return [...behavioralQs, ...technicalQs];
    }

    // Fallback: legacy `questions` array
    if (Array.isArray(out.questions)) {
      return (out.questions as Question[]).map((q, i) => ({
        ...q,
        id: `ai-${i}`,
        category: (q.category as Exclude<Category, "All">) || "Company-Specific",
        difficulty: (q.difficulty as Difficulty) || "Medium",
      }));
    }

    return [];
  })();

  const elevatorPitch =
    lastRun?.output && typeof (lastRun.output as Record<string, unknown>).elevator_pitch === "string"
      ? (lastRun.output as Record<string, unknown>).elevator_pitch as string
      : null;

  const questionsToAsk =
    lastRun?.output && Array.isArray((lastRun.output as Record<string, unknown>).questions_to_ask)
      ? (lastRun.output as Record<string, unknown>).questions_to_ask as string[]
      : null;

  const warnings = Array.isArray(lastRun?.output?.warnings) ? lastRun.output.warnings : [];

  const allQuestions = [...aiQuestions, ...BASE_QUESTIONS];
  const filtered =
    activeTab === "All" ? allQuestions : allQuestions.filter((q) => q.category === activeTab);

  const generateMutation = useMutation({
    mutationFn: async (): Promise<{ run_id: string; status: string }> => {
      const { data } = await apiClient.post("/agents/run", {
        task_type: "interview_prep",
        context: {
          mode: "generate",
          company: company || "any company",
          role: role || "Software Engineer",
          count: 8,
        },
      });
      return data;
    },
    onSuccess: (data) => {
      setReviewRunId(data.run_id);
      toast.success("Generating questions — check Agents page for results");
    },
    onError: (error) => {
      toast.error(getApiErrorMessage(error, "Agent unavailable — backend not connected"));
    },
  });

  const aiScore =
    lastRun?.output && typeof (lastRun.output as Record<string, unknown>).prep_score === "number"
      ? (lastRun.output as Record<string, unknown>).prep_score as number
      : null;

  const { data: videos, isLoading: loadingVideos } = useQuery<VideoResult[]>({
    queryKey: ["interview-videos", company, role],
    queryFn: async () => {
      const { data } = await apiClient.get("/interview-prep/videos", {
        params: { company, role },
      });
      return data as VideoResult[];
    },
    enabled: !!role,
    staleTime: 24 * 60 * 60 * 1000,
  });

  const commitStoryTitle = (storyId: string) => {
    setStories((s) => s.map((x) => x.id === storyId ? { ...x, title: editingTitle.trim() || x.title } : x));
    setEditingStoryId(null);
  };

  const categoryOptions = CATEGORY_TABS.map((tab) => ({
    value: tab,
    label: tab,
    count: tab === "All" ? allQuestions.length : allQuestions.filter((q) => q.category === tab).length,
  }));

  const hasBrief = Boolean(elevatorPitch) || Boolean(questionsToAsk && questionsToAsk.length > 0) || Boolean(company);
  const mockDisabled = aiQuestions.length === 0;
  const videoList = videos ?? [];

  return (
    <>
      <div className="space-y-6">
        {/* ── Target bar: the panel's primary action ─────────────────── */}
        <Reveal>
          <Bezel lifted coreClassName="grid grid-cols-1 gap-4 p-4 md:grid-cols-2 md:p-5 lg:grid-cols-12 lg:items-end">
            <Field label="Company" className="lg:col-span-4">
              {(id) => (
                <Input
                  id={id}
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  placeholder="Company"
                  autoComplete="organization"
                  leading={<Buildings size={16} weight="light" />}
                />
              )}
            </Field>
            <Field label="Role" className="lg:col-span-4">
              {(id) => (
                <Input
                  id={id}
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  placeholder="Role"
                  autoComplete="organization-title"
                  leading={<Briefcase size={16} weight="light" />}
                />
              )}
            </Field>
            <div className="flex items-end md:col-span-2 lg:col-span-4 lg:justify-end">
              <IslandButton
                className="w-full lg:w-auto"
                onClick={() => generateMutation.mutate()}
                disabled={generateMutation.isPending}
                icon={
                  generateMutation.isPending ? (
                    <CircleNotch size={16} weight="light" className="animate-spin" />
                  ) : (
                    <Sparkle size={16} weight="light" />
                  )
                }
                trailing
              >
                {generateMutation.isPending ? "Generating…" : "Generate questions"}
              </IslandButton>
            </div>
          </Bezel>
        </Reveal>

        <div aria-live="polite">
          {generateMutation.isPending && (
            <Notice tone="primary" icon={<CircleNotch size={15} weight="light" className="animate-spin" />}>
              Generating role-specific questions for {role || "your target role"}…
            </Notice>
          )}
        </div>

        {/* ── Bento row 1: question bank (8) · launcher + score (4) ────── */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <Reveal className="lg:col-span-8">
            <Bezel coreClassName="p-5 md:p-7">
              <div className="flex flex-col gap-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <PanelTitle
                    title="Interview Questions"
                    icon={<ChatCircleText size={16} weight="light" />}
                  />
                  <div className="flex items-center gap-2">
                    <span className="rounded-full bg-foreground/[0.05] px-2.5 py-1 text-[11px] font-medium tabular-nums text-muted-foreground ring-1 ring-foreground/[0.06] dark:bg-white/[0.05] dark:ring-white/10">
                      {filtered.length} shown
                    </span>
                    {aiQuestions.length > 0 && (
                      <StatusPill tone="primary">{aiQuestions.length} AI-generated</StatusPill>
                    )}
                  </div>
                </div>
                <Segmented
                  size="sm"
                  ariaLabel="Question category"
                  value={activeTab}
                  onChange={setActiveTab}
                  options={categoryOptions}
                />
              </div>

              <div className="mt-5 h-px w-full bg-foreground/[0.07] dark:bg-white/[0.07]" aria-hidden />

              {filtered.length > 0 ? (
                <motion.ul
                  key={activeTab}
                  aria-label={`${activeTab} questions`}
                  initial="hidden"
                  animate="show"
                  variants={listStagger}
                  className="divide-y divide-foreground/[0.06] dark:divide-white/[0.07]"
                >
                  {filtered.map((q) => (
                    <motion.li key={q.id} variants={listItem}>
                      <QuestionCard question={q} company={company} role={role} />
                    </motion.li>
                  ))}
                </motion.ul>
              ) : activeTab === "Company-Specific" ? (
                <EmptyPanel
                  compact
                  icon={<Buildings size={22} weight="light" />}
                  title="No company-specific questions yet"
                  description="Enter a company name above and generate questions to get tailored interview prep."
                  action={
                    <IslandButton
                      size="sm"
                      onClick={() => generateMutation.mutate()}
                      disabled={generateMutation.isPending}
                      icon={<Sparkle size={14} weight="light" />}
                    >
                      Generate company questions
                    </IslandButton>
                  }
                />
              ) : null}
            </Bezel>
          </Reveal>

          <div className="space-y-6 lg:col-span-4">
            {warnings.length > 0 && (
              <Reveal subtle>
                <Notice tone="warning" icon={<Warning size={16} weight="light" />}>
                  <p className="font-medium">Warnings</p>
                  <ul className="mt-1.5 list-disc space-y-1 pl-4">
                    {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
                  </ul>
                </Notice>
              </Reveal>
            )}

            {/* Mock interview launcher */}
            <Reveal delay={0.05}>
              <Bezel lifted tone="primary" coreClassName="p-6">
                <PanelTitle title="Scored mock interview" icon={<Microphone size={16} weight="light" />} />
                <p className="mt-4 text-sm leading-6 text-muted-foreground">
                  AI will ask questions and evaluate your answers in real time — scored on clarity, structure, and depth.
                </p>
                {mockDisabled ? (
                  <p className="mt-3 text-xs font-medium text-warning">Generate an interview plan first</p>
                ) : (
                  <p className="mt-3 text-xs tabular-nums text-muted-foreground">{aiQuestions.length} questions from your plan</p>
                )}
                <span className="mt-6 block" title={mockDisabled ? "Generate an interview plan first" : undefined}>
                  <IslandButton
                    className="w-full"
                    onClick={onStartMock}
                    disabled={mockDisabled}
                    trailing={<Play size={15} weight="light" />}
                  >
                    Start mock interview
                  </IslandButton>
                </span>
              </Bezel>
            </Reveal>

            {/* AI Prep Score */}
            <Reveal delay={0.1}>
              <Bezel coreClassName="p-6">
                <PanelTitle title="AI Prep Score" icon={<ChartBar size={16} weight="light" />} />
                {aiScore !== null ? (
                  <div className="mt-5 space-y-5">
                    <div className="flex items-center gap-5">
                      <ScoreRing value={aiScore} size={96} stroke={7} suffix="%" label="Prep score" />
                      <StatusPill tone={aiScore >= 80 ? "success" : aiScore >= 60 ? "warning" : "danger"}>
                        {aiScore >= 80 ? "Strong" : aiScore >= 60 ? "Good" : "Needs Work"}
                      </StatusPill>
                    </div>
                    <div className="space-y-3">
                      <ScoreBar label="Technical" value={
                        (lastRun?.output as Record<string, unknown>)?.technical_score as number ?? 0
                      } />
                      <ScoreBar label="Behavioral" value={
                        (lastRun?.output as Record<string, unknown>)?.behavioral_score as number ?? 0
                      } />
                    </div>
                  </div>
                ) : (
                  <p className="mt-4 text-sm leading-6 text-muted-foreground">
                    Generate questions and practice answers to see your prep score.
                  </p>
                )}
              </Bezel>
            </Reveal>
          </div>
        </div>

        {/* ── Bento row 2: STAR stories (7) · briefing stack (5) ──────── */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <Reveal className={hasBrief ? "lg:col-span-7" : "lg:col-span-12"}>
            <Bezel coreClassName="p-5 md:p-7">
              <PanelTitle
                title="STAR Story Builder"
                meta="Build answer frameworks"
                icon={<BookOpen size={16} weight="light" />}
              />
              <div className={cn("mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2", !hasBrief && "lg:grid-cols-3")}>
                {stories.map((story) => (
                  <div
                    key={story.id}
                    className="flex flex-col justify-between gap-4 rounded-[1.25rem] bg-foreground/[0.025] p-4 ring-1 ring-foreground/[0.06] dark:bg-white/[0.03] dark:ring-white/[0.08]"
                  >
                    {editingStoryId === story.id ? (
                      <Input
                        autoFocus
                        aria-label="Story title"
                        value={editingTitle}
                        onChange={(e) => setEditingTitle(e.target.value)}
                        onBlur={() => commitStoryTitle(story.id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.nativeEvent.isComposing) commitStoryTitle(story.id);
                          if (e.key === "Escape") setEditingStoryId(null);
                        }}
                        className="h-9 text-[13px] font-medium"
                      />
                    ) : (
                      <button
                        type="button"
                        title="Click to edit"
                        onClick={() => { setEditingStoryId(story.id); setEditingTitle(story.title); }}
                        className="rounded-lg text-left text-[13px] font-medium leading-5 text-foreground transition-colors duration-500 ease-vanguard hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {story.title}
                      </button>
                    )}
                    <ol className="grid grid-cols-4 gap-1" aria-label="STAR steps">
                      {STAR_STEPS.map((step) => (
                        <li
                          key={step}
                          className="rounded-full bg-card px-1.5 py-1 text-center text-[10px] text-muted-foreground ring-1 ring-foreground/[0.06] dark:bg-white/[0.04] dark:ring-white/10"
                        >
                          {step}
                        </li>
                      ))}
                    </ol>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => setStories((s) => [...s, { id: `s${Date.now()}`, title: "New story — click to edit" }])}
                  className="group flex min-h-28 flex-col items-center justify-center gap-2 rounded-[1.25rem] border border-dashed border-foreground/[0.12] text-sm font-medium text-muted-foreground transition-[background-color,color,transform] duration-500 ease-vanguard hover:bg-foreground/[0.03] hover:text-foreground active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:border-white/15 dark:hover:bg-white/[0.03]"
                >
                  <span aria-hidden className="grid h-8 w-8 place-items-center rounded-full bg-foreground/[0.05] transition-transform duration-500 ease-vanguard group-hover:scale-105 dark:bg-white/10">
                    <Plus size={15} weight="light" />
                  </span>
                  Add new story
                </button>
              </div>
            </Bezel>
          </Reveal>

          {hasBrief && (
            <div className="space-y-6 lg:col-span-5">
              {elevatorPitch && (
                <Reveal delay={0.05}>
                  <Bezel coreClassName="p-6">
                    <PanelTitle title="Elevator Pitch" icon={<Quotes size={16} weight="light" />} />
                    <p className="mt-4 text-[15px] leading-7 text-muted-foreground">{elevatorPitch}</p>
                  </Bezel>
                </Reveal>
              )}

              {questionsToAsk && questionsToAsk.length > 0 && (
                <Reveal delay={0.1}>
                  <Bezel coreClassName="p-6">
                    <PanelTitle title="Questions to Ask" icon={<QuestionIcon size={16} weight="light" />} />
                    <p className="mt-2 text-xs text-muted-foreground">Ask the interviewer these at the end</p>
                    <ol className="mt-4 space-y-3">
                      {questionsToAsk.map((q, i) => (
                        <li key={i} className="flex items-start gap-3 text-sm">
                          <span className="mt-0.5 font-geist-mono text-[11px] tabular-nums text-primary">
                            {String(i + 1).padStart(2, "0")}
                          </span>
                          <span className="leading-6 text-foreground">{q}</span>
                        </li>
                      ))}
                    </ol>
                  </Bezel>
                </Reveal>
              )}

              {company && (
                <Reveal delay={0.15}>
                  <Bezel coreClassName="p-6">
                    <PanelTitle title={`Company: ${company}`} icon={<Buildings size={16} weight="light" />} />
                    <p className="mt-4 text-sm leading-6 text-muted-foreground">
                      Generate questions targeting <span className="font-medium text-foreground">{company}</span> to get
                      company-specific interview preparation.
                    </p>
                    <IslandButton
                      tone="ghost"
                      size="sm"
                      className="mt-5 w-full"
                      onClick={() => generateMutation.mutate()}
                      disabled={generateMutation.isPending}
                      icon={<Lightbulb size={14} weight="light" />}
                    >
                      Generate {company}-specific questions
                    </IslandButton>
                  </Bezel>
                </Reveal>
              )}
            </div>
          )}
        </div>

        {/* ── Watch & Prepare — YouTube videos ─────────────────────────── */}
        {role && (
          <section aria-labelledby="prep-videos-heading" className="space-y-6 pt-10 md:pt-14">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <Eyebrow className="mb-4">Study</Eyebrow>
                <h2 id="prep-videos-heading" className="font-geist text-2xl font-semibold tracking-[-0.03em] text-foreground md:text-3xl">
                  Watch &amp; Prepare
                </h2>
              </div>
              <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <YoutubeLogo size={15} weight="light" aria-hidden />
                Sourced from YouTube
              </span>
            </div>
            {loadingVideos ? (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-busy="true" aria-label="Loading videos">
                <Skeleton className="aspect-video sm:col-span-2 sm:row-span-2 sm:aspect-auto sm:min-h-72" />
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-44" />
                ))}
              </div>
            ) : videoList.length > 0 ? (
              <motion.div
                initial="hidden"
                whileInView="show"
                viewport={REVEAL_VIEWPORT}
                variants={listStagger}
                className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4"
              >
                {videoList.map((v, i) => {
                  const featured = i === 0;
                  return (
                    <motion.a
                      key={v.video_id}
                      variants={listItem}
                      href={v.watch_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={cn(
                        bezelShell("md"),
                        "group block focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        featured && "sm:col-span-2 lg:row-span-2",
                      )}
                    >
                      <div className={cn(bezelCore("md"), "flex h-full flex-col overflow-hidden")}>
                        <div className={cn("relative w-full overflow-hidden", featured ? "aspect-video lg:aspect-auto lg:min-h-64 lg:flex-1" : "aspect-video")}>
                          <Image
                            src={v.thumbnail}
                            alt={v.title}
                            width={featured ? 640 : 320}
                            height={featured ? 360 : 180}
                            className="absolute inset-0 h-full w-full object-cover transition-transform duration-700 ease-vanguard group-hover:scale-[1.03]"
                          />
                          <div className="absolute inset-0 grid place-items-center bg-black/25 opacity-0 transition-opacity duration-500 ease-vanguard group-hover:opacity-100">
                            <span className="grid h-12 w-12 place-items-center rounded-full bg-white/90 text-black">
                              <Play size={18} weight="light" aria-hidden />
                            </span>
                          </div>
                        </div>
                        <div className={cn("p-4", featured && "md:p-5")}>
                          <p className={cn("line-clamp-2 font-medium leading-snug text-foreground", featured ? "text-[15px]" : "text-[13px]")}>{v.title}</p>
                          <p className="mt-1 text-xs text-muted-foreground">{v.channel}</p>
                        </div>
                      </div>
                    </motion.a>
                  );
                })}
              </motion.div>
            ) : null}
          </section>
        )}
      </div>


    </>
  );
}
