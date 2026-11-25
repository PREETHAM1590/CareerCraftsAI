import type { ApplicationFilters } from "./applications-api";

export function applicationFilterParams(filters: ApplicationFilters): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  if (filters.status) params.status = filters.status;
  if (filters.minMatch != null) params.min_match = filters.minMatch;
  if (filters.foundAfter) params.found_after = filters.foundAfter;
  if (filters.foundBefore) params.found_before = filters.foundBefore;
  if (filters.sort) params.sort = filters.sort;
  if (filters.location) params.location = filters.location;
  if (filters.source) params.source = filters.source;
  if (filters.postedWithinDays) params.posted_within_days = filters.postedWithinDays;
  if (filters.region) params.region = filters.region;
  if (filters.q?.trim()) params.q = filters.q.trim();
  return params;
}

