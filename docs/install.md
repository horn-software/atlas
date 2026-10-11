# Atlas Installation Guide

## Requirements

- Python 3.11 or later
- [Meilisearch](https://www.meilisearch.com/docs/learn/getting_started/installation) running locally at `localhost:7700`
- An AI coding assistant that supports MCP (Claude Desktop, Continue, Cursor, Zed)

## 1. Install Atlas

```bash
pip install atlas-mcp
```

For tree-sitter language support (TypeScript, Go, Rust, Java, C#, and 13 more):

```bash
pip install "atlas-mcp[parsers]"
```

## 2. Start Meilisearch

**macOS / Linux:**
```bash
curl -L https://install.meilisearch.com | sh
./meilisearch
```

**Windows:**
Download from [meilisearch.com/docs](https://www.meilisearch.com/docs/learn/getting_started/installation) and run `meilisearch.exe`.

**Docker:**
```bash
docker run -d -p 7700:7700 getmeili/meilisearch:latest
```

Meilisearch runs at `http://localhost:7700` with no auth key by default.

## 3. Configure your MCP client

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "atlas": {
      "command": "atlas",
      "args": ["serve"]
    }
  }
}
```

### Continue (VS Code)

Add to `.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "atlas",
      "command": "atlas serve"
    }
  ]
}
```

### Cursor

Add to Cursor settings → MCP Servers:

```
atlas: atlas serve
```

## 4. Index your codebase

Open a terminal in your project root:

```bash
atlas ingest --project my-project
```

This crawls your codebase, parses ASTs, and writes to a local Meilisearch index. Typical times:

| Codebase size | Time |
|--------------|------|
| < 1,000 files | < 10 seconds |
| 10,000 files | 1–2 minutes |
| 50,000 files | 5–10 minutes |

Re-run `atlas ingest` any time you want to update the index.

## 5. Activate Pro (optional)

If you have an Atlas Pro licence key:

```bash
atlas activate ATLAS-xxxxxxxxxxxx.xxxxxxxxxx
```

Your key is stored in `~/.atlas/config.json`. Pro features (trained model, all 18 languages, unlimited projects) activate immediately.

To check your activation status:

```bash
atlas status
```

## 6. Use Atlas

Once indexed, ask your AI assistant natural-language questions about your codebase:

- "What functions handle authentication?"
- "Which files import the database client?"
- "What would break if I rename the `process_payment` function?"
- "Find all HTTP routes that take a user ID"

Atlas answers in milliseconds from the local index — no files are sent to any cloud.

---

## Troubleshooting

**Meilisearch not running:**
```
Connection refused at localhost:7700
```
Start Meilisearch and try again.

**No results from atlas_query:**
Run `atlas ingest --project <name>` first. Check `atlas status --project <name>` to confirm the index exists.

**Pro features not activating:**
Run `atlas activate <key>` again. If the key is valid but features are still locked, check your internet connection — Atlas needs to reach the licence server once per day to refresh your session token.

---

## Uninstall

```bash
pip uninstall atlas-mcp
```

Your indexed data lives in Meilisearch only. To clear it:

```bash
atlas purge --project my-project
```

Or stop Meilisearch and delete its `data.ms/` directory.
