"""Git access: a thin subprocess wrapper (`git.py`) and repository/worktree
identity resolution (`identity.py`).

Nothing here mutates a branch, the index, or the working tree — that is
out of scope until the checkpoint/tool-layer phases (Foundation Plan §08/§10).
"""
