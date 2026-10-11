# Atlas Commercial Licence

Copyright (c) 2026 Petrus Horn trading as Horn Software Architects

## Open-Core Model

Atlas is an open-core product. Different components carry different licences:

### Open Source (Apache 2.0)

The following components are open source under the Apache 2.0 Licence:

- `atlas/ingest/` — codebase ingestion and AST parsing pipeline
- `atlas/server.py` — MCP server tool definitions and routing
- `atlas/impact.py` — call graph walker and impact analysis
- `atlas/query/net.py` — BiLSTM model architecture (randomly initialised, untrained)
- `atlas/license.py` — licence key verification (public key baked in)
- `atlas/cli.py` — command-line interface
- `atlas/usage.py` — usage counting and free-tier enforcement
- `atlas/net.py` — shared model utilities

The full Apache 2.0 licence text is at [LICENSE](LICENSE).

### Proprietary (All Rights Reserved)

The following components are proprietary and owned exclusively by Petrus Horn trading as Horn Software Architects. **They are not included in this repository.** They are delivered to paid subscribers only:

- `atlas/query/train.py` — training pipeline
- `atlas/query/generate_data.py` — training data generator
- `atlas/query/model/query_model.pt` — trained model weights
- `atlas/query/model/vocab.json` — trained vocabulary
- `atlas/query/model/config.json` — trained model config
- `atlas/keygen.py` — licence key generator
- `atlas/edge/bridge.py` — cross-language bridge detector
- `license_server/` — licence server and key management infrastructure

These components may not be reproduced, redistributed, reverse-engineered, or used to build competing products.

Access to proprietary components is governed by the [Commercial Licence Agreement](https://hornsa.com/atlas/legal/commercial-licence).

## Subscriptions

| Tier | Price | Unlocks |
|------|-------|---------|
| Free | $0 | Apache 2.0 shell, 1 project, Python only |
| Pro | $19/month | Trained model, all 18 languages, unlimited projects |
| Team | $49/seat/month | Shared indexes, SSO, audit log |

Subscribe at [hornsa.com/atlas](https://hornsa.com/atlas).

## Contact

hello@hornsoftwarearchitects.co.za
