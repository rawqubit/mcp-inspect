# mcp-inspect

Your MCP tool list is a tax on every request. This tells you how much.

`mcp-inspect` spawns an MCP server over stdio, does the `initialize` /
`tools/list` handshake, and prices the tool definitions the server hands back.
Every tool definition is a JSON Schema that the client sends to the model on
every single call, not once at startup: the whole list is pasted into the
request every time, and nothing caches it. That means a server exposing thirty
tools is paid for on every request, in every conversation, whether or not the
model ever calls one of them.

## Install

No dependencies, no build step: one file, standard library only, Python 3.9 or
newer.

```sh
git clone https://github.com/rawqubit/mcp-inspect
python3 mcp-inspect/mcp_inspect.py --help
```

Or copy `mcp_inspect.py` anywhere -- a repo, `~/bin`, next to your server -- and
run it from there. There is nothing to install.

## Use it

Point it at any stdio MCP server, quoted as a single argument:

```sh
python3 mcp_inspect.py 'npx -y @modelcontextprotocol/server-filesystem /tmp'
python3 mcp_inspect.py 'python3 my_server.py'
```

`--json` emits the same numbers for a script or a dashboard, and the gates turn
the measurement into CI:

```sh
python3 mcp_inspect.py --json 'python3 my_server.py' > tools.json
python3 mcp_inspect.py --max-tokens 5000 --max-tools 20 'python3 my_server.py' || exit 1
```

`--max-tokens N` exits 2 when the total exceeds N and prints the overage, so a
pull request that adds a 40-tool server fails the build instead of quietly
inflating every future request.

## Sample output

Real output, from the fixture server in `tests/fixture_server.py`:

```
mcp-inspect 0.1.0 | fixture-server | protocol 2024-11-05
6 tools | 2,091 schema tokens | re-sent on every request

 TOKENS   SHARE  TOOL                                                      FLAGS
  1,632   78.0%  bloated_report_generator                                  VERBOSE-DESC HUGE-SCHEMA
                     Generate a board-ready report from the warehouse...
    112    5.4%  write_file
                     Write text to a file at the given path, creating...
    106    5.1%  search
                     Search indexed documents and return ranked matches.
     98    4.7%  list_directory                                            NO-PARAM-DESC
                     List the entries of a directory, one per line, so...
     86    4.1%  read_file                                                 DUP-DESC
                     Write text to a file at the given path, creating...
     57    2.7%  do                                                        NO-DESC NO-PARAM-DESC VAGUE-NAME
                     (no description)

Adds 2,091 tokens to every call; worst offender bloated_report_generator at 1,632 (78.0%).
```

One tool in that list costs more than the other five together: 1,632 tokens
against 459. Its description runs 947 characters, and the schema behind it
prices at 1,363 tokens on its own. `read_file` carries the description of
`write_file`, because the fixture server copy-pasted it.

## Flags

| Flag | Meaning |
| --- | --- |
| `VERBOSE-DESC` | description over 500 characters |
| `NO-DESC` | description missing or empty |
| `NO-PARAM-DESC` | at least one top-level parameter has no description |
| `HUGE-SCHEMA` | the tool alone costs more than 800 tokens |
| `DUP-DESC` | description identical to an earlier tool's |
| `VAGUE-NAME` | name shorter than 4 characters, or a generic word such as `run`, `do`, `tool` |

`--json` emits `flags` as a map from each flag to the tools that carry it, plus
the same per-tool `flags` array.

## Options

| Option | Effect |
| --- | --- |
| `--json` | machine-readable output instead of the table |
| `--max-tokens N` | exit 2 when total tokens exceed N, printing the overage |
| `--max-tools N` | exit 2 when the tool count exceeds N |
| `--top N` | show only the N most expensive tools (default: all) |
| `--timeout SECONDS` | timeout for the whole handshake (default: 20) |
| `--no-color` | disable ANSI color; color also turns off when stdout is not a tty or `NO_COLOR` is set |

Exit codes: `0` success, `2` a gate was breached, `3` the server could not be
spoken to (missing binary, death, garbage output, timeout, JSON-RPC error) or
the command line itself was unusable. Every failure is one line on stderr with a
reason, never a traceback.

## How the token count is estimated

There is no tokenizer in here and no dependencies to pull one in.
`estimate_tokens()` prices word pieces and identifiers at about one token per
four characters and punctuation at about one token per two characters, then
rounds the total up with a small cushion. It is calibrated to overestimate
slightly rather than underestimate, because understating a tax is the worse
error: a bill that arrives 10% high is a nuisance, a bill that arrives 40% high
after you sized your context window around it is a bug.

Each tool is priced as `json.dumps()` of its full definition
(`name`, `description`, `inputSchema`), because that whole object is what the
model receives. Treat the numbers as an estimate with a few percent of slack in
the safe direction, not as an exact vendor count.

## How to cut the bill

- **Fewer tools per server.** Every schema is re-sent on every call, so the
  cheapest tool is the one the model never had to be told about. Split a
  40-tool server into task-scoped servers and connect only the one the task
  needs.
- **Shorter descriptions.** A 900-character description on a tool called twice
  a week costs as much as the code behind it on every call. Aim for one
  sentence that says what the tool does and when to use it.
- **Split by task, not by vendor.** One server per workflow (`billing`,
  `search`, `deploy`) keeps each connected session's list small and makes the
  cost of the next integration obvious.
- **Dynamic tool selection.** Where the client supports it, load tool groups on
  demand rather than connecting every server at session start, so unused
  schemas are never sent at all.

## What it does not do

- No authenticated servers: a server that needs OAuth, a browser flow, or a
  credential prompt of its own will not complete the handshake.
- No HTTP or SSE transport. stdio only.
- No schema validation. It reads and prices the schemas; it does not check that
  they are valid JSON Schema, nor that the server implements them.
- Token counts are estimates from the estimator above, not exact counts from a
  model's tokenizer.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite runs the CLI end to end against `tests/fixture_server.py`, a
stdlib-only stdio MCP server with six tools chosen to trip every flag, plus a
`--broken` mode that answers `tools/list` with a JSON-RPC error.

## License

MIT, 2026 Srinikhil Chakilam. See `LICENSE`.

Built by [Srinikhil Chakilam](https://srinikhilchakilam.com). If your tool list turns out to cost more than your prompt, I want to hear about it.
