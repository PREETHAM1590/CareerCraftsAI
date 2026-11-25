export type InlineToken =
  | { kind: "text" | "code"; text: string }
  | { kind: "bold"; children: InlineToken[] }
  | { kind: "link"; href: string; children: InlineToken[] };

/** Bold, inline code and http(s) links, for the companion's small chat bubble. Anything else stays as written. */
export function parseInlineMarkdown(text: string): InlineToken[] {
  const pattern = /`([^`\n]+)`|\*\*(?=\S)([^\n]+?)(?<=\S)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)/g;
  const tokens: InlineToken[] = [];
  let end = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > end) tokens.push({ kind: "text", text: text.slice(end, match.index) });
    if (match[1] !== undefined) tokens.push({ kind: "code", text: match[1] });
    else if (match[2] !== undefined) tokens.push({ kind: "bold", children: parseInlineMarkdown(match[2]) });
    else tokens.push({ kind: "link", href: match[4], children: parseInlineMarkdown(match[3]) });
    end = match.index + match[0].length;
  }
  if (end < text.length) tokens.push({ kind: "text", text: text.slice(end) });
  return tokens;
}
