// Page logic of the LinkedIn sender (src/linkedin.js) in real Chromium against
// a LinkedIn-shaped fixture. Requests to linkedin.com are answered by the test,
// so nothing reaches LinkedIn.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { probePage, sendOnPage, linkedinCommand } from "../src/linkedin.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const profile = fs.readFileSync(path.join(here, "fixtures/linkedin.com/profile.html"), "utf8");
let failed = 0;
const check = (cond, name) => {
  console.log(`${cond ? "ok  " : "FAIL"} ${name}`);
  if (!cond) failed++;
};

const browser = await chromium.launch();
const page = await browser.newPage();
await page.route("https://www.linkedin.com/**", (route) => {
  const url = new URL(route.request().url());
  if (url.pathname.startsWith("/authwall")) {
    return route.fulfill({ contentType: "text/html", body: "<form class='login__form'><input type=email><input type=password></form>" });
  }
  return route.fulfill({ contentType: "text/html", body: profile });
});
const open = (query) => page.goto(`https://www.linkedin.com/in/priya-sharma-123/${query ? `?${query}` : ""}`);
const probe = () => page.evaluate(probePage);
const send = (kind, message) => page.evaluate(`(${sendOnPage.toString()})(${JSON.stringify(kind)}, ${JSON.stringify(message)})`);
const sent = () => page.evaluate(() => window.__sent);
const NOTE = "Hi Priya, I'm applying for the Data Analyst role at Zeta and would value a quick referral.";

// Probe
await open("degree=2nd");
let p = await probe();
check(p.degree === "2nd" && p.can_connect && !p.can_message && !p.pending, `2nd degree: Connect under More ${JSON.stringify(p)}`);
await open("degree=1st");
p = await probe();
check(p.degree === "1st" && p.can_message && !p.can_connect, `1st degree: Message ${JSON.stringify(p)}`);
await open("degree=2nd&pending=1");
p = await probe();
check(p.pending && !p.can_connect, "pending request is reported, not re-sent");
await open("degree=2nd&noconnect=1");
p = await probe();
check(!p.can_connect && !(await page.evaluate(() => document.getElementById("menu").classList.contains("hidden") === false)), "More without Connect: can_connect is false and the menu is closed again");
await page.goto("https://www.linkedin.com/authwall?trk=x");
check((await probe()).signed_out === true, "signed-out page is reported");

// Invite with a note
await open("degree=2nd");
let r = await send("invite", NOTE);
let s = await sent();
check(r.delivery === "invited" && s.kind === "invite" && s.note === NOTE, `invite sent with the exact note ${JSON.stringify(r)}`);

// Message to a connection
await open("degree=1st");
r = await send("message", "Thanks for connecting, Priya!");
s = await sent();
check(r.delivery === "messaged" && s.kind === "message" && s.text === "Thanks for connecting, Priya!", `message sent and shown in the thread ${JSON.stringify(r)}`);

// An earlier identical message must not count as this send
await open("degree=1st&old=1&stuck=1");
r = await send("message", "Thanks for connecting, Priya!");
check(r.reason === "unconfirmed" && (await sent()) === null, `same text already in the thread, Send did nothing: unconfirmed ${JSON.stringify(r)}`);
await open("degree=1st&old=1");
r = await send("message", "Thanks for connecting, Priya!");
check(r.delivery === "messaged", "same text already in the thread, Send worked: messaged");

// Limits and failures
await open("degree=2nd&nonote=1");
r = await send("invite", NOTE);
check(r.reason === "limit" && (await sent()) === null, "out of notes: limit, and no bare request instead");
await open("degree=2nd&weekly=1");
r = await send("invite", NOTE);
check(r.reason === "limit" && (await sent()) === null, "weekly limit after Send: limit");
await open("degree=2nd&silent=1");
r = await send("invite", NOTE);
check(r.reason === "unconfirmed", "Send pressed but nothing shown: unconfirmed (never 'invited')");
await open("degree=1st");
r = await send("invite", NOTE);
check(!!r.error && (await sent()) === null, "no Connect option: error, nothing sent");

// Command validation (no browser needed: rejected before any tab opens)
check((await linkedinCommand({ op: "send", as: "invite", profile_url: "https://evil.example/in/x/", message: "hi" })).status === 422, "non-LinkedIn URL refused");
check((await linkedinCommand({ op: "send", as: "invite", profile_url: "https://www.linkedin.com/in/priya-sharma-123/", message: "x".repeat(301) })).status === 422, "note over 300 characters refused");
check((await linkedinCommand({ op: "delete", profile_url: "https://www.linkedin.com/in/priya-sharma-123/" })).status === 422, "unknown operation refused");

await browser.close();
process.exit(failed ? 1 : 0);
