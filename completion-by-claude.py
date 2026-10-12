#!/usr/bin/env python3
"""Generate a zsh completion script for the `pi` coding agent CLI.

Runs `pi --help`, discovers subcommands, runs `pi <cmd> --help` (and
`pi <cmd> <sub> --help` for nested commands), parses usage lines, command
tables and option tables, and prints a zsh completion script to stdout.

Usage:
    ~/.pi/completion-by-claude.py > ~/.cache/zsh/pi-completion-by-claude.zsh
    source ~/.cache/zsh/pi-completion-by-claude.zsh
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

PROG = "pi"

# ---------------------------------------------------------------------------
# running pi


def run_help(pi, args):
    env = dict(os.environ, PI_OFFLINE="1", NO_COLOR="1", TERM="dumb")
    try:
        out = subprocess.run(
            [pi, *args, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=30,
            text=True,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        sys.exit(f"completion-by-claude: failed to run {pi} {' '.join(args)} --help: {e}")
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out)


# ---------------------------------------------------------------------------
# parsing help text

FLAG = r"-{1,2}[A-Za-z][\w-]*"
OPT_LINE = re.compile(
    rf"^(?P<flags>{FLAG}(?:,\s*{FLAG})*)"
    rf"(?:\s+(?P<arg><[^>]+>|\[[^\]]+\]))?"
    rf"(?:,\s*(?P<flags2>{FLAG}(?:,\s*{FLAG})*))?"
    rf"(?:\s+(?P<desc>.*))?$"
)


class Option:
    def __init__(self, names, arg=None, optional_arg=False, desc=""):
        self.names = names  # e.g. ['--print', '-p']
        self.arg = arg  # argument name, e.g. 'path'
        self.optional_arg = optional_arg
        self.desc = desc

    @property
    def long(self):
        return max(self.names, key=len)

    def merge(self, other):
        for n in other.names:
            if n not in self.names:
                self.names.append(n)
        self.arg = self.arg or other.arg
        self.optional_arg = self.optional_arg and other.optional_arg
        if not self.desc:
            self.desc = other.desc


class Help:
    def __init__(self, text):
        self.text = text
        self.usage = []  # usage lines, stripped
        self.sections = {}  # header -> list of (indent, text) lines
        self.prose = []  # free-text lines outside sections
        self._split(text)

    def _split(self, text):
        section = None
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                if section == "Usage":
                    section = None
                continue
            m = re.match(r"^(\S[^:]*):\s*$", line)
            if m:
                section = m.group(1)
                self.sections.setdefault(section, [])
                continue
            if section == "Usage":
                self.usage.append(line.strip())
            elif section and line.startswith(" "):
                indent = len(line) - len(line.lstrip())
                self.sections[section].append((indent, line.strip()))
            else:
                section = None
                self.prose.append(line.strip())

    def entries(self, header):
        """Group a section's lines into (head, description) entries.

        Continuation lines (more deeply indented) are appended to the previous
        entry's description."""
        out = []
        base = None
        for indent, text in self.sections.get(header, []):
            if base is None or indent <= base:
                base = indent
                out.append([text, ""])
            elif out:
                out[-1][1] = (out[-1][1] + " " + text).strip()
        return out

    def option_sections(self):
        return [h for h in self.sections if re.search(r"\boptions\b", h, re.I)]

    def parse_options(self, header):
        opts = []
        for head, extra in self.entries(header):
            if head.startswith("--") and head[2:3] in ("", " "):
                continue  # bare `--`
            m = OPT_LINE.match(head)
            if not m:
                continue
            names = re.findall(FLAG, m.group("flags"))
            if m.group("flags2"):
                names += re.findall(FLAG, m.group("flags2"))
            arg = m.group("arg")
            optional = bool(arg and arg.startswith("["))
            desc = ((m.group("desc") or "") + " " + extra).strip()
            opts.append(Option(names, arg[1:-1] if arg else None, optional, desc))
        return opts

    def prose_mentioning(self, flag):
        text = " ".join(self.prose)
        for sentence in re.split(r"(?<=\.)\s+", text):
            if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", sentence):
                return sentence.rstrip(".")
        return ""


