"""Merging: combining two lines of history.

Comparing the two branch tips directly cannot work. If a line is present on one
side and absent on the other, that is equally consistent with "they added it"
and "we deleted it", and those need opposite outcomes. You need a third point
of reference, the last commit both branches shared, to tell them apart.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from . import commits, diff, objects, trees


def merge_base(git_dir: Path, a: str, b: str) -> str | None:
    """Find the best common ancestor of two commits.

    Breadth first from `a` marking everything reachable, then breadth first
    from `b` until we meet a marked commit. Because we expand by generation,
    the first meeting point is the nearest common ancestor.

    The subtlety is that history is a directed acyclic graph, not a tree. A
    commit can have several parents, so two branches may share several common
    ancestors with none of them an ancestor of the others. That happens after
    criss cross merges. Real git recursively merges the multiple bases into a
    virtual one; we return the nearest, which is correct for every history a
    single user can produce without deliberately constructing the pathological
    case.
    """
    reachable = set(commits.ancestors(git_dir, a))

    queue = deque([b])
    seen = {b}
    while queue:
        oid = queue.popleft()
        if oid in reachable:
            return oid
        for parent in commits.read(git_dir, oid).parents:
            if parent not in seen:
                seen.add(parent)
                queue.append(parent)

    return None


def merge_blobs(
    git_dir: Path,
    base_oid: str | None,
    ours_oid: str | None,
    theirs_oid: str | None,
    path: str,
) -> tuple[bytes, bool]:
    """Three way merge of one file. Returns (content, had_conflict).

    The whole of merging is these few rules, applied first at file level and
    then, when both sides changed, line by line.
    """
    def load(oid: str | None) -> bytes:
        return objects.read_object_of_type(git_dir, oid, "blob") if oid else b""

    if ours_oid == theirs_oid:
        return load(ours_oid), False
    if ours_oid == base_oid:
        return load(theirs_oid), False
    if theirs_oid == base_oid:
        return load(ours_oid), False

    # Both sides changed the file. Fall through to line level merging.
    base = load(base_oid).decode(errors="replace").splitlines()
    ours = load(ours_oid).decode(errors="replace").splitlines()
    theirs = load(theirs_oid).decode(errors="replace").splitlines()

    merged, conflict = merge_lines(base, ours, theirs)
    return ("\n".join(merged) + "\n").encode(), conflict


def merge_lines(base: list[str], ours: list[str], theirs: list[str]) -> tuple[list[str], bool]:
    """Line level three way merge, the diff3 approach.

    Diff base against each side, then walk both edit scripts together. Regions
    only one side touched are taken silently. Regions both sides touched are a
    conflict unless the two happen to be identical.
    """
    ours_edits = diff.diff_lines(base, ours)
    theirs_edits = diff.diff_lines(base, theirs)

    # Reduce each edit script to "for base line i, what does this side say?".
    def project(edits: list[diff.Edit]) -> tuple[dict[int, list[str]], set[int]]:
        replacement: dict[int, list[str]] = {}
        deleted: set[int] = set()
        base_pos = 0
        pending: list[str] = []

        for edit in edits:
            if edit.op is diff.Op.EQUAL:
                if pending:
                    replacement.setdefault(base_pos, []).extend(pending)
                    pending = []
                base_pos += 1
            elif edit.op is diff.Op.DELETE:
                deleted.add(base_pos)
                base_pos += 1
            else:
                pending.append(edit.text)

        if pending:
            replacement.setdefault(base_pos, []).extend(pending)

        return replacement, deleted

    ours_ins, ours_del = project(ours_edits)
    theirs_ins, theirs_del = project(theirs_edits)

    merged: list[str] = []
    conflict = False

    for i in range(len(base) + 1):
        o_ins = ours_ins.get(i, [])
        t_ins = theirs_ins.get(i, [])

        if o_ins and t_ins and o_ins != t_ins:
            conflict = True
            merged.append("<<<<<<< ours")
            merged.extend(o_ins)
            merged.append("=======")
            merged.extend(t_ins)
            merged.append(">>>>>>> theirs")
        else:
            merged.extend(o_ins or t_ins)

        if i < len(base):
            o_gone = i in ours_del
            t_gone = i in theirs_del
            if not o_gone and not t_gone:
                merged.append(base[i])
            # If either side deleted the line it stays deleted. Both sides
            # deleting the same line is agreement, not a conflict.

    return merged, conflict


def merge_trees(
    git_dir: Path, base: str | None, ours: str, theirs: str
) -> tuple[dict[str, tuple[str, bytes]], list[str]]:
    """Merge two commit trees. Returns ({path: (mode, content)}, conflicts)."""
    base_files = trees.flatten(git_dir, commits.read(git_dir, base).tree) if base else {}
    ours_files = trees.flatten(git_dir, commits.read(git_dir, ours).tree)
    theirs_files = trees.flatten(git_dir, commits.read(git_dir, theirs).tree)

    result: dict[str, tuple[str, bytes]] = {}
    conflicts: list[str] = []

    for path in sorted(set(base_files) | set(ours_files) | set(theirs_files)):
        b = base_files.get(path)
        o = ours_files.get(path)
        t = theirs_files.get(path)

        # Deleted on one side and untouched on the other means it stays deleted.
        if o is None and t is None:
            continue
        if o is None and b is not None and t == b:
            continue
        if t is None and b is not None and o == b:
            continue

        content, had_conflict = merge_blobs(
            git_dir,
            b[1] if b else None,
            o[1] if o else None,
            t[1] if t else None,
            path,
        )
        mode = (o or t or b)[0]
        result[path] = (mode, content)
        if had_conflict:
            conflicts.append(path)

    return result, conflicts
