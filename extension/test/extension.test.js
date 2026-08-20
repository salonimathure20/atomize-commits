"use strict";

/**
 * Tests for the extension's pure logic.
 *
 * Run with: npm test  (node --test, no framework needed)
 *
 * These cover the parts that can go wrong silently — escaping source code
 * into HTML, merging commits without losing changes, and refusing to apply a
 * plan that would drop work.
 */

const test = require("node:test");
const assert = require("node:assert");

const planner = require("../src/planner");
const panel = require("../src/panel");

const samplePlan = () => ({
  commits: [
    {
      content_key: ["def charge_card(card, amount):", "import stripe"],
      message: { full: "feat(payments): add charging", header: "feat(payments): add charging", generated_by: "model" },
      files: ["app/payments.py"],
      additions: 5,
      deletions: 0,
      reasons: ["uses charge_card defined by the other"],
      hunks: [{ id: "app/payments.py#abc123", path: "app/payments.py", symbol: "charge_card", additions: 5, deletions: 0 }]
    },
    {
      content_key: ["def verify_token(header):", "import jwt"],
      message: { full: "feat(auth): add token check", header: "feat(auth): add token check", generated_by: "model" },
      files: ["app/auth.py"],
      additions: 7,
      deletions: 0,
      reasons: [],
      hunks: [{ id: "app/auth.py#def456", path: "app/auth.py", symbol: "verify_token", additions: 7, deletions: 0 }]
    }
  ],
  warnings: [],
  unsplittable: []
});

// --- argument building -----------------------------------------------------

test("plan args include the repo and subcommand", () => {
  const args = planner.buildArgs("/tmp/repo", "plan", { provider: "gemini" });
  assert.deepStrictEqual(args, ["-m", "splitter", "--repo", "/tmp/repo", "plan"]);
});

test("no provider means the model is skipped", () => {
  const args = planner.buildArgs("/tmp/repo", "plan", { provider: "none" });
  assert.ok(args.includes("--no-model"));
});

test("history can be turned off", () => {
  const args = planner.buildArgs("/tmp/repo", "plan", { provider: "gemini", useHistory: false });
  assert.ok(args.includes("--no-history"));
});

test("extra arguments are appended", () => {
  const args = planner.buildArgs("/r", "apply", { provider: "none", extra: ["--plan", "/tmp/p.json"] });
  assert.strictEqual(args[args.length - 2], "--plan");
  assert.strictEqual(args[args.length - 1], "/tmp/p.json");
});

// --- environment -----------------------------------------------------------

test("provider and model reach the child environment", () => {
  const env = planner.buildEnv({ PATH: "/usr/bin" }, { provider: "gemini", model: "gemini-3.6-flash" });
  assert.strictEqual(env.SPLITTER_LLM, "gemini");
  assert.strictEqual(env.SPLITTER_MODEL, "gemini-3.6-flash");
  assert.strictEqual(env.PATH, "/usr/bin", "the rest of the environment survives");
});

test("no model setting leaves SPLITTER_MODEL alone", () => {
  const env = planner.buildEnv({}, { provider: "ollama", model: "" });
  assert.strictEqual(env.SPLITTER_MODEL, undefined);
});

test("buildEnv does not mutate the environment it was given", () => {
  const original = { PATH: "/usr/bin" };
  planner.buildEnv(original, { provider: "openai" });
  assert.strictEqual(original.SPLITTER_LLM, undefined);
});

// --- finding the engine ----------------------------------------------------

test("an explicit engine path is honoured", () => {
  const exists = (p) => p === "/work/engine/splitter/__init__.py";
  assert.strictEqual(planner.findEngine("/work/engine", [], exists), "/work/engine");
});

test("a wrong explicit path returns null rather than guessing", () => {
  assert.strictEqual(planner.findEngine("/nope", ["/work"], () => false), null);
});

test("the engine is found inside a workspace folder", () => {
  const exists = (p) => p === "/work/engine/splitter/__init__.py";
  assert.strictEqual(planner.findEngine("", ["/work"], exists), "/work/engine");
});

test("a workspace opened directly on the engine folder still works", () => {
  const exists = (p) => p === "/work/splitter/__init__.py";
  assert.strictEqual(planner.findEngine("", ["/work"], exists), "/work");
});

