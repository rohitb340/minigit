"""Stage 1 cross checks against real git.

Every test here compares minigit against the actual git binary. That is the
whole point of the project: we are not asserting that our code agrees with
itself, we are asserting that it agrees with a twenty year old C implementation
that millions of people depend on.

Run with:   python -m pytest tests/ -v
Or plain:   python tests/test_stage1_objects.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minigit import objects, repository  # noqa: E402

GIT = shutil.which("git")


def run_git(cwd: Path, *args: str) -> bytes:
    """Run real git and return its stdout, raising if it fails."""
    result = subprocess.run(
        [GIT, *args], cwd=cwd, capture_output=True, check=True
    )
    return result.stdout


# The payloads below are chosen to hit the places byte level bugs actually
# hide: empty content, missing trailing newline, non ascii text, embedded null
# bytes, and content large enough to make zlib do real work.
PAYLOADS = {
    "simple": b"Hello world\n",
    "empty": b"",
    "no_trailing_newline": b"no newline at end",
    "unicode": "hello 世界 \U0001f600\n".encode("utf-8"),
    "binary_with_nulls": bytes(range(256)) * 4,
    "crlf": b"line one\r\nline two\r\n",
    "large": b"x" * 100_000,
}


def test_ids_match_real_git():
    """Our object ids are identical to git's for every payload.

    If this passes, our header format and hashing are exactly right. If it
    fails, nothing else in the project can possibly be correct.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        run_git(work, "init", "-q")

        for name, payload in PAYLOADS.items():
            target = work / name
            target.write_bytes(payload)

            theirs = run_git(work, "hash-object", "--no-filters", name).decode().strip()
            ours = objects.hash_object("blob", payload)

            assert ours == theirs, f"{name}: minigit={ours} git={theirs}"


def test_git_can_read_objects_we_wrote():
    """We write an object, then real git reads it back correctly.

    This proves our zlib compression and our objects/ path layout match git's.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        git_dir = repository.init(work)

        for name, payload in PAYLOADS.items():
            oid = objects.write_object(git_dir, "blob", payload)

            assert run_git(work, "cat-file", "-p", oid) == payload
            assert run_git(work, "cat-file", "-t", oid).decode().strip() == "blob"
            assert int(run_git(work, "cat-file", "-s", oid)) == len(payload)


def test_we_can_read_objects_git_wrote():
    """The inverse direction: git writes, we read."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        run_git(work, "init", "-q")
        git_dir = work / ".git"

        for name, payload in PAYLOADS.items():
            (work / name).write_bytes(payload)
            oid = run_git(work, "hash-object", "--no-filters", "-w", name).decode().strip()

            obj_type, content = objects.read_object(git_dir, oid)
            assert obj_type == "blob"
            assert content == payload


def test_repository_we_create_passes_git_fsck():
    """Real git's own integrity checker approves a repository we built."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        git_dir = repository.init(work)

        for payload in PAYLOADS.values():
            objects.write_object(git_dir, "blob", payload)

        # fsck exits non zero on corruption, so check=True is the assertion.
        run_git(work, "fsck", "--strict")


def test_writing_is_deduplicated():
    """Storing identical content twice produces one file on disk.

    This is git's deduplication, and note that we never wrote deduplication
    logic. It falls out of naming objects by their contents.
    """
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = repository.init(Path(tmp))

        first = objects.write_object(git_dir, "blob", b"identical\n")
        second = objects.write_object(git_dir, "blob", b"identical\n")

        assert first == second
        stored = list((git_dir / "objects").rglob("*"))
        assert len([p for p in stored if p.is_file()]) == 1


def test_corruption_is_detected():
    """Tampering with a stored object is caught by the size check on read."""
    import zlib

    with tempfile.TemporaryDirectory() as tmp:
        git_dir = repository.init(Path(tmp))
        oid = objects.write_object(git_dir, "blob", b"original content\n")

        path = objects.object_path(git_dir, oid)
        path.write_bytes(zlib.compress(b"blob 17\x00tampered!\n"))

        try:
            objects.read_object(git_dir, oid)
        except objects.ObjectStoreError as exc:
            assert "corrupt" in str(exc)
        else:
            raise AssertionError("corruption was not detected")


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
            print(f"  FAIL  {test.__name__}: {exc}")

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
