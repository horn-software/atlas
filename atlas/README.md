# Atlas

**Local MCP server for codebase ingestion, AI-powered query, impact analysis, and edge-case detection.**

Atlas gives AI coding assistants (Claude, Cursor, Windsurf, VS Code) a structured knowledge base of your codebase — so they make 1 tool call instead of 15.

```
Without Atlas: Claude opens 7 files, runs 5 greps, makes 15 tool calls to understand context
With Atlas:    1 call to atlas_query. Done.
```

Measured **93% reduction in tool-call overhead** per coding task.

---

## How it works

Atlas runs as a local MCP server. It indexes your codebase into a local [Meilisearch](https://www.meilisearch.com/) instance, then exposes 5 tools your AI client calls over stdio — no cloud, no API key, no data leaving your machine.

### The five tools

| Tool | What it does |
|------|-------------|
| `atlas_ingest` | Crawl a codebase, parse 18 languages, build a call graph, detect cross-language bridges, write to local index |
| `atlas_query` | Natural language question → BiLSTM query model → structured results |
| `atlas_impact` | Given a list of functions, find every function that would break if they change |
| `atlas_edge_check` | Full safety pass: impact analysis + verification layer over proposed code change *(Pro)* |
| `atlas_status` | Check if a project is indexed and how many documents are in the index |

### Supported languages

Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++, Ruby, PHP, Swift, Kotlin, Scala, Lua, Elixir, Dart, Zig

### Cross-language bridge detection

Atlas detects relationships that span language boundaries:
- HTTP/REST route matching between frontend and backend
- Shared environment variable references
- Shared config file references
- Database schema/table bridges across ORMs
- Shared type/interface definitions

---

## Installation

```bash
pip install atlas-mcp
atlas init
```

`atlas init` will:
1. Download and start Meilisearch locally
2. Crawl and index your codebase
3. Write the MCP config to `.claude/settings.json`

### MCP config (Claude Desktop / VS Code)

```json
{
  "mcpServers": {
    "atlas": {
      "command": "python",
      "args": ["-m", "atlas.server"],
      "env": { "ATLAS_KEY": "your-licence-key" }
    }
  }
}
```

---

## Pricing

| Tier | Price | Projects | Languages | Query model |
|------|-------|----------|-----------|-------------|
| Free | $0 | 1 | Python only | Raw keyword search |
| Pro | $19/month | Unlimited | All 18 | Trained BiLSTM |
| Team | $79/seat/month | Unlimited | All 18 | Trained BiLSTM + shared indexes |
| Enterprise | $149/seat/month | Unlimited | All 18 | Full suite + SSO + SLA |

Get your licence key at **[hornsa.com/atlas](https://hornsa.com/atlas)**

---

## Open-core model

The ingestion pipeline, MCP server, call graph walker, and model architecture are open source (Apache 2.0).

The training pipeline, trained model weights, and verifier bridge are proprietary — distributed to paid licence holders only.

| Component | Licence |
|-----------|---------|
| `atlas/ingest/` | Apache 2.0 |
| `atlas/server.py` | Apache 2.0 |
| `atlas/edge/impact.py` | Apache 2.0 |
| `atlas/query/net.py` | Apache 2.0 |
| `atlas/cli.py` | Apache 2.0 |
| Training pipeline | Proprietary |
| Trained weights | Proprietary |
| Verifier bridge | Proprietary |

---

## Requirements

- Python 3.11+
- 4 GB RAM (8 GB recommended for large codebases)
- 2 GB disk (Meilisearch + weights)
- Internet required at install and for daily licence token refresh

---

## Support

- Issues: [github.com/horn-software/atlas/issues](https://github.com/horn-software/atlas/issues)
- Email: hello@hornsoftwarearchitects.co.za
- Docs: [hornsa.com/atlas/docs](https://hornsa.com/atlas/docs)

---

*Built by [Horn Software Architects](https://hornsa.com)*
