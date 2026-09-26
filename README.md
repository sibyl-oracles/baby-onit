# baby-onit

A tiny, **~1000-line single-file agent harness** distilled from [onit](https://github.com/sibyl-oracles/onit) for teaching how an agent harness actually works. Suitable for tiny devices too!

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
| Total (`baby_onit.py`) | 1198 |
| Code (non-blank, non-comment) | 1085 |
| Comments | 36 |
| Blank | 77 |

*Recompute with:* `wc -l baby_onit.py` and
`grep -vE '^\s*(#|$)' baby_onit.py | wc -l`. Input uses stdlib `readline`
(up/down history, in-place editing, `❯` prompt) in ~15 lines instead of the
earlier 412-line raw-mode editor.

## Install

```bash
uv venv && source .venv/bin/activate
uv pip install -e . -U
baby-onit setup          # pick an endpoint, paste a key, choose a data dir
                         # (also sets GitHub/HF/Tavily keys; enter keeps every
                         # existing value — rerunning never erases a setup)
baby-onit setup --show   # print config, all endpoints (key + remembered
                         # model), and secrets — no prompts

baby-onit chat           # interactive

- OR run a single task -

baby-onit run "show me all big files in my Downloads folder"
...
Here are the largest files in your Downloads folder, sorted by size:

## 🔴 Over 100 MB
| Size | File |
|---|---|
| 764 MB | `oMLX-0.6.3rc2-macos26-27.dmg` |
| 300 MB | `Codex.dmg` |

```

`baby-onit doctor` smoke-tests every endpoint this machine knows (config host +
every stored `endpoint_key:` + presets) with one minimal task, so a dead key or
an unreachable server is caught before a real task. Each probe is bounded at
20s and rows stream in as they land.

Common flags (before or after the subcommand): `--host`, `--model`,
`--data-path`, `--no-think` (thinking is **on** by default), `--max-iterations`,
`--max-context-tokens`, `--config`.

## What's inside (map of `baby_onit.py`)

| Section | Purpose |
|---|---|
| S1 config loader | merges defaults < `config.yaml` < CLI flags into one dict — the single source of truth every other section reads its settings from |
| S2 keychain | resolves secrets (endpoint key, GitHub/HF tokens) at runtime: env var > OS keychain > 0600 file — so credentials never live in `config.yaml` |
| S3 provider adapters | normalizes OpenAI-compatible, Ollama-native, and OpenAI Responses (gpt-6) servers behind one `chat()` — Claude rides the OpenAI-compatible path via Anthropic's official OpenAI-SDK compat layer (`api.anthropic.com/v1`) |
| S4 system prompt | tells the model who it is, where it works, and when to use each tool — the constitution the loop sends with every call |
| S5 tools | the agent's hands: search, fetch, bash, file ops, BM25 retrieval — plain functions that do the actual work |
| S6 registry + dispatch | pairs each tool's JSON schema with its function, repairs malformed calls, and guards against repeated identical calls |
| S7 agent loop | the engine: call model → run tools → compact context → return answer, until the model stops calling tools |
| S7.5 session memory | each REPL line is a fresh task, so prior turns are replayed from `session_history.jsonl` (recent answers verbatim, older ones truncated) |
| S8 text UI | renders loop events (tool lines, streaming, footer) for the human — one-way display, never talks to the model |
| S8.9 doctor | smoke-tests every known endpoint with one minimal task (`what is the date today?`), each probe bounded at 20s, and prints a pass/fail table |
| S9 setup + CLI | entry points: `setup` writes config + secrets once so `chat`/`run` need zero flags |

## System diagram

How the sections fit together at runtime. `setup` (S9) runs once to write
config + secrets; everything else is the per-turn path of a `chat` session.

```
                        ┌──────────────────────────────┐
                        │  S9 setup + CLI (entry)      │
                        │  writes config + secrets     │
                        └──────────────┬───────────────┘
                                       │ once
                                       ▼
   ┌──────────────┐   reads    ┌──────────────┐
   │ S2 keychain  │◄───────────┤ S1 config    │  one dict: defaults < yaml < CLI
   │ env>keychain │  secrets   │ loader       │
   │ >0600 file   │            └──────┬───────┘
   └──────┬───────┘                   │ settings
          │ key                       ▼
          │            ┌─────────────────────────────┐
          │            │        S7 AGENT LOOP        │
          │            │  call model → run tools →   │
          │            │  compact → return           │
          │            └──┬───────────┬───────────┬──┘
          │               │           │           │
          │      messages │           │ tool      │ events
          │               ▼           ▼ calls     ▼
          │  ┌──────────────────┐  ┌──────────┐  ┌──────────────┐
          └─►│ S3 provider      │  │ S6 reg + │  │ S8 text UI   │
             │ adapters         │  │ dispatch │  │ renders only │
             │ OpenAI-compat /  │  └────┬─────┘  └──────────────┘
             │ Ollama native    │       │ name+args
             └────────┬─────────┘       ▼
                      │            ┌────────────────────────┐
                      │            │ S5 tools (functions)   │
                      │            │ search·fetch·bash·files│
                      │            └────────────────────────┘
                      ▼
             ┌──────────────────┐
             │  LLM server      │
             └──────────────────┘

  S4 system prompt: injected by S7 into every call — identity + tool routing.
```

## The agent loop in one paragraph

Send the message list to the model. If it returns **tool calls**, run them
(read-only ones concurrently), append each result as a tool message, and loop.
If the context is nearly full, **compact**: summarize the history with one LLM
call, keep the system message, end on a user turn. If it returns **plain
text**, that is the answer. Everything else in onit — MCP, load balancing,
verification, four frontends — hangs off these four moves.

## Two client dialects (the most instructive branch)

The same logical call has two wire shapes, and `Provider.chat()` is the single
place they are reconciled:

| | OpenAI-compatible (vLLM/SGLang/OpenRouter/…) | Ollama native |
|---|---|---|
| client | `openai.AsyncOpenAI(base_url, api_key)` | `ollama.AsyncClient(host, headers=Bearer)` |
| tool calls stream as | index-keyed deltas to accumulate | complete dicts per chunk |
| tool result message | `tool_call_id` (+ id on the call) | `tool_name` |
| assistant echo | `arguments` as JSON string | `arguments` as dict |
| usage | only with `stream_options={"include_usage": True}` | `prompt_eval_count`/`eval_count` on the final chunk |
| thinking | `extra_body.chat_template_kwargs` (vLLM/SGLang) | `think=True` (default — keeps reasoning out of the visible answer) |

**Claude is a third host on the first dialect, not a fourth dialect.**
Anthropic ships an official OpenAI-SDK compatibility layer at
`https://api.anthropic.com/v1/` that serves `/chat/completions` with the same
tool/stream/usage wire format above — tools, streaming, `stream_options`,
`max_tokens`, and usage are all fully supported; `reasoning_effort` and
`strict` are silently ignored; temperature is capped at 1 (baby-onit clamps
it); system messages are hoisted into one; thinking is adaptive and on by
default on Claude 5 models. Setup: `baby-onit setup` → preset `claude`, paste
an Anthropic key (stored as `endpoint_key:https://api.anthropic.com/v1`, or
env `BABY_ONIT_API_KEY`).
The one thing Claude does *not* support is `/v1/responses` — but that path
only ever triggers for gpt-6 models, so it never fires here.

## Security model (and what onit adds)

baby-onit now carries two of onit's safety features, distilled:

- **Path jail** — `_resolve()` resolves symlinks and `..` with `.resolve()`,
  then requires the result to stay under `data_path`. Absolute paths outside
  the jail, `..` traversal, and symlink escapes all raise `ValueError`
  (returned to the model as an error). Distilled from onit's
  `_validate_read_path`/`_validate_write_path`. Disable with
  `enforce_jail: false` in config.
- **Bash gate** — `_gate_bash()` refuses the `NEVER_ASK_COMMANDS` set
  (privilege escalation, remote access, host control: `sudo`, `docker`,
  `ssh`, `mount`, `chroot`, …) and the `curl|sh` / `wget|sh` pipe, before
  `subprocess.run` is reached. Distilled from onit's `command_policy.py`
  `NEVER_ASK_COMMANDS` + `_gate_command`. Disable with `block_dangerous: false`.

What baby-onit still lacks vs onit: the **fail-closed AST command-policy
allowlist** (onit parses the whole command and allowlists every executable)
and **human-in-the-loop approvals** (single-use tickets bound to the exact
command and session). Those two are the next things to port if you extend
this for untrusted tasks.

## Secrets

`baby-onit setup` stores the endpoint key in the OS keychain (service
`baby-onit`), falling back to `~/.baby-onit/secrets.yaml` (mode 0600). Each
endpoint's key is stored under its own name, `endpoint_key:<host>`.
Setup also prompts for the optional keys — GitHub and Hugging Face tokens
(used by `bash`'s `GIT_ASKPASS` and HF downloads) and a Tavily key (the
preferred `web_search` tier) — press enter to keep what's already stored,
or type `d` to delete.
Rerunning setup is safe: blank answers keep every previous value, and the
config is merged, not overwritten. Setup also remembers the model you used for
each endpoint (persisted in `~/.baby-onit/models.yaml`, written on auto-detect
and by the setup wizard) — switch back to a preset and its model is prefilled.
The endpoints table (shown at the end of setup and in `--show`) lists every
known endpoint with its key status and remembered model.
To delete an endpoint's configuration (its API key + remembered model), type
`d` at the `endpoint:` prompt — the wizard then re-prompts so you can pick
another endpoint or press enter to keep the current one.
`GITHUB_TOKEN` / `HF_TOKEN` / `OLLAMA_API_KEY` / `TAVILY_API_KEY` env vars
override everything; an endpoint key can be set with `BABY_ONIT_API_KEY`.
GitHub/HF tokens are injected into `bash` via `GIT_ASKPASS` — the token is read
from the environment at git's call time, never written into a file.

## License

Apache-2.0 (inherited from onit).
