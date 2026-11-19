#!/usr/bin/env python3
"""A tiny, deterministic MCP server over stdio, used by mcp-inspect's tests.

It implements the three methods mcp-inspect speaks: initialize,
notifications/initialized (a notification, never answered), and tools/list.
Nothing else is required and nothing else is implemented. It exits cleanly as
soon as stdin closes, which is what makes it safe as a test fixture.

The six tools are chosen to trip every flag the CLI knows about:

    search                    tight, well-written schema (no flags)
    bloated_report_generator  ~900-character description, huge nested schema
                              (VERBOSE-DESC, HUGE-SCHEMA)
    do                        no description, no parameter descriptions
                              (NO-DESC, NO-PARAM-DESC, VAGUE-NAME)
    write_file                clean description, listed first
    read_file                 description copied verbatim from write_file,
                              a classic copy-paste bug (DUP-DESC)
    list_directory            clean description, one undocumented parameter
                              (NO-PARAM-DESC)

Run it with --broken to make tools/list answer with a JSON-RPC error instead,
which is how the tests exercise the error path.
"""

import json
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fixture-server"
SERVER_VERSION = "1.0.0"

SEARCH = {
    "name": "search",
    "description": "Search indexed documents and return ranked matches.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Text to search for.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of matches to return.",
                "default": 10,
                "minimum": 1,
            },
        },
        "required": ["query"],
    },
}

BLOAT_DESCRIPTION = (
    "Generate a board-ready report from the warehouse for a period, segment "
    "and metric list of your choosing. The full specification travels in one "
    "call: sections, titles, sorting, filters, comparisons, rounding, currency "
    "normalization and delivery. Scheduled delivery, PDF and XLSX rendering and "
    "an audit trail of every applied filter are supported. Prefer this tool over "
    "calling the smaller report tools in sequence when a report spans several "
    "sections, because layout, numbering, footnotes and the appendix are then "
    "computed once. Large reports may take minutes, the response is paginated "
    "past the inline size limit, and each section may override the global "
    "formatting. A missing metric fails the whole render with a structured "
    "error instead of a partial report, so validate the metric list before "
    "scheduling anything recurring. The result carries a download URL valid for "
    "seven days, a stable report identifier and a checksum of the rendered bytes."
)

SECTIONS = [
    ("executive_summary", "Opening prose summary of the reporting period."),
    ("kpi_table", "Key performance indicators with period-over-period deltas."),
    ("revenue_breakdown", "Revenue split by product line, region and channel."),
]

FILTERS = [
    ("account_ids", "Account identifiers to include; empty means all accounts."),
    ("region_codes", "ISO region codes to include, for example us-east-1."),
    ("product_lines", "Product line slugs to include in every breakdown."),
    ("channels", "Acquisition channels to include in attribution tables."),
]

DELIVERY_FORMATS = ["pdf", "xlsx", "csv", "html", "markdown"]

FORMATTING = [
    ("number_style", "Thousands separators and decimal places for numbers."),
    ("currency_style", "Currency symbol placement and precision."),
    ("date_style", "Date rendering for every table and footnote."),
]


def _formatting_options():
    options = {}
    for name, description in FORMATTING:
        options[name] = {
            "type": "string",
            "description": description,
            "enum": ["default", "compact", "accounting", "scientific"],
        }
    return options


def _section_schema():
    sections = {}
    for name, description in SECTIONS:
        sections[name] = {
            "type": "object",
            "description": description,
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "Render this section in the report.",
                },
                "sort": {
                    "type": "object",
                    "description": "Ordering applied to this section's rows.",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": "Column to sort on.",
                        },
                        "direction": {
                            "type": "string",
                            "description": "Sort direction for the column.",
                            "enum": ["ascending", "descending"],
                        },
                    },
                    "required": ["field"],
                },
            },
            "additionalProperties": False,
        }
    return sections


def _bloated_schema():
    filters = {}
    for name, description in FILTERS:
        filters[name] = {
            "type": "array",
            "description": description,
            "items": {
                "type": "string",
                "description": "One value for this filter.",
            },
        }
    return {
        "type": "object",
        "description": "Full specification of the report to render.",
        "properties": {
            "period": {
                "type": "object",
                "description": "Reporting period the report covers.",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "Inclusive period start date.",
                    },
                    "end": {
                        "type": "string",
                        "description": "Inclusive period end date.",
                    },
                    "granularity": {
                        "type": "string",
                        "description": "Bucket size used for time series.",
                        "enum": ["day", "week", "month", "quarter", "year"],
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone bounding every boundary.",
                    },
                },
                "required": ["start", "end"],
            },
            "filters": {
                "type": "object",
                "description": "Row filters applied before any aggregation.",
                "properties": filters,
                "additionalProperties": False,
            },
            "sections": {
                "type": "object",
                "description": "Per-section rendering options.",
                "properties": _section_schema(),
                "additionalProperties": False,
            },
            "formatting": {
                "type": "object",
                "description": "Number, currency, percent and date formatting.",
                "properties": _formatting_options(),
                "additionalProperties": False,
            },
            "delivery": {
                "type": "object",
                "description": "Where and how the finished report is delivered.",
                "properties": {
                    "format": {
                        "type": "string",
                        "description": "File format of the rendered report.",
                        "enum": DELIVERY_FORMATS,
                    },
                    "recipients": {
                        "type": "array",
                        "description": "Email addresses that receive the report.",
                        "items": {
                            "type": "string",
                            "description": "One email address.",
                            "pattern": "^[^@]+@[^@]+$",
                        },
                        "maxItems": 50,
                    },
                    "webhook_url": {
                        "type": "string",
                        "description": "URL that receives a completion notice.",
                    },
                },
                "required": ["format"],
            },
        },
        "required": ["period", "sections"],
        "additionalProperties": False,
    }


FILE_WRITE_DESCRIPTION = (
    "Write text to a file at the given path, creating parent directories as "
    "needed and overwriting any existing file."
)

WRITE_FILE = {
    "name": "write_file",
    "description": FILE_WRITE_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to write.",
            },
            "text": {
                "type": "string",
                "description": "Content written to the file.",
            },
        },
        "required": ["path", "text"],
    },
}

READ_FILE = {
    "name": "read_file",
    "description": FILE_WRITE_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to read.",
            }
        },
        "required": ["path"],
    },
}

DO = {
    "name": "do",
    "description": "",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "target": {"type": "string"},
        },
        "required": ["action"],
    },
}

LIST_DIRECTORY = {
    "name": "list_directory",
    "description": "List the entries of a directory, one per line, sorted by name.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to list.",
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["path"],
    },
}

TOOLS = [
    SEARCH,
    {
        "name": "bloated_report_generator",
        "description": BLOAT_DESCRIPTION,
        "inputSchema": _bloated_schema(),
    },
    DO,
    WRITE_FILE,
    READ_FILE,
    LIST_DIRECTORY,
]


def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _handle(message, broken):
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        )
    elif method == "tools/list":
        if broken:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "tools/list is not supported by this build",
                    },
                }
            )
        else:
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    else:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "unknown method: %s" % method},
            }
        )


def main(argv):
    broken = "--broken" in argv
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            _handle(message, broken)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
