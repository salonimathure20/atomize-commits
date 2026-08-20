"use strict";

/**
 * The review panel's HTML.
 *
 * Kept as a pure string builder with no VS Code imports so it can be tested,
 * and so the escaping rules live in one obvious place.
 */

/**
 * Escape text before it goes anywhere near HTML.
 *
 * This matters more than usual here: every string on this panel is source
 * code the user just wrote, so it is full of angle brackets, quotes and
 * ampersands, and dropping any of them raw would break the page or worse.
 */
function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Group a commit's hunks by file for display.
 *
 * A commit routinely touches the same file in several places, and listing
 * "Makefile" three times looks like a rendering bug rather than three
 * separate edits. So we show each file once and describe the pieces under it.
 */
function groupHunksByFile(hunks) {
  const byFile = new Map();
  for (const hunk of hunks || []) {
    if (!byFile.has(hunk.path)) {
      byFile.set(hunk.path, []);
    }
    byFile.get(hunk.path).push(hunk);
  }

  return Array.from(byFile.entries()).map(([path, pieces]) => ({
    path: path,
    count: pieces.length,
    symbols: Array.from(
      new Set(pieces.map((p) => p.symbol).filter(Boolean))
    ).sort(),
    additions: pieces.reduce((sum, p) => sum + (p.additions || 0), 0),
    deletions: pieces.reduce((sum, p) => sum + (p.deletions || 0), 0)
  })).sort((a, b) => a.path.localeCompare(b.path));
}

function renderFile(file) {
  const symbols = file.symbols.length
    ? `<span class="symbol">${escapeHtml(file.symbols.slice(0, 4).join(", "))}` +
      (file.symbols.length > 4 ? ` +${file.symbols.length - 4}` : "") + `</span>`
    : "";
  const places = file.count > 1
    ? `<span class="count">${file.count} places</span>`
    : "";

  return `<li>
    <span class="path">${escapeHtml(file.path)}</span>
    ${symbols}
    ${places}
    <span class="stat">+${file.additions} −${file.deletions}</span>
  </li>`;
}

function renderCommit(commit, index, total) {
  const message = (commit.message && commit.message.full) || "";
  const source = (commit.message && commit.message.generated_by) || "heuristic";
  const reasons = (commit.reasons || [])
    .map((r) => `<li>${escapeHtml(r)}</li>`)
    .join("");

  const moveOptions = [];
  for (let i = 0; i < total; i += 1) {
    if (i !== index) {
      moveOptions.push(`<option value="${i}">commit ${i + 1}</option>`);
    }
  }

  return `<section class="commit" data-index="${index}">
    <header>
      <h2>Commit ${index + 1}</h2>
      <span class="badge ${source === "model" ? "model" : "heuristic"}">${escapeHtml(source)}</span>
      <span class="stat">${(commit.files || []).length} file(s) · +${commit.additions || 0} −${commit.deletions || 0}</span>
    </header>

    <label class="field-label" for="msg-${index}">Commit message</label>
    <textarea id="msg-${index}" class="message" rows="3"
      data-index="${index}">${escapeHtml(message)}</textarea>

    ${reasons ? `<details class="why"><summary>Why these changes are together</summary><ul>${reasons}</ul></details>` : ""}

    <details class="changes" open>
      <summary>Changes in ${(commit.files || []).length} file(s)</summary>
      <ul class="hunks">${groupHunksByFile(commit.hunks).map(renderFile).join("")}</ul>
    </details>

    ${
      total > 1
        ? `<div class="actions">
            <label for="merge-${index}">Merge into</label>
            <select id="merge-${index}" class="merge-target">
              <option value="">—</option>${moveOptions.join("")}
            </select>
            <button class="merge-btn" data-index="${index}">Merge</button>
          </div>`
        : ""
    }
  </section>`;
}

/**
 * @param {{commits: any[], warnings: string[], unsplittable: string[]}} plan
 * @param {string} nonce  content-security-policy nonce for the inline script
 */