// --- parsing ---------------------------------------------------------------

test("a plan is parsed out of stdout", () => {
  const parsed = planner.parsePlan('{"commits": [], "warnings": []}');
  assert.deepStrictEqual(parsed.commits, []);
});

test("warning lines printed before the json do not break parsing", () => {
  const parsed = planner.parsePlan('note: something\n{"commits": [], "warnings": []}');
  assert.ok(Array.isArray(parsed.commits));
});

test("output with no json is an error", () => {
  assert.throws(() => planner.parsePlan("everything is on fire"), /no plan/);
});

test("json of the wrong shape is an error", () => {
  assert.throws(() => planner.parsePlan('{"nope": 1}'), /unexpected shape/);
});

// --- merging ---------------------------------------------------------------

test("merging keeps every change from both commits", () => {
  const merged = planner.mergeCommits(samplePlan(), 1, 0);
  assert.strictEqual(merged.commits.length, 1);
  const keys = merged.commits[0].content_key;
  assert.ok(keys.includes("import stripe"));
  assert.ok(keys.includes("import jwt"), "the absorbed commit's changes must survive");
});

test("merging sums the line counts", () => {
  const merged = planner.mergeCommits(samplePlan(), 1, 0);
  assert.strictEqual(merged.commits[0].additions, 12);
});

test("merging keeps the target's message", () => {
  const merged = planner.mergeCommits(samplePlan(), 1, 0);
  assert.match(merged.commits[0].message.full, /payments/);
});

test("merging a commit into itself changes nothing", () => {
  const plan = samplePlan();
  assert.strictEqual(planner.mergeCommits(plan, 0, 0).commits.length, 2);
});

test("merging does not mutate the original plan", () => {
  const plan = samplePlan();
  planner.mergeCommits(plan, 1, 0);
  assert.strictEqual(plan.commits.length, 2);
});

// --- validation ------------------------------------------------------------

test("a healthy plan has no problems", () => {
  assert.deepStrictEqual(planner.validatePlan(samplePlan()), []);
});

test("an empty plan is rejected", () => {
  assert.strictEqual(planner.validatePlan({ commits: [] }).length, 1);
});

test("an empty commit message is rejected", () => {
  const plan = samplePlan();
  plan.commits[0].message.full = "   ";
  assert.match(planner.validatePlan(plan)[0], /empty message/);
});

test("an over-long subject line is flagged", () => {
  const plan = samplePlan();
  plan.commits[0].message.full = "feat: " + "x".repeat(80);
  assert.match(planner.validatePlan(plan)[0], /72 characters/);
});

test("a commit claiming no changes is rejected", () => {
  const plan = samplePlan();
  plan.commits[0].content_key = [];
  assert.match(planner.validatePlan(plan)[0], /claims no changes/);
});

test("two commits claiming the same change are rejected", () => {
  const plan = samplePlan();
  plan.commits[1].content_key = plan.commits[0].content_key.slice();
  assert.match(planner.validatePlan(plan)[0], /same change/);
});

// --- html escaping ---------------------------------------------------------

test("angle brackets in code cannot inject markup", () => {
  assert.strictEqual(panel.escapeHtml("<script>alert(1)</script>"),
    "&lt;script&gt;alert(1)&lt;/script&gt;");
});

test("quotes are escaped so attributes cannot be broken out of", () => {
  assert.strictEqual(panel.escapeHtml('a "b" \'c\''), "a &quot;b&quot; &#39;c&#39;");
});

test("ampersands are escaped first, not double-escaped", () => {
  assert.strictEqual(panel.escapeHtml("a && b"), "a &amp;&amp; b");
});

test("null and undefined render as empty, not as the word null", () => {
  assert.strictEqual(panel.escapeHtml(null), "");
  assert.strictEqual(panel.escapeHtml(undefined), "");
});

test("a commit message containing html is escaped in the panel", () => {
  const plan = samplePlan();
  plan.commits[0].message.full = "feat: handle <img src=x onerror=alert(1)>";
  const html = panel.renderPanel(plan, "testnonce");
  assert.ok(!html.includes("<img src=x"), "raw markup must not reach the page");
  assert.ok(html.includes("&lt;img"));
});

