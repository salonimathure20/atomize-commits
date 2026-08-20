"use strict";

/**
 * VS Code entry point.
 *
 * The extension is deliberately thin. It finds the repository, shells out to
 * the Python engine, renders the result, and sends an edited plan back. It
 * reimplements none of the analysis, so everything interesting stays covered
 * by the Python test suite.
 */

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");

const planner = require("./planner");
const panel = require("./panel");

const TIMEOUT_MS = 180000;

let reviewPanel = null;
let currentPlan = null;
let currentRepo = null;

function config() {
  const settings = vscode.workspace.getConfiguration("commitSplitter");
  return {
    pythonPath: settings.get("pythonPath") || "python3",
    enginePath: settings.get("enginePath") || "",
    provider: settings.get("provider") || "none",
    model: settings.get("model") || "",
    useHistory: settings.get("useHistory") !== false
  };
}

function nonce() {
  let text = "";
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i += 1) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}

/** The folder we should analyse — the first workspace folder holding a .git. */
function findRepository() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const folder of folders) {
    if (fs.existsSync(path.join(folder.uri.fsPath, ".git"))) {
      return folder.uri.fsPath;
    }
  }
  return folders.length ? folders[0].uri.fsPath : null;
}

function locateEngine(settings) {
  const folders = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
  const found = planner.findEngine(settings.enginePath, folders, null);
  if (found) {
    return found;
  }
  throw new Error(
    "Could not find the splitter engine. Set commitSplitter.enginePath to the " +
      "folder containing the 'splitter' package (usually .../auto-commit/engine)."
  );
}

async function runPlan(repoPath, settings) {
  const enginePath = locateEngine(settings);
  const args = planner.buildArgs(repoPath, "plan", {
    useHistory: settings.useHistory,
    provider: settings.provider
  });
  const env = planner.buildEnv(process.env, settings);
  const { stdout } = await planner.runEngine(
    settings.pythonPath, enginePath, args, env, TIMEOUT_MS
  );
  return planner.parsePlan(stdout);
}

/**
 * Apply a reviewed plan.
 *
 * The plan goes to a temp file rather than the command line: commit messages
 * are arbitrary user text, and passing that through a shell is how quoting
 * bugs and injection problems happen.
 */
async function applyPlan(repoPath, settings, plan) {
  const enginePath = locateEngine(settings);
  const file = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "splitter-")),
    "plan.json"
  );

  try {
    fs.writeFileSync(file, JSON.stringify(plan), "utf8");
    const args = planner.buildArgs(repoPath, "apply", {
      provider: settings.provider,
      extra: ["--plan", file]
    });
    const env = planner.buildEnv(process.env, settings);
    const { stdout } = await planner.runEngine(
      settings.pythonPath, enginePath, args, env, TIMEOUT_MS
    );
    return stdout.trim();
  } finally {
    fs.rmSync(path.dirname(file), { recursive: true, force: true });
  }
}

/** Copy edited message text from the webview back into the plan. */
function applyEdits(plan, messages) {
  if (!messages) {
    return plan;
  }
  const commits = plan.commits.map((commit, index) => {
    const edited = messages[String(index)];
    if (edited === undefined) {
      return commit;
    }
    const text = String(edited).trim();
    return Object.assign({}, commit, {
      message: Object.assign({}, commit.message, {
        full: text,
        header: text.split("\n")[0]
      })
    });
  });
  return Object.assign({}, plan, { commits });
}

function showPlan(context, plan) {
  currentPlan = plan;

  if (!reviewPanel) {
    reviewPanel = vscode.window.createWebviewPanel(
      "commitSplitter.review",
      "Split Changes into Commits",
      vscode.ViewColumn.One,
      { enableScripts: true, retainContextWhenHidden: true }
    );
    reviewPanel.onDidDispose(() => {
      reviewPanel = null;
      currentPlan = null;
    }, null, context.subscriptions);
    reviewPanel.webview.onDidReceiveMessage(
      (message) => handleMessage(context, message),
      null,
      context.subscriptions
    );
  }

  reviewPanel.webview.html = panel.renderPanel(plan, nonce());
  reviewPanel.reveal(vscode.ViewColumn.One);
}

async function handleMessage(context, message) {
  const settings = config();

  if (message.type === "merge") {
    const edited = applyEdits(currentPlan, message.messages);
    showPlan(context, planner.mergeCommits(edited, message.source, message.target));
    return;
  }

  if (message.type === "refresh") {
    await review(context);
    return;
  }

  if (message.type !== "apply") {
    return;
  }

  const plan = applyEdits(currentPlan, message.messages);

  const problems = planner.validatePlan(plan);
  if (problems.length) {
    vscode.window.showErrorMessage(`Cannot apply: ${problems[0]}`);
    return;
  }

  const count = plan.commits.length;
  const confirmed = await vscode.window.showWarningMessage(
    `Create ${count} commit${count === 1 ? "" : "s"}?`,
    { modal: true, detail: "Your working tree will not change. This can be undone with git reset." },
    "Create commits"
  );
  if (confirmed !== "Create commits") {
    return;
  }

  try {
    const output = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Creating commits..." },
      () => applyPlan(currentRepo, settings, plan)
    );
    vscode.window.showInformationMessage(`Created ${count} commit${count === 1 ? "" : "s"}.`);
    if (output) {
      channel().appendLine(output);
    }
    if (reviewPanel) {
      reviewPanel.dispose();
    }
    await vscode.commands.executeCommand("git.refresh");
  } catch (err) {
    vscode.window.showErrorMessage(`Could not create commits: ${err.message}`);
    channel().appendLine(String(err.stack || err.message));
    channel().show(true);
  }
}

let output = null;
function channel() {
  if (!output) {
    output = vscode.window.createOutputChannel("Commit Splitter");
  }
  return output;
}

async function review(context) {
  const settings = config();
  currentRepo = findRepository();

  if (!currentRepo) {
    vscode.window.showErrorMessage("Open a folder containing a git repository first.");
    return;
  }

  try {
    const plan = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Analysing your changes..." },
      () => runPlan(currentRepo, settings)
    );

    if (!plan.commits.length) {
      vscode.window.showInformationMessage("No uncommitted changes to split.");
      return;
    }
    showPlan(context, plan);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
    channel().appendLine(String(err.stack || err.message));
    channel().show(true);
  }
}

async function doctor() {
  const settings = config();
  try {
    const enginePath = locateEngine(settings);
    const args = planner.buildArgs(".", "doctor", {});
    const env = planner.buildEnv(process.env, settings);
    const result = await planner.runEngine(
      settings.pythonPath, enginePath, args, env, 60000
    );
    channel().appendLine(result.stdout);
    channel().show(true);
  } catch (err) {
    channel().appendLine(String(err.message));
    channel().show(true);
  }
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("commitSplitter.review", () => review(context)),
    vscode.commands.registerCommand("commitSplitter.doctor", doctor)
  );
}

function deactivate() {
  if (output) {
    output.dispose();
  }
}

module.exports = { activate, deactivate, applyEdits };
