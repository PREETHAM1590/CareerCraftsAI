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

/**
 * Careers-portal families the server can read (backend job_connectors
 * CAREERS_URL_FAMILIES; a backend test keeps the two in step), then the
 * public aggregators. Used by the search "Source" filter.
 */
export const PORTAL_SOURCES: Record<string, string> = {
  workday: "Workday",
  oracle: "Oracle Recruiting",
  taleo: "Taleo",
  successfactors: "SAP SuccessFactors",
  zohorecruit: "Zoho Recruit",
  greenhouse: "Greenhouse",
  ashby: "Ashby",
  lever: "Lever",
  smartrecruiters: "SmartRecruiters",
  workable: "Workable",
  recruitee: "Recruitee",
  personio: "Personio",
  bamboohr: "BambooHR",
  teamtailor: "Teamtailor",
  breezy: "Breezy",
  pinpoint: "Pinpoint",
  hibob: "HiBob",
  trakstar: "Trakstar",
  jobscore: "JobScore",
  gem: "Gem",
  rippling: "Rippling",
  icims: "iCIMS",
  jobvite: "Jobvite",
  jazzhr: "JazzHR",
  dover: "Dover",
  paylocity: "Paylocity",
  keka: "Keka",
  freshteam: "Freshteam",
  kula: "Kula",
};

export const AGGREGATOR_SOURCES: Record<string, string> = {
  adzuna: "Adzuna",
  himalayas: "Himalayas",
  remotive: "Remotive",
  remoteok: "RemoteOK",
  arbeitnow: "Arbeitnow",
  jsonld: "Company sites",
};

/** [family, label] for the Source filter: portals A–Z, then aggregators. */
export function searchSources(): [string, string][] {
  const portals = Object.entries(PORTAL_SOURCES).sort((a, b) => a[1].localeCompare(b[1]));
  return [...portals, ...Object.entries(AGGREGATOR_SOURCES)];
}
