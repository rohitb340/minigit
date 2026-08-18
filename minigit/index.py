"""The index, also called the staging area.

The third place files exist, alongside the working directory and object store.
It decides what goes into the next commit, and caches each file's size and
mtime so status can skip reading files whose stat data still matches.

Implements git's real binary format (version 2), so real git can run
`git status` against a repository this staged. All integers big endian:

    header    "DIRC" | version (4) | entry count (4)
    entries   ctime_s ctime_ns mtime_s mtime_ns dev ino mode uid gid size
              (4 bytes each) | oid (20 raw bytes) | flags (2)
              | path bytes | null padding to a multiple of 8
    trailer   sha1 of everything above (20 bytes)
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from .trees import MODE_EXEC, MODE_FILE

SIGNATURE = b"DIRC"
VERSION = 2


@dataclass
class IndexEntry:
    path: str
    oid: str
    mode: int
    size: int = 0
    ctime_s: int = 0
    ctime_ns: int = 0
    mtime_s: int = 0
    mtime_ns: int = 0
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0

    @property
    def mode_text(self) -> str:
        """The mode as trees record it, which is only ever file or executable."""
        return MODE_EXEC if self.mode & 0o111 else MODE_FILE


def entry_from_file(path: str, full: Path, oid: str) -> IndexEntry:
    """Capture a staged file's identity plus the stat data used for caching."""
    st = full.stat()
    executable = bool(st.st_mode & 0o111)

    return IndexEntry(
        path=path,
        oid=oid,
        # Git only records the executable bit, so we normalise here rather than
        # storing the platform's real permission bits. On Windows the execute
        # bit is meaningless anyway, which would otherwise make our index
        # disagree with git's for the same file.
        mode=0o100755 if executable else 0o100644,
        size=st.st_size & 0xFFFFFFFF,
        ctime_s=int(st.st_ctime) & 0xFFFFFFFF,
        ctime_ns=getattr(st, "st_ctime_ns", 0) % 1_000_000_000,
        mtime_s=int(st.st_mtime) & 0xFFFFFFFF,
        mtime_ns=getattr(st, "st_mtime_ns", 0) % 1_000_000_000,
        dev=st.st_dev & 0xFFFFFFFF,
        ino=st.st_ino & 0xFFFFFFFF,
        uid=getattr(st, "st_uid", 0) & 0xFFFFFFFF,
        gid=getattr(st, "st_gid", 0) & 0xFFFFFFFF,
    )


def serialize(entries: list[IndexEntry]) -> bytes:
    """Encode the index, including its trailing checksum."""
    ordered = sorted(entries, key=lambda e: e.path)

    out = bytearray()
    out += SIGNATURE + struct.pack(">II", VERSION, len(ordered))

    for e in ordered:
        raw_path = e.path.encode()

        out += struct.pack(
            ">IIIIIIIIII",
            e.ctime_s, e.ctime_ns, e.mtime_s, e.mtime_ns,
            e.dev, e.ino, e.mode, e.uid, e.gid, e.size,
        )
        out += bytes.fromhex(e.oid)

        # The low 12 bits hold the path length. Longer paths clamp to 0xFFF,
        # and readers then scan to the null terminator instead.
        out += struct.pack(">H", min(len(raw_path), 0xFFF))
        out += raw_path

        # Pad with nulls so each entry starts on an 8 byte boundary. The 62
        # fixed bytes plus the path must round up to a multiple of 8, and there
        # is always at least one null so the path is terminated.
        padding = 8 - ((62 + len(raw_path)) % 8)
        out += b"\x00" * padding

    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def parse(data: bytes) -> list[IndexEntry]:
    """Decode an index file, verifying its checksum first."""
    if data[:4] != SIGNATURE:
        raise ValueError("not an index file")

    body, checksum = data[:-20], data[-20:]
    if hashlib.sha1(body).digest() != checksum:
        raise ValueError("index checksum mismatch, file is corrupt")

    version, count = struct.unpack(">II", data[4:12])
    if version != VERSION:
        raise ValueError(f"unsupported index version {version}")

    entries: list[IndexEntry] = []
    pos = 12

    for _ in range(count):
        fields = struct.unpack(">IIIIIIIIII", data[pos:pos + 40])
        oid = data[pos + 40:pos + 60].hex()
        (flags,) = struct.unpack(">H", data[pos + 60:pos + 62])

        name_len = flags & 0xFFF
        start = pos + 62
        if name_len < 0xFFF:
            path = data[start:start + name_len].decode()
        else:
            end = data.index(b"\x00", start)
            path = data[start:end].decode()

        entries.append(IndexEntry(
            path=path, oid=oid,
            ctime_s=fields[0], ctime_ns=fields[1],
            mtime_s=fields[2], mtime_ns=fields[3],
            dev=fields[4], ino=fields[5], mode=fields[6],
            uid=fields[7], gid=fields[8], size=fields[9],
        ))

        pos = start + len(path.encode())
        pos += 8 - ((62 + len(path.encode())) % 8)

    return entries


class Index:
    """Read/modify/write wrapper over the index file."""

    def __init__(self, git_dir: Path):
        self.path = git_dir / "index"
        self.entries: dict[str, IndexEntry] = {}
        # The index file's own modification time is what makes the stat cache
        # safe. See looks_unchanged() for why.
        self.written_at = int(self.path.stat().st_mtime) if self.path.exists() else 0
        if self.path.exists():
            self.entries = {e.path: e for e in parse(self.path.read_bytes())}

    def add(self, entry: IndexEntry) -> None:
        self.entries[entry.path] = entry

    def remove(self, path: str) -> None:
        self.entries.pop(path, None)

    def write(self) -> None:
        self.path.write_bytes(serialize(list(self.entries.values())))

    def staged_paths(self) -> dict[str, tuple[str, str]]:
        """The {path: (mode, oid)} mapping that tree building consumes."""
        return {e.path: (e.mode_text, e.oid) for e in self.entries.values()}

    def looks_unchanged(self, entry: IndexEntry, full: Path) -> bool:
        """Fast path for status: can we skip reading this file entirely?

        If size and mtime both match what we cached, the contents are almost
        certainly unchanged and we never open the file. That cache is the only
        reason `git status` is fast on a large repository.

        Almost certainly is not certainly, and the gap has a name: the racy
        index problem. Filesystem timestamps have one second granularity in
        the index, so a file written, staged, and edited again all within the
        same second keeps its recorded mtime. If the edit also happens to leave
        the size unchanged, "v1" becoming "v2" for instance, the cache would
        report the file as clean and status would silently miss a real edit.

        Git's fix, which this mirrors, is to treat any entry whose mtime is not
        strictly older than the index file itself as suspect and fall back to
        hashing the contents. Entries older than the index are safe, so the
        fast path still applies to almost everything.
        """
        try:
            st = full.stat()
        except OSError:
            return False

        if entry.mtime_s >= self.written_at:
            return False  # racily clean, the caller must compare contents

        return (
            st.st_size & 0xFFFFFFFF == entry.size
            and int(st.st_mtime) & 0xFFFFFFFF == entry.mtime_s
        )