def usage_options(usage_tail):
    """Options mentioned in a usage line, e.g. `[--provider <provider>] [-l]`."""
    opts = []
    for m in re.finditer(rf"(?<![\w-])({FLAG}(?:\|{FLAG})*)(?:\s+<([^>]+)>)?", usage_tail):
        for name in m.group(1).split("|"):
            opts.append(Option([name], m.group(2)))
    return opts


def usage_positionals(usage_tail):
    """Positional args from a usage line tail: list of (name, required, literals, rest)."""
    out = []
    tail = re.sub(rf"\[?{FLAG}(?:\|{FLAG})*(?:\s+<[^>]+>)?\]?", " ", usage_tail.split(" -- ")[0])
    for m in re.finditer(r"<([^>]+)>|\[([^\]]+)\]", tail):
        if m.group(1):
            out.append((m.group(1), True, [], False))
        else:
            inner = m.group(2)
            if inner in ("options",):
                continue
            rest = inner.endswith("...")
            parts = inner.rstrip(".").split("|")
            out.append((parts[0].lstrip("@"), False, parts[1:], rest))
    return out


# ---------------------------------------------------------------------------
# command model


class Command:
    def __init__(self, path, desc=""):
        self.path = path  # e.g. ['mcp', 'add']
        self.desc = desc
        self.options = []
        self.positionals = []
        self.subcommands = []
        self.passthrough = False  # has `-- <command> [args...]`

    @property
    def fname(self):
        return "_" + "_".join([PROG, *self.path]).replace("-", "_")

    def add_option(self, opt):
        for o in self.options:
            if set(o.names) & set(opt.names):
                o.merge(opt)
                return
        self.options.append(opt)


def discover(pi):
    top_help = Help(run_help(pi, []))
    root = Command([])
    for o in (o for h in top_help.option_sections() for o in top_help.parse_options(h)):
        root.add_option(o)
    root.tools = []
    for head, _ in top_help.entries("Built-in Tool Names"):
        m = re.match(r"^([\w-]+)\s+-\s+(.*)$", head)
        if m:
            root.tools.append((m.group(1), m.group(2)))

    for head, extra in top_help.entries("Commands"):
        m = re.match(rf"^{PROG}\s+([a-z][\w-]*)\b(.*?)(?:\s{{2,}}(.*))?$", head)
        if not m:
            continue
        name, desc = m.group(1), (m.group(3) or "") + (" " + extra if extra else "")
        cmd = Command([name], desc.strip())
        text = run_help(pi, [name])
        if text.strip() == top_help.text.strip():
            continue  # pi didn't recognise it as a subcommand
        build_command(pi, cmd, Help(text))
        root.subcommands.append(cmd)
    return root


def build_command(pi, cmd, h):
    prefix = " ".join([PROG, *cmd.path])
    # usage lines belonging to this command (an alias may print its target's usage)
    usage = [u for u in h.usage if u.startswith(PROG + " ")]
    own = [u[len(prefix):] for u in usage if u == prefix or u.startswith(prefix + " ")]
    if not own and usage:
        # e.g. `pi uninstall --help` prints `pi remove ...`; use the first line's tail
        first = usage[0].split()
        own = [" " + " ".join(first[1 + len(cmd.path):])]

    # nested subcommands: `pi mcp add <server>`, `pi auth check [...]`
    nested = {}
    for tail in own:
        m = re.match(r"^\s+([a-z][\w-]*)\b(.*)$", tail)
        if m:
            nested.setdefault(m.group(1), []).append(m.group(2))
    if len(nested) < 2:
        nested = {}

    sec_opts = {hdr: h.parse_options(hdr) for hdr in h.option_sections()}

    if not nested:
        for opts in sec_opts.values():
            for o in opts:
                cmd.add_option(o)
        for tail in own:
            for o in usage_options(tail):
                cmd.add_option(o)
        for o in cmd.options:
            o.desc = o.desc or h.prose_mentioning(o.long)
        if own:
            cmd.positionals = usage_positionals(own[0])
            cmd.passthrough = any(re.search(r"\s--\s+<", t) for t in own)
        return

    descs = {}
    for head, extra in h.entries("Commands"):
        m = re.match(r"^([a-z][\w-]*)\b.*?(?:\s{2,}(.*))?$", head)
        if m:
            descs[m.group(1)] = ((m.group(2) or "") + " " + extra).strip()

    for name, tails in nested.items():
        sub = Command(cmd.path + [name], descs.get(name, ""))
        sub_text = run_help(pi, cmd.path + [name])
        if sub_text.strip() and sub_text.strip() != h.text.strip():
            build_command(pi, sub, Help(sub_text))
            cmd.subcommands.append(sub)
            continue

        mentioned = {o.names[0] for t in tails for o in usage_options(t)}
        for hdr, opts in sec_opts.items():
            m = re.match(r"options for (.*)", hdr, re.I)
            if m:
                targets = re.split(r",\s*|\s+and\s+|\s+", m.group(1).strip())
                if name not in targets:
                    continue
                for o in opts:
                    sub.add_option(Option(list(o.names), o.arg, o.optional_arg, o.desc))
            else:
                # generic section: keep options this command's usage mentions, or
                # every option if no nested usage line mentions it
                all_mentioned = {o.names[0] for ts in nested.values() for t in ts for o in usage_options(t)}
                for o in opts:
                    if set(o.names) & mentioned or not set(o.names) & all_mentioned:
                        sub.add_option(Option(list(o.names), o.arg, o.optional_arg, o.desc))
        for t in tails:
            for o in usage_options(t):
                sub.add_option(o)
        for o in sub.options:
            o.desc = o.desc or h.prose_mentioning(o.long)
        sub.positionals = usage_positionals(tails[0])
        sub.passthrough = any(re.search(r"\s--\s+<", t) for t in tails)
        cmd.subcommands.append(sub)


