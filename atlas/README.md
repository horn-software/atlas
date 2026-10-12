# Atlas

**Stop your AI from reading your codebase on every question.**

Atlas is a local MCP server that indexes your codebase into a structured search database. Instead of opening files and grepping for context, your AI assistant asks Atlas once and gets the answer.

---

## The problem

Every time you ask your AI assistant a question about your code, it reads files:

```
Tool: Read(src/auth/middleware.py)
Tool: Read(src/auth/tokens.py)
Tool: Grep(pattern="verify_token", path="src/")
Tool: Read(src/users/models.py)
Tool: Read(tests/test_auth.py)
...
```

That's 5–15 tool calls, thousands of tokens, and several seconds — for a single question.

## The fix

```
Tool: atlas_query(project_id="my-app", question="what functions handle token verification")

→ Result in 2ms. 0 files read.
```

## Benchmark

| | Without Atlas | With Atlas |
|---|---|---|
| Tool calls | 15 | 1 |
| Files opened | 5 | 0 |
| Grep operations | 7 | 0 |
| Time | ~8s | ~2ms |
| Tokens used | ~18,000 | ~2,300 |

**Token cost saving: ~$30/month** on a normal workload. The Pro subscription costs $19/month.

---

## Install

```bash
pip install "hornsa-atlas[parsers]"
```

Python 3.11+ required.

## Quick start

```bash
# Inside your project directory:
atlas init

# Reload your AI assistant (VS Code: Ctrl+Shift+P → Reload Window)
# Then ask your assistant anything about your codebase.
```

`atlas init` handles everything: downloads Meilisearch, indexes your project, writes the MCP config for your client automatically.

---

## MCP Tools

| Tool | What it does |
|------|-------------|
| `atlas_ingest` | Index your codebase (AST parsing, 18 languages) |
| `atlas_query` | Natural language question → structured results |
| `atlas_impact` | Find all functions affected by a change |
| `atlas_edge_check` | Full impact analysis + edge case detection |
| `atlas_status` | Check index status for a project |

## Supported languages

Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++, Ruby, PHP, Swift, Kotlin, Scala, Lua, Elixir, Dart, Zig

## Supported clients

Claude Code, Claude Desktop, Cursor, Continue (VS Code), Zed — any MCP-compatible client.

---

## How it works

Atlas parses your codebase using tree-sitter and stores every function, class, method, and module in a local Meilisearch index. At query time, a small BiLSTM model (~652K parameters, trained on your code, running at 2ms on CPU) extracts the relevant tokens from your question and retrieves the matching entities.

**No code leaves your machine.** Meilisearch runs locally. The model runs locally. The only data sent to Atlas servers is your licence key (once per day for validation) and your project ID (for free-tier limit enforcement). No source code. Ever.

---

## Pricing

| Tier | Price | Projects | Languages | Query model |
|------|-------|----------|-----------|-------------|
| Free | $0 | 1 | Python | Keyword search |
| Pro | $19/month | Unlimited | All 18 | Trained BiLSTM |
| Team | $49/seat/month | Shared indexes | All 18 | Trained BiLSTM |

[Get Atlas Pro →](https://paystack.shop/pay/atlas-pro)

---

## Pro activation

After purchase, you receive a licence key by email. Activate it once:

```bash
atlas activate ATLAS-xxxx.xxxx
```

The key is stored locally in `~/.atlas/config.json`. No ongoing server call after activation.

---

## Commands

```bash
atlas init              # Set up Atlas for the current project
atlas status            # Check index status
atlas activate <key>    # Activate a Pro licence key
atlas guard on          # Block Grep/Glob to enforce atlas_query usage
atlas guard off         # Remove the guard
atlas retrain           # Retrain the query model on updated codebase (Pro)
```

---

## Architecture

- **Ingest layer**: tree-sitter AST parsing for 18 languages, stored in Meilisearch (`code_{project_id}` index)
- **Query layer**: BiLSTM token classifier extracts KEEP tokens from natural language, fires structured query at Meilisearch
- **Edge layer**: call graph walker for impact analysis, bridge resolver for cross-language detection (HTTP routes, env vars, config files, DB schemas, shared types)
- **Transport**: MCP stdio (works with all MCP clients on Windows, Mac, Linux)
- **Local-only**: Meilisearch runs at `127.0.0.1:7700`, auto-started by Atlas

## Licence

The ingest engine, server scaffolding, and MCP tool definitions are open source (Apache 2.0).  
The trained query model weights, training pipeline, and bridge resolver are proprietary (see [Commercial Licence](https://hornsa.com/atlas/legal/commercial-licence)).

---

Built by [Horn Software Architects](https://hornsa.com) · [hornsa.com/atlas](https://hornsa.com/atlas)
