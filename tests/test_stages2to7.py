"""Cross checks for trees, commits, the index, diff, merge and semantic merge.

Wherever real git can verify a claim, it does. The tests that git cannot check
(semantic merge, since git has no such concept) instead assert the contrast:
git merges cleanly and produces code that does not run, and we refuse.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minigit import (  # noqa: E402
    commits, diff, index as index_mod, merge, objects, porcelain, refs, repository,
    semantic, trees,
)

GIT = shutil.which("git")

FIXED_ENV = {
    "GIT_AUTHOR_NAME": "minigit", "GIT_AUTHOR_EMAIL": "minigit@localhost",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_NAME": "minigit", "GIT_COMMITTER_EMAIL": "minigit@localhost",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **FIXED_ENV}
    result = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, env=env, check=True)
    return result.stdout.decode(errors="replace")


class Sandbox:
    """A minigit repository with the environment pinned for reproducible ids."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in FIXED_ENV}
        os.environ.update(FIXED_ENV)
        self.git_dir = repository.init(self.root)
        git(self.root, "config", "core.autocrlf", "false")
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def write(self, path: str, text: str) -> None:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str) -> str:
        porcelain.add(self.git_dir, ["."])
        return porcelain.commit(self.git_dir, message)


# ------------------------------------------------------------ stage 2

def test_tree_ids_match_git():
    """Nested trees hash identically to git's, including the sort rule."""
    with Sandbox() as box:
        # "src.txt" beside a directory "src" is the case that breaks a naive
        # sort: git compares the directory as though it were named "src/".
        box.write("README.md", "# Hello\n")
        box.write("src.txt", "not a directory\n")
        box.write("src/main.py", "print(1)\n")
        box.write("src/lib/util.py", "x = 1\n")
        box.commit("initial")

        ours = commits.read(box.git_dir, refs.resolve_head(box.git_dir)).tree
        theirs = git(box.root, "rev-parse", "HEAD^{tree}").strip()
        assert ours == theirs, f"root tree: ours={ours} git={theirs}"


def test_git_log_reads_our_commits():
    with Sandbox() as box:
        box.write("a.txt", "one\n")
        box.commit("first")
        box.write("a.txt", "two\n")
        box.commit("second")

        log = git(box.root, "log", "--format=%s")
        assert log.split() == ["second", "first"]
        git(box.root, "fsck", "--strict")


# ------------------------------------------------------------ stage 3

def test_our_index_is_valid_to_git():
    """git status against our index reports a clean tree, so git accepts it."""
    with Sandbox() as box:
        box.write("a.txt", "hello\n")
        box.write("src/main.py", "print(1)\n")
        box.commit("initial")

        assert git(box.root, "status", "--short").strip() == ""


def test_index_roundtrip():
    with Sandbox() as box:
        box.write("a.txt", "hello\n")
        porcelain.add(box.git_dir, ["."])

        reloaded = index_mod.Index(box.git_dir)
        assert "a.txt" in reloaded.entries
        assert reloaded.entries["a.txt"].oid == objects.hash_object("blob", b"hello\n")


def test_status_sections():
    with Sandbox() as box:
        box.write("tracked.txt", "v1\n")
        box.commit("initial")

        box.write("tracked.txt", "v2\n")
        box.write("brand_new.txt", "hi\n")

        st = porcelain.status(box.git_dir)
        assert st.modified == ["tracked.txt"]
        assert st.untracked == ["brand_new.txt"]


# ------------------------------------------------------------ stage 4

def test_branch_and_checkout():
    with Sandbox() as box:
        box.write("a.txt", "main version\n")
        box.commit("on main")

        porcelain.branch(box.git_dir, "feature")
        porcelain.checkout(box.git_dir, "feature")
        box.write("a.txt", "feature version\n")
        box.commit("on feature")

        porcelain.checkout(box.git_dir, "main")
        assert (box.root / "a.txt").read_text() == "main version\n"

        porcelain.checkout(box.git_dir, "feature")
        assert (box.root / "a.txt").read_text() == "feature version\n"


def test_branch_is_just_a_file():
    with Sandbox() as box:
        box.write("a.txt", "x\n")
        head = box.commit("initial")
        porcelain.branch(box.git_dir, "copy")

        ref_file = box.git_dir / "refs" / "heads" / "copy"
        assert ref_file.read_text().strip() == head
        assert len(ref_file.read_bytes()) == 41  # 40 hex characters plus newline


# ------------------------------------------------------------ stage 5

def test_myers_matches_difflib_edit_count():
    """Our edit script is as short as Python's own differ finds."""
    import difflib

    cases = [
        (["a", "b", "c"], ["a", "x", "c"]),
        (["a", "b", "c", "d"], []),
        ([], ["x", "y"]),
        (list("abcabba"), list("cbabac")),
        (["same"] * 5, ["same"] * 5),
    ]

    for a, b in cases:
        ours = sum(1 for e in diff.diff_lines(a, b) if e.op is not diff.Op.EQUAL)
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        theirs = sum(
            (i2 - i1) + (j2 - j1)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
        )
        assert ours <= theirs, f"{a} -> {b}: ours={ours} difflib={theirs}"


