# Commit Splitter

Splits tangled changes into separate, well-described commits.

You spent the afternoon on authentication *and* pagination. Now you have
fourteen changed files and one commit message that says "wip". This finds the
seams and proposes one commit per feature, each with a Conventional Commits
message.

## Requirements

**Python 3.10 or newer.** That is all. The analysis engine ships inside the
extension and uses only the standard library.

macOS and most Linux distributions already have Python. On Windows, install it
from [python.org](https://python.org) and tick "Add Python to PATH".

## Usage

Make some changes, then run **Split Changes into Commits** from the command
palette, or press the commit icon in the Source Control title bar.

You get a panel listing the proposed commits, the reasoning behind each
grouping, and an editable message per commit. Nothing touches git until you
press Apply.

## Commit messages

Out of the box, messages are generated from code structure — no network calls,
nothing leaves your machine. Configure a model for better ones:

| Provider | Setup |
|---|---|
| `ollama` | Runs locally. Free and private. |
| `gemini` | Set `GEMINI_API_KEY` in your environment. |
| `anthropic` | Set `ANTHROPIC_API_KEY`. |
| `openai` | Set `OPENAI_API_KEY`. |

Keys are read from your shell environment. The extension never stores one.

Run **Check Model Configuration** to confirm yours works.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `commitSplitter.pythonPath` | `python3` | Interpreter to use. |
| `commitSplitter.provider` | `none` | Which model writes the messages. |
| `commitSplitter.model` | *(provider default)* | Override the model name. |
| `commitSplitter.useHistory` | `true` | Mine `git log` for files that change together. |
| `commitSplitter.enginePath` | *(bundled)* | Point at your own engine checkout. |

## How it decides

Every pair of changes is scored on whether they touch the same function,
whether one introduces a name the other uses, whether they are an
implementation and its test, and how much vocabulary they share. Tightly
connected clusters become commits.

The signal that matters most is deliberately *negative*: two changes in the
same file but different functions score almost nothing. File proximity is what
makes naive tools glue unrelated features together.

Commits are then ordered so nothing is referenced before the commit that
creates it.

## Privacy

Your code never leaves your machine unless you configure a cloud model, and
even then only a short summary of each proposed commit is sent — never the
full diff. With `provider: none` or Ollama, nothing is transmitted at all.
