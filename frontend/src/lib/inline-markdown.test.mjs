import assert from "node:assert/strict";
import test from "node:test";
import { parseInlineMarkdown } from "./inline-markdown.ts";

test("bold, code and links become tokens and the rest stays text", () => {
  assert.deepEqual(parseInlineMarkdown("1. **Start a run** with `x`"), [
    { kind: "text", text: "1. " },
    { kind: "bold", children: [{ kind: "text", text: "Start a run" }] },
    { kind: "text", text: " with " },
    { kind: "code", text: "x" },
  ]);
  assert.deepEqual(parseInlineMarkdown("**[Role](https://a.example/j_(1))**"), [
    { kind: "bold", children: [{ kind: "link", href: "https://a.example/j_(1)", children: [{ kind: "text", text: "Role" }] }] },
  ]);
});
test("unsafe links and unmatched markers are left as plain text", () => {
  for (const text of ["[x](javascript:alert(1))", "2 ** 3 ** 4", "**open", "job_search_run", "<b>hi</b>"])
    assert.deepEqual(parseInlineMarkdown(text), [{ kind: "text", text }]);
});
