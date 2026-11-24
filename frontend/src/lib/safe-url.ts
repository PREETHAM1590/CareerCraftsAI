/** The URL when it is a plain http(s) link, else null: job links come from scraped data and must never be javascript: or data:. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  try {
    const url = new URL(value ?? "");
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
