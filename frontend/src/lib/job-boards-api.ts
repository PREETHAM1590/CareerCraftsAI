// Company-portal search and saved boards.
// Backend: backend/app/api/v1/jobs.py — POST /jobs/portal-search, /jobs/boards.
import { apiClient } from "@/lib/api";
import type { ApplicationRecord } from "@/lib/applications-api";

export type SavedBoard = {
  id: string;
  source_id: string;
  family: string;
  label: string;
  careers_url: string;
  created_at: string;
};

export type PortalSearchResult = {
  board: { source_id: string; family: string; label: string; saved_board_id: string | null };
  total_found: number;
  saved_count: number;
  jobs: ApplicationRecord[];
};

export type PortalSearchRequest = {
  url: string;
  query?: string;
  locations?: string[];
  max_results?: number;
  save_board?: boolean;
};

export async function searchCompanyPortal(body: PortalSearchRequest): Promise<PortalSearchResult> {
  return (await apiClient.post<PortalSearchResult>("/jobs/portal-search", body)).data;
}

export async function listSavedBoards(): Promise<SavedBoard[]> {
  return (await apiClient.get<SavedBoard[]>("/jobs/boards")).data;
}

export async function deleteSavedBoard(id: string): Promise<void> {
  await apiClient.delete(`/jobs/boards/${id}`);
}
