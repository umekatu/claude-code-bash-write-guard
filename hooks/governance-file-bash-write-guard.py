# -*- coding: utf-8 -*-
"""Block Bash/PowerShell writes to AI-governance files, so their Edit/Write hooks cannot be bypassed.

A hook registered on `Edit|Write` -- a PreToolUse validator, a PostToolUse checker --
sees a file only when it is changed through those tools.

A file changed with `sed -i`, a `>` redirection, `tee`, or a Python one-liner inside a
Bash command reaches none of them. That is not theoretical: on 2026-08-24 a session
edited a project CLAUDE.md and two ~/.claude/references files entirely through Bash
heredocs and no Edit-side hook ran once.

The fix is to make the tool choice stop mattering for these files. This hook blocks the
Bash path so the Edit/Write path -- the one those hooks can see -- is the only way in.

SCOPE is deliberately narrow: the `.md` files named by GOVERNANCE and GOV_DIRS below.
Edit those two lists to match the files your own Edit/Write hooks cover. Source, data,
scratch scripts and everything else are untouched.

DETECTION is deliberately blunt: the whole command is searched for a governance path AND
for a write primitive, with no attempt to parse shell structure. A path and the write
that targets it are routinely on different lines of a heredoc'd script (`P = "...CLAUDE.md"`
then `io.open(P, "w")`), so any segmentation splits exactly the case worth catching. The
cost is over-firing on a read-and-redirect such as `grep x CLAUDE.md > out.txt`; the
message says what to do about it.

ESCAPE HATCH: identical resend within the TTL passes. Its purpose is FALSE-DETECTION
relief: the substring heuristic cannot tell a real governance write from a command that
merely carries a governance path near a write-like token (git commit messages, test
payloads, read-and-redirect), so commands of that shape that cannot be restructured
pass on the second, considered send. Real writes have their exit named in the block
text: the Edit/Write tools. There is deliberately no bypass flag: a correctly-behaving
agent exits to the Edit tool as directed, and a reflexive one takes the zero-effort
resend anyway, so a typed flag discriminates nothing.

    python governance-file-bash-write-guard.py --selftest
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time

GOVERNANCE = [
    (r"[\w.:~/\\-]*CLAUDE\.md\b", "CLAUDE.md"),
    (r"[\w.:~/\\-]*SKILL\.md\b", "SKILL.md"),
    (r"[\w.:~/\\-]*[/\\]\.claude[/\\](?:rules|references|agents)[/\\][\w.-]+\.md\b",
     "~/.claude/{rules,references,agents}/*.md"),
    (r"[\w.:~/\\-]*[/\\]\.claude[/\\]projects[/\\][^/\\]+[/\\]memory[/\\][\w.-]+\.md\b",
     "project memory entry"),
]

# A command that cd's into a governance directory can then name the file bare --
# `cd ~/.claude/references && cat >> notes.md` was the shape actually used
# on 2026-08-24 -- so the directory plus any bare .md token counts as a hit too.
GOV_DIRS = [
    (r"[/\\]\.claude[/\\]references\b", "~/.claude/references/"),
    (r"[/\\]\.claude[/\\]rules\b", "~/.claude/rules/"),
    (r"[/\\]\.claude[/\\]agents\b", "~/.claude/agents/"),
    (r"[/\\]memory\b", "a memory directory"),
]
BARE_MD_RX = re.compile(r"[\w.-]+\.md\b", re.IGNORECASE)

WRITES = [
    # The lookbehind keeps ASCII arrows out: `->` / `=>` / `<->` in prose or
    # code carry a `>` followed by a word character, which is exactly the
    # redirection shape, and prose in commit messages uses them constantly.
    # The lookahead keeps numeric comparisons out: `length($0)>0 {` in an awk
    # program is `>` followed by a bare integer and then a non-filename
    # character; a redirection target is never a bare integer, while `>1.txt`
    # and `>2026-09-18.md` keep going and still count.
    (r"(?<![<>=-])>>?\s*(?!\d+(?![\w.:~/\\-]))[\"'\w.:~/\\-]", "shell redirection"),
    (r"\bsed\s+-i\b", "sed -i"),
    (r"\btee\b", "tee"),
    (r"\bopen\s*\([^)]*[\"'](?:w|a|r\+|w\+|a\+|wb|ab)[\"']", "open() for writing"),
    (r"\.write_text\s*\(", "Path.write_text"),
    (r"\.write_bytes\s*\(", "Path.write_bytes"),
    (r"\btruncate\s*\(", "truncate()"),
    (r"\b(?:Set-Content|Add-Content|Out-File)\b", "PowerShell content cmdlet"),
]

REASON = (
    "[governance-file-bash-write-guard] BLOCKED: this command writes to {kind} ({hit}) "
    "through {prim}.\n\n"
    "Use the Edit or Write tool for this file instead. Hooks registered on Edit/Write "
    "see a file only when it is changed through those tools; a write made through Bash "
    "or PowerShell reaches none of them, so the file changes with no check having "
    "run.\n\n"
    "If this command only READS that file and the write goes somewhere else, the guard "
    "cannot tell them apart -- drop the redirection (pipe to head/tail instead).\n\n"
    "The resend escape exists for FALSE DETECTIONS: when no governance write actually "
    "happens and the command cannot reasonably be restructured, sending this exact "
    "command again within 15 minutes goes through. A real write resent this way skips "
    "every Edit-side check -- that is on you."
)

# The terminal line the user sees (systemMessage). The reason the model receives
# is always English. Set CLAUDE_HOOK_USER_LANG=ja in settings.json `env` for Japanese.
USER_LANG = os.environ.get("CLAUDE_HOOK_USER_LANG", "en").strip().lower()

REASON_USER = {
    "en": (
        "[governance-file-bash-write-guard] Blocked: this command writes to {kind} "
        "({hit}) through {prim}. The agent was told to use the Edit or Write tool "
        "instead, so the hooks registered on those tools get to see the change. A "
        "false detection passes when the identical command is sent again within "
        "15 minutes."
    ),
    "ja": (
        "[governance-file-bash-write-guard] ブロック: このコマンドは {prim} 経由で "
        "{kind} ({hit}) に書き込もうとしています。この file には Edit または Write "
        "tool を使ってください — Bash 経由の書き込みは Edit / Write に登録された "
        "hook のチェックを素通りします。読み取り"
        "だけのつもりでリダイレクトが誤検知された場合は redirection を外して"
        "ください (head/tail へ pipe する)。再送での通過は誤検知のための逃し弁"
        "です: 実際には governance file への書き込みが起きず、コマンドの組み替え"
        "も難しい場合に限り、同一コマンドを 15 分以内に再送すると通ります。"
    ),
}


def find(patterns, text):
    for rx, label in patterns:
        m = re.search(rx, text, re.IGNORECASE)
        if m:
            return label, m.group(0)
    return None, None


HEREDOC_RX = re.compile(r"<<-?\s*['\"]?([A-Za-z_][\w]*)['\"]?\s*\n(.*?)\n\1\b",
                        re.DOTALL)


def strip_data_heredocs(command):
    """Drop heredoc bodies that only carry DATA, keeping ones that carry code.

    Writing documentation is the common case where a governance path appears inside
    a heredoc body while the redirection target is an ordinary file:

        cat >> .work/review_note.md <<'EOF'
        ... prose that mentions CLAUDE.md ...
        EOF

    Blocking that is pure noise -- nothing is written to a governance file. But the
    body cannot simply be ignored, because the case this hook exists for looks almost
    identical: a heredoc'd SCRIPT whose path and write sit on different lines.

        cat > p.py <<'EOF'
        P = r"E:/proj/CLAUDE.md"
        io.open(P, "w").write(s)
        EOF

    The discriminator is whether the body itself contains a write primitive. Prose
    does not; a script that writes does. So a body with no write primitive in it is
    dropped before the governance search, and any other body is kept.
    """
    out = command
    for m in HEREDOC_RX.finditer(command):
        body = m.group(2)
        prim, _ = find(WRITES, body)
        if prim is None:
            out = out.replace(body, "", 1)
    return out


MSG_ARG_RX = re.compile(
    r"(?:^|\s)(?:-m|--message(?:=|\s+))\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')",
    re.DOTALL)


def strip_message_args(command):
    """Drop quoted `-m` / `--message` payloads before judging.

    A commit message is data the VCS stores, never shell the host executes,
    yet it routinely carries the exact tokens the substring heuristic hunts
    for: governance file names in prose, and a `cd <memory-repo> &&` prefix
    supplies the directory hit from outside the string. Measured 2026-08-29:
    three same-day blocks on ordinary `git commit -m` commands whose only
    "write" was message text. The stripped span is the quoted string
    following the flag, single- or double-quoted, multi-line included.
    """
    return MSG_ARG_RX.sub(" ", command)


NULL_REDIRECT_RX = re.compile(
    r"\d*>>?\s*(?:/dev/null|\$null|NUL\b|nul\b)", re.IGNORECASE)


def strip_null_redirects(command):
    """Drop `2>/dev/null` and friends before looking for a write primitive.

    Discarding output is not writing, and `2>/dev/null` rides along on ordinary read
    commands constantly -- `sed -n 1,50p CLAUDE.md 2>/dev/null` has no write in it at
    all, and blocking it teaches the reader to reach for the escape hatch on commands
    that never needed one.
    """
    return NULL_REDIRECT_RX.sub(" ", command)


ANGLE_EMAIL_RX = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def strip_angle_emails(command):
    """Drop `<name@host>` address tokens before looking for a write primitive.

    A commit trailer such as `Co-Authored-By: X <noreply@anthropic.com>` closes with
    `>`; when another trailer line follows it (`Claude-Session: https://...`), the
    closing bracket, the newline and the next line's first letter form exactly the
    redirection shape, so a commit message fed through a heredoc kept its body and
    the governance names in it blocked the commit. An address is never a write.
    """
    return ANGLE_EMAIL_RX.sub(" ", command)


def judge(command):
    """Return (kind, hit, primitive) when the command must be blocked, else None."""
    if not command:
        return None
    command = strip_angle_emails(command)
    command = strip_null_redirects(strip_data_heredocs(strip_message_args(command)))
    kind, hit = find(GOVERNANCE, command)
    if not kind:
        kind, hit = find(GOV_DIRS, command)
        if kind:
            m = BARE_MD_RX.search(command)
            if not m:
                return None
            hit = "%s + %s" % (hit, m.group(0))
    if not kind:
        return None
    prim, _ = find(WRITES, command)
    if not prim:
        return None
    return kind, hit, prim


SEEN_TTL_S = 900


def seen_path():
    override = os.environ.get("CLAUDE_GOV_BASH_WRITE_SEEN")
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "claude-governance-bash-write-seen.json")


def _load_seen():
    try:
        with open(seen_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def repeat_of_a_blocked_command(command, now=None):
    """True when this exact command was blocked recently, and clears the record.

    The first attempt is refused and remembered; an identical second attempt goes
    through. Anyone who was going to switch to Edit/Write has already switched by
    then, and anyone who means it can say so by repeating themselves rather than
    being held at the door.
    """
    now = time.time() if now is None else now
    key = hashlib.sha1(command.encode("utf-8", "replace")).hexdigest()
    seen = _load_seen()
    ts = seen.get(key)
    fresh = {k: v for k, v in seen.items() if now - v <= SEEN_TTL_S and k != key}
    hit = ts is not None and (now - ts) <= SEEN_TTL_S
    if not hit:
        fresh[key] = now
    try:
        with open(seen_path(), "w", encoding="utf-8") as f:
            json.dump(fresh, f)
    except Exception:
        pass
    return hit


def emit_block(reason, system_message):
    # ensure_ascii=True: this hook can run on a cp932 console, where a non-ASCII
    # character in stdout raises UnicodeEncodeError and the block silently fails open.
    try:
        sys.stdout.write(json.dumps(
            {"decision": "block", "reason": reason, "systemMessage": system_message},
            ensure_ascii=True))
    except Exception:
        pass


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name", "") not in ("Bash", "PowerShell"):
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    verdict = judge(command)
    if verdict and not repeat_of_a_blocked_command(command):
        kind, hit, prim = verdict
        emit_block(
            REASON.format(kind=kind, hit=hit, prim=prim),
            REASON_USER.get(USER_LANG, REASON_USER["en"]).format(
                kind=kind, hit=hit, prim=prim),
        )
    return 0


def selftest():
    home = "C:/Users/me/.claude"
    block = [
        ("heredoc python rewriting a project CLAUDE.md, path and write on different lines",
         'cat > p.py <<\'EOF\'\nP = r"E:/proj/CLAUDE.md"\ns = io.open(P).read()\nio.open(P, "w").write(s)\nEOF'),
        ("append to a references file",
         'cd %s/references && cat >> notes.md <<\'EOF\'\ntext\nEOF' % home),
        ("sed -i on a SKILL.md", "sed -i 's/a/b/' %s/skills/compact-loop/SKILL.md" % home),
        ("redirect over a memory entry",
         'echo x > %s/projects/E--x/memory/note.md' % home),
        ("references file via write_text",
         'python -c "Path(r\'%s/references/style.md\').write_text(s)"' % home),
        ("rules file via tee", "echo x | tee %s/rules/git.md" % home),
        ("PowerShell Set-Content on an agents file",
         'Set-Content %s/agents/worker.md -Value $t' % home),
        ("commit message stripped but a real redirect outside it still blocks",
         'cd %s/projects/E--x/memory && git commit -m "note" && echo x > note.md' % home),
        ("redirect to a memory file whose name starts with digits",
         'cd %s/projects/E--x/memory && echo x > 2026-09-18_note.md' % home),
    ]
    allow = [
        ("reading a CLAUDE.md", "grep -n rule E:/proj/CLAUDE.md | head -5"),
        ("reading a references file", "sed -n '1,40p' %s/references/style_guide.md" % home),
        ("writing a non-governance file", 'cat > .work/notes.md <<\'EOF\'\nx\nEOF'),
        ("writing a tool that mentions no governance path",
         'python - <<\'EOF\'\nio.open("out.json","w").write(s)\nEOF'),
        ("governance path with no write primitive at all",
         "wc -l %s/references/tool_quirks.md" % home),
        ("a .py under .claude is not governance",
         'cat > %s/hooks/x.py <<\'EOF\'\nx\nEOF' % home),
        ("documentation appended to an ordinary file, quoting a governance path in prose",
         'cat >> .work/review_note.md <<\'EOF\'\nItem 2 changed: E:/proj/CLAUDE.md\n'
         'now asks for the derated value.\nEOF'),
        ("a governance path named in a heredoc that writes nothing",
         'cat > notes.txt <<\'EOF\'\nsee %s/references/style_guide.md\nEOF' % home),
        ("reading a references file with stderr discarded",
         "sed -n '20,50p' %s/references/pipeline_lessons.md 2>/dev/null" % home),
        ("grep across a governance file discarding stderr, PowerShell form",
         "Select-String fanout %s/references/tool_quirks.md 2>$null" % home),
        ("git commit from a memory-repo cwd whose message quotes a governance "
         "path and an ASCII arrow",
         'cd %s/projects/E--x/memory && git commit -m "fix: leader<->worker '
         'crossing; see references/lifecycle.md for the write-up"' % home),
        ("multi-line commit message naming memory files",
         'cd %s/projects/E--x/memory && git commit -m "title line\n\nbody names '
         'send_hazard.md and MEMORY.md explicitly"' % home),
        ("ASCII arrow in grep prose over a governance file is not a redirect",
         "grep 'A->B' E:/proj/CLAUDE.md"),
        ("heredoc commit message from a memory-repo cwd whose trailer email "
         "bracket is followed by another trailer line",
         'cd %s/projects/E--x/memory && git add subprocess_isolation.md '
         '&& git commit -q -F - <<\'EOF\'\nrecord the exemption\n\n'
         'Co-Authored-By: Claude <noreply@anthropic.com>\n'
         'Claude-Session: https://claude.ai/code/session_x\nEOF' % home),
        ("awk numeric comparison over a memory file is not a redirect",
         "cd %s/projects/E--x/memory && awk 'NR>=8 && NR<=39 && length($0)>0 "
         "{print NR\": \"substr($0,1,160)}' video_models.md | head -40" % home),
    ]
    bad = 0
    for label, cmd in block:
        if judge(cmd) is None:
            print("FAIL  should block: %s" % label)
            bad += 1
        else:
            print("ok    blocks:  %s" % label)
    for label, cmd in allow:
        v = judge(cmd)
        if v is not None:
            print("FAIL  should allow: %s  -> %r" % (label, v))
            bad += 1
        else:
            print("ok    allows:  %s" % label)
    total = len(block) + len(allow)
    print("selftest: %d/%d" % (total - bad, total))
    return 2 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
