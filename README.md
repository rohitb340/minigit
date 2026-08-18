# minigit

A git implementation in Python that is byte compatible with the real thing, plus one feature git does not have: **it refuses merges that would break your code.**

```
$ minigit merge caller

semantic conflict: the merge is textually clean but semantically broken

  src/app.py
  'get_user' would be undefined after this merge
     branch 'main' removed or renamed the definition
     branch 'caller' references it (line 7)
     did you mean 'fetch_user'?

merge aborted, nothing was written.
real git would have committed this. use --force-text to do the same.
```

Real git merges those same two branches with exit code 0 and produces a file that raises `NameError`. That is not a hypothetical, it is a test in this repository.

---

## Why the object store is byte compatible

Because it means every claim here is checkable rather than asserted. Build a repository with `minigit`, then open it with real `git`:

```bash
minigit init .
echo "hello" > a.txt
minigit add a.txt
minigit commit -m "first commit"

git log --oneline     # real git reads our commits
git ls-tree -r HEAD   # real git reads our trees
git status            # real git reads our binary index
git fsck --strict     # real git finds no corruption
```

All four work. Nothing in this README asks to be believed.

## Install and run

Python 3.10 or newer, no dependencies.

```bash
git clone <this repo> && cd minigit
python -m minigit --help
```

## Commands

| Layer | Commands |
| ----- | -------- |
| Plumbing | `hash-object`, `cat-file` |
| Porcelain | `init`, `add`, `commit`, `log`, `status`, `diff`, `branch`, `checkout`, `merge` |
| Not in git | `merge` semantic gate, `diff --semantic`, `--explain` |

### `--explain`

Makes the object model visible instead of describing it:

```
$ minigit commit -m "fix parser" --explain

  read index          2 entries
  wrote  tree   a7e421  /  (root)
  wrote  commit 23f428  parent none
  updated refs/heads/main -> 23f428
```

`add --explain` distinguishes `wrote` from `reused`, which is deduplication happening in front of you.

### `diff --semantic`

Line diffs are noise when code moves. With the syntax tree parsed:

```
$ minigit diff HEAD~1 HEAD --semantic

  renamed function   process_data -> transform_data
  moved   function   validate  (line 14 -> line 87)  body unchanged
```

## How it works

The whole design follows from one decision: **objects are named by a hash of their contents.** Deduplication, integrity checking, and instant branching all fall out of that for free.

| Module | Responsibility |
| ------ | -------------- |
| `objects.py` | Content addressed store. `<type> <size>\0<content>`, zlib compressed, sharded by the first two hex characters of the id. |
| `trees.py` | Directory listings. Includes git's sort rule where a directory compares as though its name ended in `/`. |
| `commits.py` | Snapshot plus parents. History is a DAG, so ancestor walks need a seen set. |
| `index.py` | Git's real binary index (version 2), including racy index detection. |
| `refs.py` | Branches. A branch is a 41 byte file. |
| `diff.py` | Myers diff, the shortest edit script as a shortest path problem. |
| `merge.py` | Merge base by breadth first search, then three way merge. |
| `semantic.py` | AST analysis. The part git does not have. |
| `fuzz.py` | Differential fuzzing against real git. |

Two algorithms are worth reading: **Myers diff** in `diff.py` treats diffing as pathfinding through an edit graph where matching lines are free moves, and **merge base** in `merge.py` is lowest common ancestor on a directed acyclic graph, which is harder than on a tree because two branches can share several unrelated ancestors.

## Testing

```bash
python -m pytest tests -q               # 27 tests
python tests/fuzz.py -n 200             # differential fuzzing
```

Every test that can be cross checked against real git is. `test_tree_ids_match_git` asserts our tree ids equal `git rev-parse HEAD^{tree}`. `test_merge_base_matches_git` asserts our answer equals `git merge-base`. `test_our_index_is_valid_to_git` asserts `git status` reports a clean tree against our index.

### Differential fuzzing

The fuzzer generates random operation sequences, runs each identical sequence through both minigit and real git in parallel directories, and asserts the resulting object stores are identical. Any divergence comes with the exact sequence that caused it.

```
$ python tests/fuzz.py -n 200 -l 12
200/200 sequences byte identical to real git
```

It earned its place immediately. Divergences it found, all now fixed or scoped:

1. **CRLF normalisation.** On Windows git defaults `core.autocrlf` to true, so `git add` rewrites line endings before hashing. Scoped out deliberately: line ending translation is a configuration layer above the object model. The fuzzer sets `core.autocrlf=false` to compare like with like.
2. **CRLF in ref files.** `Path.write_text` applies platform newline translation, so our refs and `HEAD` contained `\r\n` where git writes `\n`. A real bug, fixed by writing bytes.
3. **Dirty tree checkout.** Git leaves uncommitted work in place when you check out the branch you are already on; minigit restores the committed tree. A policy difference above this layer, so the fuzzer exercises checkout from a clean state and says so.

Two more were found later, and the second is the more interesting one.

4. **`add` never staged deletions.** It only ever added index entries, so a renamed or deleted file kept its old path in the index forever and every later commit recorded both names. Real git stages deletions and reports `R100 old -> new`. **The fuzzer did not catch this because it never generates deletes or renames**, which is a real gap in the generator rather than a defence of the bug.

5. **The fuzz harness could not tell a crash from a wrong answer.** Its subprocess runner captured output but never checked the exit code, and the child processes ran with a working directory where the package was not importable. So every command failed with `No module named minigit`, wrote nothing, and the object comparison then reported git's objects as missing from ours. It printed `0/3 sequences byte identical` for a tool that had never executed. Fixed by injecting the package root into `PYTHONPATH` and raising on any non-zero exit. A harness that reports a plausible failure for something that never ran is worse than no harness.

The racy index problem was found the same way, by a test rather than the fuzzer: a file written, staged, and edited again within the same second keeps its recorded mtime, and if the edit leaves the size unchanged the stat cache would report it clean. `index.py` mirrors git's fix, treating any entry not strictly older than the index file as suspect.

## Scope

**Implemented:** the object store, trees, commits, the binary index, refs and branching, Myers diff, merge base, three way merge, semantic merge, structural diff.

**Deliberately not implemented:** remotes, `push` and `pull`, packfiles, rebase, stash, submodules, hooks, line ending translation. Local single user version control is the whole model.

**Semantic merge is Python only**, using the standard library `ast` module. The scope analysis resolves module level definitions and function local bindings. It does not follow star imports, names injected through `globals()`, or attributes on objects whose type would have to be inferred. In those cases it stays quiet rather than report a conflict that is not real, so false negatives are possible by design and false positives should be rare.

## Prior art

Structural diffing and semantic merging are not new. [difftastic](https://difftastic.wilfred.me.uk/) does structural diffs today, GumTree is the academic AST diffing algorithm, and Plastic SCM shipped a commercial semantic merge. Reimplementing git is also well trodden ground.

The accurate claim is not that nobody thought of this. It is that **git does not do it, and this does**, and that every compatibility claim here can be verified in one command.
