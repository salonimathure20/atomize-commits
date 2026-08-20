"use strict";

/**
 * Everything that decides *how* to call the Python engine.
 *
 * This module deliberately holds no VS Code API calls, so it can be unit
 * tested with plain node. Anything that touches the editor lives in
 * extension.js instead.
 */

const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

/**
 * Build the environment for the Python process.
 *
 * API keys are read from the user's own shell environment and merely passed
 * through. The extension never stores a key, never writes one to settings,
 * and never puts one in a log line.
 *
 * @param {NodeJS.ProcessEnv} baseEnv
 * @param {{provider: string, model: string}} config
 */
function buildEnv(baseEnv, config) {
  const env = Object.assign({}, baseEnv);
  env.SPLITTER_LLM = config.provider || "none";
  if (config.model) {
    env.SPLITTER_MODEL = config.model;
  }
  return env;
}

/**
 * Find the folder containing the `splitter` package.
 *
 * Checked in order: the user's explicit setting, then a few conventional
 * spots inside the workspace. Returning null is a normal outcome, not an
 * error — the caller turns it into a helpful message.
 *
 * @param {string} configured  value of commitSplitter.enginePath
 * @param {string[]} workspaceFolders
 * @param {(p: string) => boolean} exists  injected for testing
 * @returns {string | null}
 */
function findEngine(configured, workspaceFolders, exists) {
  const check = exists || ((p) => fs.existsSync(p));

  if (configured) {
    return check(path.join(configured, "splitter", "__init__.py")) ? configured : null;
  }

  const candidates = [];

  // The copy shipped inside the extension. This is what makes a Marketplace
  // install work with no setup: the user needs Python, nothing else.
  candidates.push(path.join(__dirname, "..", "engine"));

  for (const folder of workspaceFolders || []) {
    candidates.push(
      path.join(folder, "engine"),
      folder,
      path.join(folder, "auto-commit", "engine"),
      path.join(folder, "commit-splitter", "engine")
    );
  }

  for (const candidate of candidates) {
    if (check(path.join(candidate, "splitter", "__init__.py"))) {
      return candidate;
    }
  }
  return null;
}

/**
 * Turn CLI settings into command line arguments.
 *
 * @param {string} repoPath
 * @param {string} subcommand
 * @param {{useHistory?: boolean, provider?: string, extra?: string[]}} options
 */
function buildArgs(repoPath, subcommand, options) {
  const opts = options || {};
  const args = ["-m", "splitter", "--repo", repoPath, subcommand];

  if (subcommand === "plan" || subcommand === "apply") {
    if (opts.useHistory === false) {
      args.push("--no-history");
    }
    if (!opts.provider || opts.provider === "none") {
      args.push("--no-model");
    }
  }
  for (const extra of opts.extra || []) {
    args.push(extra);
  }
  return args;
}

/**
 * Run the engine and collect its output.
 *
 * Rejects with a message that already reads like something a person can act
 * on, because it goes straight into a notification.
 *
 * @returns {Promise<{stdout: string, stderr: string}>}
 */
function runEngine(pythonPath, enginePath, args, env, timeoutMs) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(pythonPath, args, { cwd: enginePath, env });
    } catch (err) {
      reject(new Error(`could not start ${pythonPath}: ${err.message}`));
      return;
    }

    let stdout = "";
    let stderr = "";
    let finished = false;

    const timer = setTimeout(() => {
      if (!finished) {
        child.kill();
        reject(new Error(`the engine took longer than ${Math.round(timeoutMs / 1000)}s`));
      }
    }, timeoutMs || 120000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });

    child.on("error", (err) => {
      finished = true;
      clearTimeout(timer);
      if (err.code === "ENOENT") {
        reject(new Error(`python not found at "${pythonPath}". Set commitSplitter.pythonPath.`));
      } else {
        reject(err);
      }
    });

    child.on("close", (code) => {
      finished = true;
      clearTimeout(timer);
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        reject(new Error(stderr.trim() || `the engine exited with code ${code}`));
      }
    });
  });
}

/**
 * Parse the engine's JSON, tolerating warning lines printed alongside it.
 *
 * @param {string} stdout
 */
function parsePlan(stdout) {
  const start = stdout.indexOf("{");
  if (start === -1) {
    throw new Error("the engine returned no plan");
  }
  const parsed = JSON.parse(stdout.slice(start));
  if (!parsed || !Array.isArray(parsed.commits)) {
    throw new Error("the engine returned an unexpected shape");
  }
  return parsed;
}

/**
 * Merge two commits in a plan into one.
 *
 * Used when the split was too aggressive. The combined commit keeps the first
 * message and takes every change from both, so nothing can be dropped.
 *
 * @param {{commits: any[]}} plan
 * @param {number} sourceIndex  the commit being absorbed
 * @param {number} targetIndex  the commit absorbing it
 */
function mergeCommits(plan, sourceIndex, targetIndex) {
  const commits = plan.commits.slice();
  if (
    sourceIndex === targetIndex ||
    !commits[sourceIndex] ||
    !commits[targetIndex]
  ) {
    return plan;
  }

  const source = commits[sourceIndex];
  const target = commits[targetIndex];

  const merged = Object.assign({}, target, {
    content_key: Array.from(
      new Set((target.content_key || []).concat(source.content_key || []))
    ).sort(),
    files: Array.from(new Set((target.files || []).concat(source.files || []))).sort(),
    hunks: (target.hunks || []).concat(source.hunks || []),
    additions: (target.additions || 0) + (source.additions || 0),
    deletions: (target.deletions || 0) + (source.deletions || 0),
    reasons: ["merged by hand"]
  });

  commits[targetIndex] = merged;
  commits.splice(sourceIndex, 1);
  return Object.assign({}, plan, { commits });
}

/**
 * Check a plan is safe to execute before we touch git.
 *
 * The dangerous mistakes are a commit that claims no changes, and two commits
 * claiming the same change — the second would silently commit nothing.
 *
 * @param {{commits: any[]}} plan
 * @returns {string[]} problems, empty when the plan is fine
 */
function validatePlan(plan) {
  const problems = [];
  if (!plan || !Array.isArray(plan.commits) || plan.commits.length === 0) {
    return ["the plan contains no commits"];
  }

  const seen = new Map();
  plan.commits.forEach((commit, index) => {
    const label = `commit ${index + 1}`;

    const header = (commit.message && commit.message.full) || "";
    if (!header.trim()) {
      problems.push(`${label} has an empty message`);
    }
    if (header.split("\n")[0].length > 72) {
      problems.push(`${label}'s first line is over 72 characters`);
    }

    const keys = commit.content_key || [];
    if (keys.length === 0) {
      problems.push(`${label} claims no changes`);
    }
    for (const key of keys) {
      if (seen.has(key)) {
        problems.push(`${label} and commit ${seen.get(key) + 1} claim the same change`);
        break;
      }
      seen.set(key, index);
    }
  });

  return problems;
}

module.exports = {
  buildArgs,
  buildEnv,
  findEngine,
  mergeCommits,
  parsePlan,
  runEngine,
  validatePlan
};
