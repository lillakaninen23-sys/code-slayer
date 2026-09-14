"""Git access: a thin subprocess wrapper (`git.py`) and repository/worktree
identity resolution (`identity.py`).

`git.py` itself still mutates none of a branch, the index, or the working
tree — that was out of scope until the checkpoint/job-worktree phases
(Foundation Plan §08/§10), which each got their own narrowly-scoped
plumbing module instead: `checkpoint_git.py` (blob/tree/commit/ref
objects only, never a real branch/index/working tree) and
`job_worktree_git.py` (Phase 7.5c's `git worktree add --detach`/`remove`,
which does create/remove a real linked working tree, but only ever a
Code-Slayer-managed one, never the primary worktree the caller already
has open).
"""
