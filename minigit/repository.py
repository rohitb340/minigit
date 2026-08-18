"""Creating a repository and finding the one you are standing in."""

from pathlib import Path

GIT_DIR_NAME = ".git"
DEFAULT_BRANCH = "main"


class RepositoryError(Exception):
    """Raised when there is no repository, or it is already initialised."""


def init(where: Path) -> Path:
    """Create a fresh repository and return the path to its .git directory.

    A brand new repository is genuinely just three things:

        .git/objects/      empty, the content addressed store
        .git/refs/heads/   empty, one file per branch will live here
        .git/HEAD          text, says which branch we are on

    Nothing else is required. There is no database, no manifest, no index yet.
    The index file only appears the first time something is staged.

    Note that HEAD points at refs/heads/main before that file exists. Git
    treats a dangling HEAD as "a branch with no commits yet", which is how a
    repository can be on a branch before anything has been committed.
    """
    git_dir = where / GIT_DIR_NAME
    if git_dir.exists():
        raise RepositoryError(f"{git_dir} already exists")

    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    # Written as bytes rather than text: on Windows, write_text would translate
    # the trailing newline to CRLF, and git always writes LF in its metadata.
    (git_dir / "HEAD").write_bytes(f"ref: refs/heads/{DEFAULT_BRANCH}\n".encode())

    return git_dir


def find_git_dir(start: Path | None = None) -> Path:
    """Walk upward from `start` looking for a .git directory.

    This is why git commands work from any subdirectory of a project. We check
    the current directory, then its parent, and so on until we either find a
    repository or hit the filesystem root.
    """
    current = (start or Path.cwd()).resolve()

    for candidate in [current, *current.parents]:
        git_dir = candidate / GIT_DIR_NAME
        if git_dir.is_dir():
            return git_dir

    raise RepositoryError("not a minigit repository (or any parent directory)")


def work_tree(git_dir: Path) -> Path:
    """The project directory, which is simply the parent of .git."""
    return git_dir.parent
