# Configuration

How baby-onit is configured: where settings come from, every key it
understands, and how secrets are kept out of the config file.

Everything below reflects `baby_onit.py` (S1 config, S2 secrets, S9 setup/CLI)
and the sample `config.yaml` in this repo. When this document and the code
disagree, the code wins — please file an issue.

## Where configuration lives

Settings are resolved in this order (later wins):

1. Built-in `DEFAULTS` in `baby_onit.py`
2. `~/.baby-onit/config.yaml` (or the file passed with `--config`)

The merge is a deep merge: a partial `config.yaml` that only sets
`serving.model` keeps every other default. You never have to write a complete
file. A missing or corrupt config file falls back to defaults silently.

Secrets are **never** read from `config.yaml` — see "Secrets" below.

The repo-root `config.yaml` is a sample/reference copy. The file the CLI
actually reads by default is `~/.baby-onit/config.yaml`; pass `--config PATH`
to use another file.

## Keys

### `serving.*`

| Key | Default | Meaning |
|---|---|---|
| `serving.provider` | `auto` | `auto`\|`ollama`\|`vllm`\|`sglang`\|`openrouter`\|`vercel`\|`openai`\|`claude`. `auto` detects the family from the host URL. |
| `serving.host` | `http://localhost:11434` | Endpoint URL. `/v1` or `/chat/completions` suffixes are stripped, so either spelling works. |
| `serving.model` | `""` | Blank = auto-detect via `client.models.list()`. |
| `serving.think` | `true` | Route reasoning to a separate field instead of the answer. |
| `serving.max_tokens` | `32768` | Max output tokens per response. |
| `serving.max_chat_iterations` | `-1` | Agent-loop turn cap; `-1` = no cap. |
| `serving.max_context_tokens` | `262144` | Compaction trigger threshold (tokens). |
| `serving.history_budget_tokens` | `8000` | Hard cap on replayed session history (pre-call trim). |
| `serving.temperature` | `0.6` | Sampling temperature. |
| `serving.top_p` | `0.95` | Nucleus sampling. |

Notes:

- `temperature` / `top_p` are gated off for the `claude` provider (newer
  Claude models reject them).
- For OpenAI, the output cap is sent as `max_completion_tokens`; other
  providers get `max_tokens`.
- `think: true` only takes effect on hosts that expose thinking
  (vLLM/SGLang via `chat_template_kwargs`, or ollama's native thinking field).

### Top-level keys

| Key | Default | Meaning |
|---|---|---|
| `data_path` | `~/baby-sandbox` | The agent's jail root; cwd for all tools. `~` is expanded. |
| `enforce_jail` | `true` | Confine file tools to `data_path`. |
| `block_dangerous` | `true` | Refuse `NEVER_ASK` commands in `bash`. |
| `verbose` | `false` | Verbose agent-loop logging. |

## CLI flags

Flags may be passed before or after the subcommand. An omitted flag leaves
the config value untouched (defaults shown are the built-in values):

| Flag | Purpose | Default |
|---|---|---|
| `--config` | alternate config file | `~/.baby-onit/config.yaml` |
| `--host` | endpoint host | `http://localhost:11434` |
| `--model` | model name | auto-detect from the endpoint |
| `--data-path` | working directory | `~/baby-sandbox` |
| `--no-think` | disable thinking | on |
| `--max-iterations` | cap the agent loop | `-1` (no cap) |
| `--max-context-tokens` | compaction trigger (tokens) | `262144` |
| `--history-budget-tokens` | cap on replayed session history (tokens) | see note below |
| `--max-tokens` | max output tokens per response | `32768` |

> **Known discrepancy:** the built-in default for `serving.history_budget_tokens`
> in `baby_onit.py` is `8000`, while the `--history-budget-tokens` help text and
> the README say `16000`. The code value (`8000`) is what actually applies when
> the key is unset.

## Secrets

Secrets never live in `config.yaml`. `get_secret(name)` resolves in this
order, first hit wins:

1. **Environment variable** — `GITHUB_TOKEN`, `HF_TOKEN`, `OLLAMA_API_KEY`,
   `TAVILY_API_KEY` for the named services; `BABY_ONIT_API_KEY` for endpoint
   keys (`endpoint_key:<host>`), or `BABY_ONIT_<NAME>` for other names.
2. **OS keychain** — `keyring.get_password("baby-onit", name)`.
3. **0600 file** — `~/.baby-onit/secrets.yaml` (mode `0600`).

Endpoint API keys are stored as `endpoint_key:<normalized-host>`. The setup
wizard (`baby-onit setup`) writes keys to the keychain (or the fallback
file), so after a one-time setup, `baby-onit chat` needs zero flags.
`baby-onit setup --show` prints the config and secret status (no prompts);
`baby-onit setup --reset` ignores the existing config and starts fresh.

A remembered model per endpoint is stored separately in
`~/.baby-onit/models.yaml`.

## Example

```yaml
serving:
  provider: auto            # auto|ollama|vllm|sglang|openrouter|vercel|openai|claude
  host: http://localhost:11434
  model: ""                 # blank = auto-detect via client.models.list()
  think: true
  max_tokens: 32768
  max_chat_iterations: -1   # -1 = no turn cap
  max_context_tokens: 262144
  history_budget_tokens: 8000
  temperature: 0.6
  top_p: 0.95
data_path: ~/baby-sandbox
enforce_jail: true
block_dangerous: true
verbose: false
```

## Setup flow

```bash
baby-onit setup          # interactive: endpoint, key, data dir, optional keys
baby-onit setup --show   # print config + endpoints + secret status
baby-onit chat           # uses ~/.baby-onit/config.yaml
baby-onit run "task"     # one-shot
```
