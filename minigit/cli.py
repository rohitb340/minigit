"""Command line dispatch.

Flags mirror real git's wherever they overlap, so the same invocation can be
run against both tools and the output compared. The commands that do not exist
in git at all are `merge --semantic` and `diff --semantic`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import objects, porcelain, refs, repository, trees


def cmd_init(args) -> int:
    where = Path(args.directory).resolve()
    where.mkdir(parents=True, exist_ok=True)
    print(f"Initialised empty minigit repository in {repository.init(where)}")
    return 0


def cmd_hash_object(args) -> int:
    content = sys.stdin.buffer.read() if args.path is None else Path(args.path).read_bytes()
    if args.write:
        oid = objects.write_object(repository.find_git_dir(), args.type, content)
    else:
        oid = objects.hash_object(args.type, content)
    print(oid)
    return 0


def cmd_cat_file(args) -> int:
    git_dir = repository.find_git_dir()
    obj_type, content = objects.read_object(git_dir, args.object)

    if args.type:
        print(obj_type)
    elif args.size:
        print(len(content))
    elif obj_type == "tree":
        # Trees hold raw binary ids, so dumping them verbatim is unreadable.
        # git pretty prints them and so do we.
        print(trees.pretty(trees.parse(content)))
    else:
        sys.stdout.buffer.write(content)
    return 0


def cmd_add(args) -> int:
    git_dir = repository.find_git_dir()
    narrator = porcelain.Narrator(args.explain)
    staged = porcelain.add(git_dir, args.paths, narrator)
    if args.explain:
        print(narrator.dump())
    else:
        print(f"staged {len(staged)} file(s)")
    return 0


def cmd_commit(args) -> int:
    git_dir = repository.find_git_dir()
    narrator = porcelain.Narrator(args.explain)
    oid = porcelain.commit(git_dir, args.message, narrator)
    if args.explain:
        print(narrator.dump())
    print(f"[{refs.current_branch(git_dir)} {oid[:7]}] {args.message.splitlines()[0]}")
    return 0


def cmd_log(args) -> int:
    for oid, commit in porcelain.log(repository.find_git_dir()):
        print(f"commit {oid}")
        if len(commit.parents) > 1:
            print(f"Merge: {' '.join(p[:7] for p in commit.parents)}")
        print(f"Author: {commit.author}")
        print()
        for line in commit.message.rstrip().splitlines():
            print(f"    {line}")
        print()
    return 0


def cmd_status(args) -> int:
    st = porcelain.status(repository.find_git_dir())
    print(f"On branch {st.branch}")

    if st.clean:
        print("nothing to commit, working tree clean")
        return 0

    if st.staged:
        print("\nChanges to be committed:")
        for path in st.staged:
            print(f"        new/modified:   {path}")
    if st.modified:
        print("\nChanges not staged for commit:")
        for path in st.modified:
            print(f"        modified:   {path}")
    if st.deleted:
        print("\nDeleted:")
        for path in st.deleted:
            print(f"        deleted:    {path}")
    if st.untracked:
        print("\nUntracked files:")
        for path in st.untracked:
            print(f"        {path}")
    return 0


def cmd_branch(args) -> int:
    git_dir = repository.find_git_dir()
    if args.name is None:
        current = refs.current_branch(git_dir)
        for name in sorted(refs.list_branches(git_dir)):
            print(f"{'*' if name == current else ' '} {name}")
        return 0
    porcelain.branch(git_dir, args.name)
    print(f"created branch {args.name}")
    return 0


def cmd_checkout(args) -> int:
    git_dir = repository.find_git_dir()
    porcelain.checkout(git_dir, args.name, force=args.force)
    print(f"switched to {args.name}")
    return 0


def cmd_diff(args) -> int:
    git_dir = repository.find_git_dir()
    if args.commits:
        a, b = (args.commits + ["HEAD"])[:2] if len(args.commits) == 1 else args.commits[:2]
        out = porcelain.diff_commits(git_dir, a, b, semantic_mode=args.semantic)
    else:
        out = porcelain.diff_working(git_dir)
    sys.stdout.write(out or "")
    return 0


def cmd_merge(args) -> int:
    """Merge, with the semantic gate on by default.

    This is the command that does something git cannot. Git merges the same
    branches cleanly and commits code that does not run.
    """
    git_dir = repository.find_git_dir()
    result = porcelain.merge_branch(git_dir, args.branch, allow_semantic_break=args.force_text)

    if result.semantic_conflicts and not result.ok:
        print("semantic conflict: the merge is textually clean but semantically broken\n")
        for path, problems in result.semantic_conflicts:
            print(f"  {path}")
            for problem in problems:
                print(problem.render())
            print()
        print("merge aborted, nothing was written.")
        print("real git would have committed this. use --force-text to do the same.")
        return 1

    if result.text_conflicts:
        print("conflicts in:")
        for path in result.text_conflicts:
            print(f"  {path}")
        print("\nfix the markers, then add and commit.")
        return 1

    if result.fast_forward:
        print(f"fast forward to {result.commit_oid[:7]}")
    else:
        print(f"merged as {result.commit_oid[:7]}")

    if result.semantic_conflicts:
        print("\nwarning: merged with known semantic breakage (--force-text)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minigit", description="A git implementation.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create an empty repository")
    p.add_argument("directory", nargs="?", default=".")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("hash-object", help="compute an object id, optionally storing it")
    p.add_argument("path", nargs="?")
    p.add_argument("-t", "--type", default="blob", choices=objects.VALID_TYPES)
    p.add_argument("-w", "--write", action="store_true")
    p.set_defaults(func=cmd_hash_object)

    p = sub.add_parser("cat-file", help="inspect a stored object")
    p.add_argument("object")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-p", "--pretty", action="store_true")
    g.add_argument("-t", "--type", action="store_true")
    g.add_argument("-s", "--size", action="store_true")
    p.set_defaults(func=cmd_cat_file)

    p = sub.add_parser("add", help="stage files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--explain", action="store_true", help="narrate what happens to the store")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("commit", help="record the staged snapshot")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--explain", action="store_true", help="narrate what happens to the store")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("log", help="show history")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("status", help="show working tree state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("branch", help="list or create branches")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("checkout", help="switch branches")
    p.add_argument("name")
    p.add_argument("-f", "--force", action="store_true")
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("diff", help="show changes")
    p.add_argument("commits", nargs="*", help="two revisions, or none for working tree")
    p.add_argument("--semantic", action="store_true", help="structural diff instead of line diff")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("merge", help="merge another branch, with semantic checking")
    p.add_argument("branch")
    p.add_argument("--force-text", action="store_true",
                   help="merge even if semantically broken, the way git would")
    p.set_defaults(func=cmd_merge)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (objects.ObjectStoreError, repository.RepositoryError) as exc:
        print(f"minigit: {exc}", file=sys.stderr)
        return 1
