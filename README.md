# claude-code-bash-write-guard

*[日本語版 README はこちら](README_ja.md)*

A Claude Code `PreToolUse` hook that stops the agent from rewriting its own
rule files (`CLAUDE.md`, `SKILL.md`, memory entries, …) through **Bash or
PowerShell**, and sends it to the **Edit / Write tools** instead.

## Why

Hooks registered on `Edit|Write` — a validator before the edit, a checker
after it — see a file only when it is changed through those tools. The same
file changed with `sed -i`, a `>` redirection, `tee`, or a Python one-liner
inside a Bash command reaches none of them. If you rely on an Edit-side hook
to review changes to your rule files, one heredoc is enough to skip it.

This hook closes that side door: for the files you list, the shell path is
blocked, so the Edit/Write path — the one your other hooks can see — is the
only way in. It is also useful on its own: a rule-file change made with the
Edit tool shows up as a reviewable diff in the terminal, where a shell
one-liner does not.

## What it does

On every `Bash` / `PowerShell` tool call the hook searches the command text
for **a protected path** and **a write primitive**. When both are present the
call is blocked and the agent is told to use Edit or Write.

Protected paths (`.md` files only; edit the lists to fit your setup):

| List | Default entries |
| --- | --- |
| `GOVERNANCE` (full paths) | any `CLAUDE.md`, any `SKILL.md`, `.claude/{rules,references,agents}/*.md`, `.claude/projects/<project>/memory/*.md` |
| `GOV_DIRS` (a directory named in the command plus any bare `*.md` token, for `cd <dir> && cat >> notes.md`) | `.claude/references`, `.claude/rules`, `.claude/agents`, any `memory` directory |

Write primitives (`WRITES`):

| Primitive | Examples |
| --- | --- |
| Shell redirection | `> file`, `>> file` |
| In-place edit | `sed -i` |
| `tee` | `echo x \| tee file` |
| Python | `open(..., "w")` and the other write modes, `.write_text(`, `.write_bytes(`, `truncate(` |
| PowerShell | `Set-Content`, `Add-Content`, `Out-File` |

Before judging, the hook drops text that looks like a write but is not one:

- quoted `-m` / `--message` payloads (commit messages are data)
- redirections to `/dev/null`, `$null`, `NUL`
- heredoc bodies that contain no write primitive (prose that merely mentions a
  protected file)
- `<name@host>` address tokens (commit trailers)
- ASCII arrows (`->`, `=>`, `<->`) and numeric comparisons (`x>0`)

## The escape hatch

Detection is a substring search over the whole command, with no shell
parsing, so it can fire on a command that only *reads* a protected file and
writes somewhere else (`grep x CLAUDE.md > out.txt`). For those cases,
**sending the identical command again within 15 minutes passes**. The block
message tells the agent this, and tells it that the resend is for false
detections — a real write belongs in the Edit tool.

There is deliberately no bypass flag or environment variable. An agent that
behaves correctly switches to Edit as directed, and one that does not would
take the cheapest exit anyway.

## Install

Requires Python 3 (standard library only).

1. Copy `hooks/governance-file-bash-write-guard.py` to `~/.claude/hooks/`.
2. Add the wiring from [`examples/settings.json.example`](examples/settings.json.example)
   to `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash|PowerShell",
           "hooks": [
             {
               "type": "command",
               "command": "python ~/.claude/hooks/governance-file-bash-write-guard.py"
             }
           ]
         }
       ]
     }
   }
   ```

   On Windows, prefix the command with `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 `.
3. Run the self-test: `python ~/.claude/hooks/governance-file-bash-write-guard.py --selftest`
   (24 cases: 9 that must block, 15 that must pass).

## Settings

| Where | What |
| --- | --- |
| `GOVERNANCE`, `GOV_DIRS` in the hook | Which files are protected |
| `WRITES` in the hook | What counts as a write |
| `SEEN_TTL_S` in the hook (default `900`) | How long an identical resend passes |
| env `CLAUDE_HOOK_USER_LANG` (`en` / `ja`, default `en`) | Language of the one-line notice the user sees in the terminal. The text the agent receives is always English |

## Limits

- This is a guard against a habit, not a security boundary. The identical
  resend passes by design.
- Only the primitives listed above are detected. `cp`, `mv`, `git checkout`,
  `git apply` and a path assembled from variables are not.
- Only `Bash` and `PowerShell` tool calls are inspected. A write made by a
  script file that the agent created earlier and runs later is seen only if
  the command that created the script carried both the path and the write.

## License

MIT
