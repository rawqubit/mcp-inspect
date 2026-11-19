#!/usr/bin/env python3
"""mcp-inspect: price the MCP tool list a server adds to every model call.

Spawns an MCP server over stdio, performs the initialize / tools/list handshake,
and reports how many tokens its tool schemas cost on every single request.
"""

import argparse
import collections
import json
import math
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time

__version__ = "0.1.0"

PROTOCOL_VERSION = "2024-11-05"

EXIT_OK = 0
EXIT_GATE = 2
EXIT_PROTOCOL = 3

VERBOSE_DESC_CHARS = 500
HUGE_SCHEMA_TOKENS = 800
VAGUE_NAME_MAX = 4

FLAG_VERBOSE_DESC = "VERBOSE-DESC"
FLAG_NO_DESC = "NO-DESC"
FLAG_NO_PARAM_DESC = "NO-PARAM-DESC"
FLAG_HUGE_SCHEMA = "HUGE-SCHEMA"
FLAG_DUP_DESC = "DUP-DESC"
FLAG_VAGUE_NAME = "VAGUE-NAME"

FLAG_ORDER = (
    FLAG_VERBOSE_DESC,
    FLAG_NO_DESC,
    FLAG_NO_PARAM_DESC,
    FLAG_HUGE_SCHEMA,
    FLAG_DUP_DESC,
    FLAG_VAGUE_NAME,
)

RED_FLAGS = frozenset((FLAG_NO_DESC, FLAG_HUGE_SCHEMA))

GENERIC_NAMES = frozenset(
    (
        "do",
        "run",
        "tool",
        "tools",
        "call",
        "exec",
        "execute",
        "use",
        "get",
        "go",
        "make",
        "process",
        "handle",
        "query",
        "action",
        "task",
    )
)

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

_PIECE = re.compile(r"([A-Za-z0-9_]+)|([^A-Za-z0-9_\s]+)")


def _identifier_pieces(word):
    """Split an identifier into pieces on underscores and camelCase humps."""
    pieces = []
    for chunk in word.split("_"):
        if not chunk:
            continue
        current = chunk[0]
        for char in chunk[1:]:
            if char.isupper() and not current[-1].isupper():
                pieces.append(current)
                current = char
            else:
                current += char
        pieces.append(current)
    return pieces


def estimate_tokens(text):
    """Approximate the number of model tokens in text.

    This is an approximation, not a tokenizer: the real count depends on the
    model's vocabulary. Word pieces and identifiers cost about one token per
    four characters, punctuation about one token per two characters, and the
    total is rounded up with a small cushion.

    The estimator is calibrated to overestimate slightly rather than
    underestimate, because understating a tax is the worse error: a tool list
    that quietly costs more than advertised is worse than one that turns out to
    cost a little less.
    """
    if not text:
        return 0
    total = 0.0
    for match in _PIECE.finditer(text):
        word = match.group(1)
        if word is not None:
            for piece in _identifier_pieces(word):
                total += max(1.0, len(piece) / 4.0)
        else:
            total += max(1.0, len(match.group(2)) / 2.0)
    return max(1, int(math.ceil(total * 1.05)))


