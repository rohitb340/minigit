"""Differential fuzzing against real git.

This is not a feature. It is rigor. A user of minigit gains nothing from the
fuzzer existing; what it buys is evidence that stages 1 through 7 are actually
correct rather than merely passing the tests we happened to think of.

The idea is standard practice for compilers, database engines and parsers, and
it is simple: generate a random sequence of operations, run the identical
sequence through both minigit and real git in two separate directories, then
compare the resulting object stores. Any divergence is a bug in ours, and the
generating sequence is a minimal reproduction handed to you for free.

Hand written tests only cover cases you already imagined. The fuzzer finds the
ones you did not: filenames that sort strangely next to directories, files with
no trailing newline, empty files, and content that happens to hash to something
already present.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

GIT = shutil.which("git")

# Both tools must agree on identity and timestamps or every commit id would
# differ for reasons that have nothing to do with correctness.
FIXED_ENV = {
    "GIT_AUTHOR_NAME": "minigit",
    "GIT_AUTHOR_EMAIL": "minigit@localhost",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_NAME": "minigit",
    "GIT_COMMITTER_EMAIL": "minigit@localhost",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}

# Names picked to stress the tree sort rule: "src" as a directory sits next to
# "src.txt" as a file, and git compares the directory as though it ended in a
# slash. Getting that wrong changes the root tree id.
FILE_NAMES = [
    "a.txt", "b.txt", "README.md", "src.txt", "src/main.py",
    "src/lib/util.py", "z", "dir/nested/deep.txt", "no-ext",
]

CONTENTS = [
    b"hello\n", b"", b"no trailing newline", b"line1\nline2\n",
    "unicode 世界\n".encode(), b"\x00\x01\x02binary\xff",
    b"x" * 5000, b"a\r\nb\r\n",
]


@dataclass
class Divergence:
    step: int
    kind: str
    detail: str


class HarnessError(RuntimeError):
    """A command failed to run at all, as opposed to producing wrong output."""


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one command, and refuse to let a crash masquerade as a divergence.

    Two things here were wrong and both hid the same way.

    The child runs with cwd set to a scratch directory, so `python -m minigit`
    could not import the package unless it happened to be installed or
    PYTHONPATH was already exported. The repository root is injected below so
    the child always finds it.

    And the exit code was never checked. A command that failed to start wrote
    no objects, the comparison then found git's objects missing from our store,
    and the harness reported a byte level divergence. It printed "0/3 sequences
    byte identical" for a tool that never executed, which is the most
    misleading thing a test harness can do.
    """
    import os

    package_root = str(Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    env = {
        **os.environ,
        **FIXED_ENV,
        "PYTHONPATH": package_root + (os.pathsep + existing if existing else ""),
    }

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env)
    if result.returncode != 0:
        raise HarnessError(
            f"{' '.join(cmd)} exited {result.returncode} in {cwd}\n"
            f"  stdout: {result.stdout.decode(errors='replace').strip()[:400]}\n"
            f"  stderr: {result.stderr.decode(errors='replace').strip()[:400]}"
        )
    return result


def read_store(git_dir: Path) -> dict[str, bytes]:
    """Every object in the store, as {id: decompressed content}.

    We compare decompressed bytes rather than the files themselves because
    zlib compression levels may differ between implementations. The content is
    what has to match; the exact compressed encoding does not.
    """
    store: dict[str, bytes] = {}
    objects_dir = git_dir / "objects"
    if not objects_dir.is_dir():
        return store

    for shard in objects_dir.iterdir():
        if not shard.is_dir() or len(shard.name) != 2:
            continue
        for blob in shard.iterdir():
            if blob.is_file() and not blob.name.endswith(".tmp"):
                store[shard.name + blob.name] = zlib.decompress(blob.read_bytes())
    return store


def read_refs(git_dir: Path) -> dict[str, str]:
    heads = git_dir / "refs" / "heads"
    if not heads.is_dir():
        return {}
    return {
        str(p.relative_to(heads)).replace("\\", "/"): p.read_text().strip()
        for p in heads.rglob("*") if p.is_file()
    }


def compare(step: int, ours: Path, theirs: Path) -> list[Divergence]:
    """Assert the two repositories are equivalent, reporting every difference."""
    found: list[Divergence] = []

    our_store, their_store = read_store(ours / ".git"), read_store(theirs / ".git")

    missing = set(their_store) - set(our_store)
    extra = set(our_store) - set(their_store)

    for oid in sorted(missing):
        preview = their_store[oid][:60]
        found.append(Divergence(step, "missing object", f"{oid[:12]} {preview!r}"))
    for oid in sorted(extra):
        preview = our_store[oid][:60]
        found.append(Divergence(step, "extra object", f"{oid[:12]} {preview!r}"))

    for oid in sorted(set(our_store) & set(their_store)):
        if our_store[oid] != their_store[oid]:
            # This should be impossible: matching ids with differing content
            # would mean a sha1 collision. Checking anyway costs nothing.
            found.append(Divergence(step, "content mismatch", oid))

    our_refs, their_refs = read_refs(ours / ".git"), read_refs(theirs / ".git")
    if our_refs != their_refs:
        found.append(Divergence(step, "refs differ", f"ours={our_refs} theirs={their_refs}"))

    return found


