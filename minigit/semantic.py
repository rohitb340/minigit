"""Semantic merge using Python ASTs.

Git merges text, so it reports a conflict when two branches touch the same
lines and stays silent when they touch different lines and break each other:

    branch A:  rename  get_user  ->  fetch_user
    branch B:  add     result = get_user(uid)     (a different line)

    git merge  ->  success
    python     ->  NameError: name 'get_user' is not defined

This parses both sides, works out which names each version defines and
references, and refuses merges that leave a reference dangling.

Scope: Python only, via the stdlib ast module. Resolves module-level and
function-local bindings. Does not follow star imports, globals() injection, or
attributes on objects whose type would need inferring. Stays quiet in those
cases, so false negatives are possible by design and false positives rare.

Prior art: difftastic does structural diffing, GumTree is the academic AST
diff algorithm, Plastic SCM shipped a commercial semantic merge.
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass

BUILTINS = frozenset(dir(builtins)) | {"__name__", "__file__", "__doc__", "self", "cls"}


@dataclass
class Reference:
    name: str
    line: int


@dataclass
class Analysis:
    """What one version of a source file defines and what it refers to."""
    defined: set[str]
    references: list[Reference]
    parsed: bool = True
    error: str = ""

    @property
    def referenced_names(self) -> set[str]:
        return {r.name for r in self.references}


def _bindings_in_scope(body: list[ast.stmt], include_nested: bool = False) -> set[str]:
    """Names bound within one scope.

    We do not descend into nested function or class bodies, because those have
    their own scopes. The nested function's *name* is still bound here, which
    is why FunctionDef contributes its name before we skip its body.
    """
    bound: set[str] = set()

    def bind_target(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                bind_target(element)
        elif isinstance(node, ast.Starred):
            bind_target(node.value)

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(child.name)
                if not include_nested:
                    continue
            elif isinstance(child, (ast.Assign,)):
                for target in child.targets:
                    bind_target(target)
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                bind_target(child.target)
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                bind_target(child.target)
            elif isinstance(child, ast.NamedExpr):
                bind_target(child.target)
            elif isinstance(child, ast.comprehension):
                bind_target(child.target)
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        bind_target(item.optional_vars)
            elif isinstance(child, ast.ExceptHandler):
                if child.name:
                    bound.add(child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                bound.update(child.names)

            walk(child)

    for statement in body:
        walk(ast.Module(body=[statement], type_ignores=[]))

    return bound


def _function_locals(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters plus everything assigned inside the function body."""
    args = node.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names | _bindings_in_scope(node.body)


