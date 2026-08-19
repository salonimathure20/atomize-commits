# commit-splitter

Splits a tangled working tree into atomic, well-described commits.

If you changed authentication and pagination in the same sitting, this tool
figures out which changes belong to which feature, splits them into separate
commits, and writes a Conventional Commits message for each.

## Status

**Phase 1 complete — mechanical foundation, no intelligence yet.**

| Phase | What it adds | State |
|---|---|---|
| 1 | Hunk parsing, partial staging, CLI | done |
| 2 | Relatedness graph + clustering | next |
| 3 | LLM refinement and message generation | |
| 4 | FastAPI service + Docker | |
| 5 | VS Code extension | |
| 6 | Kubernetes: verification jobs, HPA | |
| 7 | Deploy, CI/CD, publish | |

## Quick start

```bash
cd engine
python3 -m splitter --repo /path/to/your/repo list -v
python3 -m splitter --repo /path/to/your/repo verify
python3 -m splitter --repo /path/to/your/repo commit \
    -m "feat(auth): add JWT token issuance" -H a784e4 -H 9e8696
```

Hunks are addressed by a short content hash, a full id
(`app/auth.py#a784e4`), or a file path meaning "every hunk in this file".

Run the tests with `pip install -e '.[dev]'` then `python -m pytest tests -q`.

## How the pieces fit

```
gitrepo.py    shells out to git; every call is one you could type yourself
parser.py     git diff text  ->  FileDiff / Hunk objects
patch.py      a subset of hunks  ->  patch text git will accept
models.py     the Hunk and FileDiff dataclasses
cli.py        list / verify / commit
```

## The one idea worth remembering

We apply patches to the git **index**, which is identical to HEAD at the moment
we apply. That means every hunk's `-` side line numbers are always correct, and
only the `+` side needs fixing when we skip hunks:

```
new_start = old_start + offset
offset   += (new_count - old_count)   # advances only for included hunks
```

`test_skipping_middle_hunk_lands_correctly` is the regression test for this.
If you change `patch.py`, run it.

## Known limits in Phase 1

- Diffs are generated with `-U3`, so edits within ~6 lines of each other merge
  into one hunk and cannot currently be separated. Phase 2 will likely move to
  `-U1` and re-expand context when building the patch.
- Binary files and renames are all-or-nothing (`FileDiff.is_atomic`).
- File paths containing spaces are parsed from the `+++ b/` line and may be
  wrong in exotic cases.
- Nothing checks whether each commit actually builds. That is the Kubernetes
  verification job in Phase 6.
