# commit-splitter

Splits a tangled working tree into atomic, well-described commits.

If you changed authentication and pagination in the same sitting, this tool
figures out which changes belong to which feature, splits them into separate
commits, and writes a Conventional Commits message for each.

## Status

**Complete.** One engine, three ways to use it.

| Phase | What it adds | State |
|---|---|---|
| 1 | Hunk parsing, partial staging, CLI | done |
| 2 | Relatedness graph + clustering | done |
| 3 | Dependency ordering + LLM commit messages | done |
| 4 | FastAPI service + Docker | done |
| 5 | VS Code extension | done |
| 6 | CI check for pull requests | done |

## Quick start

```bash
cd engine
pip install networkx          # the only dependency

# what changed, broken into hunks
python3 -m splitter --repo ~/your-repo list -v

# prove the parser loses nothing
python3 -m splitter --repo ~/your-repo verify

# let the tool propose a split, with its reasoning
python3 -m splitter --repo ~/your-repo group

# see the raw relatedness scores (use this when tuning weights)
python3 -m splitter --repo ~/your-repo why

# preview the whole plan, then run it
python3 -m splitter --repo ~/your-repo apply --dry-run
python3 -m splitter --repo ~/your-repo apply

# or drive it by hand
python3 -m splitter --repo ~/your-repo commit -m "feat(auth): ..." -H a784e4
```

Hunks are addressed by a short content hash, a full id
(`app/auth.py#a784e4`), or a file path meaning "every hunk in this file".

Run everything with `make test`.

| Suite | Tests | Covers |
|---|---|---|
| `engine/tests` | 39 | parsing, splitting, grouping, ordering, messages |
| `service/tests` | 22 | the analysis core and the HTTP API |
| `ci/tests` | 25 | the PR check, against a stub HTTP server |
| `extension/test` | 37 | argument building, merging, HTML escaping |

## How the pieces fit

```
gitrepo.py    shells out to git; every call is one you could type yourself
parser.py     git diff text  ->  FileDiff / Hunk objects
subhunks.py   cuts oversized hunks at function boundaries
models.py     the Hunk and FileDiff dataclasses
symbols.py    which function/class does a line live in? (regex today,
              tree-sitter later — swapping it touches only this file)
features.py   builds a profile card per hunk
grouping.py   scores every pair, builds the graph, runs Louvain
core.py       the whole analysis with git removed; CLI and service both
              call this, so they cannot drift apart
ordering.py   sorts commits so nothing is used before it exists
llm.py        pluggable model providers, stdlib HTTP only
messages.py   Conventional Commits generation and validation
patch.py      a subset of hunks  ->  patch text git will accept
cli.py        list / verify / group / why / apply / commit
```

## How the grouping decides

Every pair of hunks gets a score. The weights live in one dict at the top of
`grouping.py` — that is the dial board for the whole system.

| Signal | Weight | Why |
|---|---|---|
| same enclosing function | 0.60 | strongest structural evidence |
| one defines what the other uses | 0.55 | a genuine code dependency |
| same file | 0.30 | weak, and deliberately overridden below |
| **same file, different functions** | **0.08** | the tangled case: file proximity must not glue unrelated features together |
| implementation and its test | 0.45 | `auth.py` + `test_auth.py` |
| shared vocabulary | 0.40 x overlap | scaled by Jaccard similarity |
| historically committed together | 0.20 x strength | mined from `git log` |
| same directory | 0.12 | tiebreaker only |

Pairs scoring under 0.35 get no edge at all. Louvain then finds groups that are
densely connected inside and sparsely connected outside, and picks the number
of groups by itself — which matters, because you never know in advance how many
features got tangled.

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

## Commit messages

Messages come from a language model when one is configured, and from
structural heuristics otherwise. The tool works fully offline; the model is an
upgrade, never a requirement.

```bash
# free and private: a model on your own machine
export SPLITTER_LLM=ollama
export SPLITTER_MODEL=qwen2.5-coder:7b

# or bring your own key
export SPLITTER_LLM=gemini      && export GEMINI_API_KEY=...
export SPLITTER_LLM=anthropic   && export ANTHROPIC_API_KEY=...
export SPLITTER_LLM=openai      && export OPENAI_API_KEY=...

# or turn it off entirely
export SPLITTER_LLM=none
```

Check your setup before relying on it — a misconfigured model fails quietly by
falling back to heuristics, which is easy to miss:

```bash
python3 -m splitter --repo . doctor
```

`doctor` reads the provider's own error back to you — retired model names,
rejected keys, and an unreachable Ollama each get a specific fix rather than a
stack trace.

**On credentials.** Keys are read from the environment and never written to
disk. The Gemini key travels as a request header rather than a query string,
because query strings end up in shell history and proxy logs. Anything that
looks like a credential is masked before it reaches a terminal or an error
message — see `redact()` in `llm.py` and its tests.

**Model names go stale.** Providers retire them every few months. If a
default stops working, override it rather than editing the code:

```bash
export SPLITTER_MODEL=gemini-3.6-flash
```

## See it work

`demo.sh` rebuilds a repo where payments and authentication were edited into
the same file, then shows the grouping, the heuristic messages, and your
model's messages side by side.

```bash
cd engine
./demo.sh                # rebuild and show the plan
./demo.sh --commit       # ...and make the commits, with safety checks
```

