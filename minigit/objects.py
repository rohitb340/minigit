"""The object store: content addressed storage for blobs, trees and commits.

This is the foundation of the whole system. Everything git does sits on top of
these three operations: turn bytes into an id, write bytes under that id, read
bytes back by id.

The byte formats here must match real git exactly. If they do not, every id we
produce will disagree with git's and nothing downstream can be cross checked.
"""

import hashlib
import zlib
from pathlib import Path

# The only object types git stores. "tag" also exists in real git but we do not
# implement annotated tags, so it is deliberately absent.
VALID_TYPES = ("blob", "tree", "commit")


class ObjectStoreError(Exception):
    """Raised when an object is missing, malformed or of an unexpected type."""


def serialize(obj_type: str, content: bytes) -> bytes:
    """Wrap raw content in git's object header.

    The on disk format for every object is:

        <type> <size-in-bytes>\\0<content>

    So a blob holding b"Hello world\\n" (12 bytes) becomes:

        b"blob 12\\x00Hello world\\n"

    The header is part of what gets hashed, which is why a blob's id is not
    simply the sha1 of the file contents. This trips up nearly everyone who
    tries to reproduce git ids by hand.
    """
    if obj_type not in VALID_TYPES:
        raise ObjectStoreError(f"unknown object type: {obj_type!r}")
    header = f"{obj_type} {len(content)}".encode()
    return header + b"\x00" + content


def hash_object(obj_type: str, content: bytes) -> str:
    """Return the 40 character hex id for an object, without storing it.

    This is the pure function at the heart of content addressing: the same
    bytes always produce the same id, so the id can serve as the name.
    """
    return hashlib.sha1(serialize(obj_type, content)).hexdigest()


def object_path(git_dir: Path, oid: str) -> Path:
    """Map an object id to its location on disk.

    Git splits the id so the first two hex characters become a directory name
    and the remaining 38 become the filename:

        4d8e0b7c1f...  ->  .git/objects/4d/8e0b7c1f...

    The split exists purely to avoid putting a million files in one directory,
    which some filesystems handle badly. It carries no other meaning.
    """
    if len(oid) != 40:
        raise ObjectStoreError(f"malformed object id: {oid!r}")
    return git_dir / "objects" / oid[:2] / oid[2:]


def exists(git_dir: Path, oid: str) -> bool:
    """Is this object already stored? Used by --explain to say new vs reused."""
    return object_path(git_dir, oid).exists()


def write_object(git_dir: Path, obj_type: str, content: bytes) -> str:
    """Store an object and return its id.

    Writing is idempotent. If an object with this id already exists then its
    contents are, by definition, identical to what we were about to write, so
    we skip the write entirely. That single check is the whole of git's
    deduplication: two files with the same contents anywhere in the project
    are stored exactly once, and we never wrote any deduplication logic.
    """
    data = serialize(obj_type, content)
    oid = hashlib.sha1(data).hexdigest()
    path = object_path(git_dir, oid)

    if path.exists():
        return oid

    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary name first, then rename into place. Rename is atomic
    # on the same filesystem, so a crash midway cannot leave a half written
    # object that would later fail its own checksum.
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(zlib.compress(data))
    tmp.replace(path)
    return oid


def read_object(git_dir: Path, oid: str) -> tuple[str, bytes]:
    """Read an object by id, returning (type, content).

    This is the exact inverse of write_object: decompress, split the header off
    at the null byte, and verify the recorded size matches what we actually
    have. That size check is a cheap integrity guard against truncation.
    """
    path = object_path(git_dir, oid)
    if not path.exists():
        raise ObjectStoreError(f"object not found: {oid}")

    raw = zlib.decompress(path.read_bytes())

    null_at = raw.find(b"\x00")
    if null_at == -1:
        raise ObjectStoreError(f"object {oid} has no header terminator")

    header = raw[:null_at].decode()
    try:
        obj_type, size_text = header.split(" ", 1)
        declared_size = int(size_text)
    except ValueError as exc:
        raise ObjectStoreError(f"object {oid} has a malformed header: {header!r}") from exc

    content = raw[null_at + 1:]
    if len(content) != declared_size:
        raise ObjectStoreError(
            f"object {oid} is corrupt: header says {declared_size} bytes, found {len(content)}"
        )

    return obj_type, content


def read_object_of_type(git_dir: Path, oid: str, expected: str) -> bytes:
    """Read an object and assert its type, returning just the content.

    Callers almost always know what they expect. Checking here means a wrong
    id surfaces as a clear error at the point of use rather than as confusing
    output several layers later.
    """
    obj_type, content = read_object(git_dir, oid)
    if obj_type != expected:
        raise ObjectStoreError(f"expected {oid} to be a {expected}, found a {obj_type}")
    return content
