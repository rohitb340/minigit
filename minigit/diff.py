"""Myers diff: the shortest edit script between two sequences of lines.

Treats diffing as pathfinding. Old file along x, new file along y:

    move right     delete a line from the old file    cost 1
    move down      insert a line from the new file    cost 1
    move diagonal  the lines match, keep it           cost 0

Diagonals being free means the cheapest route keeps the most matching lines,
so the shortest path is the minimal diff.

Rather than search the whole grid, for a budget of d non-free moves it tracks
only the furthest point reachable on each diagonal, increasing d until the far
corner is in range. Real edits are small, so d stays small.
"""

from __future__ import annotations

from enum import Enum


class Op(Enum):
    EQUAL = " "
    DELETE = "-"
    INSERT = "+"


class Edit:
    __slots__ = ("op", "old_line", "new_line", "text")

    def __init__(self, op: Op, text: str, old_line: int | None, new_line: int | None):
        self.op = op
        self.text = text
        self.old_line = old_line
        self.new_line = new_line

    def __repr__(self) -> str:
        return f"{self.op.value}{self.text}"


def _trace(a: list[str], b: list[str]) -> list[list[int]]:
    """Run the forward pass, recording the frontier after each value of d.

    `v` maps a diagonal k (which is x minus y) to the furthest x reached on it.
    Python has no negative array indices in the mathematical sense, so k is
    offset by `max_d` to keep it inside the list.
    """
    n, m = len(a), len(b)
    max_d = n + m
    if max_d == 0:
        return [[0]]

    v = [0] * (2 * max_d + 1)
    trace: list[list[int]] = []

    for d in range(max_d + 1):
        trace.append(v.copy())

        for k in range(-d, d + 1, 2):
            # Choose whether we arrived here by inserting (coming from the
            # diagonal above) or deleting (from the diagonal below). We take
            # whichever had reached further, since that is the greedy choice.
            if k == -d or (k != d and v[k - 1 + max_d] < v[k + 1 + max_d]):
                x = v[k + 1 + max_d]
            else:
                x = v[k - 1 + max_d] + 1

            y = x - k

            # Follow the free diagonal as far as the lines keep matching. This
            # is where the algorithm gets its speed: long runs of unchanged
            # lines cost nothing at all.
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1

            v[k + max_d] = x

            if x >= n and y >= m:
                return trace

    return trace


def _walk_back(a: list[str], b: list[str], trace: list[list[int]]):
    """Reconstruct the path by walking the recorded frontiers backwards."""
    x, y = len(a), len(b)
    max_d = len(a) + len(b)

    for d in range(len(trace) - 1, -1, -1):
        v = trace[d]
        k = x - y

        if k == -d or (k != d and v[k - 1 + max_d] < v[k + 1 + max_d]):
            prev_k = k + 1
        else:
            prev_k = k - 1

        prev_x = v[prev_k + max_d] if 0 <= prev_k + max_d < len(v) else 0
        prev_y = prev_x - prev_k

        while x > prev_x and y > prev_y:
            yield prev_x, prev_y, x - 1, y - 1, x, y, True
            x, y = x - 1, y - 1

        if d > 0:
            yield prev_x, prev_y, prev_x, prev_y, x, y, False

        x, y = prev_x, prev_y


def diff_lines(a: list[str], b: list[str]) -> list[Edit]:
    """Produce the edit script turning `a` into `b`."""
    edits: list[Edit] = []

    for prev_x, prev_y, _, _, x, y, is_diagonal in _walk_back(a, b, _trace(a, b)):
        if is_diagonal:
            edits.append(Edit(Op.EQUAL, a[x - 1], x, y))
        elif x == prev_x:
            edits.append(Edit(Op.INSERT, b[prev_y], None, prev_y + 1))
        else:
            edits.append(Edit(Op.DELETE, a[prev_x], prev_x + 1, None))

    edits.reverse()
    return edits


def unified(a_text: str, b_text: str, a_name: str, b_name: str, context: int = 3) -> str:
    """Render a diff in the usual unified format with @@ hunk headers."""
    a = a_text.splitlines()
    b = b_text.splitlines()
    edits = diff_lines(a, b)

    if all(e.op is Op.EQUAL for e in edits):
        return ""

    # Group changes into hunks, keeping `context` unchanged lines around each
    # run of changes so the output is readable rather than a bare list.
    interesting = [i for i, e in enumerate(edits) if e.op is not Op.EQUAL]
    groups: list[list[int]] = []
    for i in interesting:
        if groups and i - groups[-1][-1] <= context * 2:
            groups[-1].append(i)
        else:
            groups.append([i])

    out = [f"--- a/{a_name}", f"+++ b/{b_name}"]

    for group in groups:
        start = max(0, group[0] - context)
        end = min(len(edits), group[-1] + context + 1)
        window = edits[start:end]

        old_start = next((e.old_line for e in window if e.old_line), 1)
        new_start = next((e.new_line for e in window if e.new_line), 1)
        old_count = sum(1 for e in window if e.op in (Op.EQUAL, Op.DELETE))
        new_count = sum(1 for e in window if e.op in (Op.EQUAL, Op.INSERT))

        out.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@")
        out += [f"{e.op.value}{e.text}" for e in window]

    return "\n".join(out) + "\n"