test("a file path containing quotes cannot break the markup", () => {
  const plan = samplePlan();
  plan.commits[0].hunks[0].path = 'weird"name.py';
  const html = panel.renderPanel(plan, "testnonce");
  assert.ok(html.includes("weird&quot;name.py"));
});

// --- panel rendering -------------------------------------------------------

test("the panel renders one section per commit", () => {
  const html = panel.renderPanel(samplePlan(), "n");
  assert.strictEqual((html.match(/class="commit"/g) || []).length, 2);
});

test("the nonce is used for the script tag", () => {
  const html = panel.renderPanel(samplePlan(), "abc123");
  assert.ok(html.includes('script nonce="abc123"'));
  assert.ok(html.includes("'nonce-abc123'"));
});

test("warnings are shown when present", () => {
  const plan = samplePlan();
  plan.warnings = ["commit 2 references charge_card before it exists"];
  const html = panel.renderPanel(plan, "n");
  assert.ok(html.includes("before it exists"));
});

test("an empty plan renders a message instead of an apply button", () => {
  const html = panel.renderPanel({ commits: [], warnings: [], unsplittable: [] }, "n");
  assert.ok(html.includes("No changes to split"));
  assert.ok(html.includes("disabled"));
});

test("a single commit has no merge controls", () => {
  const plan = samplePlan();
  plan.commits = [plan.commits[0]];
  const html = panel.renderPanel(plan, "n");
  assert.ok(!html.includes("merge-target"));
});

// --- the bundled engine ----------------------------------------------------

test("the bundled engine is preferred over anything in the workspace", () => {
  const path = require("path");
  const bundled = path.join(__dirname, "..", "src", "..", "engine", "splitter", "__init__.py");
  const exists = (p) => p === bundled || p === "/work/engine/splitter/__init__.py";
  const found = planner.findEngine("", ["/work"], exists);
  assert.ok(found.endsWith("engine"), "should resolve to the bundled copy");
  assert.ok(!found.startsWith("/work"), "workspace copy must not win over the bundle");
});

test("a user-configured engine path still overrides the bundle", () => {
  const exists = (p) => p === "/custom/splitter/__init__.py";
  assert.strictEqual(planner.findEngine("/custom", ["/work"], exists), "/custom");
});

// --- grouping hunks for display --------------------------------------------

test("several edits to one file are listed once, not repeatedly", () => {
  const files = panel.groupHunksByFile([
    { path: "Makefile", symbol: null, additions: 2, deletions: 0 },
    { path: "Makefile", symbol: null, additions: 3, deletions: 1 },
    { path: "README.md", symbol: null, additions: 5, deletions: 0 }
  ]);
  assert.strictEqual(files.length, 2);
  const makefile = files.find((f) => f.path === "Makefile");
  assert.strictEqual(makefile.count, 2);
  assert.strictEqual(makefile.additions, 5, "line counts are summed");
  assert.strictEqual(makefile.deletions, 1);
});

test("repeated symbols in one file are shown once", () => {
  const files = panel.groupHunksByFile([
    { path: "core.py", symbol: "analyze", additions: 1, deletions: 0 },
    { path: "core.py", symbol: "analyze", additions: 1, deletions: 0 },
    { path: "core.py", symbol: "Analysis", additions: 1, deletions: 0 }
  ]);
  assert.deepStrictEqual(files[0].symbols, ["Analysis", "analyze"]);
});

test("files are listed in a stable order", () => {
  const files = panel.groupHunksByFile([
    { path: "z.py", additions: 1, deletions: 0 },
    { path: "a.py", additions: 1, deletions: 0 }
  ]);
  assert.deepStrictEqual(files.map((f) => f.path), ["a.py", "z.py"]);
});

test("a single edit shows no places badge", () => {
  const plan = samplePlan();
  const html = panel.renderPanel(plan, "n");
  assert.ok(!html.includes("1 places"));
});

test("multiple edits to one file show a places badge", () => {
  const plan = samplePlan();
  plan.commits[0].hunks = [
    { id: "a", path: "Makefile", symbol: null, additions: 1, deletions: 0 },
    { id: "b", path: "Makefile", symbol: null, additions: 1, deletions: 0 }
  ];
  assert.ok(panel.renderPanel(plan, "n").includes("2 places"));
});
