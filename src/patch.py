"""The apply_patch format of Codex (codex-rs/apply-patch): parsing a patch and applying its changes to a file."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

BEGIN, END, EOF = "*** Begin Patch", "*** End Patch", "*** End of File"
ADD, DELETE, UPDATE, MOVE = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: "


@dataclass
class Chunk:
    contexts: list[str] = field(default_factory=list)  # @@ lines: the change is looked for after them
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    eof: bool = False


@dataclass
class Section:
    kind: str  # add, delete or update
    path: str
    move: str = ""
    content: str = ""
    chunks: list[Chunk] = field(default_factory=list)


def parse_patch(text: str) -> list[Section]:
    """The file sections of a patch; ValueError names the first line that breaks the format."""
    sections: list[Section] = []
    section: Section | None = None
    for number, line in enumerate(text.strip().splitlines(), 1):
        # Envelopes may be omitted or repeated between files, but content outside a file section is invalid.
        if line.strip() in (BEGIN, END):
            section = None
            continue
        header = next((h for h in (ADD, DELETE, UPDATE) if line.startswith(h)), None)
        if header:
            kind = {ADD: "add", DELETE: "delete", UPDATE: "update"}[header]
            section = Section(kind, line[len(header) :].strip())
            sections.append(section)
            continue
        if section is None:
            if not line.strip():
                continue
            raise ValueError(
                f"line {number}: expected a file header ('{ADD}<path>', '{UPDATE}<path>' or "
                f"'{DELETE}<path>'), got {line[:80]!r}"
            )
        if section.kind == "delete":
            raise ValueError(f"line {number}: nothing may follow '{DELETE}{section.path}'")
        if section.kind == "add":  # a line without its + (common inside multi-line strings) is kept as it is
            section.content += (line[1:] if line.startswith("+") else line) + "\n"
            continue
        if line.startswith(MOVE) and not section.chunks:
            section.move = line[len(MOVE) :].strip()
            continue
        if line.strip() == EOF:
            if section.chunks:
                section.chunks[-1].eof = True
            continue
        if line.startswith("@@"):
            chunk = section.chunks[-1] if section.chunks else None
            if chunk is None or chunk.old or chunk.new or chunk.eof:  # stacked @@ lines narrow one change
                chunk = Chunk()
                section.chunks.append(chunk)
            if line[2:].strip():
                chunk.contexts.append(line[2:].strip())
            continue
        if not section.chunks or section.chunks[-1].eof:
            section.chunks.append(Chunk())
        chunk = section.chunks[-1]
        tag, rest = (line[0], line[1:]) if line else (" ", "")
        if tag == "-":
            chunk.old.append(rest)
        elif tag == "+":
            chunk.new.append(rest)
        else:  # a context line, also when its leading space was left out; matching is whitespace-tolerant
            rest = rest if tag == " " else line
            chunk.old.append(rest)
            chunk.new.append(rest)
    for section in sections:
        if section.kind == "update" and not section.chunks and not section.move:
            raise ValueError(f"'{UPDATE}{section.path}' has no changes")
    if not sections:
        raise ValueError("the patch has no file sections")
    return sections


PUNCTUATION = str.maketrans(
    {
        **dict.fromkeys("‐‑‒–—―−", "-"),
        **dict.fromkeys("‘’‚‛", "'"),
        **dict.fromkeys("“”„‟", '"'),
        **dict.fromkeys("            　", " "),
    }
)
# Codex's seek_sequence: exact, then without trailing whitespace, then without surrounding whitespace, then
# also with typographic punctuation folded to ASCII.
LEVELS = (lambda s: s, str.rstrip, str.strip, lambda s: s.strip().translate(PUNCTUATION))


def seek(lines: list[str], pattern: list[str], start: int, eof: bool = False) -> list[int]:
    """Start indexes (at or after `start`) where `pattern` matches, at the strictest level that matches at all.
    With `eof` a match that ends at the last line is preferred."""
    if not pattern:
        return [start]
    last = len(lines) - len(pattern)
    for level in LEVELS:
        text, wanted = [level(line) for line in lines], [level(line) for line in pattern]
        if eof and last >= start and text[last:] == wanted:
            return [last]
        hits = [i for i in range(start, last + 1) if text[i : i + len(pattern)] == wanted]
        if hits:
            return hits
    return []


def closest(lines: list[str], pattern: list[str], start: int = 0) -> str:
    """Where the change was probably meant, and the first of its lines that differs from the file there."""
    fold = LEVELS[-1]
    text, wanted = [fold(line) for line in lines], [fold(line) for line in pattern]
    best, at = 0, -1
    for i in range(start, len(lines)):
        k = 0
        while k < len(wanted) and i + k < len(text) and text[i + k] == wanted[k]:
            k += 1
        if k > best and any(wanted[:k]):  # a run of blank lines alone locates nothing
            best, at = k, i
    if at >= 0:
        where = at + best
        found = f"{lines[where]!r} (file line {where + 1})" if where < len(lines) else "the end of the file"
        return (
            f"Its first {best} lines match file lines {at + 1}-{at + best}; its next line {pattern[best]!r} "
            f"differs from {found}."
        )
    probe = next((line.strip() for line in pattern if line.strip()), "")
    stripped = [line.strip() for line in lines]
    match = difflib.get_close_matches(probe, stripped, n=1, cutoff=0.5) if probe else []
    if not match:
        return ""
    at = stripped.index(match[0])
    first, end = max(0, at - 2), min(len(lines), at + len(pattern) + 2)
    return "Closest lines in the file:\n" + "\n".join(f"{i + 1:5}| {lines[i]}" for i in range(first, end))


def update_text(original: str, chunks: list[Chunk], path: str) -> str:
    """`original` with every change applied, in order; ValueError lists every change that cannot be applied."""
    lines = original.split("\n")
    if original.endswith("\n"):
        lines.pop()
    replacements: list[tuple[int, int, list[str]]] = []
    errors: list[str] = []
    index = 0
    for number, chunk in enumerate(chunks, 1):
        anchored, at_context, missing = index, index, None
        for context in chunk.contexts:
            found = seek(lines, [context], anchored)
            if not found:
                missing = context
                break
            at_context, anchored = found[0], found[0] + 1
        if missing is not None:
            errors.append(
                f"change {number}: the @@ line {missing!r} was not found in {path}"
                + (f" after line {index}" if index else "")
            )
            continue
        if not chunk.old:  # a pure insertion: after its @@ line, or at the end of the file
            if not chunk.contexts and not chunk.eof:  # often meant to replace the file, which Add File does
                errors.append(
                    f"change {number}: only + lines and no @@ line, so its place in {path} is unknown; "
                    "use Add File to replace the whole file, an @@ line or context lines to insert, or "
                    "end the change with *** End of File to append"
                )
                continue
            at = anchored if chunk.contexts else len(lines)
            replacements.append((at, 0, chunk.new))
            index = at
            continue
        # The change is looked for from the @@ line itself: models often repeat that line as the first context line.
        old, new, start = chunk.old, chunk.new, at_context
        hits = seek(lines, old, start, chunk.eof)
        if not hits and old[-1] == "":  # a trailing empty line standing for the file's final newline
            old, new = old[:-1], new[:-1] if new and new[-1] == "" else new
            hits = seek(lines, old, start, chunk.eof)
        if not hits:
            near = closest(lines, old, start)
            errors.append(
                f"change {number}: its context and - lines were not found in {path}"
                + (" after the @@ line" if chunk.contexts else "")
                + (f". {near}" if near else "")
            )
            continue
        if len(hits) > 1 and not chunk.contexts and not chunk.eof:
            where = ", ".join(str(i + 1) for i in hits[:5])
            errors.append(
                f"change {number}: its context and - lines match {len(hits)} places in {path} (lines "
                f"{where}); add an @@ line naming the enclosing function or more context lines"
            )
            continue
        replacements.append((hits[0], len(old), new))
        index = hits[0] + len(old)
    if errors:
        raise ValueError("\n  ".join(errors))
    # from the end, so earlier indexes stay valid; insertions at one index keep their order
    for start, length, new in reversed(sorted(replacements, key=lambda r: r[0])):
        lines[start : start + length] = new
    return "\n".join(lines) + "\n" if lines else ""
