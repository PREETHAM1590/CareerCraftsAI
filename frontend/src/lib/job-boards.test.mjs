import assert from "node:assert/strict";
import test from "node:test";
import { normalizeCareersUrl, pickSearchLocations, searchSources, splitPlaces } from "./job-boards.ts";

test("a pasted careers link is accepted with or without a scheme", () => {
  assert.equal(normalizeCareersUrl("https://boards.greenhouse.io/acme").url, "https://boards.greenhouse.io/acme");
  assert.equal(normalizeCareersUrl("  jobs.lever.co/acme ").url, "https://jobs.lever.co/acme");
});

test("unusable input gets a clear message", () => {
  assert.match(normalizeCareersUrl("").error, /Paste a company careers link/);
  assert.match(normalizeCareersUrl("two words").error, /single web link/);
  assert.match(normalizeCareersUrl("localhost").error, /web link/);
  assert.match(normalizeCareersUrl("ftp://files.example.com/x").error, /Only web links/);
  assert.match(normalizeCareersUrl("javascript:alert(1)").error, /web link|Only web links/);
  assert.ok(normalizeCareersUrl(`https://a.example/${"x".repeat(2100)}`).error);
});

test("several comma-separated places are all kept, once", () => {
  assert.deepEqual(splitPlaces("Pune, Mumbai ,, pune,Delhi"), ["Pune", "Mumbai", "Delhi"]);
  assert.deepEqual(splitPlaces("  "), []);
});

test("every selected city and saved city counts for a city search", () => {
  const places = pickSearchLocations({
    typed: "",
    selected: ["Mumbai", "Hyderabad"],
    profile: ["Pune", "mumbai"],
    remote: false,
  });
  assert.deepEqual(places, ["Mumbai", "Hyderabad", "Pune"]);
});

test("a typed place wins, remote names no city, and the list is capped", () => {
  assert.deepEqual(pickSearchLocations({ typed: " Lisbon ", selected: ["Pune"], profile: [], remote: false }), ["Lisbon"]);
  assert.deepEqual(pickSearchLocations({ typed: "", selected: ["Pune"], profile: [], remote: true }), []);
  const many = ["A", "B", "C", "D", "E", "F", "G"];
  assert.equal(pickSearchLocations({ typed: "", selected: many, profile: [], remote: false }).length, 5);
});

test("the source filter lists every portal family once, then the aggregators", () => {
  const sources = searchSources();
  const ids = sources.map(([id]) => id);
  assert.equal(new Set(ids).size, ids.length);
  for (const family of ["workday", "oracle", "taleo", "successfactors", "zohorecruit", "keka", "kula", "icims", "dover"]) {
    assert.ok(ids.includes(family), family);
  }
  assert.equal(ids.at(-1), "jsonld");
  assert.equal(sources.find(([id]) => id === "jsonld")[1], "Company sites");
});