def analyze(source: str) -> Analysis:
    """Work out what a source file defines and which free names it uses."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return Analysis(defined=set(), references=[], parsed=False, error=str(exc))

    module_level = _bindings_in_scope(tree.body)
    references: list[Reference] = []

    class Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scopes: list[set[str]] = []

        def visit_FunctionDef(self, node):  # noqa: N802
            self.scopes.append(_function_locals(node))
            self.generic_visit(node)
            self.scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_ClassDef(self, node):  # noqa: N802
            self.scopes.append(_bindings_in_scope(node.body))
            self.generic_visit(node)
            self.scopes.pop()

        def visit_Name(self, node):  # noqa: N802
            if not isinstance(node.ctx, ast.Load):
                return
            if node.id in BUILTINS:
                return
            if any(node.id in scope for scope in self.scopes):
                return
            references.append(Reference(node.id, node.lineno))

    Walker().visit(tree)
    return Analysis(defined=module_level, references=references)


@dataclass
class SemanticConflict:
    name: str
    line: int
    removed_by: str | None
    referenced_by: str | None
    suggestion: str | None = None

    def render(self) -> str:
        lines = [f"  '{self.name}' would be undefined after this merge"]
        if self.removed_by:
            lines.append(f"     branch '{self.removed_by}' removed or renamed the definition")
        if self.referenced_by:
            lines.append(f"     branch '{self.referenced_by}' references it (line {self.line})")
        if self.suggestion:
            lines.append(f"     did you mean '{self.suggestion}'?")
        return "\n".join(lines)


def _guess_rename(removed: set[str], added: set[str], target: str) -> str | None:
    """Best effort guess at which new name replaced a removed one.

    Purely a hint for the error message. We score by shared word fragments, so
    get_user to fetch_user scores well because both end in "user". Being wrong
    here costs nothing since the conflict is reported either way.
    """
    if target not in removed or not added:
        return None

    def parts(name: str) -> set[str]:
        return set(name.replace("-", "_").split("_"))

    target_parts = parts(target)
    best, best_score = None, 0
    for candidate in added:
        score = len(target_parts & parts(candidate))
        if score > best_score:
            best, best_score = candidate, score

    return best if best_score else None


def find_conflicts(
    base_src: str | None,
    ours_src: str,
    theirs_src: str,
    merged_src: str,
    ours_name: str = "ours",
    theirs_name: str = "theirs",
) -> list[SemanticConflict]:
    """Detect references the textual merge left dangling.

    The merged file is the thing we actually check, since that is what would
    land on disk. The three inputs are only used to attribute blame, which is
    what makes the error message useful rather than merely correct.
    """
    merged = analyze(merged_src)

    if not merged.parsed:
        return [SemanticConflict(
            name="<syntax>", line=0, removed_by=None, referenced_by=None,
            suggestion=f"merged result does not parse: {merged.error}",
        )]

    base = analyze(base_src) if base_src is not None else Analysis(set(), [])
    ours = analyze(ours_src)
    theirs = analyze(theirs_src)

    # Anything referenced but not defined anywhere we can see. Imports count as
    # definitions, so this does not fire on library calls.
    dangling = [r for r in merged.references if r.name not in merged.defined]

    removed_by_ours = base.defined - ours.defined
    removed_by_theirs = base.defined - theirs.defined
    added_by_ours = ours.defined - base.defined
    added_by_theirs = theirs.defined - base.defined

    conflicts: list[SemanticConflict] = []
    reported: set[str] = set()

    for ref in dangling:
        if ref.name in reported:
            continue

        # If the name was never defined in any version, it was already broken
        # before the merge and is not something the merge introduced. Staying
        # quiet here keeps us from blaming a merge for a pre existing bug.
        if ref.name not in base.defined:
            continue

        if ref.name in removed_by_ours:
            removed_by, suggestion = ours_name, _guess_rename(
                removed_by_ours, added_by_ours, ref.name)
        elif ref.name in removed_by_theirs:
            removed_by, suggestion = theirs_name, _guess_rename(
                removed_by_theirs, added_by_theirs, ref.name)
        else:
            continue

        # Which side still refers to the removed name.
        ours_refs = ours.referenced_names
        theirs_refs = theirs.referenced_names
        if ref.name in theirs_refs and ref.name not in ours_refs:
            referenced_by = theirs_name
        elif ref.name in ours_refs and ref.name not in theirs_refs:
            referenced_by = ours_name
        else:
            referenced_by = theirs_name if removed_by == ours_name else ours_name

        conflicts.append(SemanticConflict(
            name=ref.name, line=ref.line,
            removed_by=removed_by, referenced_by=referenced_by, suggestion=suggestion,
        ))
        reported.add(ref.name)

    return conflicts


def structural_diff(old_src: str, new_src: str) -> list[str]:
    """Describe changes structurally rather than line by line.

    Line diffs are useless when code moves. Rename a function and re indent a
    block and you get a wall of red and green with no signal in it. With the
    tree parsed we can say what actually happened.
    """
    try:
        old_tree, new_tree = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError as exc:
        return [f"could not parse: {exc}"]

    def top_level(tree: ast.Module) -> dict[str, ast.AST]:
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found[node.name] = node
        return found

    old_defs, new_defs = top_level(old_tree), top_level(new_tree)
    removed = set(old_defs) - set(new_defs)
    added = set(new_defs) - set(old_defs)
    common = set(old_defs) & set(new_defs)

    report: list[str] = []

    # A definition that vanished and one that appeared with an identical body
    # is a rename, not a delete plus an add. Comparing dumped subtrees with the
    # name stripped is a cheap and reliable way to spot it.
    def body_signature(node: ast.AST) -> str:
        clone = ast.parse(ast.unparse(node)).body[0]
        clone.name = "_"  # type: ignore[attr-defined]
        return ast.dump(clone, annotate_fields=False)

    matched_new: set[str] = set()
    for gone in sorted(removed):
        signature = body_signature(old_defs[gone])
        for appeared in sorted(added - matched_new):
            if body_signature(new_defs[appeared]) == signature:
                report.append(f"  renamed {_kind(old_defs[gone])}   {gone} -> {appeared}")
                matched_new.add(appeared)
                break
        else:
            report.append(f"  removed {_kind(old_defs[gone])}   {gone}")

    for appeared in sorted(added - matched_new):
        report.append(f"  added   {_kind(new_defs[appeared])}   {appeared}")

    for name in sorted(common):
        old_node, new_node = old_defs[name], new_defs[name]
        changed_body = ast.dump(old_node, annotate_fields=False) != ast.dump(
            new_node, annotate_fields=False)
        moved = old_node.lineno != new_node.lineno

        if changed_body:
            report.append(f"  modified {_kind(new_node)}  {name}")
        elif moved:
            report.append(
                f"  moved   {_kind(new_node)}   {name}  "
                f"(line {old_node.lineno} -> {new_node.lineno})  body unchanged"
            )

    return report or ["  no structural changes"]


def _kind(node: ast.AST) -> str:
    return "class   " if isinstance(node, ast.ClassDef) else "function"
