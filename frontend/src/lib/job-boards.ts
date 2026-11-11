// Pure helpers for company-portal search and multi-location job search.
// Kept import-free so node:test can run them (see job-boards.test.mjs).

export const MAX_SEARCH_LOCATIONS = 5;

export type CareersUrlCheck = { url: string; error?: undefined } | { url?: undefined; error: string };

/** Light client-side check; the server decides which portals are supported. */
export function normalizeCareersUrl(input: string): CareersUrlCheck {
  const text = input.trim();
  if (!text) return { error: "Paste a company careers link." };
  if (/\s/.test(text)) return { error: "That doesn't look like a single web link." };
  if (text.length > 2048) return { error: "That link is too long." };
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`;
  let parsed: URL;
  try {
    parsed = new URL(withScheme);
  } catch {
    return { error: "That doesn't look like a web link." };
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return { error: "Only web links (https://…) can be searched." };
  }
  if (!parsed.hostname.includes(".")) return { error: "That doesn't look like a web link." };
  return { url: withScheme };
}

/** Comma-separated places typed into one field; blanks and repeats dropped. */
export function splitPlaces(value: string): string[] {
  return dedupePlaces(value.split(","));
}

function dedupePlaces(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of values) {
    const place = raw.trim();
    const key = place.toLowerCase();
    if (place && !seen.has(key)) {
      seen.add(key);
      out.push(place);
    }
  }
  return out;
}

/**
 * Every place a search should cover. A typed place is what the member asked for;
 * otherwise all selected city chips and saved cities count (not just the first).
 * Remote searches name no city.
 */
export function pickSearchLocations(opts: {
  typed: string;
  selected: string[];
  profile: string[];
  remote: boolean;
  max?: number;
}): string[] {
  const typed = opts.typed.trim();
  if (typed) return [typed];
  if (opts.remote) return [];
  return dedupePlaces([...opts.selected, ...opts.profile]).slice(0, opts.max ?? MAX_SEARCH_LOCATIONS);
}