function renderPanel(plan, nonce) {
  const commits = plan.commits || [];
  const warnings = (plan.warnings || [])
    .map((w) => `<li>${escapeHtml(w)}</li>`)
    .join("");
  const unsplittable = (plan.unsplittable || [])
    .map((p) => `<li>${escapeHtml(p)}</li>`)
    .join("");

  const body = commits.length
    ? commits.map((c, i) => renderCommit(c, i, commits.length)).join("")
    : `<p class="empty">No changes to split. Edit some files and run this again.</p>`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    padding: 1rem 1.25rem 4rem;
    line-height: 1.5;
  }
  h1 { font-size: 1.2rem; margin: 0 0 0.25rem; }
  .lede { color: var(--vscode-descriptionForeground); margin: 0 0 1.5rem; }
  .commit {
    border: 1px solid var(--vscode-panel-border);
    border-radius: 6px;
    padding: 0.85rem 1rem;
    margin-bottom: 1rem;
  }
  .commit header { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.6rem; }
  .commit h2 { font-size: 0.95rem; margin: 0; }
  .badge {
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 0.1rem 0.4rem; border-radius: 3px;
    background: var(--vscode-badge-background); color: var(--vscode-badge-foreground);
  }
  .badge.model { background: var(--vscode-charts-blue); color: var(--vscode-editor-background); }
  .stat { color: var(--vscode-descriptionForeground); font-size: 0.8rem; margin-left: auto; }
  .field-label {
    display: block; font-size: 0.75rem; text-transform: uppercase;
    letter-spacing: 0.04em; color: var(--vscode-descriptionForeground);
    margin-bottom: 0.25rem;
  }
  textarea.message {
    width: 100%; box-sizing: border-box; resize: vertical;
    font-family: var(--vscode-editor-font-family);
    font-size: 0.85rem; padding: 0.5rem;
    background: var(--vscode-input-background);
    color: var(--vscode-input-foreground);
    border: 1px solid var(--vscode-input-border, transparent);
    border-radius: 4px;
  }
  textarea.message:focus { outline: 1px solid var(--vscode-focusBorder); }
  details { margin-top: 0.6rem; }
  summary { cursor: pointer; font-size: 0.8rem; color: var(--vscode-descriptionForeground); }
  ul { margin: 0.4rem 0 0; padding-left: 1.1rem; }
  li { font-size: 0.8rem; margin-bottom: 0.15rem; }
  .hunks li {
    list-style: none; margin-left: -1.1rem;
    display: flex; gap: 0.5rem; align-items: baseline;
  }
  .count {
    font-size: 0.7rem; padding: 0.05rem 0.35rem; border-radius: 3px;
    background: var(--vscode-badge-background);
    color: var(--vscode-badge-foreground);
  }
  .path { font-family: var(--vscode-editor-font-family); }
  .symbol { color: var(--vscode-charts-purple); font-family: var(--vscode-editor-font-family); }
  .actions { margin-top: 0.75rem; display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; }
  select, button {
    font-family: inherit; font-size: 0.8rem; padding: 0.25rem 0.6rem;
    border-radius: 3px; border: 1px solid var(--vscode-button-border, transparent);
  }
  select { background: var(--vscode-dropdown-background); color: var(--vscode-dropdown-foreground); }
  button {
    background: var(--vscode-button-secondaryBackground);
    color: var(--vscode-button-secondaryForeground);
    cursor: pointer;
  }
  button:hover { background: var(--vscode-button-secondaryHoverBackground); }
  .warnings {
    border-left: 3px solid var(--vscode-charts-yellow);
    padding: 0.5rem 0.75rem; margin-bottom: 1.25rem;
    background: var(--vscode-textBlockQuote-background);
  }
  .warnings h3 { font-size: 0.8rem; margin: 0 0 0.3rem; }
  .footer {
    position: fixed; bottom: 0; left: 0; right: 0;
    padding: 0.75rem 1.25rem;
    background: var(--vscode-editor-background);
    border-top: 1px solid var(--vscode-panel-border);
    display: flex; gap: 0.6rem; align-items: center;
  }
  .primary {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    padding: 0.4rem 1rem;
  }
  .primary:hover { background: var(--vscode-button-hoverBackground); }
  .hint { color: var(--vscode-descriptionForeground); font-size: 0.75rem; }
  .empty { color: var(--vscode-descriptionForeground); }
</style>
</head>
<body>
  <h1>Proposed commits</h1>
  <p class="lede">Edit any message, merge commits that belong together, then apply.
     Your working tree is not changed until you press Apply.</p>

  ${warnings ? `<div class="warnings"><h3>Worth checking</h3><ul>${warnings}</ul></div>` : ""}
  ${unsplittable ? `<div class="warnings"><h3>Cannot be split</h3><ul>${unsplittable}</ul></div>` : ""}

  ${body}

  <div class="footer">
    <button class="primary" id="apply" ${commits.length ? "" : "disabled"}>
      Apply ${commits.length} commit${commits.length === 1 ? "" : "s"}
    </button>
    <button id="refresh">Recalculate</button>
    <span class="hint">Commits are made in the order shown.</span>
  </div>

<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();

  function collectMessages() {
    const messages = {};
    document.querySelectorAll("textarea.message").forEach((box) => {
      messages[box.dataset.index] = box.value;
    });
    return messages;
  }

  document.getElementById("apply").addEventListener("click", () => {
    vscode.postMessage({ type: "apply", messages: collectMessages() });
  });

  document.getElementById("refresh").addEventListener("click", () => {
    vscode.postMessage({ type: "refresh" });
  });

  document.querySelectorAll(".merge-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const source = Number(button.dataset.index);
      const select = document.getElementById("merge-" + source);
      if (select.value === "") { return; }
      vscode.postMessage({
        type: "merge",
        source: source,
        target: Number(select.value),
        messages: collectMessages()
      });
    });
  });
</script>
</body>
</html>`;
}

module.exports = { escapeHtml, renderPanel, renderCommit, groupHunksByFile };
