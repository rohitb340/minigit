"""Trees: the objects that give filenames to blobs.

A blob is anonymous content. A tree is a directory listing that binds names to
content ids. Trees can point at blobs (files) or at other trees (subdirectories),
so a single root tree describes an entire project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import objects

# Git only records whether a file is executable, not the full unix permission
# bits. Everything regular is 100644 and everything executable is 100755.
MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_SYMLINK = "120000"
MODE_TREE = "40000"


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    oid: str

    @property
    def is_tree(self) -> bool:
        return self.mode == MODE_TREE


def sort_key(entry: TreeEntry) -> bytes:
    """Produce the byte string git sorts tree entries by.

    Git sorts entries by name, but with one subtlety that is easy to miss and
    silently produces the wrong tree id: a directory is compared as though its
    name ends with a slash.

    That matters whenever a file and a directory share a prefix. Comparing
    "foo.txt" against a directory "foo" means comparing "foo.txt" to "foo/",
    and since "." (0x2E) sorts before "/" (0x2F) the file comes first. Sorting
    the bare names instead would put them the other way round, and every tree
    containing such a pair would then disagree with real git.
    """
    suffix = b"/" if entry.is_tree else b""
    return entry.name.encode() + suffix


def serialize(entries: list[TreeEntry]) -> bytes:
    """Encode tree entries into git's on disk format.

    Each entry is packed with no separator between entries:

        <mode> <name>\\0<20 byte raw binary oid>

    Note the two different encodings in one record. The mode and name are
    ASCII text, but the oid is raw binary, 20 bytes rather than the 40
    character hex string used everywhere else.
    """
    out = bytearray()
    for entry in sorted(entries, key=sort_key):
        out += f"{entry.mode} {entry.name}".encode()
        out += b"\x00"
        out += bytes.fromhex(entry.oid)
    return bytes(out)


def parse(data: bytes) -> list[TreeEntry]:
    """Decode a tree object back into entries."""
    entries: list[TreeEntry] = []
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        null = data.index(b"\x00", space)

        mode = data[pos:space].decode()
        name = data[space + 1:null].decode()
        oid = data[null + 1:null + 21].hex()

        entries.append(TreeEntry(mode, name, oid))
        pos = null + 21
    return entries


def write_tree_from_paths(git_dir: Path, staged: dict[str, tuple[str, str]]) -> str:
    """Build a full tree hierarchy from a flat path list, returning the root oid.

    The index is flat. It holds entries like:

        "README.md"     -> (mode, oid)
        "src/main.py"   -> (mode, oid)
        "src/lib/a.py"  -> (mode, oid)

    But trees are nested, and a parent tree cannot be written until its
    children exist, because the parent stores its children's ids. So we build
    depth first: assemble the deepest directories, write them, take the ids
    that come back, and use those to build the level above.

    This is the fiddliest part of a commit, and it is where the sort rule in
    sort_key() actually bites.
    """
    # Group the flat paths into a nested dictionary mirroring the directory
    # structure, so that recursion has something to walk.
    root: dict = {}
    for path, (mode, oid) in staged.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, oid)

    def build(node: dict) -> str:
        entries: list[TreeEntry] = []
        for name, value in node.items():
            if isinstance(value, dict):
                # A subdirectory. Write it first so we have an id to point at.
                entries.append(TreeEntry(MODE_TREE, name, build(value)))
            else:
                mode, oid = value
                entries.append(TreeEntry(mode, name, oid))
        return objects.write_object(git_dir, "tree", serialize(entries))

    return build(root)


def flatten(git_dir: Path, tree_oid: str, prefix: str = "") -> dict[str, tuple[str, str]]:
    """Walk a tree recursively and return a flat {path: (mode, oid)} mapping.

    The inverse of write_tree_from_paths. Used by checkout, status and diff,
    all of which want to compare two snapshots path by path rather than
    navigate the nesting by hand.
    """
    result: dict[str, tuple[str, str]] = {}
    data = objects.read_object_of_type(git_dir, tree_oid, "tree")

    for entry in parse(data):
        path = f"{prefix}{entry.name}"
        if entry.is_tree:
            result.update(flatten(git_dir, entry.oid, prefix=f"{path}/"))
        else:
            result[path] = (entry.mode, entry.oid)
    return result


def pretty(entries: list[TreeEntry]) -> str:
    """Format a tree the way `git cat-file -p` does."""
    lines = []
    for entry in sorted(entries, key=sort_key):
        kind = "tree" if entry.is_tree else "blob"
        # git pads the mode to six characters, so 40000 prints as 040000.
        lines.append(f"{entry.mode:0>6} {kind} {entry.oid}\t{entry.name}")
    return "\n".join(lines)
