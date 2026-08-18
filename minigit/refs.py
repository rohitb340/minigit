"""Refs: the sticky notes that point at commits.

A branch is a file containing one 40 character id. That is the entire
implementation, and it is why creating a branch is instant: you are writing
41 bytes, not copying a tree.
"""

from __future__ import annotations

from pathlib import Path

from .repository import RepositoryError


def read_head(git_dir: Path) -> str:
    """Return the raw contents of HEAD.

    Either "ref: refs/heads/main" when attached to a branch, or a bare commit
    id when in detached HEAD state.
    """
    return (git_dir / "HEAD").read_text(encoding="utf-8").strip()


def current_branch(git_dir: Path) -> str | None:
    """The branch name we are on, or None if HEAD is detached."""
    head = read_head(git_dir)
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return None


def resolve_head(git_dir: Path) -> str | None:
    """The commit id HEAD points at, or None if the branch has no commits yet.

    A repository immediately after init is in exactly that state: HEAD names
    refs/heads/main, but that file does not exist. Git calls this an unborn
    branch, and it is why the first commit has no parent.
    """
    head = read_head(git_dir)
    if not head.startswith("ref: "):
        return head
    return read_ref(git_dir, head[len("ref: "):])


def read_ref(git_dir: Path, ref: str) -> str | None:
    """Read a ref path such as "refs/heads/main"."""
    path = git_dir / ref
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def write_text_lf(path: Path, text: str) -> None:
    """Write text with unix line endings regardless of platform.

    Path.write_text() applies the platform's newline translation, so on Windows
    a trailing "\\n" silently becomes "\\r\\n". Git always writes LF in its
    metadata files, so using write_text here would put CRLF in every ref and in
    HEAD, and our bytes would stop matching git's. Writing bytes sidesteps the
    translation entirely.
    """
    path.write_bytes(text.encode("utf-8"))


def write_ref(git_dir: Path, ref: str, oid: str) -> None:
    path = git_dir / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(path, f"{oid}\n")


def update_head(git_dir: Path, oid: str) -> None:
    """Point whatever HEAD refers to at a new commit.

    This is the last step of every commit. If HEAD is attached to a branch we
    move that branch forward; if it is detached we rewrite HEAD itself, which
    is precisely why commits made on a detached HEAD get lost. Nothing is
    bookmarking them.
    """
    head = read_head(git_dir)
    if head.startswith("ref: "):
        write_ref(git_dir, head[len("ref: "):], oid)
    else:
        write_text_lf(git_dir / "HEAD", f"{oid}\n")


def set_head_to_branch(git_dir: Path, branch: str) -> None:
    write_text_lf(git_dir / "HEAD", f"ref: refs/heads/{branch}\n")


def detach_head(git_dir: Path, oid: str) -> None:
    write_text_lf(git_dir / "HEAD", f"{oid}\n")


def list_branches(git_dir: Path) -> dict[str, str]:
    heads = git_dir / "refs" / "heads"
    if not heads.is_dir():
        return {}
    return {
        str(p.relative_to(heads)).replace("\\", "/"): p.read_text(encoding="utf-8").strip()
        for p in heads.rglob("*")
        if p.is_file()
    }


def resolve(git_dir: Path, name: str) -> str:
    """Turn a branch name, HEAD, or raw commit id into a commit id."""
    if name in ("HEAD", "@"):
        oid = resolve_head(git_dir)
        if oid is None:
            raise RepositoryError("HEAD does not point at any commit yet")
        return oid

    oid = read_ref(git_dir, f"refs/heads/{name}")
    if oid:
        return oid

    if len(name) == 40:
        return name

    raise RepositoryError(f"unknown revision: {name}")
