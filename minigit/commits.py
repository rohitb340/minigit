"""Commits: a snapshot plus the history that led to it."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import objects


@dataclass
class Commit:
    tree: str
    parents: list[str] = field(default_factory=list)
    author: str = ""
    committer: str = ""
    message: str = ""

    @property
    def summary(self) -> str:
        return self.message.strip().splitlines()[0] if self.message.strip() else ""


def identity() -> str:
    """Build the "Name <email> timestamp timezone" string git records.

    Environment variables win so that tests and the fuzzer can pin the identity
    and the timestamp. Without a fixed timestamp two runs of the same sequence
    would produce different commit ids, and the differential fuzzer could never
    compare anything.
    """
    name = os.environ.get("GIT_AUTHOR_NAME", "minigit")
    email = os.environ.get("GIT_AUTHOR_EMAIL", "minigit@localhost")

    stamp = os.environ.get("GIT_AUTHOR_DATE")
    if stamp:
        return f"{name} <{email}> {stamp}"

    seconds = int(time.time())
    offset = -time.timezone if not time.daylight else -time.altzone
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return f"{name} <{email}> {seconds} {sign}{offset // 3600:02d}{(offset % 3600) // 60:02d}"


def serialize(commit: Commit) -> bytes:
    """Encode a commit into git's on disk format.

    Unlike trees, commits are plain text: a block of headers, one blank line,
    then the message. The blank line is what separates them, so a message is
    free to contain anything at all without ambiguity.
    """
    lines = [f"tree {commit.tree}"]
    lines += [f"parent {p}" for p in commit.parents]
    lines.append(f"author {commit.author}")
    lines.append(f"committer {commit.committer}")
    lines.append("")
    lines.append(commit.message)
    return "\n".join(lines).encode()


def parse(data: bytes) -> Commit:
    """Decode a commit object."""
    text = data.decode()
    head, _, message = text.partition("\n\n")

    commit = Commit(tree="", message=message)
    for line in head.splitlines():
        key, _, value = line.partition(" ")
        if key == "tree":
            commit.tree = value
        elif key == "parent":
            commit.parents.append(value)
        elif key == "author":
            commit.author = value
        elif key == "committer":
            commit.committer = value
    return commit


def write(git_dir: Path, tree: str, parents: list[str], message: str) -> str:
    """Create and store a commit object, returning its id."""
    who = identity()
    if not message.endswith("\n"):
        message += "\n"
    commit = Commit(tree=tree, parents=parents, author=who, committer=who, message=message)
    return objects.write_object(git_dir, "commit", serialize(commit))


def read(git_dir: Path, oid: str) -> Commit:
    return parse(objects.read_object_of_type(git_dir, oid, "commit"))


def ancestors(git_dir: Path, start: str) -> list[str]:
    """Every commit reachable from `start`, including itself.

    History is a directed acyclic graph rather than a chain, because a merge
    commit has more than one parent. So this is a graph traversal with a seen
    set, not a simple while loop following one pointer. Without the seen set,
    any repository containing a merge would revisit whole sections of history.
    """
    seen: set[str] = set()
    order: list[str] = []
    stack = [start]

    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        order.append(oid)
        stack.extend(read(git_dir, oid).parents)

    return order


def log_order(git_dir: Path, start: str) -> list[str]:
    """Commits newest first, the way `git log` presents them.

    Sorting by committer timestamp descending approximates git's default well
    enough for our purposes. Real git uses a priority queue keyed on date while
    still respecting parent ordering.
    """
    def timestamp(oid: str) -> int:
        parts = read(git_dir, oid).committer.split()
        try:
            return int(parts[-2])
        except (IndexError, ValueError):
            return 0

    return sorted(ancestors(git_dir, start), key=timestamp, reverse=True)