def test_diff_reconstructs_target():
    """Applying our edit script to the old text yields exactly the new text."""
    a = "one\ntwo\nthree\nfour\n".splitlines()
    b = "one\nTWO\nthree\nfour\nfive\n".splitlines()

    rebuilt = [e.text for e in diff.diff_lines(a, b) if e.op is not diff.Op.DELETE]
    assert rebuilt == b


# ------------------------------------------------------------ stage 6

def test_merge_base_matches_git():
    with Sandbox() as box:
        box.write("a.txt", "base\n")
        base = box.commit("base")

        porcelain.branch(box.git_dir, "feature")
        box.write("a.txt", "main change\n")
        box.commit("main work")

        porcelain.checkout(box.git_dir, "feature")
        box.write("b.txt", "feature file\n")
        box.commit("feature work")

        ours = merge.merge_base(
            box.git_dir,
            refs.resolve(box.git_dir, "main"),
            refs.resolve(box.git_dir, "feature"),
        )
        theirs = git(box.root, "merge-base", "main", "feature").strip()
        assert ours == base == theirs


def test_merge_combines_disjoint_changes():
    with Sandbox() as box:
        box.write("shared.txt", "line1\nline2\nline3\n")
        box.commit("base")

        porcelain.branch(box.git_dir, "feature")
        box.write("shared.txt", "CHANGED\nline2\nline3\n")
        box.commit("edit top")

        porcelain.checkout(box.git_dir, "feature")
        box.write("shared.txt", "line1\nline2\nALTERED\n")
        box.commit("edit bottom")

        porcelain.checkout(box.git_dir, "main")
        result = porcelain.merge_branch(box.git_dir, "feature")

        assert result.ok, result.text_conflicts
        assert (box.root / "shared.txt").read_text() == "CHANGED\nline2\nALTERED\n"


def test_conflicting_edits_produce_markers():
    with Sandbox() as box:
        box.write("f.txt", "original\n")
        box.commit("base")

        porcelain.branch(box.git_dir, "feature")
        box.write("f.txt", "ours wins\n")
        box.commit("ours")

        porcelain.checkout(box.git_dir, "feature")
        box.write("f.txt", "theirs wins\n")
        box.commit("theirs")

        porcelain.checkout(box.git_dir, "main")
        result = porcelain.merge_branch(box.git_dir, "feature")

        assert not result.ok
        assert "f.txt" in result.text_conflicts
        assert "<<<<<<<" in (box.root / "f.txt").read_text()


# ------------------------------------------------------------ stage 7

# The two branches must edit lines far enough apart that git's textual merge
# sees no conflict at all. That separation is the entire point of the test: it
# is precisely when git is most confident that it is most wrong.
_FILLER = "\n".join(f"CONSTANT_{c} = {i}" for i, c in enumerate("ABCDEFGH"))

BASE_SIDE = (
    "def get_user(uid):\n    return uid\n\n\n"
    f"{_FILLER}\n\n\n"
    "def main():\n    a = get_user(1)\n    return a\n"
)

# Branch one renames the definition and updates its own call site.
RENAME_SIDE = (
    "def fetch_user(uid):\n    return uid\n\n\n"
    f"{_FILLER}\n\n\n"
    "def main():\n    a = fetch_user(1)\n    return a\n"
)

# Branch two adds a new function in the middle, calling the original name.
CALLER_SIDE = (
    "def get_user(uid):\n    return uid\n\n\n"
    "CONSTANT_A = 0\nCONSTANT_B = 1\nCONSTANT_C = 2\n\n\n"
    "def report():\n    return get_user(2)\n\n\n"
    "CONSTANT_D = 3\nCONSTANT_E = 4\nCONSTANT_F = 5\nCONSTANT_G = 6\nCONSTANT_H = 7\n\n\n"
    "def main():\n    a = get_user(1)\n    return a\n"
)


def _semantic_scenario(box: Sandbox):
    box.write("app.py", BASE_SIDE)
    box.commit("base")

    porcelain.branch(box.git_dir, "caller")
    box.write("app.py", RENAME_SIDE)
    box.commit("rename get_user to fetch_user")

    porcelain.checkout(box.git_dir, "caller")
    box.write("app.py", CALLER_SIDE)
    box.commit("add a second call")

    porcelain.checkout(box.git_dir, "main")


def test_semantic_conflict_is_caught():
    """The headline feature: we refuse a merge git would accept."""
    with Sandbox() as box:
        _semantic_scenario(box)
        result = porcelain.merge_branch(box.git_dir, "caller")

        assert not result.ok
        assert result.semantic_conflicts

        _path, problems = result.semantic_conflicts[0]
        assert problems[0].name == "get_user"
        assert problems[0].suggestion == "fetch_user"


