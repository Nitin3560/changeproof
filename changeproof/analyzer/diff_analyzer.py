from pathlib import Path
import re
import subprocess


class AnalysisError(ValueError):
    pass


def git(repo, *args):
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnalysisError(f"Git failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise AnalysisError("Git could not read the repository or base revision")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def changed_lines(repo, base):
    root = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    revision = git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
    # Worktree comparison includes staged and unstaged changes; untracked files are added below.
    names = git(root, "diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z", "--no-renames", "--diff-filter=ACM", revision, "--", "*.py")
    changed = {}
    for name in filter(None, names.split("\0")):
        patch = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0",
                    revision, "--", name)
        ranges = []
        for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", patch, re.MULTILINE):
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            ranges.append((max(1, start), max(1, start + max(count, 1) - 1)))
        changed[name] = ranges
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py")
    for name in filter(None, untracked.split("\0")):
        changed[name] = None
    return root, revision, changed


def touches(function, ranges):
    return ranges is None or any(start <= function["end_line"] and end >= function["start_line"]
                                 for start, end in ranges)
