"use client";

import { useEffect, useState } from "react";
import { CircleNotch, Image as ImageIcon, SealCheck } from "@phosphor-icons/react";
import { apiClient } from "@/lib/api";
import { IslandButton, PanelTitle, StatusPill } from "@/components/vanguard";

export type SubmissionAnswer = {
  label: string;
  type: string | null;
  value: string | boolean | string[] | null;
  source: string;
};

export type Submission = {
  id: string;
  application_id: string | null;
  auto_approved: boolean;
  approved_by: "member" | "auto_submit_setting";
  approved_at: string | null;
  review_url: string | null;
  answers: SubmissionAnswer[];
  has_screenshot: boolean;
};

const SOURCE_LABELS: Record<string, string> = {
  user: "your saved answer",
  profile: "your profile",
  page: "already on the page",
  generated: "AI draft",
  resume: "from your resume",
  unresolved: "entered by you",
};

function valueText(answer: SubmissionAnswer) {
  if (answer.type === "file") return "File attached";
  if (typeof answer.value === "boolean") return answer.value ? "Yes" : "No";
  if (Array.isArray(answer.value)) return answer.value.join(", ");
  return answer.value || "—";
}

/** Who approved the submission (the member or their auto-submit setting),
 * when, the answers submitted, and the review screenshot when one exists. */
export function SubmissionEvidence({ applicationId, submission }: { applicationId: string; submission: Submission }) {
  const [shot, setShot] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => () => { if (shot) URL.revokeObjectURL(shot); }, [shot]);

  async function showScreenshot() {
    setLoading(true);
    setFailed(false);
    try {
      const { data } = await apiClient.get<Blob>(`/extension/applications/${applicationId}/submission/screenshot`, { responseType: "blob" });
      setShot(URL.createObjectURL(data));
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }

  const answers = submission.answers.filter((a) => a.label);
  return (
    <section className="space-y-4">
      <PanelTitle
        title="Submission"
        icon={<SealCheck size={15} weight="light" />}
        meta={
          <StatusPill tone={submission.auto_approved ? "primary" : "success"}>
            {submission.auto_approved ? "Auto-submitted" : "Approved by you"}
          </StatusPill>
        }
      />
      <p className="text-xs leading-5 text-muted-foreground">
        {submission.auto_approved
          ? "Your auto-submit setting approved this form because every answer came from your saved answers or profile."
          : "You approved this form in the browser extension."}
        {submission.approved_at ? ` ${new Date(submission.approved_at).toLocaleString()}.` : ""}
      </p>
      {answers.length ? (
        <dl className="divide-y divide-foreground/[0.06] rounded-2xl text-sm ring-1 ring-foreground/[0.06] dark:divide-white/[0.07] dark:ring-white/10">
          {answers.map((answer, index) => (
            <div key={`${answer.label}-${index}`} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] gap-3 px-4 py-2.5">
              <dt className="min-w-0 break-words text-muted-foreground">{answer.label}</dt>
              <dd className="min-w-0 break-words text-foreground">
                {valueText(answer)}
                <span className="block text-[11px] text-muted-foreground">{SOURCE_LABELS[answer.source] ?? answer.source}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      {submission.has_screenshot ? (
        shot ? (
          // A blob URL from our own API: next/image cannot optimise it.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={shot} alt="The application form as it was reviewed before submitting" className="w-full rounded-2xl ring-1 ring-foreground/[0.08]" />
        ) : (
          <div className="flex items-center gap-3">
            <IslandButton tone="ghost" size="sm" onClick={showScreenshot} disabled={loading} icon={loading ? <CircleNotch size={14} className="animate-spin" /> : <ImageIcon size={14} weight="light" />}>
              Show the reviewed form
            </IslandButton>
            {failed ? <span role="alert" className="text-xs text-danger">The screenshot could not be loaded.</span> : null}
          </div>
        )
      ) : null}
    </section>
  );
}
