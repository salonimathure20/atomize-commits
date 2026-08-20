"""The analysis, with git taken out of it.

WHY THIS FILE EXISTS
--------------------
Everything up to Phase 3 assumed a checkout on the local disk: run `git diff`,
read files, mine `git log`. A web service has none of that. It gets text over
HTTP and must answer from text alone.

So the pipeline gets one entry point that takes plain data in and gives plain
data out. The CLI collects that data from git; the service receives it in a
request body. Both then call `analyze()`, which means there is exactly one
implementation of the interesting logic and no chance of the two drifting
apart as the project grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .features import HunkFeatures, Source, build_features, empty_source
from .grouping import build_graph, cluster, explain_group
from .llm import Provider
from .messages import CommitMessage, generate
from .ordering import missing_references, order_groups
from .parser import parse_diff
from .patch import content_key
from .subhunks import split_all


@dataclass
class ProposedCommit:
    message: CommitMessage
    members: list[HunkFeatures]
    reasons: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return sorted({m.path for m in self.members})

    @property
    def stat(self) -> tuple[int, int]:
        return (
            sum(m.hunk.stat[0] for m in self.members),
            sum(m.hunk.stat[1] for m in self.members),
        )

    def content_keys(self) -> frozenset[str]:
        """Identify this commit's hunks by their text, not their position.

        Hunk ids change once an earlier commit lands and the diff regenerates.
        Content keys survive that, which is what lets a plan be executed one
        commit at a time.
        """
        keys = [content_key(m.hunk) for m in self.members]
        return frozenset().union(*keys) if keys else frozenset()

    def to_dict(self) -> dict:
        adds, dels = self.stat
        return {
            # The changed text itself, which is how a commit is identified
            # once hunk ids have shifted. A saved plan stays valid because of
            # this field.
            "content_key": sorted(self.content_keys()),
            "message": {
                "header": self.message.header(),
                "full": self.message.full(),
                "type": self.message.type,
                "scope": self.message.scope,
                "subject": self.message.subject,
                "body": self.message.body,
                "generated_by": self.message.generated_by,
            },
            "files": self.paths,
            "additions": adds,
            "deletions": dels,
            "reasons": self.reasons,
            "hunks": [
                {
                    "id": m.hunk_id,
                    "path": m.path,
                    "symbol": m.symbol,
                    "start_line": m.hunk.new_start,
                    "additions": m.hunk.stat[0],
                    "deletions": m.hunk.stat[1],
                }
                for m in sorted(self.members, key=lambda x: (x.path, x.hunk.new_start))
            ],
        }


@dataclass
class Analysis:
    commits: list[ProposedCommit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsplittable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "commits": [c.to_dict() for c in self.commits],
            "warnings": self.warnings,
            "unsplittable": self.unsplittable,
        }


def analyze(
    diff_text: str,
    source: Source | None = None,
    co_change: dict[tuple[str, str], float] | None = None,
    provider: Provider | None = None,
    algorithm: str = "louvain",
) -> Analysis:
    """Split a diff into proposed commits, ordered and named.

    diff_text  output of `git diff`
    source     supplies file contents for symbol detection; optional, and the
               analysis simply gets coarser without it
    co_change  file pairs that historically change together; optional
    provider   a language model for commit messages; optional
    algorithm  community detection method; see eval/ for the comparison
    """
    source = source or empty_source()
    warnings: list[str] = []

    files = split_all(parse_diff(diff_text))
    unsplittable = [fd.path for fd in files if fd.is_atomic]

    profiles = build_features(source, files)
    if not profiles:
        return Analysis(warnings=warnings, unsplittable=unsplittable)

    graph, edges = build_graph(profiles, co_change or {})
    groups = cluster(graph, algorithm)

    by_id = {p.hunk_id: p for p in profiles}
    member_groups = [[by_id[hid] for hid in g] for g in groups]
    group_reasons = [explain_group(g, edges) for g in groups]

    # Order so no commit uses something a later commit creates.
    order, cycle_warnings = order_groups(member_groups)
    warnings.extend(cycle_warnings)
    member_groups = [member_groups[i] for i in order]
    group_reasons = [group_reasons[i] for i in order]

    for index, dangling in missing_references(member_groups):
        names = ", ".join(sorted(dangling)[:3])
        warnings.append(f"commit {index + 1} references {names} before it exists")

    messages, message_warning = generate(member_groups, provider)
    if message_warning:
        warnings.append(message_warning)

    commits = [
        ProposedCommit(message=message, members=members, reasons=reasons)
        for message, members, reasons in zip(messages, member_groups, group_reasons)
    ]

    return Analysis(commits=commits, warnings=warnings, unsplittable=unsplittable)
