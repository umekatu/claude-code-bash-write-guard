# -*- coding: utf-8 -*-
"""Block Bash/PowerShell writes to AI-governance files, so their Edit/Write hooks cannot be bypassed.

A hook registered on `Edit|Write` -- a PreToolUse validator, a PostToolUse checker --
sees a file only when it is changed through those tools.

A file changed with `sed -i`, a `>` redirection, `tee`, or a Python one-liner inside a
Bash command reaches none of them. That is not theoretical: a project CLAUDE.md and several
reference files have been edited entirely through Bash heredocs without any Edit-side
hook running once.

The fix is to make the tool choice stop mattering for these files. This hook blocks the
Bash path so the Edit/Write path -- the one those hooks can see -- is the only way in.

SCOPE is deliberately narrow: the `.md` files named by GOVERNANCE below.
Edit that list to match the files your own Edit/Write hooks cover. Source, data,
scratch scripts and everything else are untouched.

DETECTION is the union of the original whole-command heuristic (legacy_judge)
and destination/program parsing (structural_judge). Legacy detection retains its
original data filters, with exact basename/component matching and anchored path
regexes. Structural analysis only adds blocks; it cannot exempt a legacy hit.
Thus some printed literals and documentation examples still cost one resend.
The structural side uses destination paths for shell redirects, copy/move/install
commands, and content cmdlets, parsed only at executable positions. In-place sed options
are recognized in any order. Program input keeps its path AND write-primitive
heuristic with named files and slash-prefixed governance directories only, without
shell cwd resolution or project-relative directory recognition. Paths and writes routinely
sit on different lines of a heredoc'd script (`P = "...CLAUDE.md"` then
`io.open(P, "w")`). Shell destinations resolve against the active segment cwd;
output arguments and heredocs sent to documentation files are data. Structural
word-initial unquoted # comments end at the newline. Subshell cwd changes are
restored at closing parentheses; brace-group cwd changes persist. Interpreter
input and staged script bodies remain inspected for cross-line writes.

LEDGER is scoped by agent_id else session_id and normalized cwd, expires after
15 minutes, consumes the resend record, and keeps the newest 1000 entries.
Writes use a same-directory temporary file and atomic replacement.

ESCAPE HATCH: identical resend within the TTL passes. Its purpose is FALSE-DETECTION
relief: program-input heuristics can mistake a literal example for a write.
Commands of that shape that cannot be restructured
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
import shlex
import sys
import tempfile
import time

# Distinctive anchors avoid repeated greedy path-prefix rescans on long negatives.
GOVERNANCE = [
    (r"(?:^|[/\\])CLAUDE\.md$", "CLAUDE.md"),
    (r"(?:^|[/\\])SKILL\.md$", "SKILL.md"),
    (r"(?:^|[/\\])\.claude[/\\](?:rules|references|agents)[/\\][^/\\]+\.md$",
     "~/.claude/{rules,references,agents}/*.md"),
    (r"(?:^|[/\\])\.claude[/\\]projects[/\\][^/\\]+[/\\]memory[/\\][^/\\]+\.md$",
     "project memory entry"),
]

WRITES = [
    (r"\bopen\s*\([^)]*[\"'](?:w|a|r\+|w\+|a\+|wb|ab)[\"']", "open() for writing"),
    (r"\.write_text\s*\(", "Path.write_text"),
    (r"\.write_bytes\s*\(", "Path.write_bytes"),
    (r"\btruncate\s*\(", "truncate()"),
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


OPEN_WRITE_TOKENS = re.compile(
    r'''\bopen\s*\(|\)|["'](?:w|a|r\+|w\+|a\+|wb|ab)["']''', re.I)


def open_write_hit(text):
    """Equivalent to open([^)]*MODE, without retrying every nested open prefix."""
    start, end = None, None
    for m in OPEN_WRITE_TOKENS.finditer(text):
        token = m.group(0)
        if token == ")":
            if end is not None:
                return text[start:end]
            start = None
        elif token[:1] in {"'", '"'}:
            if start is not None:
                end = m.end()
        elif start is None:
            start = m.start()
    return text[start:end] if end is not None else None


def find(patterns, text):
    for rx, label in patterns:
        if label == "open() for writing":
            hit = open_write_hit(text)
            if hit is not None:
                return label, hit
            continue
        m = re.search(rx, text, re.IGNORECASE)
        if m:
            return label, m.group(0)
    return None, None


class HeredocMatch:
    """Small span-only match interface; bodies are copied only for accepted matches."""
    def __init__(self, text, spans):
        self.text, self.spans = text, spans

    def start(self, group=0):
        return self.spans[group][0]

    def end(self, group=0):
        return self.spans[group][1]

    def group(self, group=0):
        begin, end = self.spans[group]
        return self.text[begin:end]


def heredoc_matches(command, legacy=False):
    """Index lines/terminators once, retaining the two historical regex grammars.

    Tag queues advance monotonically. The structural header permits arbitrary
    trailing text, so its greedy tag can backtrack to a prefix; a trie preserves
    that behavior without rescanning either the tag or the remaining command.
    """
    lines = list(re.finditer(r"[^\n]*\n|[^\n]+$", command))
    terminators, trie = {}, {}
    for line in lines:
        text = line.group(0).rstrip("\n")
        term = re.match(r"([A-Za-z_]\w*)" if legacy else r"[\t ]*([A-Za-z_]\w*)", text)
        if term and line.start() > 0:
            tag = term.group(1)
            terminators.setdefault(tag, []).append((line.start() - 1, line.start() + term.end()))
            node = trie
            for char in tag:
                node = node.setdefault(char, {})
            node[None] = tag
    header = re.compile(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?\s*\n" if legacy else
                        r"<<-?[^\S\n]*['\"]?([A-Za-z_]\w*)['\"]?")
    cursors, line_index, consumed = {}, 0, 0
    for head in header.finditer(command):
        if head.start() < consumed:
            continue
        while line_index < len(lines) and lines[line_index].end() <= head.end() - 1:
            line_index += 1
        if legacy:
            body_start = head.end()
        else:
            if line_index == len(lines) or not lines[line_index].group(0).endswith("\n"):
                continue
            body_start = lines[line_index].end()
        tags = []
        node = trie
        for char in head.group(1):
            node = node.get(char)
            if node is None:
                break
            if None in node:
                tags.append(node[None])
        for tag in reversed(tags):
            if legacy and tag != head.group(1):
                continue
            terms = terminators[tag]
            cursor = cursors.get(tag, 0)
            while cursor < len(terms) and terms[cursor][0] < body_start:
                cursor += 1
            cursors[tag] = cursor
            if cursor == len(terms):
                continue
            body_end, end = terms[cursor]
            consumed = end
            yield HeredocMatch(command, [(head.start(), end),
                                         (head.start(1), head.start(1) + len(tag)),
                                         (body_start, body_end)])
            break


def heredoc_sub(command, replace, legacy=False):
    chunks, previous = [], 0
    for match in heredoc_matches(command, legacy):
        chunks.extend((command[previous:match.start()], replace(match)))
        previous = match.end()
    chunks.append(command[previous:])
    return "".join(chunks)


# Quotes are consumed as part of a word before shell separators are recognized.
# Backslashes in unquoted Windows paths are literal; escaped shell punctuation
# and line continuations still stay inside their word.
SHELL_TOKEN = re.compile(
    r'''(?P<comment>\#[^\n]*)|(?P<word>(?:"(?:\\.|[^"\\])*"|'[^']*'|\\[\s#;&|<>(){}]|[^\s;&|<>()"'])+)|(?P<op>&&|\|\||>>|<<-?|[;|\n<>()])''')


def shell_segments(command):
    part = []
    for m in SHELL_TOKEN.finditer(re.sub(r"\\\r?\n", "", command)):
        if m.group("comment"):
            continue
        # Braces are reserved words only when standalone, unlike parentheses.
        # Keep path/variable fragments such as dir{a,b} and ${name} intact.
        if m.group("op") in {"(", ")"}:
            if part:
                yield part, ""
            yield [], m.group("op")
            part = []
        elif m.group("op") in {"&&", "||", ";", "|", "\n"} or m.group(0) in {"{", "}"}:
            if part:
                yield part, m.group("op")
            part = []
        else:
            part.append(m.group(0))
    if part:
        yield part, ""


def shell_word(raw):
    if raw[:1] in {"'", '"'} and raw[-1:] == raw[0] and raw[0] not in raw[1:-1]:
        return raw[1:-1]
    if not any(c in raw for c in "'\""):
        return raw
    try:
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""
        return next(iter(lexer))
    except (ValueError, StopIteration):
        return raw


def command_argv(raw):
    """Remove executable-prefix control words, assignments and standard wrappers.

    Keep raw quotes until control classification: quoted control words are argv.
    Grouping punctuation has already separated command positions.
    """
    argv = [shell_word(t) for t in raw]
    controls = {"then", "do", "else", "elif", "if", "while", "until", "!"}
    i = 0
    while i < len(argv):
        if raw[i] in controls or re.match(r"^[A-Za-z_]\w*=", argv[i]):
            i += 1
        elif argv[i].lower() in {"sudo", "env", "command"}:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                option = argv[i]
                i += 1
                if option in {"-u", "-g", "--user", "--group", "-C", "--chdir"} and i < len(argv):
                    i += 1
                if option == "--":
                    break
        else:
            break
    return argv[i:]


def resolved_path(path, cwd):
    path = os.path.expanduser(os.path.expandvars(path)).replace("\\", "/")
    return os.path.normpath(os.path.join(cwd, path)).replace("\\", "/")


def governance_path(path, cwd):
    """Exact basenames and exact directory components, never filename suffixes."""
    path = resolved_path(path, cwd)
    kind, _ = find(GOVERNANCE, path)
    return (kind, path) if kind else (None, None)


def output_paths(argv, cwd):
    """Return only the destination operands of a recognized shell primitive."""
    if not argv:
        return [], ""
    exe = argv[0].lower()
    positional, named = [], {}
    i, options = 1, True
    value_options = {"-t", "--target-directory", "-destination", "-path", "-literalpath", "-filepath",
                     "-m", "--mode", "-o", "--owner", "-g", "--group", "-S", "--suffix",
                     "-value", "-inputobject", "-encoding", "-width", "-filter", "-include", "-exclude"}
    if exe == "sed":
        inplace, explicit_script = False, False
        while i < len(argv):
            arg = argv[i]
            if options and arg == "--":
                options = False
            elif options and (arg == "--in-place" or arg.startswith("--in-place=")):
                inplace = True
            elif options and arg in {"-e", "--expression", "-f", "--file"}:
                explicit_script = True
                i += 1
            elif options and arg.startswith("-"):
                if not arg.startswith("--") and "i" in arg[1:]:
                    inplace = True
                if arg.startswith(("-e", "-f", "--expression=", "--file=")):
                    explicit_script = True
            else:
                positional.append(arg)
            i += 1
        operands = positional if explicit_script else positional[1:]
        return (operands if inplace else []), "sed -i"
    if exe not in {"cp", "mv", "install", "copy-item", "move-item", "set-content", "add-content", "out-file", "sc", "ac", "tee"}:
        return [], ""
    ps = exe in {"copy-item", "move-item", "set-content", "add-content", "out-file", "sc", "ac"}
    while i < len(argv):
        arg = argv[i]
        key, equal, value = arg.partition("=")
        if options and arg == "--":
            options = False
        elif options and exe in {"cp", "mv", "install"} and arg.startswith("-t") and len(arg) > 2:
            named["-t"] = arg[2:]
        elif options and (key.lower() if ps else key) in value_options:
            if not equal and i + 1 < len(argv):
                i += 1
                value = argv[i]
            named[key.lower()] = value
        elif options and arg.startswith("-"):
            pass
        else:
            positional.append(arg)
        i += 1
    if exe in {"cp", "mv", "install", "copy-item", "move-item"}:
        target = named.get("-destination", named.get("-t", named.get("--target-directory")))
        sources = ([named["-path"]] if "-path" in named else
                   [named["-literalpath"]] if "-literalpath" in named else []) + positional
        directory_option = target is not None and ("-t" in named or "--target-directory" in named)
        if target is None:
            if len(sources) < 2:
                return [], ""
            target = sources.pop()
        directory = (directory_option or target.endswith(("/", "\\")) or len(sources) > 1
                     or os.path.isdir(resolved_path(target, cwd)))
        paths = [target.rstrip("/\\") + "/" + re.split(r"[/\\]", s)[-1] for s in sources] if directory else [target]
        return paths, "copy/move/install destination"
    if exe in {"set-content", "add-content", "out-file", "sc", "ac"}:
        target = next((named[k] for k in ("-path", "-literalpath", "-filepath") if k in named),
                      positional[0] if positional else None)
        return ([target] if target else []), "PowerShell content cmdlet"
    if exe == "tee":
        return positional, "tee"
    return [], ""


# Program paths retain the original slash-prefix scope for directory patterns.
# Exact basenames from the shell matcher still exclude NOT_CLAUDE.md and backups.
PROGRAM_GOVERNANCE = [
    (rx if label in {"CLAUDE.md", "SKILL.md"} else
     rx.replace(r"(?:^|[/\\])", r"[/\\]", 1), label)
    for rx, label in GOVERNANCE
]


def code_write(code):
    """Cross-line program heuristic; shell cwd never changes literal path scope."""
    prim, _ = find(WRITES, code)
    if not prim:
        return None
    for m in re.finditer(r'''["']([^"'\n]+)["']''', code):
        kind, hit = find(PROGRAM_GOVERNANCE, m.group(1))
        if kind:
            return kind, hit, prim
    return None


def shell_substitutions(text, heredoc=False):
    """Yield executable substitution bodies; quoted documentation is removed first."""
    i, quote = 0, None
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if not heredoc and c in {"'", '"'}:
            if quote == c:
                quote = None
            elif quote is None:
                quote = c
        elif quote != "'" and text.startswith("$(", i):
            start, j, inner, stack = i + 2, i + 2, None, [None]
            while j < len(text) and stack:
                ch = text[j]
                if ch == "\\" and inner != "'":
                    j += 2
                    continue
                if ch in {"'", '"'}:
                    if inner == ch:
                        inner = None
                    elif inner is None:
                        inner = ch
                elif inner != "'" and text.startswith("$(", j):
                    stack.append(inner)
                    inner = None
                    j += 1
                elif inner is None and ch == "(":
                    stack.append(None)
                elif inner is None and ch == ")":
                    inner = stack.pop()
                j += 1
            yield text[start:j - 1] if not stack else text[start:]
            i = j
            continue
        elif quote != "'" and c == "`":
            end = text.find("`", i + 1)
            if end != -1:
                yield text[i + 1:end]
                i = end + 1
                continue
        i += 1


def structural_judge(command, cwd="", depth=0):
    """Judge executable shell segments and program input, not arbitrary prose."""
    if not command or depth > 8:
        return None
    code_bodies = {}
    def heredoc(m):
        body = m.group(2)
        line_start = command.rfind("\n", 0, m.start()) + 1
        head = command[line_start:m.start()] + m.group(0).split("\n", 1)[0]
        head_parts = list(shell_segments(head))
        consumer_index = next((i for i, (part, _) in enumerate(head_parts) if "<<" in part or "<<-" in part), None)
        consumer = head_parts[consumer_index][0] if consumer_index is not None else []
        if consumer_index is None:
            # A header-like string in a quoted word or comment is not a redirect.
            # Keep later physical lines available to the structural shell lexer.
            return m.group(0)
        argv = command_argv(consumer)
        exe = argv[0].lower() if argv else ""
        quoted = bool(re.match(r"<<-?\s*['\"]", m.group(0)))
        redirects = re.findall(r'''>>?\s*(["']?)([^\s"'<>]+)\1''', head)
        sinks = [p for _, p in redirects]
        if exe == "tee":
            sinks.extend(output_paths(argv, cwd)[0])
        script_sink = any(re.search(r"\.(?:py|sh|ps1|bash|zsh|js|ts|pl|rb|cmd|bat)$", s, re.I) for s in sinks)
        interpreters = {"python", "python3", "py", "bash", "sh", "pwsh", "powershell", "node", "perl", "ruby"}
        interpreter = exe in interpreters
        piped_interpreters = []
        i = consumer_index
        while i is not None and i + 1 < len(head_parts) and head_parts[i][1] == "|":
            i += 1
            downstream = command_argv(head_parts[i][0])
            if downstream and downstream[0].lower() in interpreters:
                piped_interpreters.append(downstream[0].lower())
        inspect_body = interpreter or script_sink or bool(piped_interpreters) or (not quoted and ("$(" in body or "`" in body))
        begin, end = m.start(2) - m.start(), m.end(2) - m.start()
        header, tail = m.group(0)[:begin], m.group(0)[end:]
        if inspect_body:
            marker = "__GOV_BODY_%d__" % len(code_bodies)
            program_body = (exe in {"python", "python3", "py", "node", "perl", "ruby"}
                            or any(name in {"python", "python3", "py", "node", "perl", "ruby"} for name in piped_interpreters)
                            or any(re.search(r"\.(?:py|js|ts|pl|rb)$", s, re.I) for s in sinks))
            code_bodies[marker] = (body, program_body, not quoted)
            # The marker keeps program inspection at its consumer's active cwd.
            header = header.rstrip("\n") + " " + marker + "\n"
        return header + tail
    command = heredoc_sub(command, heredoc)
    segments = list(shell_segments(command))
    base, subshell_cwds = cwd, []
    for index, (raw, separator) in enumerate(segments):
        if not raw:
            if separator == "(":
                subshell_cwds.append(base)
            elif separator == ")" and subshell_cwds:
                base = subshell_cwds.pop()
            continue
        for token in raw:
            if token in code_bodies:
                body, program_body, expands = code_bodies[token]
                if expands:
                    for substitution in shell_substitutions(body, heredoc=True):
                        verdict = structural_judge(substitution, base, depth + 1)
                        if verdict:
                            return verdict
                verdict = code_write(body) if program_body else structural_judge(body, base, depth + 1)
                if verdict:
                    return verdict
        # Substitutions execute even in double quotes, but not single quotes.
        for body in shell_substitutions(" ".join(raw)):
            verdict = structural_judge(body, base, depth + 1)
            if verdict:
                return verdict
        args, destinations = [], []
        i = 0
        while i < len(raw):
            if raw[i] in {">", ">>"} and i + 1 < len(raw):
                destinations.append((shell_word(raw[i + 1]), "shell redirection"))
                i += 2
            elif raw[i] in {"<", "<<", "<<-"}:
                i += 2
            else:
                args.append(raw[i])
                i += 1
        argv = command_argv(args)
        if not argv:
            continue
        exe = argv[0].lower()
        if exe == "cd" and len(argv) > 1:
            base = resolved_path(argv[-1], base)
            continue
        paths, prim = output_paths(argv, base)
        destinations.extend((p, prim) for p in paths)
        for target, primitive in destinations:
            kind, hit = governance_path(target, base)
            if kind:
                return kind, hit, primitive
        if exe in {"python", "python3", "py"} and "-c" in argv:
            pos = argv.index("-c") + 1
            if pos < len(argv):
                verdict = code_write(argv[pos])
                if verdict:
                    return verdict
        if exe in {"bash", "sh", "pwsh", "powershell"}:
            for pos, arg in enumerate(argv[:-1]):
                if (arg.lower() in {"-c", "-command"}
                        or (exe in {"bash", "sh"} and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg))):
                    verdict = structural_judge(argv[pos + 1], base, depth + 1)
                    if verdict:
                        return verdict
        if exe in {"echo", "printf", "write-output"} and separator == "|" and index + 1 < len(segments):
            following = command_argv(segments[index + 1][0])
            if following and following[0].lower() in {"bash", "sh", "pwsh", "powershell"}:
                verdict = structural_judge(" ".join(argv[1:]), base, depth + 1)
                if verdict:
                    return verdict
    return None


# Legacy raw-text scope, with exact path components (R1). No greedy prefix.
# The right boundary excludes suffixes/backups and embedded filename characters.
legacy_GOVERNANCE = [
    (r"(?<![\w.-])CLAUDE\.md(?![\w.:-])", "CLAUDE.md"),
    (r"(?<![\w.-])SKILL\.md(?![\w.:-])", "SKILL.md"),
    (r"[/\\]\.claude[/\\](?:rules|references|agents)[/\\][\w.-]+\.md(?![\w.:-])", "~/.claude/{rules,references,agents}/*.md"),
    (r"[/\\]\.claude[/\\]projects[/\\][^/\\]+[/\\]memory[/\\][\w.-]+\.md(?![\w.:-])", "project memory entry"),
]

# A command that cd's into a governance directory can then name the file bare --
# `cd ~/.claude/references && cat >> notes.md` is a common shape
# -- so the directory plus any bare .md token counts as a hit too.
legacy_GOV_DIRS = [
    (r"[/\\]\.claude[/\\]references(?![\w.-])", "~/.claude/references/"),
    (r"[/\\]\.claude[/\\]rules(?![\w.-])", "~/.claude/rules/"),
    (r"[/\\]\.claude[/\\]agents(?![\w.-])", "~/.claude/agents/"),
    (r"[/\\]memory(?![\w.-])", "a memory directory"),
]
legacy_BARE_MD_RX = re.compile(r"(?<![\w.-])[\w.-]+\.md(?![\w.:-])", re.IGNORECASE)

legacy_WRITES = [
    # The lookbehind keeps ASCII arrows out: `->` / `=>` / `<->` in prose or
    # code carry a `>` followed by a word character, which is exactly the
    # redirection shape, and prose in commit messages uses them constantly.
    # The lookahead keeps numeric comparisons out: `length($0)>0 {` in an awk
    # program is `>` followed by a bare integer and then a non-filename
    # character; a redirection target is never a bare integer, while `>1.txt`
    # and `>20260918.md` keep going and still count.
    (r"(?<![<>=-])>>?\s*(?!\d+(?![\w.:~/\\-]))[\"'\w.:~/\\-]", "shell redirection"),
    (r"\bsed\s+-i\b", "sed -i"),
    (r"\btee\b", "tee"),
    (r"\bopen\s*\([^)]*[\"'](?:w|a|r\+|w\+|a\+|wb|ab)[\"']", "open() for writing"),
    (r"\.write_text\s*\(", "Path.write_text"),
    (r"\.write_bytes\s*\(", "Path.write_bytes"),
    (r"\btruncate\s*\(", "truncate()"),
    (r"\b(?:Set-Content|Add-Content|Out-File)\b", "PowerShell content cmdlet"),
]


def legacy_find(patterns, text):
    # Keep the legacy matcher independent of structural find/filter changes.
    for rx, label in patterns:
        if label == "open() for writing":
            hit = open_write_hit(text)
            if hit is not None:
                return label, hit
            continue
        m = re.search(rx, text, re.IGNORECASE)
        if m:
            return label, m.group(0)
    return None, None


def legacy_remove_bodies(command, bodies):
    """Preserve first-occurrence replacement, coalescing identical removals.

    Legacy removes the first copy of the body, even when that copy precedes its
    header. Replacing by match offsets would change those decisions. Adjacent
    identical removals share one scan and one allocation instead of copying the
    remaining command once per heredoc.
    """
    previous, count = None, 0
    for body in bodies:
        if body == previous:
            count += 1
        else:
            if previous is not None:
                command = command.replace(previous, "", count)
            previous, count = body, 1
    return command.replace(previous, "", count) if previous is not None else command


def legacy_strip_data_heredocs(command):
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
    return legacy_remove_bodies(command, (m.group(2) for m in heredoc_matches(command, legacy=True)
                                           if legacy_find(legacy_WRITES, m.group(2))[0] is None))


legacy_MSG_ARG_RX = re.compile(
    r"(?:^|\s)(?:-m|--message(?:=|\s+))\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')",
    re.DOTALL)


def legacy_strip_message_args(command):
    """Drop quoted `-m` / `--message` payloads before judging.

    A commit message is data the VCS stores, never shell the host executes,
    yet it routinely carries the exact tokens the substring heuristic hunts
    for: governance file names in prose, and a `cd <memory-repo> &&` prefix
    supplies the directory hit from outside the string. Ordinary
    `git commit -m` commands were blocked whose only "write" was message
    text. The stripped span is the quoted string
    following the flag, single- or double-quoted, multi-line included.
    """
    return legacy_MSG_ARG_RX.sub(" ", command)


legacy_NULL_REDIRECT_RX = re.compile(
    r"(?<!\d)\d*>>?\s*(?:/dev/null|\$null|NUL\b|nul\b)", re.IGNORECASE)


def legacy_strip_null_redirects(command):
    """Drop `2>/dev/null` and friends before looking for a write primitive.

    Discarding output is not writing, and `2>/dev/null` rides along on ordinary read
    commands constantly -- `sed -n 1,50p CLAUDE.md 2>/dev/null` has no write in it at
    all, and blocking it teaches the reader to reach for the escape hatch on commands
    that never needed one.
    """
    return legacy_NULL_REDIRECT_RX.sub(" ", command)


legacy_ANGLE_EMAIL_RX = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def legacy_strip_angle_emails(command):
    """Drop `<name@host>` address tokens before looking for a write primitive.

    A commit trailer such as `Co-Authored-By: X <noreply@anthropic.com>` closes with
    `>`; when another trailer line follows it (`Claude-Session: https://...`), the
    closing bracket, the newline and the next line's first letter form exactly the
    redirection shape, so a commit message fed through a heredoc kept its body and
    the governance names in it blocked the commit. An address is never a write.
    """
    return legacy_ANGLE_EMAIL_RX.sub(" ", command)


legacy_SHELL_QUOTE = r"\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'"
legacy_SHELL_WORD = r"(?:" + legacy_SHELL_QUOTE + r"|[^\s;&|<>]+)"
legacy_SHELL_PART = re.compile(r"(?:" + legacy_SHELL_QUOTE + r"|[^\n;&|'\"])+")
legacy_SHELL_REDIRECT = re.compile(legacy_SHELL_QUOTE + r"|(?P<redirect>(?<![<>=-])>>?\s*" + legacy_SHELL_WORD + r")")
legacy_READ_ONLY = re.compile(
    r"^\s*(?:(?:grep|rg|wc|Get-Content|Select-String|Test-Path)\b|"
    r"git(?:\s+(?:-[cC]\s+" + legacy_SHELL_WORD + r"|--no-pager))*\s+"
    r"(?:status|diff|show|log|ls-files|ls-tree|rev-parse)\b)", re.I)


def legacy_strip_read_only_segments(command):
    """Keep read commands' redirects, and content cmdlets' paths, not prose."""
    def segment(m):
        part = m.group(0)
        if legacy_READ_ONLY.match(part) and not re.search(r"\$\(|`|[<>]\(|--pre\b", part):
            return " ".join(r.group("redirect") for r in legacy_SHELL_REDIRECT.finditer(part)
                            if r.group("redirect"))
        cmdlet = re.match(r"\s*(?:Set-Content|Add-Content|Out-File)\b", part, re.I)
        if cmdlet:
            target = re.search(r"\s+-(?:LiteralPath|Path|FilePath)\s+(" + legacy_SHELL_WORD + r")", part, re.I)
            if target:
                span = target.span(1)
            else:
                target = re.match(r"\s+(?!-)(" + legacy_SHELL_WORD + r")", part[cmdlet.end():])
                if not target:
                    return part  # Unknown argument order: retain the old heuristic.
                span = tuple(cmdlet.end() + i for i in target.span(1))
            return re.sub(legacy_SHELL_QUOTE, lambda q: q.group(0) if q.span() == span or "$(" in q.group(0)
                          or legacy_find(legacy_WRITES, q.group(0))[0] else " ", part)
        return part  # In particular, do not split up inline or heredoc'd code.
    return legacy_SHELL_PART.sub(segment, command)


def legacy_judge(command, cwd=""):
    """Return (kind, hit, primitive) when the command must be blocked, else None."""
    if not command:
        return None
    command = legacy_strip_angle_emails(command)
    command = legacy_strip_null_redirects(legacy_strip_data_heredocs(legacy_strip_message_args(command)))
    command = legacy_strip_read_only_segments(command)
    kind, hit = legacy_find(legacy_GOVERNANCE, command)
    if not kind:
        kind, hit = legacy_find(legacy_GOV_DIRS, command + "\n" + cwd)
        if kind:
            m = legacy_BARE_MD_RX.search(command)
            if not m:
                return None
            hit = "%s + %s" % (hit, m.group(0))
    if not kind:
        return None
    prim, _ = legacy_find(legacy_WRITES, command)
    if not prim:
        return None
    return kind, hit, prim




def judge(command, cwd=""):
    """Additive detection: structural analysis can never exempt a legacy hit."""
    return legacy_judge(command, cwd) or structural_judge(command, cwd)


SEEN_TTL_S = 900


def seen_path():
    override = os.environ.get("CLAUDE_GOV_BASH_WRITE_SEEN")
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "claude-governance-bash-write-seen.json")


def _load_seen():
    try:
        with open(seen_path(), encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}


MAX_ENTRIES = 1000


def _write_seen(seen):
    """Publish complete JSON atomically; concurrent lost updates cost a resend."""
    tmp = None
    try:
        path = os.path.abspath(seen_path())
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(tmp, path)
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def repeat_of_a_blocked_command(command, now=None, identity="", cwd=""):
    """True when this exact command was blocked recently, and clears the record.

    The first attempt is refused and remembered; an identical second attempt goes
    through. Anyone who was going to switch to Edit/Write has already switched by
    then, and anyone who means it can say so by repeating themselves rather than
    being held at the door.
    """
    now = time.time() if now is None else now
    keytext = str(identity) + "\x00" + os.path.normcase(os.path.abspath(cwd or ".")) + "\x00" + command
    key = hashlib.sha1(keytext.encode("utf-8", "replace")).hexdigest()
    seen = _load_seen()
    ts = seen.get(key)
    fresh = {k: v for k, v in seen.items() if isinstance(v, (int, float))
             and now - v <= SEEN_TTL_S and k != key}
    hit = isinstance(ts, (int, float)) and (now - ts) <= SEEN_TTL_S
    if not hit:
        fresh[key] = now
    fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[:MAX_ENTRIES])
    _write_seen(fresh)
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
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name", "") not in ("Bash", "PowerShell"):
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str):
        return 0
    cwd = str(payload.get("cwd") or "")
    verdict = judge(command, cwd)
    if verdict and not repeat_of_a_blocked_command(
            command, identity=payload.get("agent_id") or payload.get("session_id") or "", cwd=cwd):
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
