"""Git fixtures may mutate temporary repositories; inspection runtime may not."""

import hashlib
import os
import subprocess


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo, message="fixture"):
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", message)


def filesystem_snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
    return result
