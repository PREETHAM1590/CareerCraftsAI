import assert from "node:assert/strict";
import test from "node:test";
import { companionActivity, conversationRunIds, readCompanion, modelSetupRequired, opensByItself } from "./copilot-activity.ts";
const id = "4954fb84-44e7-4f95-8a8f-2c10e32d7666";
const run = (status, output = null) => ({ id, status, agent_type: "computer_task", output });
test("only structured tool results identify current conversation runs", () => {
  assert.deepEqual(conversationRunIds([
    { role: "user", content: JSON.stringify({ run_id: id }) },
    { role: "tool", content: JSON.stringify({ run_id: "../../foreign" }) },
    { role: "tool", content: JSON.stringify({ run_id: id }) },
    { role: "tool", content: JSON.stringify({ run_id: id }) },
  ]), [id]);
});
test("approval outranks chat activity and failures; it cannot appear completed", () => {
  const activity = companionActivity([run("failed"), run("awaiting_approval", { summary: "Review upload" })], true, "old error", []);
  assert.equal(activity.state, "waiting");
  assert.equal(activity.detail, "Review upload");
});
test("a successful new task supersedes a historical failure", () => {
  assert.equal(companionActivity([run("failed"), run("completed")], false, null, []).state, "review");
});
test("browser handoff remains input-needed even when planner finished", () => {
  assert.equal(companionActivity([run("completed", { input_required: "browser_handoff", message: "Finish login" })], false, null, []).state, "waiting");
});
test("final assistant question requests input but a user answer clears it", () => {
  const messages = [{ role: "assistant", content: "Which location?" }];
  assert.equal(companionActivity([], false, null, messages).state, "waiting");
  assert.equal(companionActivity([], false, null, [...messages, { role: "user", content: "Remote" }]).state, "idle");
});
test("corrupt preferences recover; persisted options are validated", () => {
  assert.equal(readCompanion("broken").character, "mint");
  const prefs = readCompanion(JSON.stringify({ name: "x".repeat(40), character: "../../invalid", size: "invalid", corner: "invalid", motion: false, visible: false }));
  assert.equal(prefs.name.length, 24);
  assert.equal(prefs.character, "mint");
  assert.equal(prefs.size, "medium");
  assert.equal(prefs.corner, "right");
  assert.equal(prefs.motion, false);
  assert.equal(prefs.visible, false);
});
test("old companion preferences migrate while retaining name and visibility", () => {
  const prefs = readCompanion(JSON.stringify({ name: "Milo", species: "fox", color: "peach", motion: false, visible: false }));
  assert.equal(prefs.character, "orange");
  assert.equal(prefs.name, "Milo");
  assert.equal(prefs.visible, false);
  assert.equal(prefs.motion, false);
});
test("model setup response shows required input, and a newer reply clears it", () => {
  const message = { role: "assistant", content: "No active model is configured. Set one up in Settings → Models." };
  assert.equal(modelSetupRequired([message]), true);
  assert.equal(companionActivity([], false, null, [message]).state, "waiting");
  assert.equal(modelSetupRequired([{ ...message, role: "user" }]), false);
  assert.equal(modelSetupRequired([message, { role: "assistant", content: "Hello" }]), false);
});
test("the companion opens itself for a task awaiting the member, not for a chat question", () => {
  const question = companionActivity([], false, null, [{ role: "assistant", content: "Which one?" }]);
  assert.equal(question.state, "waiting");
  assert.equal(opensByItself(question), false);
  const run = { id: "r", status: "awaiting_approval", agent_type: "email", output: null };
  assert.equal(opensByItself(companionActivity([run], false, null, [])), true);
});
