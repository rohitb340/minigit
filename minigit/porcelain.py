"""Porcelain: the commands you actually type.

Each one is a thin composition of the plumbing in the other modules, which is
how real git is layered too. `add` is hash-object plus an index write. `commit`
is write-tree plus commit-tree plus a ref update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import commits, diff, index as index_mod, merge, objects, refs, semantic, trees
from .repository import RepositoryError, work_tree


class Narrator:
    """Collects a running commentary for --explain.

    Off by default and free when off. The point is to make the object model
    visible: instead of describing content addressing you can show it.
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.lines: list[str] = []

    def say(self, text: str) -> None:
        if self.enabled:
            self.lines.append(text)

    def object(self, kind: str, oid: str, label: str, reused: bool) -> None:
        verb = "reused " if reused else "wrote  "
        self.say(f"  {verb} {kind:<6} {oid[:6]}  {label}")

    def dump(self) -> str:
        return "\n".join(self.lines)


def _iter_files(root: Path):
    """Every file in the working tree, skipping .git itself."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            yield path


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


# ---------------------------------------------------------------- add

def add(git_dir: Path, paths: list[str], narrator: Narrator | None = None) -> list[str]:
    """Stage files: store their contents, then record them in the index.

    The blob is written to the object database here, at add time, not later at
    commit time. That is why staging a file and then deleting it does not lose
    your work.

    Adding a directory also stages DELETIONS inside it, matching `git add .`
    since git 2.0. Without this a renamed file leaves its old path in the index
    forever, so every later commit records both names and rolling back to the
    rename looks like it did nothing.
    """
    narrator = narrator or Narrator()
    root = work_tree(git_dir)
    idx = index_mod.Index(git_dir)
    staged: list[str] = []

    targets: list[Path] = []
    directories: list[Path] = []
    for raw in paths:
        candidate = (root / raw).resolve() if raw != "." else root
        if candidate.is_dir():
            targets.extend(_iter_files(candidate))
            directories.append(candidate)
        elif candidate.is_file():
            targets.append(candidate)
        elif idx.entries.get(_rel(root, candidate)) is not None:
            # Named a file that is indexed but no longer on disk. That is a
            # deletion, not an error.
            rel = _rel(root, candidate)
            idx.remove(rel)
            narrator.say(f"  removed  {rel}  (deleted from working tree)")
            staged.append(rel)
        else:
            raise RepositoryError(f"path does not match any file: {raw}")

    # Prune indexed paths that live under an added directory but are gone.
    for directory in directories:
        prefix = "" if directory == root else _rel(root, directory) + "/"
        for rel in [p for p in idx.entries if p.startswith(prefix)]:
            if not (root / rel).is_file():
                idx.remove(rel)
                narrator.say(f"  removed  {rel}  (deleted from working tree)")
                staged.append(rel)

    for full in targets:
        rel = _rel(root, full)
        content = full.read_bytes()
        oid = objects.hash_object("blob", content)
        reused = objects.exists(git_dir, oid)
        objects.write_object(git_dir, "blob", content)

        idx.add(index_mod.entry_from_file(rel, full, oid))
        narrator.object("blob", oid, rel, reused)
        staged.append(rel)

    idx.write()
    narrator.say(f"  index now holds {len(idx.entries)} entries")
    return staged


# ---------------------------------------------------------------- commit

def commit(git_dir: Path, message: str, narrator: Narrator | None = None) -> str:
    """Turn the index into a permanent snapshot."""
    narrator = narrator or Narrator()
    idx = index_mod.Index(git_dir)

    if not idx.entries:
        raise RepositoryError("nothing staged to commit")

    staged = idx.staged_paths()
    narrator.say(f"  read index          {len(staged)} entries")

    tree_oid = trees.write_tree_from_paths(git_dir, staged)
    narrator.object("tree", tree_oid, "/  (root)", False)

    parent = refs.resolve_head(git_dir)
    parents = [parent] if parent else []

    oid = commits.write(git_dir, tree_oid, parents, message)
    narrator.object("commit", oid, f"parent {parent[:6] if parent else 'none'}", False)

    refs.update_head(git_dir, oid)
    branch = refs.current_branch(git_dir)
    narrator.say(f"  updated refs/heads/{branch} -> {oid[:6]}")

    return oid


# ---------------------------------------------------------------- log

def log(git_dir: Path) -> list[tuple[str, commits.Commit]]:
    head = refs.resolve_head(git_dir)
    if head is None:
        return []
    return [(oid, commits.read(git_dir, oid)) for oid in commits.log_order(git_dir, head)]


# ---------------------------------------------------------------- status

@dataclass
class Status:
    staged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    branch: str | None = None

    @property
    def clean(self) -> bool:
        return not (self.staged or self.modified or self.deleted or self.untracked)


def status(git_dir: Path) -> Status:
    """The three comparisons: index vs HEAD, working tree vs index, and the rest.

    Each of the three sections git prints is one of these comparisons. Nothing
    more mysterious than that.
    """
    root = work_tree(git_dir)
    idx = index_mod.Index(git_dir)
    result = Status(branch=refs.current_branch(git_dir))

    head_oid = refs.resolve_head(git_dir)
    head_files: dict[str, tuple[str, str]] = {}
    if head_oid:
        head_files = trees.flatten(git_dir, commits.read(git_dir, head_oid).tree)

    # index vs HEAD: what would be committed
    for path, entry in idx.entries.items():
        if path not in head_files or head_files[path][1] != entry.oid:
            result.staged.append(path)
    for path in head_files:
        if path not in idx.entries:
            result.deleted.append(path)

    # working tree vs index: what is changed but not staged
    for path, entry in idx.entries.items():
        full = root / path
        if not full.exists():
            result.deleted.append(path)
            continue
        # The stat cache short circuit. If size and mtime are untouched we do
        # not open the file at all, which is why status stays fast.
        if idx.looks_unchanged(entry, full):
            continue
        if objects.hash_object("blob", full.read_bytes()) != entry.oid:
            result.modified.append(path)

    # everything else on disk
    for full in _iter_files(root):
        rel = _rel(root, full)
        if rel not in idx.entries:
            result.untracked.append(rel)

    for bucket in (result.staged, result.modified, result.deleted, result.untracked):
        bucket.sort()
    return result


# ---------------------------------------------------------------- branch / checkout

def branch(git_dir: Path, name: str) -> str:
    head = refs.resolve_head(git_dir)
    if head is None:
        raise RepositoryError("cannot branch before the first commit")
    if refs.read_ref(git_dir, f"refs/heads/{name}"):
        raise RepositoryError(f"branch already exists: {name}")
    refs.write_ref(git_dir, f"refs/heads/{name}", head)
    return head


def checkout(git_dir: Path, name: str, force: bool = False) -> None:
    """Replace the working tree with a branch's snapshot.

    Refusing to run with a dirty tree matters: without the check, switching
    branches would silently overwrite work that was never committed.
    """
    root = work_tree(git_dir)
    current = status(git_dir)
    if not force and (current.modified or current.staged):
        raise RepositoryError(
            "you have local changes that would be overwritten; commit them or use --force"
        )

    target = refs.resolve(git_dir, name)
    wanted = trees.flatten(git_dir, commits.read(git_dir, target).tree)

    idx = index_mod.Index(git_dir)
    for path in list(idx.entries):
        if path not in wanted:
            (root / path).unlink(missing_ok=True)
            idx.remove(path)

    for path, (_mode, oid) in wanted.items():
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(objects.read_object_of_type(git_dir, oid, "blob"))
        idx.add(index_mod.entry_from_file(path, full, oid))

    idx.write()

    if refs.read_ref(git_dir, f"refs/heads/{name}"):
        refs.set_head_to_branch(git_dir, name)
    else:
        refs.detach_head(git_dir, target)


# ---------------------------------------------------------------- diff

def diff_working(git_dir: Path) -> str:
    """Unified diff of the working tree against the index."""
    root = work_tree(git_dir)
    idx = index_mod.Index(git_dir)
    out: list[str] = []

    for path, entry in sorted(idx.entries.items()):
        full = root / path
        if not full.exists():
            continue
        current = full.read_bytes()
        if objects.hash_object("blob", current) == entry.oid:
            continue
        staged_text = objects.read_object_of_type(git_dir, entry.oid, "blob").decode(
            errors="replace")
        out.append(diff.unified(staged_text, current.decode(errors="replace"), path, path))

    return "".join(out)


def diff_commits(git_dir: Path, a: str, b: str, semantic_mode: bool = False) -> str:
    """Diff two commits, either line by line or structurally."""
    a_files = trees.flatten(git_dir, commits.read(git_dir, refs.resolve(git_dir, a)).tree)
    b_files = trees.flatten(git_dir, commits.read(git_dir, refs.resolve(git_dir, b)).tree)
    out: list[str] = []

    for path in sorted(set(a_files) | set(b_files)):
        old_oid = a_files.get(path, (None, None))[1]
        new_oid = b_files.get(path, (None, None))[1]
        if old_oid == new_oid:
            continue

        def load(oid):
            return objects.read_object_of_type(git_dir, oid, "blob").decode(
                errors="replace") if oid else ""

        old_text, new_text = load(old_oid), load(new_oid)

        if semantic_mode and path.endswith(".py"):
            out.append(f"{path}\n" + "\n".join(semantic.structural_diff(old_text, new_text)) + "\n")
        else:
            out.append(diff.unified(old_text, new_text, path, path))

    return "".join(out)


# ---------------------------------------------------------------- merge

@dataclass
class MergeResult:
    ok: bool
    commit_oid: str | None = None
    text_conflicts: list[str] = field(default_factory=list)
    semantic_conflicts: list[tuple[str, list[semantic.SemanticConflict]]] = field(
        default_factory=list)
    base: str | None = None
    fast_forward: bool = False


def merge_branch(
    git_dir: Path, other: str, allow_semantic_break: bool = False
) -> MergeResult:
    """Merge another branch into the current one.

    The semantic check runs after the textual merge and before anything is
    written. If it finds a dangling reference we abort with nothing touched,
    which is the entire point: git would have committed this.
    """
    root = work_tree(git_dir)
    ours = refs.resolve_head(git_dir)
    theirs = refs.resolve(git_dir, other)

    if ours is None:
        raise RepositoryError("nothing to merge into")

    base = merge.merge_base(git_dir, ours, theirs)

    if base == theirs:
        return MergeResult(ok=True, commit_oid=ours, base=base)
    if base == ours:
        # Fast forward: our branch has no commits the other lacks, so there is
        # nothing to combine. Just move the pointer.
        checkout(git_dir, other, force=True)
        refs.write_ref(git_dir, f"refs/heads/{refs.current_branch(git_dir)}", theirs)
        return MergeResult(ok=True, commit_oid=theirs, base=base, fast_forward=True)

    merged, text_conflicts = merge.merge_trees(git_dir, base, ours, theirs)

    # ---- the semantic gate ----
    base_files = trees.flatten(git_dir, commits.read(git_dir, base).tree) if base else {}
    ours_files = trees.flatten(git_dir, commits.read(git_dir, ours).tree)
    theirs_files = trees.flatten(git_dir, commits.read(git_dir, theirs).tree)
    ours_label = refs.current_branch(git_dir) or "ours"

    semantic_problems: list[tuple[str, list[semantic.SemanticConflict]]] = []

    for path, (_mode, content) in merged.items():
        if not path.endswith(".py") or path in text_conflicts:
            continue

        def source(files, key=path):
            entry = files.get(key)
            return objects.read_object_of_type(git_dir, entry[1], "blob").decode(
                errors="replace") if entry else None

        found = semantic.find_conflicts(
            base_src=source(base_files),
            ours_src=source(ours_files) or "",
            theirs_src=source(theirs_files) or "",
            merged_src=content.decode(errors="replace"),
            ours_name=ours_label,
            theirs_name=other,
        )
        if found:
            semantic_problems.append((path, found))

    if semantic_problems and not allow_semantic_break:
        return MergeResult(
            ok=False, text_conflicts=text_conflicts,
            semantic_conflicts=semantic_problems, base=base,
        )

    # Write the merged tree out and stage it.
    idx = index_mod.Index(git_dir)
    for path, (_mode, content) in merged.items():
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        oid = objects.write_object(git_dir, "blob", content)
        idx.add(index_mod.entry_from_file(path, full, oid))
    for path in list(idx.entries):
        if path not in merged:
            (root / path).unlink(missing_ok=True)
            idx.remove(path)
    idx.write()

    if text_conflicts:
        return MergeResult(ok=False, text_conflicts=text_conflicts, base=base,
                           semantic_conflicts=semantic_problems)

    tree_oid = trees.write_tree_from_paths(git_dir, idx.staged_paths())
    oid = commits.write(git_dir, tree_oid, [ours, theirs], f"Merge branch '{other}'")
    refs.update_head(git_dir, oid)

    return MergeResult(ok=True, commit_oid=oid, base=base,
                       semantic_conflicts=semantic_problems)