# ---------------------------------------------------------------------------
# value completion heuristics


def choices_from_desc(desc):
    text = desc.split(":")[-1] if ":" in desc else desc
    text = re.sub(r"\(default\)", "", text)
    text = re.sub(r"\s+", " ", text).replace(" ,", ",").strip().rstrip(".")
    words = r"[a-z][\w-]*"
    if re.fullmatch(rf"{words}(?:,\s*{words})*(?:,?\s+or\s+{words})", text) or \
       re.fullmatch(rf"{words}(?:,\s*{words}){{2,}}", text):
        return [w for w in re.findall(words, text) if w != "or"]
    return None


def option_action(cmd, opt):
    arg = (opt.arg or "").lower()
    names = set(opt.names)
    choices = choices_from_desc(opt.desc)
    if choices:
        return "(" + " ".join(choices) + ")"
    if names & {"--model"}:
        return f"_{PROG}_models"
    if names & {"--models"}:
        return f"_{PROG}_model_list"
    if names & {"--provider"}:
        return f"_{PROG}_providers"
    if names & {"--tools", "--exclude-tools", "-t", "-xt"}:
        return f"_{PROG}_tools"
    if names & {"--use-theme"}:
        return f"_{PROG}_themes"
    if names & {"--extension"} and cmd.path == ["update"]:
        return f"_{PROG}_packages"
    if names & {"--session", "--fork"}:
        return f"_{PROG}_sessions"
    if arg in ("dir", "directory") or "dir" in arg.split("|"):
        return "_files -/"
    if any(w in arg for w in ("path", "file")) or "path" in opt.desc.lower():
        return "_files"
    if arg == "name" and "env" in opt.long:
        return "_parameters"
    if arg == "key=value" and "env" in opt.long:
        return "_parameters -qS="
    if arg == "url":
        return "_urls"
    return ""


def positional_action(cmd, name):
    leaf = cmd.path[-1]
    if name in ("source", "package"):
        if leaf == "install":
            return f"_{PROG}_install_sources"
        return f"_{PROG}_packages"
    if name == "server" and leaf != "add":
        return f"_{PROG}_mcp_servers"
    if name in ("files", "file", "path"):
        return "_files"
    return ""


# ---------------------------------------------------------------------------
# zsh emission


def q(s):
    """Single-quote for zsh."""
    return "'" + s.replace("'", "'\\''") + "'"


def bracket(desc):
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace(":", "\\:")


