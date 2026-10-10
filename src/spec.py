"""Requirement tree loading, requirement cards and the product outline."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

QUOTED = re.compile(r'"([^"\n]{1,80})"|“([^“”\n]{1,80})”')
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


@dataclass
class Node:
    id: str
    name: str
    type: str
    description: str = ""
    scenarios: list = field(default_factory=list)
    children: list[Node] = field(default_factory=list)

    @property
    def atomics(self) -> list[Node]:
        if self.type == "ATOMIC":
            return [self]
        return [a for c in self.children for a in c.atomics]


def _node(raw: dict) -> Node:
    return Node(
        str(raw.get("id", "")),
        str(raw.get("name", "")),
        str(raw.get("type", "")),
        str(raw.get("description") or "").strip(),
        raw.get("scenarios") or [],
        [_node(c) for c in raw.get("children") or []],
    )


def load(requirement_dir: Path) -> Node:
    yml = requirement_dir / "requirements.yaml"
    if yml.is_file():
        return _node(yaml.safe_load(yml.read_text()))
    txt = requirement_dir / "requirement.txt"
    data = json.loads(txt.read_text())
    return _node(yaml.safe_load(data["requirements_yaml"]))


def quoted_names(text: str) -> list[str]:
    return list(dict.fromkeys(ascii_name or smart_name for ascii_name, smart_name in QUOTED.findall(text)))


def _scenarios(atomic: Node) -> str:
    """Keep complete steps in their original scenario order, including repeated actions and conditions."""
    parts = []
    for number, scenario in enumerate(atomic.scenarios, 1):
        lines = [f"    {step.get('keyword', '')} {step.get('content', '')}" for step in scenario.get("steps") or []]
        parts.append(f"  Scenario {number}:\n" + ("\n".join(lines) or "    (no steps)"))
    return "\n".join(parts)


def images(text: str) -> list[str]:
    return list(dict.fromkeys(IMAGE.findall(text)))


def card(atomic: Node, base: str = "") -> str:
    names = quoted_names(atomic.description)
    lines = [f"### {atomic.id} {atomic.name}", atomic.description]
    pictures = images(atomic.description)
    if pictures:
        lines.append(
            "Reference images (read them to see the intended layout; names in the text win): "
            + ", ".join(f"{base}/{p}" if base else p for p in pictures)
        )
    if names:
        lines.append("Exact UI strings (accessible names / messages): " + "; ".join(f'"{n}"' for n in names))
    lines.append(f"Acceptance scenarios: {len(atomic.scenarios)}")
    if atomic.scenarios:
        lines.append(_scenarios(atomic))
    return "\n".join(lines)


def shared(root: Node, atomic_ids: list[str] | None = None) -> str:
    """Descriptions of the groups containing the given atomic requirements (all groups when None).

    Group descriptions state rules that hold for every requirement below them (accessible names of shared
    UI, persistence, validation), so every session working on those requirements must see them."""
    wanted = set(atomic_ids) if atomic_ids is not None else None
    out: list[str] = []

    def walk(node: Node) -> bool:
        if node.type == "ATOMIC":
            return wanted is None or node.id in wanted
        hit = any([walk(c) for c in node.children])
        if hit and node.description:
            out.append(f"### {node.id} {node.name}\n{node.description}")
        return hit

    walk(root)
    return "\n\n".join(reversed(out))


def snapshot(root: Node) -> dict[str, str]:
    """Fingerprint the atomic text and its inherited group rules. v2 identifies context-aware snapshots."""
    return {
        a.id: "v2:"
        + hashlib.sha256(
            json.dumps(
                [a.name, a.description, a.scenarios, shared(root, [a.id])], sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        for a in root.atomics
    }


def changes(root: Node, earlier: dict[str, str] | None) -> dict[str, str]:
    """The atomic requirements a later stage must (re)build: "new", "changed" since the earlier stage's
    snapshot, or "unverified" when its earlier group context was not recorded. Unchanged ones are left out."""
    current = snapshot(root)
    if earlier is None:
        return {i: "unverified" for i in current}
    return {
        i: "new" if i not in earlier else "changed" if earlier[i].startswith("v2:") else "unverified"
        for i, f in current.items()
        if earlier.get(i) != f
    }


def outline(root: Node, status: dict[str, str] | None = None) -> str:
    """Compact overview of the whole product for planning; `status` marks requirements as new, changed or
    unverified."""
    lines = [f"# {root.name}", root.description, ""]
    status = status or {}

    def walk(node: Node, depth: int) -> None:
        for child in node.children:
            if child.type == "ATOMIC":
                mark = f" [{status[child.id]}]" if child.id in status else ""
                lines.append(f"{'  ' * depth}- {child.id} {child.name} ({len(child.scenarios)} tests){mark}")
            else:
                lines.append(f"{'  ' * depth}- {child.id} {child.name}")
                if child.description:  # group-level rules apply to every requirement below them
                    lines.append(f"{'  ' * (depth + 1)}{' '.join(child.description.split())}")
                walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


BACKTICKED = re.compile(r"`([^`\n]{2,80})`")


def seed_values(root: Node) -> list[str]:
    """Backticked precondition phrases for the advisory scan, including composite and derived values."""
    found: dict[str, None] = {}
    for atomic in root.atomics:
        for scenario in atomic.scenarios:
            for step in scenario.get("steps") or []:
                if step.get("keyword") == "GIVEN":
                    found.update(dict.fromkeys(BACKTICKED.findall(step.get("content", ""))))
    return list(found)
