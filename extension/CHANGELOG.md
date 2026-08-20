# Changelog

## 0.1.0

First release.

- Splits uncommitted changes into separate commits by analysing which
  functions each change touches, which names it introduces, and which
  introduced names other changes use.
- Cuts oversized hunks at function and import boundaries, so two features
  added next to each other can land in different commits.
- Orders commits so nothing is referenced before the commit that creates it.
- Writes Conventional Commits messages, using a language model when one is
  configured and structural heuristics otherwise.
- Review panel for editing messages and merging commits before anything is
  applied.
