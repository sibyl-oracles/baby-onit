# baby-onit — Implementation Plan

**Repo:** `sibyl-oracles/baby-onit` (private) · **License:** Apache-2.0 (inherited from onit)
**Goal:** a tiny, single-file agent harness distilled from [onit](https://github.com/sibyl-oracles/onit), designed for teaching how an agent harness works.

---

## 1. What baby-onit is

OnIt is a ~60k-LOC agent harness (terminal, web, voice, A2A frontends, MCP servers,
load balancers, verification). baby-onit keeps only the **core loop** and distills
each part into a short, heavily-commented section of **one Python file**:

```
model ⇄ agent loop ⇄ tools
   ↑           ↑          ↑
config.yaml  system     in-process
+ keychain   prompt     functions
```

Every section header in `baby_onit.py` states (a) the concept it teaches, and
(b) the onit file it was distilled from, so a reader can go from the toy to the
real thing.

## 2. Deliverables

| File | Purpose | Status |
|---|---|---|
| `baby_onit.py` | the entire harness, < 1000 LOC, Python 3.10+ | ✅ shipped — **1211 LOC** (1097 code), target not met, see §8 |
| `config.yaml` | non-secret settings (host, model, sampling, data_path) | ✅ |
| `README.md` | quick install + start, architecture walkthrough, teaching notes | ✅ |
| `pyproject.toml` | deps + `baby-onit` console script | ✅ |
| `LICENSE` | Apache-2.0 | ✅ |
| `.gitignore` | venv, `__pycache__`, secrets, index files | ✅ |

## 3. Architecture (12 sections inside `baby_onit.py`)

| # | Section | ~LOC | Teaches | Distilled from (onit) |
|---|---|---|---|---|
| S0 | Imports | 19 | one file, not 104 | — |
| S1 | Config | 40 | layered config: defaults → yaml → CLI flags | `src/configs/default.yaml`, `cli.py` |
| S2 | Keychain | 76 | secrets never in config files: env > OS keychain > 0600 file fallback | `src/setup.py` |
| S3 | Providers | 226 | one OpenAI-compatible client for vLLM/SGLang/OpenRouter/Vercel/OpenAI/Claude; native ollama client for ollama; provider auto-detect from host URL | `src/model/serving/chat.py` |
| S4 | System prompt | 24 | the harness's constitution: identity, working dir, tool routing, credentials | `src/prompts/*` |
| S5 | Tools | 222 | tool = schema + function; the minimal MCP set distilled to direct calls | `src/mcp/servers/tasks/*` |
| S6 | Registry | 85 | tool payload projection for the chat API; dispatch | `src/lib/tools.py`, `chat.py:_api_tool_payload` |
| S7 | Agent loop | 81 | the heart: stream → parse tool calls → execute → feed results back; JSON repair; repeat guard; iteration cap; context compaction | `chat.py` |
| S7.5 | Session memory | 48 | each REPL line is a fresh `agent_loop`; prior turns replayed | `chat.py` history |
| S8 | Text UI | 77 | rich: banner, spinner, streaming markdown, tool status lines | `src/ui/text.py` |
| S8.9 | doctor | 135 | one task, every known endpoint: smoke-test the whole fleet | `src/setup.py` probes |
| S9 | Setup wizard + CLI | 169 | `baby-onit setup` — same UX as `onit setup` | `src/setup.py`, `src/cli.py` |

**Actual: 1211 raw lines (1097 code, 36 comment, 78 blank)** — over the 1000-LOC
target; the overflow is feature growth, not bloat (§8).

### 3.1 Providers (S3)

All eight providers speak the OpenAI chat-completions shape; ollama is special-cased
because its cloud native API differs from its `/v1` shim:

- `openai` → `https://api.openai.com/v1`
- `openrouter` → `https://openrouter.ai/api/v1` (model must be `vendor/model`)
- `vercel` → `https://ai-gateway.vercel.sh/v1` (model `openai/gpt-4o` style)
- `claude` → `https://api.anthropic.com/v1` (rides the OpenAI-compatible dialect;
  `temperature`/`top_p` gated off — newer Claude models reject them)
- `vllm` → `http://localhost:8000/v1` (key optional, default `EMPTY`)
- `sglang` → `http://localhost:30000/v1`
- `ollama` → local `http://localhost:11434` or cloud `https://api.ollama.com`
  — uses the **native** `ollama.AsyncClient` (verified: cloud `/v1` returns 405).

Auto-detect by substring in host; explicit `provider:` key overrides. Model
auto-detect via `client.models.list()` when unset (OpenAI-compatible only).
OpenAI `gpt-5`/`gpt-6`-class models route through the **Responses API**
(`_chat_responses`) instead of chat-completions.

### 3.2 Tools (S5) — the minimal MCP set, in-process

onit runs tools in MCP servers over stdio/SSE; baby-onit distills the same tools
into plain async functions (one dispatch table, no protocol) — and documents why
MCP exists (process isolation, multi-client reuse) as a teaching note.

| Tool | Distilled from | Notes kept |
|---|---|---|
| `web_search` | `web/search/web_search.py` | Ollama web-search API primary, DuckDuckGo (`ddgs`) fallback; `type: news` variant |
| `fetch_content` | `web/search/mcp_server.py` | requests + BeautifulSoup text extraction, PDF via pypdf, image links |
| `bash` | `os/bash/mcp_server.py` | 300 s cap, cwd = data_path, stripped env, blocked-pattern list (rm -rf /, sudo, mkfs, dd of=/dev/…), output truncation, GitHub askpass injection |
| `write_file` | same | write/append modes, parent mkdir |
| `read_file` | same | text with `max_chars`, PDF text, binary → metadata only |
| `edit_file` | same | exact `old_string` → `new_string`, `replace_all`, must-match check |
| `local_search` | `local/search/toolkit.py` | parse (md/txt/csv/pdf/docx) → chunk (size/overlap) → BM25 (k1=1.5, b=0.75) → optional dense embeddings → RRF hybrid; JSON index with unchanged-file skip; **document openings** returned with results |
| `search_document` | `local/search/toolkit.py` | regex (`pattern=`) or question (`query=`) search inside one file |
| `grep` | — | recursive regex search across files, with `file_pattern` filter |

Nine tools total (the six-tool core plus `search_document` and `grep`).

`data_path` jail: every path argument is resolved with `realpath` and must stay
inside the working directory — the single most important safety idea in onit,
kept intact.

### 3.3 Agent loop (S7) — the teaching core

```
task → [loop: stream answer | tool_calls?]
         ├── no tools → print final answer, done
         └── tools   → repair JSON args → dispatch (read-only batch parallel)
                       → append tool results → guard: repeats / iteration cap
                       → compaction when prompt near context window
                       → next turn
```

Kept from onit, each with a comment explaining *why*:
- **JSON repair** for sloppy model arguments (single quotes, trailing commas) — `chat.py:_parse_tool_arguments`
- **Read-only batching** — searches/reads run concurrently, writes sequential — `chat.py:_handle_structured_tool_calls`
- **Repeat guard** — same call back-to-back ≥ 3 → steer notice; ≥ 5 → bail — `chat.py` repeated-call counters
- **Iteration cap** — `serving.max_chat_iterations` (default `-1` = no cap, matching onit; stuck runs are bounded by the repeat guard + context compaction instead)
- **Context compaction** — when `prompt_tokens > 0.85 × max_context_tokens`, summarize history with one LLM call, keep system message, end on user turn, prepend the compaction notice — `chat.py:_compact_context`
- **Token accounting** — prompt/completion totals, tok/s, turns, tool calls (TurnMetrics, distilled)

### 3.4 Keychain (S2)

Same shape as `onit setup`:
- service name `baby-onit`; **env var > keychain > file** precedence
- fallback file `~/.baby-onit/secrets.yaml`, `chmod 0600`
- secrets: `github_token` (env `GITHUB_TOKEN`), `huggingface_token` (env `HF_TOKEN`),
  `ollama_api_key` (env `OLLAMA_API_KEY`), plus one endpoint key per host
  (`endpoint_key:<normalized-host>`)
- `getpass` prompts with a `••••last4` hint; `-` clears an entry
- GitHub/HF tokens are injected into `bash` as `GIT_ASKPASS` (token read from env
  at git's call time, never embedded in a file) and exposed as env vars for
  `huggingface-cli` — exactly onit's mechanism, simplified

### 3.5 Text UI (S8)

rich only (no web UI): Panel banner, `console.status` spinner while the model
thinks, live Markdown streaming of the answer, one-line tool status
(`Searching duckduckgo…`, `Wrote 42 lines → plan.md`), turn summary line
(`12.3s · 1.2k tok · 3 tools`). Backslash commands: `\help \model \host \key
\reset \quit` (plus `\q`, `\bye`, `\b`, `exit`). Input uses stdlib `readline`
(up/down history, in-place editing) rather than a raw-mode editor.

### 3.6 Setup wizard (S9)

`baby-onit setup` walks: provider + endpoint URL → API key (masked, stored in
keychain) → model → data_path → optional GitHub/HF/Ollama keys.
`baby-onit setup --show` prints current config, an **endpoints table** (key
status + remembered model per endpoint) and secrets, all masked.
Same interaction grammar as `onit setup` (Enter keeps, `-` clears); `d` at the
endpoint prompt **deletes** the current endpoint's key and remembered model.
`--reset` ignores existing config and starts fresh. The wizard remembers the
model per endpoint in `~/.baby-onit/models.yaml`, so re-running never erases a
setup.

### 3.7 doctor (S8.9)

`baby-onit doctor` smoke-tests **every known endpoint** with one minimal task,
concurrently, and prints a table (endpoint, key, model, status, answer, seconds,
tokens). Each probe is bounded by a 20 s timeout (`DOCTOR_TIMEOUT`) and rows
stream as they land, so a slow endpoint cannot make the command look hung.

## 4. config.yaml (separate file, as required)

```yaml
serving:
  provider: auto            # auto|ollama|vllm|sglang|openrouter|vercel|openai|claude
  host: http://localhost:11434
  model: ""                 # blank = auto-detect
  think: true               # route reasoning to a separate field, not into the answer
  max_tokens: 32768
  max_chat_iterations: -1   # -1 = no turn cap (onit's default)
  max_context_tokens: 262144 # compaction trigger threshold
  temperature: 0.6
  top_p: 0.95
data_path: ~/sandbox        # the agent's jail root
enforce_jail: true          # confine file tools to data_path
block_dangerous: true       # refuse NEVER_ASK commands in bash
verbose: false
```

Secrets never live here — only in keychain/env (§3.4).

## 5. Environment

- **uv venv preferred** (`uv venv && uv pip install -e .`), conda documented as fallback
- Python **3.10+** (no `asyncio.timeout`, no `tomllib`, no `except*` — use `asyncio.wait_for`)
- Deps: `openai`, `ollama`, `rich`, `pyyaml`, `keyring`, `httpx`, `ddgs`, `beautifulsoup4`, `pypdf` (all pure-python or wheels; heavy onit deps like fastmcp, fastapi, torch deliberately dropped)

## 6. Build order

1. ✅ Research onit (done — distilled facts saved)
2. ✅ Create private repo `sibyl-oracles/baby-onit` (done)
3. ✅ Write `baby_onit.py` section by section, keeping the LOC budget
4. ✅ Write `config.yaml`, `pyproject.toml`, `LICENSE`, `.gitignore`
5. ✅ Test: syntax/py3.10 check → `--help` → `setup --show` → tool smoke tests
   (write/read/edit/local_search) → live chat against ollama cloud + vLLM
6. ✅ Write `README.md` (quick start, architecture, teaching map)
7. ✅ Commit, push to `main`, verify on GitHub

Post-plan work (all shipped, see §8): session memory, `doctor`, Responses API,
per-endpoint model memory + `d`-to-delete, path jail + bash gate, doctor
timeout/streaming, LOC compression 1365 → 1211.

## 7. Test plan

| Check | Command | Result |
|---|---|---|
| Syntax + import | `python3 -c "import baby_onit"` | ✅ OK |
| py3.10 compat | `python3 -m py_compile baby_onit.py` + manual scan for 3.11+ syntax | ✅ clean, no `asyncio.timeout`/`tomllib`/`except*` |
| CLI surface | `python3 baby_onit.py --help`, `setup --show` | ✅ both OK |
| Tool loop | `python3 baby_onit.py "write hello.txt, read it back, then edit it"` | ✅ (live run OK) |
| Local search | index this repo, query "compaction" | ✅ hits `PLAN.md` |
| Live providers | ollama cloud (native), vLLM (OpenAI-compatible) | ✅ ollama + claude + gpt-6 (Responses) all OK |
| Self-test | `python3 _selftest.py` | ✅ `offline: PASS` |
| Fleet smoke test | `python3 baby_onit.py doctor` | ✅ 6/8 ok (2 offline locals) |
| LOC budget | `wc -l baby_onit.py` < 1000 | ❌ **1211** — see §8 |

## 8. Open items

- **LOC target not met.** `baby_onit.py` is **1211 lines** (1097 code), not
  < 1000. The author previously hit 999 (`cdfa6b0`); the 366-line growth since
  is entirely features added afterwards — `doctor` (135), Responses API (~74),
  endpoint management (~63), jail/bash gate + Tavily tier (~60), setup growth
  (~21). A compression pass (`14079ce`) took 1365 → 1211 with no behavior change;
  reaching 1000 now requires dropping a feature (doctor, Responses API, or
  multi-endpoint management), which is a product decision, not a cleanup.
- `README.md` size table is off by one (says 1212 total / 79 blank; actual
  1211 / 78).