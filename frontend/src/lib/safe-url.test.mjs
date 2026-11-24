import assert from "node:assert/strict";
import test from "node:test";
import { safeHttpUrl } from "./safe-url.ts";

test("only http(s) links are navigable", () => {
  assert.equal(safeHttpUrl("https://jobs.lever.co/acme/1"), "https://jobs.lever.co/acme/1");
  assert.equal(safeHttpUrl("http://example.com/a"), "http://example.com/a");
  for (const bad of ["javascript:alert(1)", "data:text/html,x", "  JavaScript:alert(1)", "/relative", "", null, undefined]) {
    assert.equal(safeHttpUrl(bad), null);
  }
});