def price_tool(tool):
    """Return the tokens one tool definition costs on the wire."""
    payload = {
        "name": tool["name"],
        "description": tool["description"],
        "inputSchema": tool["inputSchema"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return estimate_tokens(encoded)


# ---------------------------------------------------------------------------
# Protocol client
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """The server could not be spoken to, or answered with an error."""


class ServerSession:
    """A stdio MCP child process with line-framed JSON-RPC.

    stdout is drained on a thread so a server that logs, emits garbage, or hangs
    can never block the client, and stderr is captured for error messages.
    """

    def __init__(self, argv, timeout):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.lines = queue.Queue()
        self.stderr_tail = collections.deque(maxlen=6)
        self.noise = 0
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            raise ProtocolError(
                "cannot run %r: no such file or directory" % (argv[0],)
            )
        except PermissionError:
            raise ProtocolError("cannot run %r: permission denied" % (argv[0],))
        except OSError as exc:
            raise ProtocolError(
                "cannot run %r: %s" % (argv[0], exc.strerror or exc)
            )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        except (ValueError, OSError):
            pass
        finally:
            self.lines.put(None)

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                text = line.strip()
                if text:
                    self.stderr_tail.append(text[:300])
        except (ValueError, OSError):
            pass

    def send(self, message):
        wire = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(wire)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            label = message.get("method", "request")
            raise ProtocolError(
                "server closed stdin before accepting %s%s" % (label, self._context())
            )

    def wait_for_response(self, request_id, method):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    "timed out after %gs waiting for the %s response%s"
                    % (self.timeout, method, self._context())
                )
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise ProtocolError(
                    "timed out after %gs waiting for the %s response%s"
                    % (self.timeout, method, self._context())
                )
            if line is None:
                raise ProtocolError(
                    "server exited before answering %s%s" % (method, self._context())
                )
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self.noise += 1
                continue
            if not isinstance(message, dict):
                self.noise += 1
                continue
            if not _same_id(message.get("id"), request_id):
                continue
            if "error" in message and message["error"] is not None:
                error = message["error"]
                if isinstance(error, dict):
                    detail = str(error.get("message") or "no message")
                    code = error.get("code")
                    reason = "%s (code %s)" % (detail, code)
                else:
                    reason = str(error)
                raise ProtocolError(
                    "server returned a JSON-RPC error for %s: %s" % (method, reason)
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProtocolError(
                    "server returned a malformed result for %s: expected a JSON object"
                    % (method,)
                )
            return result

    def _context(self):
        parts = []
        exit_code = self.proc.poll()
        if exit_code is not None:
            parts.append("exit code %d" % (exit_code,))
        if self.noise:
            parts.append("%d non-JSON line(s) on stdout" % (self.noise,))
        if self.stderr_tail:
            parts.append("last stderr line: %s" % (self.stderr_tail[-1],))
        if not parts:
            return ""
        return " (" + "; ".join(parts) + ")"

    def close(self):
        """Close stdin, then terminate the child. A child is never left running."""
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass


def _same_id(left, right):
    if left == right:
        return True
    if left is None:
        return False
    return str(left) == str(right)


def _normalize_tool(entry, index):
    if not isinstance(entry, dict):
        raise ProtocolError(
            "tools/list returned a non-object tool at position %d" % (index,)
        )
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(
            "tools/list returned a tool without a name at position %d" % (index,)
        )
    description = entry.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        description = str(description)
    schema = entry.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {}
    return {"name": name, "description": description, "inputSchema": schema}


def collect_tools(command, timeout):
    """Run the handshake and return the server's identity plus its tool list."""
    argv = shlex.split(command)
    if not argv:
        raise ProtocolError("no server command given")
    session = ServerSession(argv, timeout)
    try:
        session.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-inspect", "version": __version__},
                },
            }
        )
        initialized = session.wait_for_response(1, "initialize")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = session.wait_for_response(2, "tools/list")
    finally:
        session.close()

    server_info = initialized.get("serverInfo")
    server_name = ""
    if isinstance(server_info, dict):
        server_name = str(server_info.get("name") or "")
    protocol = initialized.get("protocolVersion")
    if protocol is not None:
        protocol = str(protocol)

    entries = listing.get("tools")
    if not isinstance(entries, list):
        raise ProtocolError("tools/list returned no tools array")
    tools = [_normalize_tool(entry, index) for index, entry in enumerate(entries)]
    return {
        "server": server_name or "(unnamed server)",
        "protocolVersion": protocol,
        "tools": tools,
    }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _schema_parameters(schema):
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return properties
    return {}


def _has_description(value):
    if not isinstance(value, dict):
        return False
    description = value.get("description")
    return isinstance(description, str) and bool(description.strip())