def opt_spec(cmd, opt):
    repeat = re.search(r"multiple times|repeatable", opt.desc, re.I)
    desc = bracket(opt.desc)
    names = sorted(opt.names, key=lambda n: (len(n), n))
    if opt.arg:
        argname = opt.arg.replace(":", "\\:")
        sep = "::" if opt.optional_arg else ":"
        tail = f"{sep}{argname}:{option_action(cmd, opt)}"
    else:
        tail = ""
    body = (f"[{desc}]" if desc else "") + tail
    if len(names) == 1:
        prefix = "*" if repeat else ""
        return q(f"{prefix}{names[0]}{body}")
    braces = "{" + ",".join(names) + "}"
    excl = "'*'" if repeat else q("(" + " ".join(names) + ")")
    return f"{excl}{braces}{q(body)}" if body else f"{excl}{braces}"


def emit_leaf(cmd, out):
    out.append(f"{cmd.fname}() {{")
    if cmd.passthrough:
        out += [
            "  local i=${words[(i)--]}",
            "  if (( i < CURRENT )); then",
            '    words=("${(@)words[i+1,-1]}")',
            "    (( CURRENT -= i ))",
            "    _normal",
            "    return",
            "  fi",
        ]
    specs = [opt_spec(cmd, o) for o in cmd.options]
    for name, required, literals, rest in cmd.positionals:
        action = positional_action(cmd, name)
        if literals:
            alt = f"_{PROG}_{'_'.join(cmd.path)}_{name}".replace("-", "_")
            out_alt = [
                f"{alt}() {{",
                "  _alternative \\",
                "    " + q(f"keywords:{name}:({' '.join(literals)})") + (" \\" if action else ""),
            ]
            if action:
                out_alt.append("    " + q(f"values:{name}:{action}"))
            out_alt += ["}", ""]
            out[-1:-1] = out_alt  # define helper before this function
            action = alt
        colons = ":" if required else "::"
        star = "*" if rest else ""
        specs.append(q(f"{star}{colons}{name}:{action}"))
    if cmd.passthrough:
        specs.append(q("--[end of options, command follows]"))
    out.append("  _arguments -S \\" if specs else "  _message 'no more arguments'")
    for i, s in enumerate(specs):
        out.append(f"    {s}" + (" \\" if i < len(specs) - 1 else ""))
    out += ["}", ""]


def emit_group(cmd, out, top=False):
    out += [
        f"{cmd.fname}() {{",
        '  local curcontext="$curcontext" context state state_descr line ret=1',
        "  typeset -A opt_args",
        "  local -a subcmds",
        "  subcmds=(",
    ]
    for sub in cmd.subcommands:
        desc = sub.desc.replace(":", "\\:")
        out.append("    " + q(f"{sub.path[-1]}:{desc}" if desc else sub.path[-1]))
    out.append("  )")
    specs = [opt_spec(cmd, o) for o in cmd.options]
    specs += [q("1: :->cmd"), q("*:: :->args")]
    out.append("  _arguments -S -C \\")
    for i, s in enumerate(specs):
        out.append(f"    {s}" + (" \\" if i < len(specs) - 1 else " && ret=0"))
    out += [
        "  case $state in",
        "    (cmd)",
        "      _describe -t commands 'command' subcmds && ret=0",
    ]
    if top:
        out.append(f"      _{PROG}_atfiles && ret=0")
    out += [
        "      ;;",
        "    (args)",
        f'      curcontext="${{curcontext%:*:*}}:{"-".join([PROG, *cmd.path])}-$line[1]:"',
        "      case $line[1] in",
    ]
    for sub in cmd.subcommands:
        out.append(f"        ({sub.path[-1]}) {sub.fname} && ret=0 ;;")
    if top:
        out.append(f"        (*) _{PROG}_atfiles && ret=0 ;;")
    out += ["      esac", "      ;;", "  esac", "  return ret", "}", ""]
    for sub in cmd.subcommands:
        (emit_group if sub.subcommands else emit_leaf)(sub, out)


