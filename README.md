# baby-onit

A tiny, **~1590-line single-file agent harness** distilled from [onit](https://github.com/sibyl-oracles/onit) for teaching how an agent harness actually works. Suitable for tiny devices too!

```
model <--> agent loop <--> tools
   |           |             |
config.yaml  system      in-process
+ keychain   prompt      functions
```

One Python file (`baby_onit.py`), Python 3.10+, no MCP servers, no load
balancer, no verification pass — only the core loop, with every section
labeled by (a) the concept it teaches and (b) the onit file it was distilled
from, so you can go from the toy to the real thing.

## Size

| Metric | Lines |
|---|---|
| Total (`baby_onit.py`) | 1590 |
| Code (non-blank, non-comment) | 1358 |
| Comments | 38 |
| Blank | 194 |

*Recompute with:* `wc -l baby_onit.py` and
`grep -vE '^\s*(#|$)' baby_onit.py | wc -l`. Input uses stdlib `readline`
(up/down history, in-place editing, `⏎` prompt) in ~15 lines instead of the
earlier 412-line raw-mode editor.

## Quickstart

### 1. Install

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv && source .venv/bin/activate
git clone https://github.com/sibyl-oracles/baby-onit.git
cd baby-onit
uv pip install -e . -U
```

### 2. Configure

```bash
baby-onit setup
```

Pick an endpoint, paste an API key, and choose a data directory. Setup also
prompts for the optional GitHub / Hugging Face / Tavily keys — press enter to
keep what's already stored, or type `d` to delete. Rerunning setup is safe:
blank answers keep every previous value, and the config is merged, not
overwritten.

```bash
baby-onit setup --show   # print config, all endpoints (key + remembered model),
                         # and secrets — no prompts
```

### 3. Use

**Interactive chat:**

```bash
baby-onit chat
```

**Run a single task:**

```bash
baby-onit run "show me all big files in my Downloads folder"
```

```
Here are the largest files in your Downloads folder, sorted by size:

## 🔴 Over 100 MB
| Size | File |
|---|---|
| 764 MB | `oMLX-0.6.3rc2-macos26-27.dmg` |
| 300 MB | `Codex.dmg` |
```

**Smoke-test your endpoints** before a real task:

```bash
baby-onit doctor
```

`doctor` probes every endpoint this machine knows (config host + every stored
`endpoint_key:` + presets) with one minimal task, so a dead key or an
unreachable server is caught early. Each probe is bounded at 20s and rows
stream in as they land.

### Common flags

Pass before or after the subcommand. An omitted flag leaves the config value
untouched (defaults below are the built-in values when the config is silent):

| Flag | Purpose | Default |
|---|---|---|
| `--host` | endpoint host | `http://localhost:11434` |
| `--model` | model name | auto-detect from the endpoint |
| `--data-path` | working directory | `~/baby-sandbox` |
| `--no-think` | disable thinking | on |
| `--max-iterations` | cap the agent loop | `-1` (no cap) |
| `--max-context-tokens` | compaction trigger (tokens) | `262144` |
| `--history-budget-tokens` | cap on replayed session history (tokens) | `16000` |
| `--config` | alternate config file | `~/.baby-onit/config.yaml` |

Long sessions are replayed into every request. Some providers (notably
OpenRouter stealth models) fail *gracelessly* — an empty body instead of a
400 — once the payload gets large, so the replayed history is trimmed to
`history_budget_tokens` before the first call, and the request is retried
up to 3× with backoff if the provider still returns an empty response.

A single tool result can also blow the context window on its own: a `grep`
match inside a minified-JSON or base64 file can be megabytes on one line.
Every tool result is therefore clamped to 40 000 characters before it enters
the transcript, and `grep` stops at 50 matches / 20 000 bytes / 300 chars per
line. Context compaction is checked at the top of each loop iteration against
a fresh estimate, so an oversized result is summarised *before* the request
that would carry it is sent.

## How it works

The full architecture — the section-by-section map of `baby_onit.py`, the
system diagram, the agent loop, the two client dialects, the security model,
and the secrets handling — is documented in
[docs/baby_onit_features.md](docs/baby_onit_features.md)
(PDF: [docs/baby_onit_features.pdf](docs/baby_onit_features.pdf)).

## License

Apache-2.0 (inherited from onit).