def analyze_tools(tools):
    """Price every tool and attach the flags a maintainer can act on."""
    seen = {}
    records = []
    for tool in tools:
        description = tool["description"]
        stripped = description.strip()
        parameters = _schema_parameters(tool["inputSchema"])
        flags = []
        if len(stripped) > VERBOSE_DESC_CHARS:
            flags.append(FLAG_VERBOSE_DESC)
        if not stripped:
            flags.append(FLAG_NO_DESC)
        if any(not _has_description(value) for value in parameters.values()):
            flags.append(FLAG_NO_PARAM_DESC)
        tokens = price_tool(tool)
        if tokens > HUGE_SCHEMA_TOKENS:
            flags.append(FLAG_HUGE_SCHEMA)
        normalized = " ".join(stripped.split())
        if normalized and normalized in seen:
            flags.append(FLAG_DUP_DESC)
        elif normalized:
            seen[normalized] = tool["name"]
        name = tool["name"]
        if len(name) < VAGUE_NAME_MAX or name.lower() in GENERIC_NAMES:
            flags.append(FLAG_VAGUE_NAME)
        records.append(
            {
                "name": name,
                "description": description,
                "tokens": tokens,
                "flags": [flag for flag in FLAG_ORDER if flag in flags],
                "parameterCount": len(parameters),
            }
        )
    records.sort(key=lambda record: (-record["tokens"], record["name"]))
    total = sum(record["tokens"] for record in records)
    for record in records:
        record["share"] = (100.0 * record["tokens"] / total) if total else 0.0
    return records, total


def flag_map(records):
    """Map every flag to the tools that carry it, in canonical order."""
    mapping = {flag: [] for flag in FLAG_ORDER}
    for record in records:
        for flag in record["flags"]:
            mapping[flag].append(record["name"])
    return mapping


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class Palette:
    def __init__(self, enabled):
        self.enabled = enabled

    def _paint(self, text, code):
        if not self.enabled or not text:
            return text
        return "\x1b[%sm%s\x1b[0m" % (code, text)

    def bold(self, text):
        return self._paint(text, "1")

    def dim(self, text):
        return self._paint(text, "2")

    def red(self, text):
        return self._paint(text, "31")

    def yellow(self, text):
        return self._paint(text, "33")


TOKENS_WIDTH = 7
SHARE_WIDTH = 6
TOOL_WIDTH = 56
FLAGS_WIDTH = 34
GAP = "  "
DESC_INDENT = " " * (TOKENS_WIDTH + len(GAP) + SHARE_WIDTH + len(GAP)) + "    "
DESC_WIDTH = TOOL_WIDTH - 4


def _trim(text, width):
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _wrap_flags(flags, width):
    lines = []
    current = ""
    for flag in flags:
        candidate = flag if not current else current + " " + flag
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = flag
    lines.append(current)
    return lines


def _paint_flags(line, palette):
    painted = []
    for flag in line.split(" "):
        if not flag:
            continue
        if flag in RED_FLAGS:
            painted.append(palette.red(flag))
        else:
            painted.append(palette.yellow(flag))
    return " ".join(painted)


def render_report(report, records, total, limit, palette):
    lines = []
    protocol = report["protocolVersion"] or "unknown"
    lines.append(
        "%s %s %s %s %s protocol %s"
        % (
            palette.bold("mcp-inspect"),
            __version__,
            palette.dim("|"),
            palette.bold(report["server"]),
            palette.dim("|"),
            protocol,
        )
    )
    lines.append(
        "%d tool%s %s %s schema tokens %s re-sent on every request"
        % (
            len(records),
            "" if len(records) == 1 else "s",
            palette.dim("|"),
            format(total, ","),
            palette.dim("|"),
        )
    )
    lines.append("")

    shown = records[:limit] if limit and limit > 0 else records
    header = "%s%s%s%s%s%s%s" % (
        "TOKENS".rjust(TOKENS_WIDTH),
        GAP,
        "SHARE".rjust(SHARE_WIDTH),
        GAP,
        "TOOL".ljust(TOOL_WIDTH),
        GAP,
        "FLAGS",
    )
    if records:
        lines.append(palette.dim(header))
    else:
        lines.append(palette.dim("(this server advertises no tools)"))

    for index, record in enumerate(shown):
        tokens_text = format(record["tokens"], ",").rjust(TOKENS_WIDTH)
        if index == 0:
            tokens_text = palette.bold(tokens_text)
        share_text = ("%.1f%%" % record["share"]).rjust(SHARE_WIDTH)
        name_text = _trim(record["name"], TOOL_WIDTH).ljust(TOOL_WIDTH)
        flag_lines = _wrap_flags(record["flags"], FLAGS_WIDTH) if record["flags"] else [""]
        row = "%s%s%s%s%s%s%s" % (
            tokens_text,
            GAP,
            share_text,
            GAP,
            name_text,
            GAP,
            _paint_flags(flag_lines[0], palette),
        )
        lines.append(row.rstrip())
        description = _trim(record["description"], DESC_WIDTH)
        if record["description"].strip():
            desc_text = palette.dim(description)
        else:
            desc_text = palette.dim("(no description)")
        if len(flag_lines) > 1:
            lines.append(
                ("%s%s%s%s" % (DESC_INDENT, desc_text, GAP, _paint_flags(flag_lines[1], palette))).rstrip()
            )
        else:
            lines.append(("%s%s" % (DESC_INDENT, desc_text)).rstrip())
        for extra in flag_lines[2:]:
            lines.append(("%s%s" % (DESC_INDENT, _paint_flags(extra, palette))).rstrip())

    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    if shown and len(shown) < len(records):
        lines.append(
            palette.dim("showing %d of %d tools by cost" % (len(shown), len(records)))
        )
        lines.append("")

    if records:
        worst = records[0]
        lines.append(
            "%s %s tokens to every call; worst offender %s at %s (%.1f%%)."
            % (
                palette.bold("Adds"),
                format(total, ","),
                palette.bold(worst["name"]),
                format(worst["tokens"], ","),
                worst["share"],
            )
        )
    else:
        lines.append(
            "Adds 0 tokens to every call; this server advertises no tools."
        )
    return "\n".join(lines) + "\n"


