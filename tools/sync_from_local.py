#!/usr/bin/env python
"""sync_from_local.py — regenerate the published copy from the file installed
under ~/.claude/, or check whether it is in sync.

    python tools/sync_from_local.py            # rewrite the repo copy
    python tools/sync_from_local.py --check    # report SAME / DIFF / MISSING, change nothing
    python tools/sync_from_local.py --diff     # --check plus the unified diff

Each repo file maps to one local path (MAPPING). A local file becomes its
published copy by dropping its private blocks: everything from a line whose
stripped text is `--- private:begin <name>` (optionally after a `#`) to the
matching `--- private:end <name>` line, inclusive. Code that stays must remain
valid without the block.

Everything else is copied verbatim, so `--check` reporting SAME means the repo
publishes exactly the local install minus the private blocks.
"""
import difflib
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")

MAPPING = {
    "hooks/governance-file-bash-write-guard.py":
        ".claude/hooks/governance-file-bash-write-guard.py",
}

PRIVATE_BEGIN = re.compile(r"^\s*#?\s*--- private:begin (\S+)\s*$")
PRIVATE_END = re.compile(r"^\s*#?\s*--- private:end (\S+)\s*$")


def strip_private(text: str, rel: str) -> str:
    out = []
    open_name = None
    for line in text.splitlines(keepends=True):
        if open_name is None:
            m = PRIVATE_BEGIN.match(line)
            if m:
                open_name = m.group(1)
                continue
            out.append(line)
        else:
            m = PRIVATE_END.match(line)
            if m:
                if m.group(1) != open_name:
                    raise SystemExit(f"{rel}: private:end {m.group(1)} closes {open_name}")
                open_name = None
    if open_name is not None:
        raise SystemExit(f"{rel}: private block {open_name} never closed")
    return "".join(out)


def publish_text(rel: str) -> str:
    local_path = os.path.join(HOME, MAPPING[rel])
    with open(local_path, encoding="utf-8") as f:
        return strip_private(f.read(), rel)


def main() -> int:
    check = "--check" in sys.argv or "--diff" in sys.argv
    show_diff = "--diff" in sys.argv
    drift = 0
    for rel in MAPPING:
        repo_path = os.path.join(HERE, rel)
        local_path = os.path.join(HOME, MAPPING[rel])
        if not os.path.exists(local_path):
            print(f"MISSING  {rel}  (no {MAPPING[rel]})")
            drift += 1
            continue
        new = publish_text(rel)
        if check:
            old = ""
            if os.path.exists(repo_path):
                with open(repo_path, encoding="utf-8") as f:
                    old = f.read()
            if old == new:
                print(f"SAME     {rel}")
                continue
            drift += 1
            diff = list(difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                "repo/" + rel, "local(sanitized)/" + rel, lineterm=""))
            plus = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            minus = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            print(f"DIFF     {rel}  (+{plus} -{minus})")
            if show_diff:
                print("\n".join(diff))
                print()
        else:
            os.makedirs(os.path.dirname(repo_path), exist_ok=True)
            with open(repo_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            print(f"WROTE    {rel}")
    return 1 if (check and drift) else 0


if __name__ == "__main__":
    raise SystemExit(main())
