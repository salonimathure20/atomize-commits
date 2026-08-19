"""Work out which function or class a hunk lives inside.

WHY THIS MATTERS
----------------
"Same file" is a weak signal. If you add a payments route and a login route
to the same routes.py, file-level analysis says they belong together and it is
wrong. Knowing that one hunk sits inside handle_payment() and the other inside
handle_login() is what rescues that case.

The proper tool for this is tree-sitter, which builds a real syntax tree. It is
an optional dependency here because it needs compiled grammars, and this
project should run for someone who just cloned it. So we ship a regex-based
reader that is roughly 90% as good on ordinary code, and we keep the interface
narrow enough that swapping in tree-sitter later touches only this file.
"""

from __future__ import annotations

import re
from pathlib import Path

# Each pattern captures the name of a thing being defined. We record the
# indentation too, so we can tell an inner method from its outer class.
DEFINITION_PATTERNS = [
    re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)"),                        # python
    re.compile(r"^(\s*)class\s+(\w+)"),                                   # python, js, java
    re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"),   # js/ts
    re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function)"),
    re.compile(r"^(\s*)func\s+(?:\([^)]*\)\s*)?(\w+)"),                   # go
    re.compile(r"^(\s*)(?:public|private|protected|static|final|\s)+[\w<>\[\],\s]+\s+(\w+)\s*\("),  # java/c#
    re.compile(r"^(\s*)(\w+)\s*\([^)]*\)\s*\{\s*$"),                      # bare js method
]

# Words that show up everywhere and tell us nothing about relatedness.
STOPWORDS = {
    "if", "else", "elif", "for", "while", "return", "import", "from", "as",
    "def", "class", "self", "this", "true", "false", "none", "null", "new",
    "const", "let", "var", "function", "async", "await", "try", "except",
    "catch", "finally", "raise", "throw", "with", "in", "is", "not", "and",
    "or", "the", "int", "str", "bool", "list", "dict", "set", "func", "err",
    "error", "type", "string", "public", "private", "static", "void", "print",
}

IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def read_file(repo_root: str | Path, path: str) -> list[str]:
    """Read the working-tree version of a file. Returns [] if unreadable."""
    full = Path(repo_root) / path
    try:
        return full.read_text(encoding="utf-8", errors="replace").split("\n")
    except (OSError, UnicodeError):
        return []


def enclosing_symbols(lines: list[str], start_line: int) -> list[str]:
    """Names of the definitions containing line `start_line` (1-based).

    We scan upward and keep any definition whose indentation is strictly less
    than the last one we kept. That walks us out of a method, into its class,
    and stops at the top of the file.

    Returns outermost-first, e.g. ["PaymentService", "charge"].
    """
    found: list[tuple[int, str]] = []
    indent_ceiling = None

    for i in range(min(start_line, len(lines)) - 1, -1, -1):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith(("#", "//", "*")):
            continue
        for pattern in DEFINITION_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            indent = len(m.group(1).expandtabs(4))
            if indent_ceiling is None or indent < indent_ceiling:
                found.append((indent, m.group(2)))
                indent_ceiling = indent
            break
        if indent_ceiling == 0:
            break

    return [name for _, name in reversed(found)]


def defined_names(added: list[str]) -> set[str]:
    """Names this hunk introduces — new functions, classes, constants."""
    names: set[str] = set()
    for line in added:
        for pattern in DEFINITION_PATTERNS:
            m = pattern.match(line)
            if m:
                names.add(m.group(2))
                break
        else:
            m = re.match(r"^\s*([A-Z][A-Z0-9_]{2,})\s*=", line)  # CONSTANT = ...
            if m:
                names.add(m.group(1))
    return names


def identifiers(lines: list[str]) -> set[str]:
    """Meaningful words in these lines, with noise stripped out.

    We also split camelCase and snake_case into parts, so `issue_token` and
    `tokenStore` recognise each other through the shared word "token".
    """
    words: set[str] = set()
    for line in lines:
        for token in IDENTIFIER.findall(line):
            lowered = token.lower()
            if lowered in STOPWORDS:
                continue
            words.add(lowered)
            for part in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", token):
                part = part.lower()
                if len(part) >= 3 and part not in STOPWORDS:
                    words.add(part)
    return words