def test_git_accepts_the_same_merge_and_the_result_crashes():
    """The contrast that makes the feature worth having.

    Real git merges these branches with exit code 0, and the file it produces
    raises NameError. This is not a hypothetical; it runs here.
    """
    with Sandbox() as box:
        _semantic_scenario(box)

        git(box.root, "merge", "caller", "-m", "merge")          # succeeds
        merged = (box.root / "app.py").read_text()

        # The definition was renamed on one branch, and the other branch's new
        # function still calls the old name. Both survived the merge.
        assert "def fetch_user" in merged and "get_user(2)" in merged

        # Calling the function that branch two added is what trips it. Note
        # that main() still works, which is exactly why this class of bug
        # reaches production: the merge looks fine and most of the code runs.
        broken = subprocess.run(
            [sys.executable, "-c", "import app; app.report()"],
            cwd=box.root, capture_output=True,
        )
        assert broken.returncode != 0
        assert b"NameError" in broken.stderr
        assert b"get_user" in broken.stderr


def test_force_text_merges_anyway():
    with Sandbox() as box:
        _semantic_scenario(box)
        result = porcelain.merge_branch(box.git_dir, "caller", allow_semantic_break=True)
        assert result.ok and result.semantic_conflicts


def test_clean_merge_is_not_falsely_flagged():
    """No false positive when the two sides genuinely do not interfere."""
    with Sandbox() as box:
        box.write("app.py", "def a():\n    return 1\n")
        box.commit("base")

        porcelain.branch(box.git_dir, "feature")
        box.write("app.py", "def a():\n    return 1\n\ndef b():\n    return a() + 1\n")
        box.commit("add b")

        porcelain.checkout(box.git_dir, "feature")
        box.write("app.py", "import os\n\ndef a():\n    return 1\n")
        box.commit("add import")

        porcelain.checkout(box.git_dir, "main")
        result = porcelain.merge_branch(box.git_dir, "feature")
        assert result.ok, result.semantic_conflicts


def test_structural_diff_reports_rename_not_rewrite():
    old = "def process_data(x):\n    return x * 2\n"
    new = "def transform_data(x):\n    return x * 2\n"

    report = "\n".join(semantic.structural_diff(old, new))
    assert "renamed" in report
    assert "process_data -> transform_data" in report


def test_add_stages_deletions_so_a_rename_is_a_rename():
    """`add .` must drop paths that no longer exist on disk.

    Without this the old name stays in the index forever, so the commit after a
    rename records BOTH names and git reports an addition rather than a rename.
    Found by hand; the fuzzer never generated a delete.
    """
    with Sandbox() as box:
        box.write("file.txt", "version one contents\n")
        box.commit("add file.txt")

        (box.root / "file.txt").rename(box.root / "file-v1.txt")
        box.commit("rename to file-v1.txt")

        listing = git(box.root, "ls-tree", "-r", "--name-only", "HEAD").split()
        assert listing == ["file-v1.txt"], listing

        # Real git should now see a 100% similarity rename, not an add.
        status = git(box.root, "log", "-1", "--name-status", "-M", "--format=")
        assert "R100" in status, status


def test_rollback_after_rename_restores_the_old_name():
    """The point of a snapshot model: an old commit still has the old path."""
    with Sandbox() as box:
        box.write("file.txt", "version one contents\n")
        first = box.commit("add file.txt")

        (box.root / "file.txt").rename(box.root / "file-v1.txt")
        box.commit("rename to file-v1.txt")

        porcelain.checkout(box.git_dir, first)

        assert (box.root / "file.txt").exists()
        assert not (box.root / "file-v1.txt").exists()
        assert (box.root / "file.txt").read_text() == "version one contents\n"


def test_rename_stores_no_new_blob():
    """A rename changes a tree, never a blob. Content addressing gives this."""
    with Sandbox() as box:
        box.write("file.txt", "version one contents\n")
        box.commit("add file.txt")
        before = {p for p in (box.git_dir / "objects").rglob("*") if p.is_file()}

        (box.root / "file.txt").rename(box.root / "file-v1.txt")
        box.commit("rename to file-v1.txt")
        after = {p for p in (box.git_dir / "objects").rglob("*") if p.is_file()}

        # New tree and new commit, but no new blob.
        added = after - before
        kinds = [objects.read_object(box.git_dir, p.parent.name + p.name)[0] for p in added]
        assert "blob" not in kinds, kinds


def test_analyzer_ignores_locals_and_builtins():
    """Guards against the obvious false positive: locals are not free names."""
    source = "def f(a, b):\n    total = a + b\n    return len(str(total))\n"
    analysis = semantic.analyze(source)
    assert analysis.referenced_names == set()


if __name__ == "__main__":
    if GIT is None:
        sys.exit("git is not on PATH, cannot cross check")

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
