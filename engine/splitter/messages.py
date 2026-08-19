"""Write the commit messages.

We send the model a compact description of each group rather than the raw
diff — the clustering already did the hard work, so the model only needs
enough context to name things. That keeps the prompt small, which keeps it
cheap and fast.

Everything the model returns is validated against the Conventional Commits
spec before we use it. If validation fails, we fall back to the heuristic
message rather than committing something malformed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .features import HunkFeatures
from .llm import LLMError, Provider, extract_json

TYPES = ("feat", "fix", "refactor", "perf", "test", "docs",
         "style", "build", "ci", "chore", "revert")

SUBJECT_MAX = 60          # keeps "type(scope): subject" under 72
BODY_WIDTH = 72
MAX_LINES_PER_HUNK = 12   # how much code we show the model per hunk

SYSTEM_PROMPT = """You write git commit messages in Conventional Commits format.

You will receive several proposed commits, each described by the files and \
functions it touches and a sample of its changed lines.

For each one, return:
  type     one of: feat, fix, refactor, perf, test, docs, style, build, ci, chore
  scope    the affected module or directory, lowercase, one word, or null
  subject  imperative mood, lowercase first letter, no trailing period, under \
60 characters. Say what the change does, not which files moved.
  body     one or two sentences of WHY, only when it is not obvious. Otherwise null.

Reply with a JSON array only. No prose, no code fences.
[{"index": 0, "type": "feat", "scope": "auth", "subject": "...", "body": null}]"""


@dataclass
class CommitMessage:
    type: str
    scope: str | None
    subject: str
    body: str | None = None
    generated_by: str = "heuristic"

    def header(self) -> str:
        scope = f"({self.scope})" if self.scope else ""
        return f"{self.type}{scope}: {self.subject}"

    def full(self) -> str:
        if not self.body:
            return self.header()
        return f"{self.header()}\n\n{_wrap(self.body)}"


def _wrap(text: str, width: int = BODY_WIDTH) -> str:
    import textwrap
    return "\n".join(textwrap.fill(p, width) for p in text.split("\n") if p.strip())


def validate(raw: dict) -> CommitMessage | None:
    """Turn one model reply into a CommitMessage, or None if it is unusable."""
    kind = str(raw.get("type", "")).strip().lower()
    if kind not in TYPES:
        return None

    subject = str(raw.get("subject", "")).strip()
    if not subject:
        return None
    subject = subject.rstrip(".").strip()
    if subject[:1].isupper() and not subject.split()[0].isupper():
        subject = subject[0].lower() + subject[1:]      # "Add x" -> "add x"
    if len(subject) > SUBJECT_MAX:
        subject = subject[:SUBJECT_MAX].rsplit(" ", 1)[0]

    scope = raw.get("scope")
    if scope in (None, "", "null"):
        scope = None
    else:
        scope = re.sub(r"[^a-z0-9\-]", "", str(scope).strip().lower()) or None

    body = raw.get("body")
    body = None if body in (None, "", "null") else str(body).strip()

    return CommitMessage(kind, scope, subject, body, generated_by="model")


def describe_group(index: int, members: list[HunkFeatures]) -> str:
    """A compact, token-cheap description of one proposed commit."""
    lines = [f"### commit {index}"]

    files = sorted({m.path for m in members})
    lines.append(f"files: {', '.join(files)}")

    symbols = sorted({m.symbol for m in members if m.symbol})
    if symbols:
        lines.append(f"functions: {', '.join(symbols)}")

    defines = sorted({d for m in members for d in m.defines})
    if defines:
        lines.append(f"introduces: {', '.join(defines[:8])}")

    adds = sum(m.hunk.stat[0] for m in members)
    dels = sum(m.hunk.stat[1] for m in members)
    lines.append(f"size: +{adds} -{dels}")

    lines.append("changes:")
    for m in sorted(members, key=lambda x: (x.path, x.hunk.new_start)):
        shown = [ln for ln in m.hunk.lines if ln[:1] in "+-"][:MAX_LINES_PER_HUNK]
        for ln in shown:
            lines.append("  " + ln.rstrip())
    return "\n".join(lines)


def heuristic_message(members: list[HunkFeatures]) -> CommitMessage:
    """The no-model fallback. Structural guesswork, always valid."""
    dirs = {m.directory for m in members}
    scope = next(iter(dirs)).split("/")[-1] if len(dirs) == 1 else None
    if scope in (".", "", "src", "app", "lib"):
        scope = None

    only_tests = all(m.is_test for m in members)
    dels = sum(m.hunk.stat[1] for m in members)
    adds = sum(m.hunk.stat[0] for m in members)

    if only_tests:
        kind = "test"
    elif dels == 0:
        kind = "feat"
    elif adds == 0:
        kind = "refactor"
    else:
        kind = "refactor"

    names = sorted({n for m in members for n in m.defines})
    if names:
        subject = f"add {', '.join(names[:3])}"
    else:
        paths = sorted({m.path.rsplit('/', 1)[-1] for m in members})
        subject = f"update {', '.join(paths[:2])}"

    return CommitMessage(kind, scope, subject[:SUBJECT_MAX])


def generate(
    groups: list[list[HunkFeatures]],
    provider: Provider | None,
) -> tuple[list[CommitMessage], str | None]:
    """Messages for every group. Returns (messages, warning-or-None).

    One call covers all groups: the model sees them together, which helps it
    keep scopes consistent and avoid giving two commits the same name.
    """
    fallback = [heuristic_message(g) for g in groups]
    if provider is None:
        return fallback, None

    prompt = "\n\n".join(describe_group(i, g) for i, g in enumerate(groups))

    try:
        reply = provider.complete(SYSTEM_PROMPT, prompt)
        parsed = extract_json(reply)
    except (LLMError, KeyError, IndexError) as exc:
        return fallback, f"model unavailable ({exc}); used heuristic messages"

    if not isinstance(parsed, list):
        return fallback, "model reply was not a list; used heuristic messages"

    messages = list(fallback)
    accepted = 0
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(messages):
            continue
        candidate = validate(item)
        if candidate:
            messages[index] = candidate
            accepted += 1

    if accepted == 0:
        return fallback, "no usable message from the model; used heuristic ones"
    if accepted < len(groups):
        return messages, f"model named {accepted}/{len(groups)} commits"
    return messages, None