RUNTIME = r'''
# --- runtime helpers --------------------------------------------------------

(( $+functions[_@P@_real] )) || _@P@_real() {
  # bypass any shell wrapper function named @P@
  print -r -- "${commands[@P@]:-@P@}"
}

_@P@_agent_dir() {
  print -r -- "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
}

_@P@_load_models() {
  (( ${#_@P@_model_cache} )) && return
  local -a rows
  rows=( ${(f)"$(PI_OFFLINE=1 "$(_@P@_real)" --offline --list-models 2>/dev/null)"} )
  typeset -ga _@P@_model_cache _@P@_provider_cache
  local row
  for row in ${rows[2,-1]}; do
    local -a f=( ${=row} )
    (( ${#f} >= 2 )) || continue
    _@P@_model_cache+=( "$f[1]/$f[2]" )
    _@P@_provider_cache+=( "$f[1]" )
  done
  _@P@_provider_cache=( ${(u)_@P@_provider_cache} )
}

_@P@_providers() {
  _@P@_load_models
  _describe -t providers 'provider' _@P@_provider_cache
}

_@P@_models() {
  _@P@_load_models
  if compset -P '*:'; then
    _describe -t thinking 'thinking level' _@P@_thinking_levels
    return
  fi
  local provider=${opt_args[--provider]}
  local -a models
  if [[ -n $provider ]]; then
    models=( ${${(M)_@P@_model_cache:#$provider/*}#$provider/} )
  else
    models=( $_@P@_model_cache )
  fi
  _multi_parts -q / models
}

_@P@_model_list() {
  _@P@_load_models
  compset -P '*,'
  _multi_parts -q -S , / _@P@_model_cache
}

_@P@_tools() {
  compset -P '*,'
  compset -P '[+-]'
  _describe -t tools 'tool' _@P@_tool_names -qS ,
}

_@P@_themes() {
  local -a themes
  themes=( dark light )
  themes+=( $(_@P@_agent_dir)/themes/*.json(N:t:r) .pi/themes/*.json(N:t:r) )
  _describe -t themes 'theme' themes
}

_@P@_sessions() {
  _files -g '*.jsonl(-.)'
}

_@P@_packages() {
  local -a pkgs
  pkgs=( ${${(M)${(f)"$("$(_@P@_real)" list 2>/dev/null)"}:#  [^ ]*}##  } )
  pkgs=( ${pkgs//:/\\:} )
  _describe -t packages 'installed package' pkgs
}

_@P@_install_sources() {
  _alternative \
    'schemes:source type:compadd -S "" npm: git: https:// ssh://' \
    'files:local path:_files -/'
}

_@P@_mcp_servers() {
  local -a servers
  local f
  for f in "$(_@P@_agent_dir)/mcp.json" .pi/mcp.json; do
    [[ -r $f ]] || continue
    servers+=( ${(f)"$(python3 -c 'import json,sys
try: print("\n".join(json.load(open(sys.argv[1])).get("mcpServers", {})))
except Exception: pass' $f 2>/dev/null)"} )
  done
  servers=( ${(u)servers} )
  _describe -t servers 'MCP server' servers
}

_@P@_atfiles() {
  [[ $PREFIX == @* ]] || return 1
  compset -P @
  _files
}
'''


def generate(root, pi):
    out = [
        f"#compdef {PROG}",
        f"# Generated by ~/.pi/completion-by-claude.py from `{PROG} --help` and subcommand help.",
        f"# {pi}",
        "# Refresh:",
        "#   ~/.pi/completion-by-claude.py > ~/.cache/zsh/pi-completion-by-claude.zsh",
        "",
        f"typeset -ga _{PROG}_tool_names _{PROG}_thinking_levels",
        f"_{PROG}_tool_names=(",
    ]
    for name, desc in root.tools:
        out.append("  " + q(f"{name}:{desc}"))
    out.append(")")
    thinking = next((o for o in root.options if "--thinking" in o.names), None)
    levels = choices_from_desc(thinking.desc) if thinking else None
    out.append(f"_{PROG}_thinking_levels=( {' '.join(levels or [])} )")
    out.append(RUNTIME.replace("@P@", PROG))
    out.append("# --- commands ----------------------------------------------------------------")
    out.append("")
    emit_group(root, out, top=True)
    out.append(f"compdef _{PROG} {PROG}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pi", default=shutil.which(PROG), help="path to the pi executable")
    args = ap.parse_args()
    if not args.pi:
        sys.exit("completion-by-claude: cannot find `pi` in $PATH (use --pi)")
    root = discover(args.pi)
    if not root.options and not root.subcommands:
        sys.exit("completion-by-claude: could not parse `pi --help`")
    sys.stdout.write(generate(root, args.pi))


if __name__ == "__main__":
    main()
