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
| `atlas_impact` | Find changed functions and their transitive callers |
| `atlas_dependencies` | Explore caller and callee relationships bidirectionally |
| `atlas_edge_check` | Full impact analysis + edge case detection |
| `atlas_status` | Check index status for a project |

Impact and dependency tools accept entity IDs, qualified names, or unique bare
function names. Ambiguous bare names must be disambiguated with a qualified name
or entity ID. Impact follows callers only; use `atlas_dependencies` to explore
both callers and callees. Re-ingest existing projects once so their indexes
gain the ID and qualified-name filter settings required for exact resolution.

## Supported languages

Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++, Ruby, PHP, Swift, Kotlin, Scala, Lua, Elixir, Dart, Zig

## Supported clients

Claude Code, Claude Desktop, Cursor, Continue (VS Code), Zed — any MCP-compatible client.

---

## How it works

Atlas parses your codebase using tree-sitter and stores every function, class, method, and module in a local Meilisearch index. Re-ingesting prunes entities for files that have since been removed or moved. At query time, the project-trained BiLSTM extracts likely search terms and predicts result kinds; natural-language queries also retrieve lexical candidates expanded with identifier pieces and common aliases, then rerank those candidates locally with `sentence-transformers/all-MiniLM-L6-v2`. `atlas init` downloads and validates the pinned public model into the Hugging Face cache so the first query does not need to download it; query text and source code are not sent to the model host, and inference runs locally on CPU. If model setup is offline or unavailable, init reports a warning and Atlas uses lexical results until you rerun init successfully. If the model cannot be loaded at query time, Atlas returns lexical results and includes a `ranking_warning` in the response. The MiniLM encoder is a pretrained semantic retrieval model, not a substitute for a code-specific relevance benchmark. Training data combines project entities with varied natural-language templates, common software concepts, and multi-topic queries; generated-data validation scores do not guarantee real-world retrieval quality. Space-separated symbol lists are searched independently and merged with exact-name matches prioritized, while broad auth searches keep their dedicated account-flow expansion.

Run the `atlas_ingest` MCP tool after adding or changing code to refresh indexed identifier pieces and aliases. Run `atlas retrain` to regenerate project-specific training examples and update the query parser. Training uses CUDA when available and otherwise falls back to CPU. The parser has about 1.1M parameters; the MiniLM encoder is downloaded on first semantic use and remains suitable for CPU inference.

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

## Reproducible benchmark

The `atlas-benchmark` command runs each manifest question against paired baseline and Atlas
adapters. The bundled `ollama_benchmark_agent.py` adapter uses a local Ollama model and
implements source-only Read/Search/Grep/Glob tools for baseline plus Atlas query/impact
tools for the Atlas condition. Each adapter receives one JSON request on stdin and writes one
JSON response to stdout. The runner pins the repository path/commit and
model metadata in every request; keep the model, prompt, decoding settings, and agent version
identical across conditions, changing only the available tools. The manifest must explicitly
allow exactly `Read`, `Search`, `Grep`, and `Glob` for baseline, and those plus `Atlas query`
for Atlas. Do not expose Atlas results or ground truth to the baseline adapter.

```bash
atlas-benchmark run --manifest benchmarking/manifest.example.json --output-dir benchmark-results --repeats 3 --seed 42

# Generate a deterministic synthetic report to inspect output format.
atlas-benchmark example --output-dir benchmark-example
```

The manifest format is described by
[manifest.schema.json](benchmarking/manifest.schema.json), and result JSON by
[results.schema.json](benchmarking/results.schema.json). The example manifest is a template:
replace its repository, entity IDs, graph-derived expected sets, adapter commands, model
revision, and commit before using it. `question_bank.json` contains 50 repository-neutral
templates (15 discovery, 15 relationship, 10 impact, 10 cross-language); substitute real
symbols/routes and manually review expected IDs against a pinned Atlas index. Atlas call-graph
edges can be converted to stable ID sets with `derive_relationships` and bounded
`derive_impact` in `atlas.benchmarking`; those derived sets still require review, and the
published ground truth must be frozen before either model condition is evaluated.

