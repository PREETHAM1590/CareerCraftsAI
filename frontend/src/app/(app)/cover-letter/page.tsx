"use client";

import { useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import {
  CircleNotch,
  ClockCounterClockwise,
  Copy,
  DownloadSimple,
  FileText,
  MagicWand,
  PenNib,
  Warning,
} from "@phosphor-icons/react";
import { toast } from "sonner";
import { apiClient } from "@/lib/api";
import { generateCoverLetter, type CoverLetterTone } from "@/lib/agent-run";
import { cn } from "@/lib/utils";
import {
  Bezel,
  Chip,
  EASE_OUT_EXPO,
  EmptyPanel,
  Eyebrow,
  Field,
  Hairline,
  IslandButton,
  Notice,
  Reveal,
  Screen,
  Segmented,
  Skeleton,
  StatusPill,
  Textarea,
  panelSwap,
} from "@/components/vanguard";
import { isComposing } from "@/lib/ime";

const TONES = ["Professional", "Enthusiastic", "Concise", "Story-driven"] as const;
type Tone = (typeof TONES)[number];

/** One-line description of each tone, shown under the tone picker. */
const TONE_NOTES: Record<Tone, string> = {
  Professional: "Measured and formal. Evidence first, no flourishes.",
  Enthusiastic: "Warm and energetic, with clear motivation for the role.",
  Concise: "Formal and short. Three tight paragraphs, nothing extra.",
  "Story-driven": "Opens with a concrete moment and builds the case from it.",
};

/** A generated draft kept for this session so tone variants can be compared. */
interface LetterVersion {
  id: number;
  tone: Tone;
  content: string;
  warnings: string[];
  createdAt: number;
}

/** Widths for the drafting skeleton lines, paragraph-shaped. */
const SKELETON_LINES = [
  ["w-2/5"],
  ["w-full", "w-11/12", "w-full", "w-3/5"],
  ["w-full", "w-10/12", "w-full", "w-11/12", "w-2/5"],
  ["w-full", "w-9/12", "w-1/2"],
] as const;

function countWords(text: string) {
  return text.trim().split(/\s+/).filter(Boolean).length;
}

function formatTime(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function CoverLetterPage() {
  const reduce = useReducedMotion();
  const [jd, setJd] = useState("");
  const [tone, setTone] = useState<Tone>("Professional");
  const [letter, setLetter] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [generating, setGenerating] = useState(false);
  const [versions, setVersions] = useState<LetterVersion[]>([]);
  const [activeVersionId, setActiveVersionId] = useState<number | null>(null);

  const generate = async () => {
    setGenerating(true);
    const toneMap: Record<Tone, CoverLetterTone> = {
      Professional: "formal",
      Concise: "concise",
      Enthusiastic: "casual",
      "Story-driven": "story",
    };
    try {
      const data = await generateCoverLetter(toneMap[tone], jd.trim());
      setWarnings(data.warnings);
      if (data.content) {
        const content = data.content;
        setLetter(content);
        const id = Date.now();
        setVersions((prev) => [...prev, { id, tone, content, warnings: data.warnings, createdAt: id }]);
        setActiveVersionId(id);
        await apiClient.post(`/agents/${data.runId}/approve`, { approved: true });
        toast.success("Cover letter generated");
      } else {
        toast.error("We couldn’t generate a cover letter. Check your active AI model in Settings and try again.");
      }
    } catch (error: unknown) {
      const apiError = error as { response?: { status?: number; data?: { detail?: string } } };
      if (apiError.response?.status === 400) {
        toast.error("Add a job description before generating your cover letter.");
      } else if (apiError.response?.status === 429) {
        toast.error("Wait for your current agent runs to finish, then try again.");
      } else {
        toast.error("We couldn’t generate a cover letter. Check your active AI model in Settings and try again.");
      }
    } finally {
      setGenerating(false);
    }
  };

  const copyToClipboard = () => {
    if (letter) {
      navigator.clipboard.writeText(letter);
      toast.success("Copied to clipboard");
    }
  };

  const downloadTxt = () => {
    if (!letter) return;
    const blob = new Blob([letter], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "cover-letter.txt";
    a.click();
    URL.revokeObjectURL(url);
  };

  /** Edits in the paper update both the working buffer and the active version. */
  const editLetter = (next: string) => {
    setLetter(next);
    if (activeVersionId !== null) {
      setVersions((prev) => prev.map((v) => (v.id === activeVersionId ? { ...v, content: next } : v)));
    }
  };

  const selectVersion = (version: LetterVersion) => {
    setActiveVersionId(version.id);
    setLetter(version.content);
    setWarnings(version.warnings);
  };

  const canGenerate = !generating && Boolean(jd.trim());
  const activeVersion = versions.find((v) => v.id === activeVersionId) ?? null;
  const wordCount = countWords(letter);
  const jdWords = countWords(jd);

  const enter = (delay: number) =>
    reduce
      ? { initial: { opacity: 0 }, animate: { opacity: 1 }, transition: { duration: 0.2 } }
      : {
          initial: { opacity: 0, y: 28, filter: "blur(10px)" },
          animate: { opacity: 1, y: 0, filter: "blur(0px)", transitionEnd: { filter: "none" } },
          transition: { duration: 0.9, ease: EASE_OUT_EXPO, delay },
        };

  const paperState = generating ? "drafting" : letter ? "letter" : "empty";

  return (
    <Screen>
      <div className="grid grid-cols-1 gap-6 pt-2 md:pt-4 lg:grid-cols-12 lg:gap-8">
        {/* ── Left: editorial headline + brief ───────────────────────────── */}
        <div className="min-w-0 lg:col-span-5">
          <div className="space-y-6 lg:sticky lg:top-24">
            <header>
              <motion.div {...enter(0)}>
                <Eyebrow>AI Writer</Eyebrow>
              </motion.div>
              <motion.h1
                {...enter(0.06)}
                className="mt-4 text-balance font-geist text-[clamp(2rem,3.5vw,3.5rem)] font-semibold leading-[1.05] tracking-[-0.045em] text-foreground"
              >
                Cover Letter
                <span className="block text-muted-foreground/70">in your voice.</span>
              </motion.h1>
              <motion.p
                {...enter(0.12)}
                className="mt-6 max-w-[46ch] text-pretty text-[15px] leading-7 text-muted-foreground"
              >
                AI‑generated, tone‑aware cover letters tailored to each job description. Draft, compare tones, then edit on the page.
              </motion.p>
            </header>

            <motion.form
              {...enter(0.18)}
              className="space-y-7"
              aria-label="Cover letter brief"
              onSubmit={(e) => {
                e.preventDefault();
                if (canGenerate) void generate();
              }}
            >
              <Field
                label={
                  <span className="inline-flex items-center gap-1.5">
                    <FileText size={14} weight="light" aria-hidden />
                    Job description
                    <span className="text-muted-foreground/60">(required)</span>
                  </span>
                }
                hint={
                  <span className="flex items-start justify-between gap-4">
                    <span>Paste the full posting. Ctrl/⌘ + Enter generates.</span>
                    <span className="shrink-0 font-geist-mono text-[11px] tabular-nums text-muted-foreground/70">
                      {jdWords} {jdWords === 1 ? "word" : "words"}
                    </span>
                  </span>
                }
              >
                {(id) => (
                  <Textarea
                    id={id}
                    name="job_description"
                    value={jd}
                    onChange={(e) => setJd(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !isComposing(e) && canGenerate) {
                        e.preventDefault();
                        void generate();
                      }
                    }}
                    placeholder="Paste the job description here to get a tailored cover letter…"
                    className="h-40 min-h-40 resize-none md:h-44"
                    aria-required="true"
                  />
                )}
              </Field>

              <div className="space-y-3">
                <p id="cover-letter-tone-label" className="pl-1 text-[12px] font-medium tracking-[-0.005em] text-muted-foreground">
                  Tone
                </p>
                <Segmented<Tone>
                  value={tone}
                  onChange={setTone}
                  asTabs={false}
                  ariaLabel="Tone"
                  size="sm"
                  options={TONES.map((t) => ({ value: t, label: t }))}
                />
                <p aria-live="polite" className="pl-1 text-xs leading-5 text-muted-foreground">
                  {TONE_NOTES[tone]}
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-4">
                <IslandButton
                  type="submit"
                  tone="primary"
                  size="md"
                  disabled={!canGenerate}
                  icon={
                    generating ? (
                      <CircleNotch size={16} weight="light" className="animate-spin" />
                    ) : (
                      <MagicWand size={16} weight="light" />
                    )
                  }
                  trailing={generating ? undefined : true}
                >
                  {generating ? "Generating…" : "Generate Cover Letter"}
                </IslandButton>
                {versions.length > 0 && !generating ? (
                  <span className="text-xs text-muted-foreground">
                    Generating again keeps earlier drafts as versions.
                  </span>
                ) : null}
              </div>
            </motion.form>
          </div>
        </div>

        {/* ── Right: the letter on paper ──────────────────────────────────── */}
        <Reveal delay={0.1} className="min-w-0 lg:col-span-7">
          <section aria-label="Cover letter draft" className="space-y-4">
            {/* Toolbar */}
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <h2 className="font-geist text-[15px] font-semibold tracking-[-0.015em] text-foreground">Cover Letter</h2>
                <span aria-live="polite">
                  {generating ? (
                    <StatusPill tone="primary" live>Drafting</StatusPill>
                  ) : letter ? (
                    <StatusPill tone="success">Draft ready</StatusPill>
                  ) : (
                    <StatusPill>Awaiting brief</StatusPill>
                  )}
                </span>
              </div>
              {letter ? (
                <div className="flex items-center gap-2">
                  <IslandButton tone="ghost" size="sm" onClick={copyToClipboard} icon={<Copy size={14} weight="light" />}>
                    Copy
                  </IslandButton>
                  <IslandButton tone="ghost" size="sm" onClick={downloadTxt} icon={<DownloadSimple size={14} weight="light" />}>
                    Download
                  </IslandButton>
                </div>
              ) : null}
            </div>

            {/* Versions / tone variants */}
            {versions.length > 0 ? (
              <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Draft versions">
                <span className="mr-1 inline-flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                  <ClockCounterClockwise size={13} weight="light" aria-hidden />
                  Versions
                </span>
                {versions.map((v, i) => (
                  <Chip
                    key={v.id}
                    active={v.id === activeVersionId}
                    disabled={generating}
                    onClick={() => selectVersion(v)}
                  >
                    <span className="font-geist-mono tabular-nums">v{i + 1}</span>
                    <span aria-hidden className="opacity-40">·</span>
                    {v.tone}
                  </Chip>
                ))}
              </div>
            ) : null}

            {warnings.length > 0 ? (
              <div role="alert">
                <Notice tone="warning" icon={<Warning size={16} weight="light" />}>
                  <p className="font-medium">Warnings</p>
                  <ul className="mt-1.5 list-disc space-y-1 pl-5">
                    {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
                  </ul>
                </Notice>
              </div>
            ) : null}

            {/* Paper */}
            <Bezel size="lg" lifted coreClassName="flex flex-col overflow-hidden">
              <div className="flex items-center justify-between gap-3 px-6 py-4 md:px-10">
                <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                  <PenNib size={14} weight="light" aria-hidden />
                  {activeVersion ? `${activeVersion.tone} tone` : `${tone} tone`}
                </span>
                <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground/80">
                  {activeVersion ? `Drafted ${formatTime(activeVersion.createdAt)}` : "Unsaved draft"}
                </span>
              </div>
              <Hairline />

              <div aria-busy={generating} className="relative min-h-[26rem] md:min-h-[30rem]">
                <AnimatePresence mode="wait" initial={false}>
                  {paperState === "drafting" ? (
                    <motion.div
                      key="drafting"
                      variants={panelSwap}
                      initial="hidden"
                      animate="show"
                      exit="exit"
                      className="space-y-7 px-6 py-10 md:px-12 md:py-12"
                    >
                      <span className="sr-only">Drafting your cover letter…</span>
                      {SKELETON_LINES.map((para, p) => (
                        <div key={p} className="space-y-3">
                          {para.map((w, i) => (
                            <Skeleton key={i} className={cn("h-3 rounded-full", w)} />
                          ))}
                        </div>
                      ))}
                    </motion.div>
                  ) : paperState === "letter" ? (
                    <motion.div
                      key="letter"
                      variants={panelSwap}
                      initial="hidden"
                      animate="show"
                      exit="exit"
                      className="h-full"
                    >
                      <label htmlFor="cover-letter-body" className="sr-only">
                        Cover letter text (editable)
                      </label>
                      <textarea
                        id="cover-letter-body"
                        value={letter}
                        onChange={(e) => editLetter(e.target.value)}
                        spellCheck
                        className="block h-[26rem] w-full resize-none bg-transparent px-6 py-10 text-[15px] leading-8 text-foreground/90 outline-none transition-colors duration-500 ease-vanguard placeholder:text-muted-foreground/60 focus-visible:bg-foreground/[0.015] md:h-[30rem] md:px-12 md:py-12 dark:focus-visible:bg-white/[0.02]"
                      />
                    </motion.div>
                  ) : (
                    <motion.div
                      key="empty"
                      variants={panelSwap}
                      initial="hidden"
                      animate="show"
                      exit="exit"
                      className="grid min-h-[26rem] place-items-center md:min-h-[30rem]"
                    >
                      <EmptyPanel
                        icon={<PenNib size={22} weight="light" />}
                        title="Your cover letter will appear here"
                        description="Paste a job description, pick a tone and generate. The draft lands on this page, ready to edit, copy or download."
                      />
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <Hairline />
              <div className="flex items-center justify-between gap-3 px-6 py-3.5 md:px-10">
                <span className="font-geist-mono text-[11px] tabular-nums text-muted-foreground/80">
                  {letter ? `${wordCount} words · ${letter.length} chars` : "0 words"}
                </span>
                <span className="text-[11px] text-muted-foreground/80">Versions last until you leave this page.</span>
              </div>
            </Bezel>
          </section>
        </Reveal>
      </div>
    </Screen>
  );
}