def json_payload(report, records, total):
    payload = {
        "server": report["server"],
        "protocolVersion": report["protocolVersion"],
        "totalTokens": total,
        "tools": [
            {
                "name": record["name"],
                "description": record["description"],
                "tokens": record["tokens"],
                "flags": record["flags"],
                "parameterCount": record["parameterCount"],
            }
            for record in records
        ],
        "flags": flag_map(records),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("mcp-inspect: %s (try --help)\n" % (message,))
        raise SystemExit(EXIT_PROTOCOL)


def build_parser():
    parser = _ArgumentParser(
        prog="mcp-inspect",
        description="Spawn an MCP server over stdio and price the tool list it re-sends on every model call.",
    )
    parser.add_argument(
        "command",
        help="server command to run over stdio, quoted as one argument, "
        "e.g. 'npx -y @modelcontextprotocol/server-filesystem /tmp'",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output instead of the table",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        metavar="N",
        help="exit 2 when the total schema tokens exceed N (CI gate)",
    )
    parser.add_argument(
        "--max-tools",
        type=int,
        metavar="N",
        help="exit 2 when the tool count exceeds N",
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="show only the N most expensive tools (default: all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        metavar="SECONDS",
        help="timeout for the whole handshake (default: 20)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="mcp-inspect " + __version__,
    )
    return parser


def _color_enabled(stream):
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        report = collect_tools(args.command, args.timeout)
    except ProtocolError as exc:
        sys.stderr.write("mcp-inspect: %s\n" % (exc,))
        return EXIT_PROTOCOL

    records, total = analyze_tools(report["tools"])

    if args.json:
        sys.stdout.write(json_payload(report, records, total))
    else:
        palette = Palette(not args.no_color and _color_enabled(sys.stdout))
        sys.stdout.write(render_report(report, records, total, args.top, palette))
    sys.stdout.flush()

    breached = False
    if args.max_tokens is not None and total > args.max_tokens:
        sys.stderr.write(
            "mcp-inspect: %s tokens exceeds --max-tokens %s by %s\n"
            % (format(total, ","), args.max_tokens, format(total - args.max_tokens, ","))
        )
        breached = True
    if args.max_tools is not None and len(records) > args.max_tools:
        sys.stderr.write(
            "mcp-inspect: %d tool%s exceeds --max-tools %d by %d\n"
            % (
                len(records),
                "" if len(records) == 1 else "s",
                args.max_tools,
                len(records) - args.max_tools,
            )
        )
        breached = True
    return EXIT_GATE if breached else EXIT_OK


def cli(argv=None):
    try:
        return main(argv)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        sys.stderr.write("mcp-inspect: interrupted\n")
        return 130
    except ProtocolError as exc:
        sys.stderr.write("mcp-inspect: %s\n" % (exc,))
        return EXIT_PROTOCOL
    except Exception as exc:  # never show a traceback to a user
        sys.stderr.write("mcp-inspect: unexpected failure: %s\n" % (exc,))
        return EXIT_PROTOCOL


if __name__ == "__main__":
    sys.exit(cli())
