import { applicationFilterParams } from "./application-filters";
export { applicationFilterParams } from "./application-filters";
// Shared contract for job rows (Applications, Jobs, Agent auto-apply panel).
// Backend: backend/app/api/v1/jobs.py — GET /jobs/applications and friends.
import { apiClient } from "@/lib/api";

export type AppStage = "saved" | "applied" | "viewed" | "interview" | "offer" | "rejected";
export type ApplyState = "opened" | "applied" | "failed";
export type ApplicationSort = "found_desc" | "found_asc" | "match_desc" | "match_asc";

export type ApplicationRecord = {
  id: string;
  company: string;
  role: string;
  location: string | null;
  job_url: string | null;
  jd_text: string | null;
  match_score: number | null;
  status: AppStage;
  applied_at: string | null;
  followup_day5: string | null;
  followup_day12: string | null;
  notes: string | null;
  source: string | null;
  posted_at: string | null;
  resume_label: string | null;
  outreach_status: string | null;
  outreach_to: string | null;
  found_at: string | null;
  apply_state: ApplyState | null;
};

/** Filters every job list shares; serialisable to/from the URL. */
export type ApplicationFilters = {
  status?: AppStage;
  minMatch?: number; // 0-100
  foundAfter?: string; // ISO timestamp
  foundBefore?: string; // ISO timestamp
  sort?: ApplicationSort;
  location?: string;
  source?: string;
  postedWithinDays?: number; // 1-90, filters on the employer's posting date
  region?: "auto" | "india" | "all"; // auto follows the India first switch
  q?: string; // company / role / location contains
};

/** total and stageCounts cover the whole filtered list, not just this page. */
export type ApplicationPage = {
  items: ApplicationRecord[];
  total: number;
  stageCounts: Partial<Record<AppStage, number>>;
};

export async function fetchApplications(
  filters: ApplicationFilters = {},
  page: { offset?: number; limit?: number } = {},
): Promise<ApplicationPage> {
  const params = applicationFilterParams(filters);
  if (page.offset) params.offset = page.offset;
  if (page.limit) params.limit = page.limit;
  const res = await apiClient.get<ApplicationRecord[]>("/jobs/applications", { params });
  const total = Number(res.headers["x-total-count"] ?? res.data.length);
  let stageCounts: ApplicationPage["stageCounts"] = {};
  try {
    stageCounts = JSON.parse(res.headers["x-stage-counts"] ?? "{}");
  } catch {
    // Older backend: callers fall back to counting the loaded rows.
  }
  return { items: res.data, total, stageCounts };
}

/** Soft delete; undo with restoreApplications(ids). */
export const deleteApplications = (ids: string[]) =>
  apiClient.post<{ deleted: string[] }>("/jobs/applications/delete", { ids });

export const restoreApplications = (ids: string[]) =>
  apiClient.post<{ restored: string[] }>("/jobs/applications/restore", { ids });

/** Job description for resume tailoring; falls back to the shared job catalog. 404 = none. */
export const fetchApplicationJd = (id: string) =>
  apiClient
    .get<{ jd_text: string; role: string; company: string; source: "application" | "catalog" | "job_page" }>(
      `/jobs/applications/${id}/jd`,
    )
    .then((r) => r.data);

/** Assisted apply in the member's own browser: opened -> applied | failed. */
export const setApplyState = (id: string, state: ApplyState) =>
  apiClient.post<ApplicationRecord>(`/jobs/applications/${id}/apply-state`, { state }).then((r) => r.data);

/** Make a resume the single active one; the backend re-scores it (dashboard + resume page read ats_score). */
export const activateResume = (documentId: string) =>
  apiClient.post(`/rag/documents/${documentId}/activate`).then((r) => r.data);

/** Auto-apply rule + resume preferences on PATCH /users/me/preferences. */
export type AutoApplyPreferences = {
  auto_apply_enabled?: boolean; // the rule's pause switch
  auto_rule_min_match?: number;
  auto_rule_action?: "apply" | "outreach" | "both" | "notify";
  outreach_auto_send?: boolean;
  resume_template?: string | null;
  resume_page_target?: 1 | 2;
  resume_tailor_per_job?: boolean;
  resume_tone?: string;
  resume_prefs_set_at?: string | null; // null = not asked yet
};
