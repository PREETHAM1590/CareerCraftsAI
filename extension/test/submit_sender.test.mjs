import assert from "node:assert/strict";
import { appOrigin, jobUrl, taskSenderAllowed, snapshotKey } from "../src/common.js";

const active = { taskId: "task", tabId: 7 };
const sender = { id: "extension", frameId: 0, tab: { id: 7 }, url: "https://jobs.lever.co/company/posting" };
assert(taskSenderAllowed(sender, active, { taskId: "task" }, "extension"));
assert(!taskSenderAllowed({ ...sender, tab: { id: 8 } }, active, { taskId: "task" }, "extension"));
assert(!taskSenderAllowed({ ...sender, frameId: 1 }, active, { taskId: "task" }, "extension"));
assert(!taskSenderAllowed(sender, active, { taskId: "another" }, "extension"));
assert(!taskSenderAllowed(sender, active, { taskId: "task", url: "https://evil.example/form" }, "extension"));
assert.throws(() => jobUrl("http://localhost/form"));
assert.throws(() => jobUrl("https://127.0.0.1/form"));
for (const host of ["172.20.1.5", "100.64.0.1", "[::1]", "2130706433", "intranet.local", "wiki.corp.internal", "printer.lan."]) {
  assert.throws(() => jobUrl(`https://${host}/form`), host);
}
assert.equal(jobUrl("https://172.32.0.1/form").hostname, "172.32.0.1"); // public range
assert.throws(() => appOrigin("https://user:password@app.example"));
assert.equal(appOrigin("http://localhost:3000"), "http://localhost:3000");
assert.equal(snapshotKey({url: "x", fields: [{id: "a", value: "b"}]}), snapshotKey({fields: [{value: "b", id: "a"}], url: "x"}));
assert.notEqual(snapshotKey({fields: [1, 2]}), snapshotKey({fields: [2, 1]}));
console.log("Task sender, frame, origin and pairing boundary checks passed.");
