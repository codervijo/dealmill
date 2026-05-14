# Dealmill

Private, local-only deal sourcing tool. Scrapes motivated-seller listings (BizBuySell, Craigslist, LoopNet, Flippa), scores them 1–10 for seller motivation with the Claude API, and surfaces high-score deals through a local Flask dashboard and a daily email digest.

- Spec & phased plan: [`docs/prd.md`](docs/prd.md)
- Agent entry point: [`AI_AGENTS.md`](AI_AGENTS.md)
- Project rules every task must follow: [`CLAUDE.md`](CLAUDE.md)

Single-user. No cloud, no auth, no external database. Runs on the owner's machine.
# dealmill