Adapter JSON responses contain `answer`, provider-reported `input_tokens` and
`output_tokens`, `tool_calls` counts (`Read`, `Search`, `Grep`, `Glob`, `Atlas query`),
`relationship_entities`, and `impact_entities`. The runner extracts entity mentions from
inline-code spans in the answer and resolves names or `name@repository/relative/path`
predictions against the pinned catalog. Entity accuracy is valid resolved mentions divided by
all extracted mentions; it is null when no entities were marked. Relationship and impact use
exact-ID precision, recall, and F1. Audit the mention format/extractor because extraction
errors bias hallucination scores. Token counts must come from provider usage data, not estimates.

Results include each answer, timestamps, end-to-end wall-clock latency, tool counts, token
counts, scores, repository/model metadata, question ground truth, manifest and entity-catalog
SHA-256 hashes, and a Markdown summary. Preserve the exact input manifest alongside results.
The runner records extracted `entity_mentions` and resolved `entity_mention_ids`; unresolved
inline-code references remain in the mention list and count against entity accuracy. Paired
bootstrap intervals resample question-level paired means with a fixed seed. Relative reductions
are recomputed as ratios of sampled condition means; questions, not tool calls, are the sampling
units. Run multiple pinned repositories per size bucket
(small ~10k–20k LOC, medium ~50k–100k LOC, large >200k LOC), repeat questions, publish all
manifests/results and exclusions, and report uncertainty. The synthetic `example` output is
explicitly not measured evidence; a single repository or run is not sufficient evidence for
general claims. Results retain model answers and may contain sensitive repository details:
review and protect generated artifacts before publishing.

### Local Ollama adapter

The Ollama adapter sends requests only to `http://127.0.0.1:11434`; it does not use a hosted
model API or external tools. Set `model.name` to an installed Ollama model, pin its digest in
`model.version`, and record temperature/seed. For workspaces with multiple Git roots,
`prepare_benchmark.py` builds an isolated Meilisearch index and a manifest recording each
selected repository commit plus a source-tree hash. Choose source roots explicitly and keep
benchmark artifacts outside those Git repositories.

On Windows, start an isolated Meilisearch instance in one terminal (the data directory must
not be an existing production Atlas database):

```powershell
$workspace = "C:\path\to\workspace"
& "$workspace\.atlas\bin\meilisearch.exe" `
  --db-path "$workspace\.atlas\benchmarking\meili_data" `
  --http-addr 127.0.0.1:17700 --no-analytics
```

Then prepare and run in another terminal:

```powershell
python "C:\path\to\atlas\prepare_benchmark.py" `
  --workspace $workspace `
  --output-dir "$workspace\.atlas\benchmarking" `
  --atlas-package "C:\path\to\atlas" `
  --meili-url http://127.0.0.1:17700 `
  --model gemma4:e4b

python "C:\path\to\atlas\benchmarking.py" run `
  --manifest "$workspace\.atlas\benchmarking\manifest.json" `
  --output-dir "$workspace\.atlas\benchmarking\results" `
  --repeats 3 --timeout 300 --seed 42
```

The preparation utility indexes only the requested Git roots, records source and Atlas-index
pipeline hashes, and generates graph-backed question sets. Generated ground truth is explicitly
marked unreviewed: inspect each question and expected entity/edge IDs before treating a result
as publishable evidence.

## Licence

The ingest engine, server scaffolding, and MCP tool definitions are open source (Apache 2.0).  
The trained query model weights, training pipeline, and bridge resolver are proprietary (see [Commercial Licence](https://hornsa.com/atlas/legal/commercial-licence)).

---

Built by [Horn Software Architects](https://hornsa.com) · [hornsa.com/atlas](https://hornsa.com/atlas)
