#!/usr/bin/env python3
"""Write a zsh completion for pi to stdout.

    ~/.pi/completion-by-grok.py > ~/.cache/zsh/pi-completion-by-grok.zsh
    source ~/.cache/zsh/pi-completion-by-grok.zsh

Runs `pi --help`, then `pi <command> --help` for every subcommand that
page names. Nested commands such as `pi auth print-api-key` are learned
from the parent page: asking them for `--help` reprints the top-level
help, so that output is ignored.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

PLACEHOLDERS = frozenset(
    {
        "source",
        "sources",
        "option",
        "options",
        "command",
        "commands",
        "arg",
        "args",
        "argument",
        "arguments",
        "file",
        "files",
        "message",
        "messages",
        "name",
        "names",
        "path",
        "dir",
        "directory",
        "folder",
        "text",
        "pattern",
        "patterns",
        "id",
        "key",
        "value",
        "mode",
        "level",
        "search",
        "url",
        "server",
        "tool",
        "tools",
        "model",
        "models",
        "provider",
        "duration",
        "seconds",
        "port",
        "target",
        "targets",
        "package",
        "packages",
        "input",
        "output",
        "secret",
    }
)
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "for",
        "and",
        "with",
        "from",
        "of",
        "on",
        "in",
        "by",
        "or",
        "nor",
        "but",
        "if",
        "as",
        "at",
        "via",
        "per",
        "not",
    }
)
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
ENUM_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+.@/-]*$")
HEADER_RE = re.compile(r"^([A-Z][A-Za-z0-9 /_-]+):$")
TOOL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s+-\s*(.*)$")
FLAG_RE = re.compile(r"^(?:--[A-Za-z0-9]|-[A-Za-z0-9])")
SKIP_TOKENS = frozenset({"--", "[options]", "[option]", "[flags]", "[--]"})


@dataclass
class Opt:
    flags: list[str]
    desc: str
    meta: str
    optional: bool
    repeat: bool
    action: str
    enum: list[str]


@dataclass
class Pos:
    msg: str
    optional: bool
    repeat: bool
    action: str
    choices: list[str]
    at: bool


@dataclass
class Node:
    name: str
    desc: str = ""
    opts: list[Opt] = field(default_factory=list)
    pos: list[Pos] = field(default_factory=list)
    children: list[Node] = field(default_factory=list)
    synopsis: str = ""
    parent: Node | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        names: list[str] = []
        node: Node | None = self
        while node is not None and node.parent is not None:
            names.append(node.name)
            node = node.parent
        return tuple(reversed(names))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_ident(word: str) -> bool:
    return IDENT_RE.match(word) is not None


def is_placeholder(word: str) -> bool:
    return word.lower() in PLACEHOLDERS


def word_has(name: str, text: str) -> bool:
    return (
        re.search(
            r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])",
            text,
        )
        is not None
    )


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] in "[<":
            token, i = read_group(text, i)
            if token:
                out.append(token)
            continue
        j = i + 1
        while j < n and not text[j].isspace():
            j += 1
        token = text[i:j].rstrip(",")
        if token:
            out.append(token)
        i = j
    return out


def read_group(text: str, i: int) -> tuple[str, int]:
    opener = text[i]
    closer = "]" if opener == "[" else ">"
    depth = 1
    j = i + 1
    while j < len(text) and depth:
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
        j += 1
    return text[i:j], j


def split_top(text: str, sep: str) -> list[str]:
    """Split on sep outside <> and [] groups."""
    parts: list[str] = []
    buf: list[str] = []
    stack: list[str] = []
    pairs = {"[": "]", "<": ">"}
    for ch in text:
        if stack and ch == stack[-1]:
            stack.pop()
            buf.append(ch)
            continue
        if ch in pairs:
            stack.append(pairs[ch])
            buf.append(ch)
            continue
        if ch == sep and not stack:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def split_2col(line: str) -> tuple[str, str]:
    """Split a help row into spec and description.

    pi pads the gap with two spaces. A long flag can leave only one
    (`-na, --no-approve Ignore...`); then the description is the first
    word that is not a flag or a <meta>/[meta] group.
    """
    text = line.replace("\t", " ").strip()
    match = re.match(r"^(\S+(?: \S+)*) {2,}(.*)$", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] in "-<[":
            if text[i] in "<[":
                _, i = read_group(text, i)
            else:
                i += 1
                while i < n and not text[i].isspace():
                    i += 1
            continue
        return text[:i].strip(), text[i:].strip()
    return text, ""


def strip_parens(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def enum_list(text: str) -> list[str] | None:
    """A choice list is `a, b, or c`, or exactly `a or b`.

    A longer "or" sentence ("file or partial UUID") is prose.
    """
    raw = strip_parens(text).strip()
    if not raw:
        return None
    if "," not in raw:
        if raw.count(" or ") != 1:
            return None
        if len(raw.replace(" or ", " ").split()) != 2:
            return None
    flat = raw.replace(", or ", ",").replace(" or ", ",").replace(",", " ")
    words = flat.split()
    if not 2 <= len(words) <= 16:
        return None
    for word in words:
        if len(word) > 32 or not ENUM_WORD_RE.match(word):
            return None
        if word.lower() in STOPWORDS:
            return None
    return words


def enum_words(text: str) -> list[str] | None:
    # "Set thinking level: off, low, high" — the label is not a choice.
    if ": " in text:
        return enum_list(text.rpartition(": ")[2])
    return enum_list(text)


def infer_action(meta: str) -> str:
    base = meta.split("|", 1)[0].split("[", 1)[0].lower()
    if base in {"dir", "directory", "cwd", "folder"}:
        return "dirs"
    if base in {"file", "path", "source"}:
        return "files"
    if base in {"tool", "tools"}:
        return "tools"
    if base in {"command", "commands"}:
        return "command"
    if "path" in base or "file" in base:
        return "files"
    if base.endswith("dir") or base.endswith("directory"):
        return "dirs"
    return ""


def normalize_meta(meta: str) -> str:
    text = meta.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text.endswith("..."):
        text = text[:-3]
    # <name[/name]> is "name" plus an optional /name, not two labels.
    text = text.split("[", 1)[0].strip()
    return text


def is_repeat_desc(desc: str) -> bool:
    low = desc.lower()
    return "multiple times" in low or "repeatable" in low


def sentence_for(flag: str, notes: str) -> str:
    if not notes or not flag:
        return ""
    pattern = re.compile(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])")
    for sentence in re.split(r"\.\s+", notes):
        if pattern.search(sentence):
            return clean(sentence.rstrip("."))
    return ""


def has_flag(node: Node, flag: str) -> bool:
    return any(flag in opt.flags for opt in node.opts)


def add_option(
    node: Node,
    flags: list[str],
    desc: str,
    meta: str,
    optional: bool,
    tools: list[tuple[str, str]],
) -> None:
    flags = [flag for flag in flags if flag == "--" or FLAG_RE.match(flag)]
    flags = [flag for flag in flags if flag != "--"]
    if not flags:
        return
    if any(has_flag(node, flag) for flag in flags):
        return
    desc = clean(desc)
    meta = normalize_meta(meta)
    enum = enum_words(desc) or []
    action = ""
    if len(enum) >= 2:
        action = "enum"
        if ": " in desc:
            label = desc.rpartition(": ")[0].strip()
            if label:
                desc = label
    else:
        action = infer_action(meta)
        if action == "tools" and not tools:
            action = ""
    node.opts.append(
        Opt(
            flags=flags,
            desc=desc,
            meta=meta,
            optional=optional,
            repeat=is_repeat_desc(desc),
            action=action,
            enum=enum,
        )
    )


def add_pos(node: Node, pos: Pos) -> None:
    for old in node.pos:
        if (
            old.msg == pos.msg
            and old.optional == pos.optional
            and old.repeat == pos.repeat
            and old.action == pos.action
            and old.choices == pos.choices
            and old.at == pos.at
        ):
            return
    node.pos.append(pos)


def ensure_child(parent: Node, name: str, desc: str = "") -> Node:
    desc = clean(desc)
    for child in parent.children:
        if child.name == name:
            if desc and not child.desc:
                child.desc = desc
            return child
    child = Node(name, desc, parent=parent)
    parent.children.append(child)
    return child


def child_by_name(parent: Node, name: str) -> Node | None:
    for child in parent.children:
        if child.name == name:
            return child
    return None


def parse_pos_token(token: str) -> Pos | None:
    optional = token.startswith("[") and token.endswith("]")
    inner = token[1:-1] if optional else token
    if inner.startswith("<") and inner.endswith(">"):
        inner = inner[1:-1]
    repeat = inner.endswith("...")
    if repeat:
        inner = inner[:-3]
    at = inner.startswith("@")
    if at:
        inner = inner[1:]
    if not inner:
        return None
    metas: list[str] = []
    literals: list[str] = []
    for part in split_top(inner, "|"):
        part = part.strip("<>@ ")
        if not part:
            continue
        if is_placeholder(part) or " " in part:
            metas.append(part)
        elif is_ident(part):
            literals.append(part)
    action = ""
    choices: list[str] = []
    if metas and literals:
        msg = metas[0]
        choices = literals
        action = infer_action(msg) or "files"
    elif literals:
        msg = "target"
        choices = literals
        action = "enum"
    elif metas:
        msg = metas[0].split("|", 1)[0]
        action = infer_action(msg)
    else:
        return None
    if msg == "files" and at:
        action = "atfiles"
    return Pos(msg, optional, repeat, action, choices, at)


def mine_flags(node: Node, spec: str, notes: str, tools: list[tuple[str, str]]) -> None:
    raw = spec.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    for alt in split_top(raw, "|"):
        flags: list[str] = []
        meta = ""
        optional = False
        for bit in tokenize(alt):
            if bit == "--" or FLAG_RE.match(bit):
                if bit != "--":
                    flags.append(bit)
            elif bit.startswith("<") and bit.endswith(">"):
                meta = bit
            elif bit.startswith("[") and bit.endswith("]"):
                meta = bit
                optional = True
        if not flags:
            continue
        add_option(node, flags, sentence_for(flags[-1], notes), meta, optional, tools)


def is_flagish(token: str) -> bool:
    return token.startswith("-") or token.startswith("[-") or token.startswith("[--")


def apply_tail(
    node: Node,
    tokens: list[str],
    notes: str,
    tools: list[tuple[str, str]],
) -> None:
    found: list[Pos] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token or token in SKIP_TOKENS:
            i += 1
            continue
        if is_flagish(token):
            spec = token
            # `--url <url>` is one option. A bracket group already holds its metavar.
            if "<" not in token and not token.startswith("["):
                nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
                if (nxt.startswith("<") and nxt.endswith(">")) or (
                    nxt.startswith("[") and nxt.endswith("]")
                ):
                    spec = f"{token} {nxt}"
                    i += 1
            mine_flags(node, spec, notes, tools)
            i += 1
            continue
        pos = parse_pos_token(token)
        if pos:
            found.append(pos)
        i += 1
    msgs = {pos.msg for pos in found}
    if "command" in msgs and msgs & {"arg", "args", "arguments"}:
        kept: list[Pos] = []
        for pos in found:
            if pos.msg in {"arg", "args", "arguments"}:
                continue
            if pos.msg == "command":
                pos.repeat = True
                pos.action = "normal"
            kept.append(pos)
        found = kept
    for pos in found:
        add_pos(node, pos)


def parse_option_block(body: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    def commit() -> None:
        nonlocal current
        if current and current.get("flags"):
            records.append(current)
        current = None

    for line in body.splitlines():
        if not line.strip():
            commit()
            continue
        stripped = line.strip()
        if stripped.startswith("-"):
            commit()
            spec, desc = split_2col(stripped)
            flags: list[str] = []
            meta = ""
            optional = False
            for bit in tokenize(spec):
                if bit == "--" or FLAG_RE.match(bit):
                    if bit != "--":
                        flags.append(bit)
                elif bit.startswith("<") and bit.endswith(">"):
                    meta = bit
                elif bit.startswith("[") and bit.endswith("]"):
                    meta = bit
                    optional = True
            if not flags:
                continue
            current = {
                "flags": flags,
                "desc": desc,
                "meta": meta,
                "optional": optional,
            }
        elif current is not None:
            current["desc"] = clean(str(current["desc"]) + " " + stripped)
    commit()
    return records


def option_targets(header: str, desc: str, node: Node) -> list[Node]:
    title = header.lower().strip()
    kids = node.children
    if title in {"option", "options"}:
        return [node]
    if title == "other options":
        named = [kid for kid in kids if word_has(kid.name, desc)]
        if named:
            return named
        return list(kids) or [node]
    named = [kid for kid in kids if word_has(kid.name, header)]
    return named or [node]


def parse_commands(body: str, node: Node, bin_name: str) -> None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        spec, desc = split_2col(stripped)
        bits = tokenize(spec)
        if not bits:
            continue
        idx = 1 if bits[0] == bin_name else 0
        if idx >= len(bits) or not is_ident(bits[idx]):
            continue
        child = ensure_child(node, bits[idx], desc)
        tail = bits[idx + 1 :]
        if tail and not child.synopsis:
            child.synopsis = " ".join(tail)


def parse_tools(body: str, tools: list[tuple[str, str]]) -> None:
    known = {name for name, _desc in tools}
    for line in body.splitlines():
        match = TOOL_RE.match(line.strip())
        if not match:
            continue
        name, desc = match.group(1), clean(match.group(2))
        if name not in known:
            tools.append((name, desc))
            known.add(name)


def sections_of(text: str) -> tuple[str, list[tuple[str, str]]]:
    preamble: list[str] = []
    order: list[tuple[str, str]] = []
    header: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf)
        if header is None:
            if body.strip():
                preamble.append(body)
        else:
            order.append((header, body))
        buf = []

    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = HEADER_RE.match(line.strip())
        if match and line.strip() == match.group(0):
            flush()
            header = match.group(1)
            continue
        buf.append(line)
    flush()
    return clean(" ".join(preamble)), order


def usage_lines_of(body: str, bin_name: str) -> tuple[list[str], str]:
    lines: list[str] = []
    notes: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == bin_name or stripped.startswith(bin_name + " "):
            lines.append(stripped)
        else:
            notes.append(stripped)
    return lines, clean(" ".join(notes))


def common_prefix(token_lines: list[list[str]], bin_name: str) -> int:
    if not token_lines:
        return 0
    limit = min(len(tokens) for tokens in token_lines)
    taken = 0
    while taken < limit:
        word = token_lines[0][taken]
        if any(len(tokens) <= taken or tokens[taken] != word for tokens in token_lines):
            break
        if taken == 0 and word == bin_name:
            taken += 1
            continue
        if is_ident(word):
            taken += 1
            continue
        break
    return taken


def parse_page(
    text: str,
    node: Node,
    bin_name: str,
    tools: list[tuple[str, str]],
) -> str:
    _preamble, order = sections_of(text)
    usage_body = ""
    notes_parts: list[str] = []
    if _preamble:
        notes_parts.append(_preamble)
    for header, body in order:
        if header == "Usage":
            usage_body = body
            break
    usage, usage_notes = usage_lines_of(usage_body, bin_name)
    if usage_notes:
        notes_parts.append(usage_notes)
    notes = clean(" ".join(notes_parts))
    if usage:
        first = tokenize(usage[0])
        if first:
            bin_name = first[0]

    for header, body in order:
        if "tool name" in header.lower():
            parse_tools(body, tools)
    for header, body in order:
        if header == "Commands":
            parse_commands(body, node, bin_name)

    opt_sections: list[tuple[str, list[dict[str, object]]]] = []
    for header, body in order:
        if "option" not in header.lower():
            continue
        opt_sections.append((header, parse_option_block(body)))
    for header, records in opt_sections:
        for rec in records:
            flags = list(rec["flags"])  # type: ignore[arg-type]
            desc = str(rec["desc"])
            meta = str(rec["meta"])
            optional = bool(rec["optional"])
            for target in option_targets(header, desc, node):
                add_option(target, flags, desc, meta, optional, tools)

    token_lines = [tokenize(line) for line in usage]
    prefix = common_prefix(token_lines, bin_name)
    leads: list[str] = []
    rests: list[list[str]] = []
    for tokens in token_lines:
        rest = tokens[prefix:]
        rests.append(rest)
        if rest and is_ident(rest[0]):
            leads.append(rest[0])
    multi = len(set(leads)) > 1
    have_commands = any(header == "Commands" for header, _body in order)
    for rest in rests:
        target = node
        if rest and is_ident(rest[0]):
            lead = rest[0]
            known = child_by_name(node, lead)
            if known is not None or (multi and not have_commands):
                target = known if known is not None else ensure_child(node, lead)
                rest = rest[1:]
        apply_tail(target, rest, notes, tools)
    return bin_name


def apply_pending(node: Node, tools: list[tuple[str, str]]) -> None:
    if node.synopsis and not node.opts and not node.pos:
        apply_tail(node, tokenize(node.synopsis), "", tools)
        node.synopsis = ""
    for child in node.children:
        apply_pending(child, tools)


def run_help(bin_path: str, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            [bin_path, *args, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def documents_command(text: str, command: str, bin_name: str) -> bool:
    """True when this help page is for `command` itself.

    `pi auth --help` is a menu of print-api-key, print-bearer-token, and
    check. That page belongs to `auth`, not to one of those children.
    `pi <child> --help` often reprints the top-level `pi [options]` page;
    that must not replace the child learned from the parent.
    """
    if "Usage:" not in text and "Options:" not in text:
        return False
    _preamble, order = sections_of(text)
    usage_body = ""
    for header, body in order:
        if header == "Usage":
            usage_body = body
            break
    usage, _notes = usage_lines_of(usage_body, bin_name)
    if not usage:
        return False
    token_lines = [tokenize(line) for line in usage]
    if not token_lines or not token_lines[0]:
        return False
    if len(token_lines[0]) >= 2 and token_lines[0][1] == "[options]":
        return False
    shared = token_lines[0][: common_prefix(token_lines, bin_name)]
    command_words = [word for word in shared[1:] if is_ident(word)]
    if command in command_words:
        return True
    # `pi uninstall --help` documents `pi remove` and mentions the alias.
    if len(usage) == 1 and word_has(command, text):
        return True
    return False


def enrich(
    node: Node,
    bin_path: str,
    bin_name: str,
    tools: list[tuple[str, str]],
) -> None:
    # One level: `pi <command> --help`. Deeper commands are described on
    # that page; `pi auth print-api-key --help` reprints the top-level help.
    for child in list(node.children):
        if len(child.parts) != 1:
            continue
        text = run_help(bin_path, [child.name])
        if not text or not documents_command(text, child.name, bin_name):
            continue
        child.opts.clear()
        child.pos.clear()
        child.synopsis = ""
        parse_page(text, child, bin_name, tools)


def sq_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("]", "\\]").replace("'", "'\\''")


def describe_escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace(":", "\\:")
    text = text.replace("[", "(").replace("]", ")")
    return text.replace("'", "'\\''")


def sanitize_label(text: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_|.=-]", "-", text).strip("-")
    return label or "value"


def fn_name(bin_s: str, parts: tuple[str, ...]) -> str:
    if not parts:
        return f"_{bin_s}"
    tail = "_".join(re.sub(r"[^A-Za-z0-9]", "_", part) for part in parts)
    return f"_{bin_s}_{tail}"


def opt_suffix(opt: Opt, tools_fn: str) -> str:
    if not opt.meta and not opt.action and not opt.enum:
        return ""
    colons = "::" if opt.optional else ":"
    meta = sanitize_label(opt.meta or "value")
    if opt.action == "files":
        return f"{colons}{meta}:_files"
    if opt.action == "dirs":
        return f"{colons}{meta}:_files -/"
    if opt.action == "tools":
        # A leading space runs the function as itself. _arguments would
        # otherwise prepend compadd options, which _values does not expect.
        return f"{colons}{meta}: {tools_fn}"
    if opt.action == "enum" and opt.enum:
        return f"{colons}{meta}:({' '.join(opt.enum)})"
    if opt.action == "command":
        return f"{colons}{meta}:_command_names"
    if opt.action == "normal":
        return f"{colons}{meta}:_normal"
    return f"{colons}{meta}:"


def emit_opt(opt: Opt, tools_fn: str) -> str | None:
    shorts = [flag for flag in opt.flags if not flag.startswith("--")]
    longs = [flag for flag in opt.flags if flag.startswith("--")]
    flags = shorts + longs
    if not flags:
        return None
    suffix = opt_suffix(opt, tools_fn)
    bracket = f"[{sq_escape(opt.desc)}]" if opt.desc else ""
    if len(flags) == 1:
        star = "*" if opt.repeat else ""
        return f"'{star}{flags[0]}{bracket}{suffix}'"
    joined = ",".join(flags)
    group = "{" + joined + "}"
    if opt.repeat:
        if bracket or suffix:
            return "'*'" + group + f"'{bracket}{suffix}'"
        return "'*'" + group
    excl = "(" + " ".join(flags) + ")"
    if bracket or suffix:
        return f"'{excl}'" + group + f"'{bracket}{suffix}'"
    return f"'{excl}'" + group


def pos_action(pos: Pos, helper: str, at_fn: str, tools_fn: str) -> str:
    if pos.choices and pos.action == "files":
        return f": {helper}"
    if pos.choices and pos.action == "enum":
        return ":(" + " ".join(pos.choices) + ")"
    if pos.action == "atfiles":
        return f": {at_fn}"
    if pos.action == "files":
        return ":_files"
    if pos.action == "dirs":
        return ":_files -/"
    if pos.action == "normal":
        return ":_normal"
    if pos.action == "command":
        return ":_command_names"
    if pos.action == "tools":
        return f": {tools_fn}"
    return ":"


def emit_pos_spec(pos: Pos, helper: str, at_fn: str, tools_fn: str) -> str:
    if pos.repeat:
        star, colons = "*", ":"
    elif pos.optional:
        star, colons = "", "::"
    else:
        star, colons = "", ":"
    msg = sanitize_label(pos.msg)
    return f"'{star}{colons}{msg}{pos_action(pos, helper, at_fn, tools_fn)}'"


def wants_atfiles(node: Node) -> bool:
    return any(pos.action == "atfiles" or pos.at for pos in node.pos)


def any_atfiles(node: Node) -> bool:
    return wants_atfiles(node) or any(any_atfiles(child) for child in node.children)


def emit_pos_helpers(
    node: Node,
    bin_s: str,
    at_fn: str,
    tools_fn: str,
    lines: list[str],
) -> None:
    del at_fn, tools_fn
    n = 0
    for pos in node.pos:
        if not (pos.choices and pos.action == "files"):
            continue
        n += 1
        helper = f"{fn_name(bin_s, node.parts)}_{n}"
        msg = sanitize_label(pos.msg)
        choice = " ".join(pos.choices)
        lines.append(f"{helper}() {{")
        lines.append("  _alternative \\")
        lines.append(f"    'choices:{msg}:({choice})' \\")
        lines.append(f"    'files:{msg}:_files'")
        lines.append("}")
        lines.append("")


def helper_for(node: Node, bin_s: str, index: int) -> str:
    return f"{fn_name(bin_s, node.parts)}_{index}"


def emit_node(
    node: Node,
    bin_s: str,
    at_fn: str,
    tools_fn: str,
    lines: list[str],
) -> None:
    fn = fn_name(bin_s, node.parts)
    if node.children:
        lines.append(f"{fn}() {{")
        lines.append(
            '  local curcontext="$curcontext" context state state_descr line ret=1'
        )
        lines.append("  typeset -A opt_args")
        lines.append("  local -a commands")
        lines.append("  commands=(")
        for child in node.children:
            if child.desc:
                lines.append(f"    '{child.name}:{describe_escape(child.desc)}'")
            else:
                lines.append(f"    '{child.name}'")
        lines.append("  )")
        specs = ['  _arguments -S -C -A "-*" \\']
        # -s is omitted on purpose: pi shorts are words (`-nt`, `-na`), not clusters.
        for opt in node.opts:
            spec = emit_opt(opt, tools_fn)
            if spec:
                specs.append(f"    {spec} \\")
        specs.append("    '1: :->cmd' \\")
        # `&& ret=0` matters. _arguments lists options and returns success,
        # but this function used to ignore that and return 1. A matcher-list
        # with more than one spec then retries, and a second _describe of the
        # same options splits flags and descriptions into separate columns.
        specs.append("    '*:: :->args' && ret=0")
        lines.extend(specs)
        lines.append("  case $state in")
        lines.append("    (cmd)")
        lines.append("      _describe -t commands 'command' commands && ret=0")
        if wants_atfiles(node):
            lines.append(f"      {at_fn} && ret=0")
        lines.append("      ;;")
        lines.append("    (args)")
        lines.append("      case $line[1] in")
        for child in node.children:
            child_fn = fn_name(bin_s, child.parts)
            lines.append(f"        ({child.name}) {child_fn} && ret=0 ;;")
        if wants_atfiles(node):
            lines.append(f"        (*) {at_fn} && ret=0 ;;")
        lines.append("      esac")
        lines.append("      ;;")
        lines.append("  esac")
        lines.append("  return ret")
        lines.append("}")
        lines.append("")
        return

    lines.append(f"{fn}() {{")
    lines.append("  local context state state_descr line")
    lines.append("  typeset -A opt_args")
    specs = ["  _arguments -S \\"]
    for opt in node.opts:
        spec = emit_opt(opt, tools_fn)
        if spec:
            specs.append(f"    {spec} \\")
    helper_n = 0
    for pos in node.pos:
        helper = ""
        if pos.choices and pos.action == "files":
            helper_n += 1
            helper = helper_for(node, bin_s, helper_n)
        specs.append(f"    {emit_pos_spec(pos, helper, at_fn, tools_fn)} \\")
    if len(specs) == 1:
        specs.append('    \'*: :_message "no further arguments"\' \\')
    specs[-1] = specs[-1][:-2] if specs[-1].endswith(" \\") else specs[-1]
    lines.extend(specs)
    lines.append("}")
    lines.append("")


def emit_tree(
    node: Node,
    bin_s: str,
    at_fn: str,
    tools_fn: str,
    lines: list[str],
) -> None:
    emit_pos_helpers(node, bin_s, at_fn, tools_fn, lines)
    emit_node(node, bin_s, at_fn, tools_fn, lines)
    for child in node.children:
        emit_tree(child, bin_s, at_fn, tools_fn, lines)


def render(root: Node, bin_name: str, tools: list[tuple[str, str]]) -> str:
    bin_s = re.sub(r"[^A-Za-z0-9]", "_", bin_name) or "pi"
    tools_fn = f"_{bin_s}_tools"
    at_fn = f"_{bin_s}_atfiles"
    lines = [
        f"#compdef {bin_name}",
        f"# Generated by ~/.pi/completion-by-grok.py from `{bin_name} --help`.",
        "# Refresh:",
        f"#   ~/.pi/completion-by-grok.py > ~/.cache/zsh/{bin_name}-completion-by-grok.zsh",
        "",
    ]
    if tools:
        lines.append(f"{tools_fn}() {{")
        lines.append("  _values -s , 'tool' \\")
        for index, (name, desc) in enumerate(tools):
            slash = " \\" if index < len(tools) - 1 else ""
            if desc:
                lines.append(f"    '{name}[{sq_escape(desc)}]'{slash}")
            else:
                lines.append(f"    '{name}'{slash}")
        lines.append("}")
        lines.append("")
    if any_atfiles(root):
        lines.append(f"{at_fn}() {{")
        lines.append('  [[ $PREFIX == @* ]] || return 1')
        lines.append('  PREFIX=${PREFIX#@}')
        lines.append("  _files -P @")
        lines.append("}")
        lines.append("")
    emit_tree(root, bin_s, at_fn, tools_fn, lines)
    lines.append(f"compdef _{bin_s} {bin_name}")
    lines.append("")
    return "\n".join(lines)


def build() -> str:
    bin_path = shutil.which("pi")
    if not bin_path:
        raise SystemExit("error: pi not found on PATH")
    text = run_help(bin_path, [])
    if "Usage:" not in text and "Options:" not in text:
        raise SystemExit("error: `pi --help` did not return help text")
    tools: list[tuple[str, str]] = []
    root = Node("root")
    bin_name = "pi"
    _preamble, order = sections_of(text)
    for header, body in order:
        if header == "Usage":
            usage, _notes = usage_lines_of(body, bin_name)
            if usage:
                first = tokenize(usage[0])
                if first:
                    bin_name = first[0]
            break
    bin_name = parse_page(text, root, bin_name, tools)
    enrich(root, bin_path, bin_name, tools)
    apply_pending(root, tools)
    if not root.opts and not root.children:
        raise SystemExit("error: no options or commands found in `pi --help`")
    return render(root, bin_name, tools)


def main(argv: list[str]) -> int:
    if any(arg in {"-h", "--help"} for arg in argv[1:]):
        print(__doc__.strip())
        return 0
    if len(argv) > 1:
        print(f"unexpected argument: {argv[1]}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    sys.stdout.write(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