Every reply is validated against Conventional Commits before use: unknown
types, empty subjects, trailing periods, and over-long headers are rejected or
repaired, and anything unusable falls back to the heuristic message. The model
never sees the whole diff — only a compact summary of each proposed commit,
which keeps calls small and cheap.

## Commit ordering

Grouping says *what* belongs together; it says nothing about *when*. If the
auth commit imports something the payments commit creates, checking out the
auth commit alone gives an ImportError.

`ordering.py` builds a second graph — group A before group B whenever A
introduces a name B uses — and topologically sorts it. Genuine cycles are
reported and the weakest link broken, rather than failing.

## Running as a service

The engine assumed a checkout on local disk: run `git diff`, read files, mine
`git log`. A web service has none of that, so `core.py` takes plain data in and
gives plain data out. The CLI gathers that data from git; the service receives
it in a request body. One implementation, two front doors.

```bash
make docker-run                    # build and start
open http://localhost:8000/docs    # interactive API documentation

curl localhost:8000/health
curl localhost:8000/ready          # also reports model configuration

curl -X POST localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d "{\"diff\": $(git diff | jq -Rs .), \"messages\": false}"
```

FastAPI generates `/docs` from the Pydantic schemas in `service/schemas.py`,
so the documentation cannot drift from the code — two tests assert the
generated OpenAPI document contains the endpoints and their error codes.

Request bodies are validated by the schema before the endpoint runs. A body
of the wrong shape returns 422; a diff or file set that is too large returns
413. Declaring response models too means FastAPI filters outgoing payloads to
exactly those fields, so nothing internal can leak by accident.

`POST /analyze` takes `diff` (required), `files` (optional map of path to
current contents — improves symbol detection), `co_change` (optional, mined
from the caller's history), and `messages` (optional bool).

The service is stateless: it never clones a repo, writes to disk, or holds
anything between requests. Any replica can answer any request, which is what
makes scaling it trivial. It also never commits — applying commits needs your
working tree and your git credentials, so that stays on your machine.

**On privacy.** Sending a diff to a server means your code leaves your
machine. That is why the whole thing ships as a container you can run
yourself: `docker compose up` and the service is on your laptop.

## Two products, one engine

| | Where it runs | What it does |
|---|---|---|
| **VS Code extension** | your machine | Split changes *before* you commit |
| **Pull request check** | GitHub Actions | Flag PRs that bundle unrelated changes |

The extension calls the local Python directly. The PR check needs the
container — a webhook has no laptop to shell out to, and CI gets a working
engine without installing packages or pinning versions.

## The pull request check

On every pull request, the workflow builds the image, starts the service,
sends it the PR's diff, and comments if the changes should have been several
commits:

> ### This pull request contains 2 unrelated changes
>
> **1. `feat(payments): add stripe charging`**
> 3 files · +17 −0
>
> **2. `feat(auth): add JWT verification`**
> 2 files · +13 −0

Design decisions worth knowing:

- **It never fails the build.** A tool that blocks merges over style gets
  switched off in a week; one that leaves a useful comment gets read. Pass
  `--fail-over N` if you want it strict.
- **It edits its own comment** rather than posting a new one per push, and
  deletes it once the PR becomes one coherent change.
- **A broken analysis is not a broken build.** If the service is unreachable,
  the check logs a warning and exits zero.
- **`base...head`, not `base..head`** — three dots compares against the merge
  base, so commits that landed on the base branch after the PR opened are not
  blamed on the author.

Try it locally:

```bash
python3 ci/hygiene.py --service http://localhost:8000 \
    --base main --head HEAD --dry-run
```

## The editor extension

`extension/` is a VS Code extension in plain JavaScript — no build step, so
there is no compiler between you and a working plugin.

```bash
cd extension
npm test          # 37 tests, zero dependencies
code .            # then press F5 to launch it
```

It is a thin client by design: it runs `python -m splitter plan`, renders the
JSON, lets you edit messages and merge commits, then sends the reviewed plan
back to `python -m splitter apply --plan`. No analysis is reimplemented in
JavaScript, so everything interesting stays covered by the Python tests.

Two decisions worth knowing about:

- The plan travels to Python through a **temp file, not the command line**.
  Commit messages are arbitrary user text, and putting that on a command line
  is how quoting bugs and injection problems begin.
- Everything rendered in the panel is **HTML-escaped**. The strings shown are
  source code the user just wrote, so they are full of angle brackets and
  quotes; five tests cover this specifically.

## Known limits

- **Mixed add/remove hunks are never split.** `subhunks.py` only cuts runs of
  pure additions, because reasoning about interleaved deletions is where
  correctness bugs live.
- **Symbol detection is regex-based**, not a real parse. Good on ordinary
  Python, JS/TS, Go, and Java; weaker on heavy macros or unusual formatting.
  `symbols.py` is the only file tree-sitter would replace.
- **Commit order is not dependency-aware yet.** In the demo, the auth commit
  imports `charge_card` before the payments commit creates it, so commit 1
  would not run on its own. Ordering by def/use edges is a Phase 3 job;
  actually *checking* it is the Kubernetes verification work in Phase 6.
- Binary files and renames are all-or-nothing (`FileDiff.is_atomic`).
- Paths containing spaces may parse incorrectly in exotic cases.