def generate(rng: random.Random, length: int) -> list[tuple[str, ...]]:
    """Build a random but always valid operation sequence.

    Sequences are constrained to remain meaningful: nothing commits before
    something is staged, and nothing checks out a branch that was never
    created. An invalid sequence would make both tools error identically,
    which tests nothing.
    """
    ops: list[tuple[str, ...]] = []
    branches = ["main"]
    live_files: list[str] = []
    committed = False
    staged = False
    dirty = False

    for _ in range(length):
        choices = ["write"]
        if live_files:
            choices += ["write", "add"]
        if staged:
            choices += ["commit", "commit"]
        if committed:
            choices += ["branch"]
        # Only check out from a clean tree. The second fuzzing run found that
        # checking out while dirty diverges, and it is a policy difference
        # rather than an object model bug: git leaves uncommitted work in place
        # when you check out the branch you are already on, while minigit
        # restores the committed tree. Deciding what to do with uncommitted
        # work is above the layer this project implements, so we exercise
        # checkout from a clean state and say so rather than quietly forcing
        # ours and calling the comparison fair.
        if committed and not dirty:
            choices += ["checkout"]

        op = rng.choice(choices)

        if op == "write":
            name = rng.choice(FILE_NAMES)
            ops.append(("write", name, str(rng.randrange(len(CONTENTS)))))
            if name not in live_files:
                live_files.append(name)
            staged, dirty = False, True
        elif op == "add":
            ops.append(("add",))
            staged = True
        elif op == "commit":
            ops.append(("commit", f"commit {len(ops)}"))
            committed, staged, dirty = True, False, False
        elif op == "branch":
            name = f"b{len(branches)}"
            branches.append(name)
            ops.append(("branch", name))
        elif op == "checkout":
            ops.append(("checkout", rng.choice(branches)))

    return ops


def apply(op: tuple[str, ...], work: Path, tool: str) -> None:
    """Perform one operation with either minigit or real git."""
    kind = op[0]

    if kind == "write":
        target = work / op[1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENTS[int(op[2])])
        return

    # Built with branches rather than a lookup table, because a dict would
    # evaluate op[1] for every entry and "add" carries no argument.
    if tool == "git":
        if kind == "add":
            args = ["add", "-A"]
        elif kind == "commit":
            args = ["commit", "-q", "-m", op[1], "--allow-empty"]
        elif kind == "branch":
            args = ["branch", op[1]]
        else:
            args = ["checkout", "-q", op[1]]
        _run([GIT, *args], work)
    else:
        if kind == "add":
            args = ["add", "."]
        elif kind == "commit":
            args = ["commit", "-m", op[1]]
        elif kind == "branch":
            args = ["branch", op[1]]
        else:
            args = ["checkout", op[1]]
        _run([sys.executable, "-m", "minigit", *args], work)


def run_case(seed: int, length: int = 12) -> tuple[list[Divergence], list[tuple[str, ...]]]:
    """Run one random sequence through both tools and compare."""
    rng = random.Random(seed)
    ops = generate(rng, length)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ours, theirs = root / "ours", root / "theirs"
        ours.mkdir()
        theirs.mkdir()

        _run([sys.executable, "-m", "minigit", "init", "."], ours)
        _run([GIT, "init", "-q", "-b", "main"], theirs)

        # The very first fuzzing run found a real divergence here: on Windows
        # git defaults core.autocrlf to true, so `git add` rewrites CRLF to LF
        # before hashing, and our raw bytes then hash differently.
        #
        # That is a genuine behavioural difference, and the honest response is
        # to scope it out rather than paper over it. Line ending translation is
        # a configuration layer sitting above the object model, not part of it,
        # and minigit deliberately does not implement it. Turning the filter off
        # compares like with like. It is also why the stage 1 tests pass
        # --no-filters to git hash-object.
        _run([GIT, "config", "core.autocrlf", "false"], theirs)
        _run([GIT, "config", "core.eol", "lf"], theirs)

        divergences: list[Divergence] = []
        for step, op in enumerate(ops):
            apply(op, ours, "minigit")
            apply(op, theirs, "git")

            problems = compare(step, ours, theirs)
            if problems:
                divergences.extend(problems)
                break

        return divergences, ops


def main(cases: int = 200, length: int = 12, start_seed: int = 0) -> int:
    if GIT is None:
        print("git is not on PATH, cannot cross check")
        return 2

    failures = 0
    for seed in range(start_seed, start_seed + cases):
        divergences, ops = run_case(seed, length)

        if divergences:
            failures += 1
            print(f"\nFAIL  seed={seed}")
            for d in divergences[:5]:
                print(f"      step {d.step}: {d.kind}: {d.detail}")
            print("      reproducing sequence:")
            for i, op in enumerate(ops[:divergences[0].step + 1]):
                print(f"        {i}: {' '.join(op)}")
            if failures >= 3:
                print("\nstopping after 3 failures")
                break
        elif (seed - start_seed + 1) % 25 == 0:
            print(f"  {seed - start_seed + 1}/{cases} sequences agree with git")

    total = min(cases, seed - start_seed + 1)
    print(f"\n{total - failures}/{total} sequences byte identical to real git")
    return 1 if failures else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Differential fuzz minigit against real git.")
    parser.add_argument("-n", "--cases", type=int, default=200)
    parser.add_argument("-l", "--length", type=int, default=12)
    parser.add_argument("-s", "--seed", type=int, default=0)
    args = parser.parse_args()

    sys.exit(main(args.cases, args.length, args.seed))
