"""baby-onit — a tiny, single-file agent harness distilled from onit
(https://github.com/sibyl-oracles/onit, ~60k-LOC). Keeps only the core loop;
each section header (S1..S9) names the concept it teaches and the onit file it
came from. Usage: baby-onit setup | chat | run "task"
"""

# S0. Imports — onit splits these across 104 files; a teaching harness needs one file.
from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# S1. Config loader — every knob lives in one deep-merged dict (onit: pydantic over defaults).
DEFAULTS = {
    "serving": {
        "provider": "auto",  # auto|ollama|vllm|sglang|openrouter|vercel|openai|claude
        "host": "http://localhost:11434",
        "model": "",  # blank = auto-detect via client.models.list()
        "think": True,  # route reasoning to a separate field, not into the answer
        "max_tokens": 32768,
        "max_chat_iterations": -1,  # -1 = no turn cap (onit's default)
        "max_context_tokens": 262144,  # compaction trigger threshold
        "history_budget_tokens": 16000,  # hard cap on replayed history (pre-call trim)
        "temperature": 0.6,
        "top_p": 0.95,
    },
    "data_path": "~/baby-sandbox",  # the agent's jail root (cwd for all tools)
    "enforce_jail": True,  # confine file tools to data_path (onit: _validate_*_path)
    "block_dangerous": True,  # refuse NEVER_ASK commands in bash (onit: command_policy)
}


def load_config(path: str | None = None) -> dict:
    """defaults <- ~/.baby-onit/config.yaml <- explicit path (deep-merged)."""

    def merge(base: dict, extra: dict) -> dict:
        out = dict(base)
        for k, v in (extra or {}).items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    cfg = dict(DEFAULTS)
    for cand in (path, str(Path.home() / ".baby-onit" / "config.yaml")):
        if cand and Path(cand).expanduser().is_file():
            cfg = merge(cfg, yaml.safe_load(Path(cand).expanduser().read_text()) or {})
            break
    cfg["data_path"] = str(Path(cfg["data_path"]).expanduser())
    return cfg


def _load_yaml(p: Path) -> dict:
    """yaml.safe_load of a file, or {} when it is missing or corrupt."""
    try:
        return yaml.safe_load(Path(p).read_text()) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt config falls back to defaults
        return {}


# S2. Keychain / secrets — secrets never live in config.yaml: env var > keychain > 0600 file.
SERVICE = "baby-onit"
SECRETS_FILE = Path.home() / ".baby-onit" / "secrets.yaml"
MODELS_FILE = Path.home() / ".baby-onit" / "models.yaml"
SECRET_ENV = {
    "github_token": "GITHUB_TOKEN",
    "huggingface_token": "HF_TOKEN",
    "ollama_api_key": "OLLAMA_API_KEY",
    "tavily_api_key": "TAVILY_API_KEY",
}
SECRET_NAMES = tuple(SECRET_ENV)


def _keyring(name: str, value: str | None = None) -> str | bool | None:
    """Read (value=None) or write a secret in the OS keychain; None/False on error."""
    try:
        import keyring

        return (
            keyring.get_password(SERVICE, name)
            if value is None
            else (keyring.set_password(SERVICE, name, value) or True)
        )
    except Exception:  # noqa: BLE001 — keyring backend can be missing or locked
        return None if value is None else False


def _file_secrets() -> dict:
    """The 0600 fallback file's contents ({} when missing/corrupt)."""
    return _load_yaml(SECRETS_FILE)


def _file_set(name: str, value: str | None) -> None:
    """Write (or delete, when value is None) a secret in the 0600 fallback file."""
    data = _file_secrets()
    data.pop(name, None) if value is None else data.update({name: value})
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(yaml.safe_dump(data))
    os.chmod(SECRETS_FILE, 0o600)


def get_secret(name: str, cfg: dict | None = None) -> str | None:
    """env > keychain > file. Endpoint keys are stored as endpoint_key:<host>."""
    env = os.environ.get(SECRET_ENV.get(name, "")) or os.environ.get(
        "BABY_ONIT_API_KEY" if name.startswith("endpoint_key:") else "BABY_ONIT_" + name.upper()
    )
    return env or _keyring(name) or _file_secrets().get(name)


def set_secret(name: str, value: str | None) -> str:
    """Store a secret. Returns where it landed (for the setup wizard to say)."""
    return "keychain" if value and _keyring(name, value) else (str(SECRETS_FILE), _file_set(name, value))[0]


def _load_models(base: Path | None = None) -> dict:
    """Remembered model per endpoint (models.yaml); {} when missing/corrupt."""
    return _load_yaml((base or MODELS_FILE.parent) / "models.yaml")


def _save_models(models: dict, base: Path | None = None) -> None:
    """Write models.yaml, creating the parent directory if needed."""
    p = (base or MODELS_FILE.parent) / "models.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(models, sort_keys=False))


def forget_endpoint(host: str) -> list[str]:
    """Delete one endpoint's API key (keychain + file) and remembered model."""
    norm, removed = normalize_host(host, is_ollama_host(host)), []
    if _keyring(f"endpoint_key:{norm}") or f"endpoint_key:{norm}" in _file_secrets():
        _keyring(f"endpoint_key:{norm}", "")
        _file_set(f"endpoint_key:{norm}", None)
        removed.append("API key")
    models = _load_models()
    if models.pop(norm, None):
        _save_models(models)
        removed.append("remembered model")
    return removed


def known_endpoints() -> list[dict]:
    """Endpoints ever configured: config host + endpoint_key:<host> entries."""
    out: dict[str, dict] = {}
    cfg, models = load_config(), _load_models()
    for host in (
        [cfg["serving"]["host"]]
        + [n.split(":", 1)[1] for n in _file_secrets() if n.startswith("endpoint_key:")]
        + list(PRESET_HOSTS.values())
    ):
        if not (host := (host or "").strip()):
            continue
        norm = normalize_host(host, is_ollama_host(host))
        ep = out.setdefault(
            norm, {"host": norm, "key": get_secret(f"endpoint_key:{norm}"), "model": "", "active": False}
        )
        if host == cfg["serving"]["host"]:
            ep["active"] = True
        ep["model"] = models.get(norm) or (cfg["serving"].get("model") or "" if ep["active"] else "")
    return sorted(out.values(), key=lambda e: (not e["active"], e["host"]))


# S3. Provider adapters — one chat call, many servers (any OpenAI-compatible endpoint).
def is_ollama_host(host: str) -> bool:
    """True if the host URL points at an Ollama server (native API, not /v1 shim)."""
    return "ollama" in host.lower()


def normalize_host(host: str, ollama: bool) -> str:
    """Strip /v1 or /chat/completions so either spelling works in config."""
    h = host.strip().rstrip("/")
    if ollama:
        return re.sub(r"/v1$", "", h)
    h = h.removesuffix("/chat/completions")
    return h if h.endswith("/v1") else h + "/v1"


def _responses_text_of(content) -> str:
    """Plain text of a message content: string, part list, or None."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def _is_openai_responses_model(model: str) -> bool:
    """gpt-6 family needs /v1/responses: /v1/chat/completions refuses its tool calls."""
    return bool(re.match(r"^gpt-6(\b|[.\-])", (model or "").lower()))


def _openai_responses_input(messages: list[dict]) -> list[dict]:
    """Chat messages -> Responses-API input items; reasoning is not replayed."""
    items: list[dict] = []
    for msg in messages:
        role, content = msg.get("role", "user"), msg.get("content")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": _responses_text_of(content),
                }
            )
        elif role == "assistant" and (tool_calls := msg.get("tool_calls")):
            if prose := _responses_text_of(content).strip():
                items.append({"type": "message", "role": "assistant", "content": prose})
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                args = fn.get("arguments", "{}")
                items.append(
                    {
                        "type": "function_call",
                        "name": fn.get("name", ""),
                        "call_id": tc.get("id", "") if isinstance(tc, dict) else "",
                        "arguments": args if isinstance(args, str) else json.dumps(args),
                    }
                )
        else:
            items.append({"type": "message", "role": role, "content": _responses_text_of(content)})
    return items


def _openai_responses_tools(tools: list[dict]) -> list[dict]:
    """Chat tool records -> Responses-API function tools (top-level name/params)."""
    return [
        (
            {
                "type": "function",
                "name": fn.get("name", ""),
                "strict": False,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if isinstance(t, dict) and isinstance(fn := t.get("function"), dict)
            else t
        )
        for t in tools or []
    ]


def _finish_calls(pending: dict) -> tuple[list, list]:
    """Accumulated tool-call slots -> (calls, raw_calls); skips nameless slots."""
    calls, raw = [], []
    for slot in pending.values():
        if not slot["name"]:
            continue  # truncated mid-call: no function name arrived
        try:
            args = json_repair(slot["arguments"]) if slot["arguments"] else {}
        except Exception:  # noqa: BLE001 — truncated mid-args: dispatch reports the parse error
            args = {}
        calls.append({"name": slot["name"], "arguments": args})
        raw.append(
            {"id": slot["id"], "type": "function", "function": {"name": slot["name"], "arguments": slot["arguments"]}}
        )
    return calls, raw


class Provider:
    """Thin async wrapper exposing one `chat()` for both client families."""

    def __init__(self, cfg: dict):
        """Build the client for the configured host; validate the URL up front."""
        self.cfg, self.host = cfg, cfg["serving"]["host"]
        if not re.match(r"^https?://", self.host):
            raise SystemExit(f"serving.host {self.host!r} is not a URL — run: baby-onit setup")
        self.ollama = is_ollama_host(self.host)
        self.host = normalize_host(self.host, self.ollama)
        self.model, self._model_cache = cfg["serving"]["model"] or None, None
        self.client = self._make_client()

    def _ensure_client(self):
        """Usable client, rebuilt if a previous turn closed it (separate loops)."""
        for obj in (self.client, getattr(self.client, "_client", None)):
            try:
                flag = getattr(obj, "is_closed", None)
                flag = flag() if callable(flag) else flag
            except Exception as e:  # noqa: BLE001 — client internals vary across SDK versions
                print(f"(client close probe skipped: {e})")
                continue
            if flag:
                self.client = self._make_client()
                break
        return self.client

    def _make_client(self):
        """Instantiate the right client family (ollama.AsyncClient or AsyncOpenAI)."""
        key = get_secret(f"endpoint_key:{self.host}", self.cfg)
        if self.ollama:  # ollama's client takes a float timeout, not httpx's dict
            import ollama

            return ollama.AsyncClient(
                host=self.host, timeout=300.0, headers={"Authorization": f"Bearer {key}"} if key else None
            )
        import httpx
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=self.host,
            api_key=key or "EMPTY",
            max_retries=0,
            timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
        )

    async def close_client(self) -> None:
        """Drop pooled connections on the loop that opened them."""
        try:
            await (getattr(self.client, "aclose", None) or getattr(self.client, "close", None))()
        except Exception as e:  # noqa: BLE001 — half-closed pool is harmless; the next turn reconnects
            print(f"(close skipped: {e})")

    async def list_models(self) -> list[str]:
        """List model ids visible at the endpoint (cached after first call)."""
        if self._model_cache is None:
            try:
                r = await (self.client.list() if self.ollama else self.client.models.list())
                self._model_cache = [m.model if self.ollama else m.id for m in (r.models if self.ollama else r.data)]
            except Exception:  # noqa: BLE001 — endpoint unreachable: empty cache, retried next turn
                self._model_cache = []
        return self._model_cache

    async def autodetect_model(self) -> str:
        """Pick the first visible model; set self.model and return it."""
        models = await self.list_models()
        await self.close_client()  # pool opened on this call's loop; close here
        if not models:
            raise SystemExit(f"No models visible at {self.host} — set serving.model")
        self.model = models[0]
        return self.model

    def remember_model(self, model: str | None = None) -> None:
        """Persist the model for this endpoint in models.yaml."""
        if (model := model or self.model) and self.host:
            models = _load_models()
            models[self.host] = model
            _save_models(models)

    async def _stream_with_retry(self, body: dict) -> list:
        """Stream a completion, retrying the provider's intermittent empty body."""
        for attempt in range(3):
            try:
                return [c async for c in await self.client.chat.completions.create(**body)]
            except Exception as e:
                if "empty response" not in str(e).lower():
                    raise
                if attempt == 2:
                    raise RuntimeError(
                        f"provider returned an empty response 3x in a row (model={self.model}, "
                        f"host={self.host}) — usually a provider-side flake on a large payload; "
                        f"lower serving.history_budget_tokens or set serving.model to another model"
                    ) from e
                await asyncio.sleep(1.5 * (attempt + 1))

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None, stream_cb: Callable[[str], None] | None = None
    ) -> tuple[dict, dict]:
        """One chat completion. Returns (assistant_message_dict, usage_dict)."""
        s = self.cfg["serving"]
        self._ensure_client()
        if not self.ollama and _is_openai_responses_model(self.model):
            return await self._chat_responses(messages, tools, stream_cb)
        if self.ollama:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "think": s["think"],
                "options": {"temperature": s["temperature"], "top_p": s["top_p"], "num_predict": s["max_tokens"]},
            }
            if tools:
                kwargs["tools"] = tools
            chunks = [c async for c in await self.client.chat(**kwargs)]
            content = "".join(c.message.content or "" for c in chunks)
            thinking = "".join(getattr(c.message, "thinking", "") or "" for c in chunks)
            if stream_cb:
                [stream_cb(c.message.content or "") for c in chunks]  # never stream thinking
            calls = [
                {"name": tc.function.name, "arguments": dict(tc.function.arguments or {})}
                for c in chunks
                for tc in c.message.tool_calls or []
            ]
            raw_calls = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
            u = next((getattr(c, "usage", None) or c for c in chunks if c.done), None)  # nested or flat
            usage = (
                {
                    "prompt_tokens": getattr(u, "prompt_eval_count", 0) or 0,
                    "completion_tokens": getattr(u, "eval_count", 0) or 0,
                }
                if u
                else {}
            )
            return (
                {
                    "role": "assistant",
                    "content": content,
                    "thinking": thinking,
                    "tool_calls": calls,
                    "raw_tool_calls": raw_calls,
                },
                usage,
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        if "api.anthropic.com" not in self.host:  # newer Claude models reject these params
            body["temperature"], body["top_p"] = s["temperature"], s["top_p"]
        body["max_completion_tokens" if "api.openai.com" in self.host else "max_tokens"] = s["max_tokens"]
        if s["think"] and self.host_has_thinking():
            body["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True}}
        chunks = await self._stream_with_retry(body)
        usage = next(
            (
                {"prompt_tokens": c.usage.prompt_tokens, "completion_tokens": c.usage.completion_tokens}
                for c in chunks
                if c.usage
            ),
            {},
        )
        deltas = [c.choices[0].delta for c in chunks if c.choices]
        content = "".join(d.content or "" for d in deltas)
        if stream_cb:
            [stream_cb(d.content or "") for d in deltas]
        pending: dict[str, dict] = {}  # index -> accumulating tool call
        for tc in (tc for d in deltas for tc in d.tool_calls or []):
            slot = pending.setdefault(tc.index, {"name": "", "arguments": "", "id": tc.id})
            if tc.function:
                slot["name"] = tc.function.name or slot["name"]
                slot["arguments"] += tc.function.arguments or ""
        calls, raw_calls = _finish_calls(pending)
        return (
            {
                "role": "assistant",
                "content": content,
                "thinking": "",
                "tool_calls": calls,
                "raw_tool_calls": raw_calls,
            },
            usage,
        )

    async def _chat_responses(self, messages, tools, stream_cb) -> tuple[dict, dict]:
        """One turn over the Responses API (gpt-6 family; onit chat.py)."""
        s = self.cfg["serving"]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": _openai_responses_input(messages),
            "max_output_tokens": s["max_tokens"],
            "store": False,
        }
        if tools:
            kwargs["tools"] = _openai_responses_tools(tools)
        content, usage, pending = "", {}, {}

        def slot(i):
            return pending.setdefault(i, {"id": "", "name": "", "arguments": ""})

        async with self.client.responses.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.output_text.delta":
                    content += event.delta
                    if stream_cb:
                        stream_cb(event.delta)
                elif etype == "response.function_call_arguments.delta":
                    slot(event.output_index)["arguments"] += event.delta or ""
                elif etype == "response.function_call_arguments.done":
                    s = slot(event.output_index)
                    for attr, k in (("arguments", "arguments"), ("name", "name"), ("call_id", "id")):
                        if v := getattr(event, attr, None):
                            s[k] = v
                elif etype == "response.output_item.added":
                    if getattr(event.item, "type", "") == "function_call":
                        s = slot(event.output_index)
                        s["id"] = getattr(event.item, "call_id", "") or s["id"]
                        s["name"] = getattr(event.item, "name", "") or s["name"]
                elif etype == "response.completed":
                    if (u := getattr(event.response, "usage", None)) is not None:
                        usage = {
                            "prompt_tokens": int(getattr(u, "input_tokens", 0) or 0),
                            "completion_tokens": int(getattr(u, "output_tokens", 0) or 0),
                        }
        calls, raw_calls = _finish_calls(pending)
        return (
            {
                "role": "assistant",
                "content": content,
                "thinking": "",
                "tool_calls": calls,
                "raw_tool_calls": raw_calls,
            },
            usage,
        )

    def host_has_thinking(self) -> bool:
        """vLLM/SGLang expose thinking via chat_template_kwargs; no-op elsewhere."""
        return any(k in self.host for k in ("vllm", "sglang", "8000", "8001", "30000"))


# S4. System prompt — the harness's constitution: identity, working dir, tool routing, credentials.
SYSTEM_PROMPT = """\
You are baby-onit, a helpful AI agent that completes tasks by calling tools.

Working directory: {data_path} (today: {today}). Every file path you write \
starts here.

Tool routing:
- Files in the working directory: read_file, grep, search_document.
- Public facts, news, current events: web search; fetch pages for details.
- Anything that can change (prices, versions, dates) needs a tool result, \
never memory. Prefer the primary source over anyone summarizing it.
- Finish with a concise answer; cite the files or URLs you used.

Credentials (pre-wired by the harness; never print, echo, or embed a token):
- GitHub: git clone/pull/push with plain https://github.com/... URLs just \
works — the harness authenticates git via GIT_ASKPASS. Never hunt for a token or put one in a URL.
- Hugging Face: HF_TOKEN rides in the bash environment for model-hub access. Use `hf download ...` or `huggingface-cli download ...`; a 401 on a gated repo means accept the terms on the model page, then retry.
"""


def build_system_prompt(cfg: dict) -> str:
    """Render the system prompt with the working directory and today's date."""
    return SYSTEM_PROMPT.format(data_path=cfg["data_path"], today=time.strftime("%Y-%m-%d"))


# S5. Tool implementations — tools are functions with a JSON-schema business card.
DATA_PATH: str = ""  # jail root, set by run(); all paths resolve inside it

# Hard ceiling on any single tool result (chars): one unbounded result (a 3 MB
# base64 grep match, a runaway log) can blow the context window in one iteration.
TOOL_RESULT_MAX_CHARS = 40_000
GREP_LINE_MAX_CHARS = 300  # a single matched line longer than this is elided
GREP_MAX_MATCHES = 50  # stop after this many matching lines
GREP_MAX_CHARS = 20_000  # ...or this many bytes of matches, whichever first


def _cap_result(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    """Clamp one tool result to `limit` chars, keeping head and tail."""
    if len(text) <= limit:
        return text
    head, tail = limit * 3 // 4, limit // 4 - 80
    return (
        f"{text[:head]}\n\n...[tool result truncated: {len(text):,} chars total, "
        f"showing first {head:,} and last {tail:,} — narrow the query "
        f"(pattern, path, file_pattern) or read a specific file]...\n\n{text[-tail:]}"
    )


# Executables refused outright in bash (onit: command_policy.py NEVER_ASK_COMMANDS):
# privilege escalation, namespace escape, host-OS writes. Matches the binary name,
# so read-only forms (`systemctl status`) are refused too; ssh/scp/rsync are NOT
# listed — the jail is about the local filesystem. block_dangerous: false disables.
# one string reads as a list; SIM905 would explode it to 43 noisy lines
NEVER_ASK_COMMANDS = frozenset(
    "sudo su doas pkexec setcap setpriv capsh chroot nsenter unshare docker dockerd podman "  # noqa: SIM905
    "nerdctl ctr containerd runc kubectl helm minikube lxc lxc-attach machinectl useradd "
    "usermod userdel groupadd gpasswd passwd chpasswd visudo chown chgrp newgrp mount "
    "umount systemctl service at crontab shutdown reboot halt poweroff".split()
)


def _resolve(path: str) -> Path:
    """Resolve a path inside DATA_PATH (the jail); ValueError on escape."""
    p, base = Path(path).expanduser(), Path(DATA_PATH).resolve()
    resolved = (base / p) if not p.is_absolute() else p
    if not load_config().get("enforce_jail", True):
        return resolved
    if (resolved := resolved.resolve()) != base and not str(resolved).startswith(str(base) + os.sep):
        raise ValueError(f"Path outside jail root {base}: {path}")
    return resolved


def _gate_bash(command: str) -> str | None:
    """Refuse dangerous bash (NEVER_ASK set + curl|sh); None = allow."""
    if not load_config().get("block_dangerous", True):
        return None
    for exe in NEVER_ASK_COMMANDS:  # match the executable as a word, not a substring
        if re.search(rf"(?:^|[;&|`$(\s]){re.escape(exe)}(?:\s|$)", command):
            return (
                f"Refused: '{exe}' is on the never-ask list "
                f"(privilege escalation, remote access, or host control)."
            )
    if re.search(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|da)?sh\b", command):
        return "Refused: piping a remote download straight into a shell."
    return None


def tool_web_search(query: str, max_results: int = 5, type: str = "web") -> str:
    """Tavily (if key) > Ollama web search API > DuckDuckGo (ddgs)."""
    max_results = max(1, min(int(max_results), 10))
    if key := get_secret("tavily_api_key"):
        try:
            import requests

            r = requests.post(
                "https://api.tavily.com/search",
                timeout=15,
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": max_results,
                    "topic": "news" if type == "news" else "general",
                },
            )
            r.raise_for_status()
            return json.dumps(
                [
                    {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])
                ]
            )
        except Exception as e:  # noqa: BLE001 — tavily is best-effort; ddgs fallback follows
            return f"(tavily failed: {e})"
    key = get_secret("ollama_api_key") or get_secret(f"endpoint_key:{load_config()['serving']['host']}")
    try:
        import ollama

        client = ollama.Client(
            host="https://api.ollama.com", headers={"Authorization": f"Bearer {key}"} if key else None
        )
        if out := [
            {"title": r.title, "url": r.url, "snippet": r.content}
            for r in client.web_search(query=query, max_results=max_results).results
        ]:
            return json.dumps(out)
    except Exception as e:  # noqa: BLE001 — ollama web search is best-effort; ddgs fallback follows
        print(f"(ollama web search failed: {e})")
    from ddgs import DDGS

    fn = DDGS(timeout=10).news if type == "news" else DDGS(timeout=10).text
    return json.dumps(
        [
            {
                "title": r.get("title"),
                "url": r.get("href") or r.get("url"),
                "snippet": r.get("body") or r.get("excerpt", ""),
            }
            for r in fn(query, max_results=max_results)
        ]
    )


def tool_fetch_content(url: str) -> str:
    """GET a page, extract text (BeautifulSoup) or PDF text (pypdf)."""
    import requests

    resp = requests.get(url, timeout=30, headers={"User-Agent": "baby-onit/0.1"})
    if "pdf" in resp.headers.get("content-type", "") or url.lower().endswith(".pdf"):
        from pypdf import PdfReader

        return "\n".join(p.extract_text() or "" for p in PdfReader(resp.content).pages)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)[:50_000]


def tool_bash(command: str, timeout: int = 300) -> str:
    """Run a shell command with cwd=DATA_PATH."""
    if gated := _gate_bash(command):
        return json.dumps({"error": gated, "command": command, "status": "refused"})
    env = dict(os.environ)
    if token := get_secret("github_token"):  # GIT_ASKPASS: git reads the token at call time
        env.update(GITHUB_TOKEN=token, GIT_ASKPASS=str(Path(__file__).with_name("_askpass.sh")))
    if token := get_secret("huggingface_token"):  # HF_TOKEN: model-hub auth
        env.setdefault("HF_TOKEN", token)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=DATA_PATH or os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # exit code is surfaced to the model, not raised
        )
        return _cap_result(
            (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr else "") or "(no output)"
        )
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"


def tool_write_file(path: str, content: str, mode: str = "write") -> str:
    """Write text to a file (mode='append' to add). Creates parent dirs."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_text(content, encoding="utf-8") if mode == "write" else p.open("a", encoding="utf-8").write(content))
    return f"wrote {len(content)} chars to {p}"


def tool_read_file(path: str, max_chars: int = 20_000) -> str:
    """Read a file's text (PDFs supported). Truncates to max_chars."""
    p = _resolve(path)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)[:max_chars]
    text = p.read_text(errors="replace")
    return text[:max_chars] + ("\n...[truncated]" if len(text) > max_chars else "")


def tool_edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace an exact old_string with new_string. Errors if ambiguous."""
    p = _resolve(path)
    text = p.read_text()
    if not (n := text.count(old_string)):
        return "ERROR: old_string not found"
    if n > 1 and not replace_all:
        return f"ERROR: old_string appears {n} times; pass replace_all=true"
    p.write_text(text.replace(old_string, new_string, -1 if replace_all else 1))
    return f"edited {p} ({n if replace_all else 1} replacement(s))"


# --- local_search: the only nontrivial tool left in -------------------------
# CONCEPT: retrieval = chunk -> index -> rank -> fuse. onit's local_search
# stop-words for BM25; one string reads as a list (SIM905 would explode it)
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or "  # noqa: SIM905
    "that the to was were will with".split()
)


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stop-words removed."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


def _chunks(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-aware chunking with overlap (same defaults as onit)."""
    paras = [p for p in text.split("\n\n") if p.strip()]
    out, cur = [], ""
    for p in paras:
        while len(p) > size:  # oversized paragraph: hard-split
            out.append(p[:size])
            p = p[size - overlap :]
        if len(cur) + len(p) + 2 > size and cur:
            out.append(cur)
            cur = cur[-overlap:] + "\n\n" + p if overlap else p
        else:
            cur = (cur + "\n\n" + p).strip()
    return out + ([cur] if cur else []) or [text]


def _bm25(corpus: list[str], query: str, k1: float = 1.5, b: float = 0.75):
    """Okapi BM25. Returns [(score, doc_idx)] sorted desc."""
    docs, qt = [_tokens(d) for d in corpus], set(_tokens(query))
    avgdl = sum(map(len, docs)) / max(len(docs), 1)
    df = Counter(t for d in docs for t in set(d))  # one pass, not one scan per term

    def score(d):
        return sum(
            math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            * (c := d.count(t))
            * (k1 + 1)
            / (c + k1 * (1 - b + b * len(d) / avgdl))
            for t in set(d) & qt
        )

    return sorted(((score(d), i) for i, d in enumerate(docs)), reverse=True)


_INDEX: dict[str, dict] = {}  # path -> {"chunks": [...], "mtime": float}


def _index_dir(root: Path) -> dict[str, dict]:
    """Build/refresh the in-memory BM25 index for files under root."""
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in (".md", ".txt", ".csv", ".json", ".yaml", ".py"):
            continue
        try:
            if (mtime := p.stat().st_mtime) != _INDEX.get(str(p), {}).get("mtime"):
                _INDEX[str(p)] = {"mtime": mtime, "chunks": _chunks(p.read_text(errors="replace"))}
        except Exception as e:  # noqa: BLE001 — unreadable file keeps its stale chunks
            print(f"(index skipped {p.name}: {e})")  # keep the stale chunk set
    return _INDEX


def tool_local_search(query: str, top_k: int = 5) -> str:
    """BM25 over DATA_PATH files. onit adds dense embeddings + RRF fusion."""
    corpus, owners = [], []
    for path, rec in _index_dir(Path(DATA_PATH)).items():
        corpus += rec["chunks"]
        owners += [(path, i) for i in range(len(rec["chunks"]))]
    if not corpus:
        return "(no indexable files under data_path)"
    hits = [(s, i) for s, i in _bm25(corpus, query)[:top_k] if s > 0]
    return json.dumps(
        [
            {
                "rank": r + 1,
                "score": round(s, 3),
                "file": owners[i][0],
                "chunk": owners[i][1],
                "text": corpus[i][:600],
            }
            for r, (s, i) in enumerate(hits)
        ]
    )


def tool_search_document(path: str, query: str = "", pattern: str = "", context_lines: int = 3) -> str:
    """Regex or question search inside one file. Distilled from onit's search_document…"""
    text = _resolve(path).read_text(errors="replace")
    if pattern:
        rx, lines = re.compile(pattern), text.splitlines()
        hits = [
            f"L{i+1}: " + "\n".join(lines[max(0, i - context_lines) : i + context_lines + 1])
            for i, line in enumerate(lines)
            if rx.search(line)
        ]
        return "\n---\n".join(hits[:20]) or "(no matches)"
    rec = _chunks(text)
    return "\n---\n".join(rec[i] for s, i in _bm25(rec, query)[:3] if s > 0) or "(no relevant section)"


def tool_grep(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
    """Recursive regex search across DATA_PATH, bounded on matches/bytes/line-len."""
    rx, out, total = re.compile(pattern), [], 0
    for p in sorted(_resolve(path).rglob(file_pattern)):
        try:
            for i, line in enumerate(p.read_text(errors="replace").splitlines()):
                if not rx.search(line):
                    continue
                line = line.strip()
                if len(line) > GREP_LINE_MAX_CHARS:  # minified JSON, base64 blobs
                    line = line[:GREP_LINE_MAX_CHARS] + f"...[{len(line):,} chars on this line]"
                hit = f"{p.relative_to(DATA_PATH)}:{i+1}: {line}"
                out.append(hit)
                total += len(hit) + 1
                if len(out) >= GREP_MAX_MATCHES or total >= GREP_MAX_CHARS:
                    return "\n".join(out) + "\n...[grep stopped: match/byte cap reached — narrow the pattern or path]"
        except Exception as e:  # noqa: BLE001 — unreadable path is skipped by grep
            print(f"(grep skipped {p.name}: {e})")
    return "\n".join(out) or "(no matches)"


# S6. Tool registry + dispatch — the model sees a *schema*; the harness runs a *function*.
TOOLS: dict[str, tuple[dict, Callable[..., str]]] = {}


def tool(name: str, description: str, fn: Callable[..., str]) -> None:
    """Register a tool + build its JSON schema from the function's type hints."""
    props, required = {}, []
    for pname, param in inspect.signature(fn).parameters.items():
        ann = param.annotation
        if isinstance(ann, str):
            ann = {"str": str, "int": int, "float": float, "bool": bool}.get(ann, str)
        jtype = {str: "string", int: "integer", float: "number", bool: "boolean"}.get(ann, "string")
        props[pname] = {"type": jtype, "description": f"{pname} ({jtype})"}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    TOOLS[name] = (
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        },
        fn,
    )


for _name, _desc, _fn in [
    ("web_search", "Search the web. Use type='news' for dated/recent items.", tool_web_search),
    ("fetch_content", "Fetch a URL and return its text (handles PDFs).", tool_fetch_content),
    (
        "bash",
        (
            "Run a shell command inside the working directory. GitHub git operations (clone/pull/push) "
            "and Hugging Face downloads are pre-authenticated — just use plain https:// URLs; never handle tokens."
        ),
        tool_bash,
    ),
    ("write_file", "Write text to a file (mode='append' to add).", tool_write_file),
    ("read_file", "Read a file's text (PDFs supported).", tool_read_file),
    ("edit_file", "Replace an exact old_string with new_string.", tool_edit_file),
    ("local_search", "BM25 search over files in the working directory.", tool_local_search),
    ("search_document", "Regex (pattern=) or question (query=) search in one file.", tool_search_document),
    ("grep", "Recursive regex search across files.", tool_grep),
]:
    tool(_name, _desc, _fn)
READ_ONLY = {"web_search", "fetch_content", "read_file", "local_search", "search_document", "grep"}
_call_history: list[tuple[str, str]] = []


def json_repair(s: str) -> Any:
    """Models emit near-JSON: single quotes, trailing commas. Fix and parse."""
    s = re.sub(r"^```[a-z]*\n|\n```$", "", s.strip())  # strip a code fence
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    return json.loads(re.sub(r"(?<!\\)'", '"', re.sub(r",\s*([}\]])", r"\1", s)))


def _coerce_args(name: str, args: dict) -> dict:
    """Cast args to the declared types (models emit `max_results: "10"`; SDKs die on it)."""
    hints = inspect.signature(TOOLS[name][1]).parameters
    out = {}
    for k, v in args.items():
        target = hints[k].annotation if k in hints else None
        if isinstance(target, str):
            target = {"int": int, "float": float, "bool": bool, "str": str}.get(target)
        try:
            if target is int and not isinstance(v, bool):
                out[k] = int(float(str(v).strip()))
            elif target is float and not isinstance(v, (int, float)):
                out[k] = float(str(v).strip())
            elif target is bool and not isinstance(v, bool):
                out[k] = str(v).strip().lower() in ("true", "1", "yes")
            else:
                out[k] = v
        except (ValueError, TypeError):
            out[k] = v  # leave it; the tool's own error path reports it
    return out


async def dispatch(name: str, args: dict) -> str:
    """Run one tool call with timeout + repeated-call guard. Returns a str."""
    if name not in TOOLS:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(TOOLS)}"
    try:
        args = args if isinstance(args, dict) else json_repair(str(args))
    except Exception:  # noqa: BLE001 — model may emit truncated JSON; the error is the reply
        return f"ERROR: could not parse arguments (truncated tool call?): {args!r}"
    key = (name, json.dumps(args := _coerce_args(name, args), sort_keys=True))
    _call_history.append(key)
    if _call_history.count(key) >= 5:
        return "ERROR: same tool call repeated 5 times — change approach or answer."
    try:
        return str(await asyncio.wait_for(asyncio.to_thread(TOOLS[name][1], **args), timeout=330))
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001 — tool errors are surfaced to the model as text
        return f"ERROR: {type(e).__name__}: {e}"


def _make_first_token_cb(sink: list) -> Callable:
    """Return a stream callback that records the first chunk's arrival time into sink."""

    def cb(_delta: str) -> None:
        if not sink:
            sink.append(time.monotonic())

    return cb


# S7. Agent loop — a while-loop with four moves: send messages, run tools, append results, stop.
async def agent_loop(
    task: str,
    cfg: dict,
    provider: Provider,
    on_event: Callable[[str, str], None] | None = None,
    max_iter: int | None = None,
    session_history: list[dict] | None = None,
    verbose: bool = False,
) -> str:
    """Run the task to completion; return the final assistant text."""
    global DATA_PATH
    DATA_PATH = cfg["data_path"]
    max_iter = max_iter if max_iter is not None else cfg["serving"]["max_chat_iterations"]
    limit = cfg["serving"]["max_context_tokens"]
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(cfg)}]
    messages += build_session_messages(session_history or [], cfg["serving"]["history_budget_tokens"])
    messages.append({"role": "user", "content": task})
    _call_history.clear()
    iteration, answer = 1, ""
    say = on_event or (lambda kind, text: None)
    while True:
        if max_iter > 0 and iteration > max_iter:
            say("turn", f"iteration cap reached ({max_iter})")
            break
        # Compact *before* sending, not after. prompt_tokens from the previous
        # call cannot see the tool results appended since, so a check placed
        # after the append is always one iteration stale — it lets a single
        # oversized result through to a 400. The estimate below is current.
        if _messages_tokens(messages) > 0.85 * limit and (max_iter <= 0 or iteration < max_iter):
            say("status", "compacting context\u2026")
            ct0 = time.monotonic()
            summary, _ = await provider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "Summarize the conversation so far: task, findings, files touched, what remains."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "\n".join(
                            f"{m['role']}: {m.get('content') or ''}"
                            + (f" {json.dumps(m['tool_calls'])}" if m.get("tool_calls") else "")
                            for m in messages[1:]
                        )[:20_000],
                    },
                ]
            )
            say("status", f"compacted in {time.monotonic() - ct0:.1f}s")
            messages = [
                messages[0],
                {"role": "user", "content": f"[compacted]\n{summary['content']}\n\nContinue the task."},
            ]
        messages = _fit_messages(messages, limit)
        t0 = time.monotonic()
        first_t: list[float] = []

        msg, usage = await provider.chat(
            messages,
            [schema for schema, _ in TOOLS.values()],
            stream_cb=_make_first_token_cb(first_t),
        )
        model_s = time.monotonic() - t0
        prompt_tokens = usage.get("prompt_tokens", 0)
        if on_event:
            on_event(
                "usage",
                json.dumps(
                    {
                        "prompt_tokens": prompt_tokens,
                        "model_s": round(model_s, 3),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "decode_s": round(model_s - (first_t[0] - t0) if first_t else model_s, 3),
                    }
                ),
            )
        calls = msg.get("tool_calls") or []
        if not calls:
            answer = msg["content"]
            if verbose:
                say("status", f"stop: no tool calls, {len(answer)} chars of content")
            break
        raw_calls = msg.pop("raw_tool_calls", None) or [
            {"function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls
        ]
        for c, raw in zip(calls, raw_calls):
            c["id"] = raw.get("id") or c.get("id") or c["name"]
        messages.append({"role": "assistant", "content": msg["content"], "tool_calls": raw_calls})

        async def run_one(c):
            say("tool", f"{c['name']}({json.dumps(c['arguments'])[:120]})")
            result = _cap_result(await dispatch(c["name"], c["arguments"]))
            say("result", result[:200].replace("\n", " "))
            return c, result

        results = list(await asyncio.gather(*(run_one(c) for c in calls if c["name"] in READ_ONLY)))
        results += [await run_one(c) for c in calls if c["name"] not in READ_ONLY]  # writes: serial, in order
        iteration += 1
        for c, result in results:
            # tool result for either family (ollama: tool_name; OpenAI: tool_call_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_name": c["name"],
                    "name": c["name"],
                    "tool_call_id": c.get("id") or c["name"],
                    "content": result,
                }
            )
    if answer:
        return answer
    # Two exit paths: a real cap (max_iter > 0) vs. the model ending the turn
    # with no tool calls AND no content (empty final message).
    return (
        f"(iteration cap reached after {max_iter} turns without a final answer)"
        if max_iter > 0
        else "(model ended the turn with no tool calls and no final answer \u2014 rephrase or retry)"
    )


# S7.5. Session memory — each REPL line is a fresh agent_loop task; prior turns are replayed.
HISTORY_FILE = "session_history.jsonl"
HISTORY_KEEP_FULL = 3  # most recent answers kept verbatim
HISTORY_DECAY_CHARS = 800  # older answers cut to this many chars


def _est_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Good enough to size a history budget."""
    return max(1, (len(text) + 3) // 4)


def _msg_text(m: dict) -> str:
    """One message as flat text: its content plus any tool calls."""
    return (m.get("content") or "") + (json.dumps(m["tool_calls"]) if m.get("tool_calls") else "")


def _messages_tokens(messages: list[dict]) -> int:
    """Estimated prompt size of a message list (look-ahead: sees pending tool results)."""
    return sum(_est_tokens(_msg_text(m)) for m in messages)


def _fit_messages(messages: list[dict], limit: int) -> list[dict]:
    """Last-resort shrink: drop the largest whole exchanges (assistant + its tool
    results, never the task/final message) until the estimate fits `limit`."""
    out = list(messages)
    while _messages_tokens(out) > limit and len(out) > 2:
        units, i = [], 1
        while i < len(out) - 1:  # never the final message
            j = i + 1
            if out[i].get("tool_calls"):
                while j < len(out) and out[j].get("role") == "tool":
                    j += 1
            units.append((_messages_tokens(out[i:j]), i, j))
            i = j
        candidates = [u for u in units if not (u[1] == 1 and out[1].get("role") == "user")]
        if not candidates:
            break  # only the task is left; nothing safe to drop
        _, lo, hi = max(candidates, key=lambda u: u[0])
        del out[lo:hi]
    return out


def _history_path(cfg: dict) -> Path:
    """Path to the session's JSONL history file."""
    return Path(cfg["data_path"]) / HISTORY_FILE


def load_session_history(cfg: dict) -> list[dict]:
    """The session's prior exchanges: [{"task", "response"}, ...]."""
    p = _history_path(cfg)
    out: list[dict] = []
    for line in p.read_text(errors="replace").splitlines() if p.is_file() else []:
        try:
            if (rec := json.loads(line)) and rec.get("task"):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def append_session_history(cfg: dict, task: str, response: str) -> None:
    """Append one exchange to the history file (one JSON per line)."""
    p = _history_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.open("a", encoding="utf-8").write(json.dumps({"task": task, "response": response}) + "\n")


def _trim_history(
    history: list[dict], keep_full: int = HISTORY_KEEP_FULL, head_chars: int = HISTORY_DECAY_CHARS
) -> list[dict]:
    """Replayed history with older answers cut to their opening; questions stay whole."""
    n, out = len(history), []
    for i, rec in enumerate(history):
        r = rec.get("response", "")
        if i < n - keep_full and len(r) > head_chars:
            r = r[:head_chars] + "\n...[earlier answer trimmed]"
        out.append({"task": rec.get("task", ""), "response": r})
    return out


def build_session_messages(history: list[dict], budget_tokens: int | None = None) -> list[dict]:
    """Replayed history as user/assistant pairs, oldest dropped until budget_tokens fits."""
    msgs = [
        m
        for rec in _trim_history(history)
        for m in [{"role": "user", "content": rec["task"]}]
        + ([{"role": "assistant", "content": rec["response"]}] if rec["response"] else [])
    ]
    if budget_tokens and budget_tokens > 0:
        total = sum(_est_tokens(m.get("content", "") or "") for m in msgs)
        while total > budget_tokens and len(msgs) > 2:
            total -= _est_tokens(msgs[0].get("content", "") or "")
            msgs.pop(0)
    return msgs


# S8. Text UI — renders loop events for the human; one-way display, never talks to the model.
def ui_banner(cfg: dict) -> None:
    """Print the startup banner (host, model, dir, tool count)."""
    from rich import box
    from rich.console import Console
    from rich.panel import Panel

    s = cfg["serving"]
    Console().print(
        Panel(
            f"[bold]baby-onit[/] — tiny agent harness distilled from onit\n"
            f"host   [cyan]{s['host']}[/]\nmodel  [cyan]{s['model'] or '(auto)'}[/]\ndir    [cyan]{cfg['data_path']}[/]\n"
            f"tools  [cyan]{len(TOOLS)}[/] · \\quit to exit · \\help for commands",
            box=box.ROUNDED,
            border_style="blue",
        )
    )


class TurnUI:
    """Per-turn status: tool lines print live, erased once the answer lands."""

    def __init__(self, console, provider, cfg: dict) -> None:
        """Set up per-turn state; console=None means non-TTY (no live output)."""
        self.model = getattr(provider, "model", None) or "?"
        self.provider = _provider_label(getattr(provider, "host", ""))
        self.data_path = cfg["data_path"]
        self.is_tty = bool(console) and sys.stdout.isatty()
        self.width = max(20, getattr(console, "width", 80))
        self._printed: list[str] = []
        self.prompt_tokens = self.completion_tokens = 0
        self.model_s = self.decode_s = 0.0

    def event(self, kind: str, text: str) -> None:
        """Handle one agent_loop event: accumulate usage or print a live line."""
        if kind == "usage":
            try:
                u = json.loads(text)
            except (ValueError, TypeError):
                return
            for k in ("prompt_tokens", "completion_tokens", "model_s", "decode_s"):
                setattr(self, k, getattr(self, k) + u.get(k, 0))
            return
        if not self.is_tty:
            return
        head = " ".join((f"call {text}" if kind == "tool" else text).split())[: self.width - 1]
        print(head, flush=True)
        self._printed.append(head)

    def finish(self) -> None:
        """Erase the intermediate tool lines once the final answer is printed."""
        if self.is_tty and (n := len(self._printed)):  # up n, clear each, back down
            print(f"\033[{n}A" + "\033[2K\n" * (n - 1) + "\033[2K", end="", flush=True)
        self._printed = []

    def footer(self) -> str:
        """One-line stats footer: model, provider, dir, tokens, tok/s, time."""
        bits = [
            f"model [cyan]{self.model}[/]",
            f"provider [cyan]{self.provider}[/]",
            f"dir [cyan]{self.data_path}[/]",
            (
                f"{self.prompt_tokens + self.completion_tokens:,} tok "
                f"({self.prompt_tokens:,} in / {self.completion_tokens:,} out)"
            ),
        ]
        if self.decode_s > 0.05:
            bits.append(f"{self.completion_tokens / self.decode_s:.1f} tok/s")
        return " · ".join(bits + [f"{self.model_s:.1f}s"])


def _provider_label(host: str) -> str:
    """Short human label for the endpoint, like onit's footer."""
    h = (host or "").lower()
    if "api.ollama.com" in h:
        return "ollama-cloud"
    if "api.anthropic.com" in h:
        return "claude"
    if "localhost" in h or "127.0.0.1" in h:
        return "openai-compat" if h.endswith("/v1") else "ollama"
    return "openai-compat" if "/v1" in h or "openai" in h else (h.split("/")[0] or "?")


def _run_turn(provider, coro):
    """One turn on a fresh loop, then close the client's pool on that loop."""

    async def _run():
        out = await coro
        await provider.close_client()
        return out

    return asyncio.run(_run())


# S8.9. doctor — smoke-test every known endpoint with one minimal task, so a dead
# key or unreachable server is caught before a real task. Each probe is bounded.
DOCTOR_TASK, DOCTOR_TIMEOUT = "what is the date today?", 20


async def _doctor_probe(provider: Provider, cfg: dict) -> dict:
    """One minimal chat (no tools) against one endpoint; report, never raise."""
    res = {
        "host": provider.host,
        "model": provider.model or "",
        "ok": False,
        "answer": "",
        "error": "",
        "model_s": 0.0,
        "prompt_tokens": 0,
    }
    t0 = time.monotonic()
    try:
        if not provider.model:
            await asyncio.wait_for(
                provider.list_models(), DOCTOR_TIMEOUT
            )  # cache under the timeout; no SystemExit in wait_for's task
            provider.model = await provider.autodetect_model()
        res["model"] = provider.model
        msg, usage = await asyncio.wait_for(
            provider.chat(
                [{"role": "system", "content": build_system_prompt(cfg)}, {"role": "user", "content": DOCTOR_TASK}],
                tools=None,
            ),
            DOCTOR_TIMEOUT,
        )
        res.update(ok=True, answer=(msg.get("content") or "").strip(), prompt_tokens=usage.get("prompt_tokens", 0))
    except Exception as e:  # noqa: BLE001 — a dead endpoint must not sink the doctor run
        res["error"] = (
            f"timed out after {DOCTOR_TIMEOUT}s"
            if isinstance(e, asyncio.TimeoutError)
            else f"{type(e).__name__}: {e}"[:200]
        )
    res["model_s"] = round(time.monotonic() - t0, 1)
    return res


def cmd_doctor(args, cfg: dict) -> None:
    """Probe every known endpoint with DOCTOR_TASK; print a pass/fail table."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    endpoints = known_endpoints()
    if not endpoints:
        console.print("[yellow]no known endpoints — run: baby-onit setup[/]")
        return

    async def probe_all() -> list[dict]:
        # as_completed, not gather: print each row the moment it lands, so a
        # slow endpoint shows progress instead of a silent, frozen terminal.
        tasks = [
            asyncio.create_task(
                _doctor_probe(
                    Provider({**cfg, "serving": {**cfg["serving"], "host": ep["host"], "model": ep["model"]}}), cfg
                )
            )
            for ep in endpoints
        ]
        by_host = {}
        for fut in asyncio.as_completed(tasks):
            res = await fut
            by_host[res["host"]] = res
            console.print(f"[dim]  {res['host']}: {'ok' if res['ok'] else res['error'][:60]}[/]")
        return [by_host[ep["host"]] for ep in endpoints]

    results = asyncio.run(probe_all())
    tbl = Table(title=f"doctor — {DOCTOR_TASK!r} against every known endpoint")
    for col, width in (
        ("endpoint", 34),
        ("key", 10),
        ("model", 26),
        ("status", 8),
        ("reply", 30),
        ("s", 6),
        ("tokens", 8),
    ):
        tbl.add_column(col, max_width=width)
    for ep, res in zip(endpoints, results):
        err = res["error"].lower()
        status = (
            "[green]ok[/]"
            if res["ok"]
            else (
                "[red]no key[/]"
                if any(k in err for k in ("401", "403", "auth"))
                else (
                    "[red]offline[/]"
                    if any(k in err for k in ("connect", "unreachable", "timed out"))
                    else "[red]error[/]"
                )
            )
        )
        tbl.add_row(
            ep["host"],
            "••••" + ep["key"][-4:] if ep["key"] else "[dim]none[/]",
            res["model"],
            status,
            (res["answer"] if res["ok"] else res["error"])[:40],
            str(res["model_s"]),
            str(res["prompt_tokens"]),
        )
    console.print(tbl)
    n_ok = sum(r["ok"] for r in results)
    console.print(f"[{'green' if n_ok == len(results) else 'yellow'}]{n_ok}/{len(results)} endpoints ok[/]")
    for ep, res in zip(endpoints, results):
        if not res["ok"]:
            console.print(f"[dim]  {ep['host']}: {res['error']}[/]")


def ui_chat(cfg: dict) -> None:
    """Interactive REPL: each line is a fresh agent_loop task."""
    from rich.console import Console
    from rich.markdown import Markdown

    console = Console()
    ui_banner(cfg)
    provider = Provider(cfg)
    if not provider.model:
        provider.model = asyncio.run(provider.autodetect_model())
        provider.remember_model()  # persist per-endpoint so setup remembers it
        console.print(f"[dim]auto-detected model: {provider.model}[/]")
    history = load_session_history(cfg)  # S7.5: replay prior turns this session
    if history:
        console.print(f"[dim]resumed session: {len(history)} prior turn(s)[/]")
    seed = [rec["task"] for rec in history if rec.get("task", "").strip()]
    while True:
        try:
            import readline

            if seed and not readline.get_current_history_length():
                [readline.add_history(s) for s in seed]  # seed once: prior tasks
            seed = None  # readline owns history from here on
            line = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("\\quit", "\\q", "\\bye", "\\b", "exit"):
            break
        if line.startswith("\\"):
            if line == "\\help":
                console.print(
                    "\\model show model · \\host show host · \\key set key · \\reset clear session memory · \\quit exit"
                )
            elif line == "\\reset":
                _history_path(cfg).unlink(missing_ok=True)
                history = []
                console.print("[dim]session memory cleared[/]")
            elif line == "\\model":
                console.print(f"model {provider.model or '(none)'} @ {provider.host}")
            elif line == "\\host":
                console.print(provider.host)
            elif line == "\\key":
                if key := getpass.getpass("API key (enter to keep): "):
                    console.print(f"stored in {set_secret(f'endpoint_key:{provider.host}', key)}")
            else:
                continue
        turn = TurnUI(console, provider, cfg)
        try:
            answer = _run_turn(
                provider,
                agent_loop(
                    line, cfg, provider, on_event=turn.event, session_history=history, verbose=cfg.get("verbose")
                ),
            )
        except (KeyboardInterrupt, EOFError):
            turn.finish()
            console.print("[dim]interrupted[/]")
            continue
        turn.finish()
        console.print(Markdown(answer))
        console.print(f"[dim]─ {turn.footer()}[/]")
        history.append({"task": line, "response": answer})
        append_session_history(cfg, line, answer)
    console.print("[dim]bye[/]")


# S9. Setup wizard + CLI — setup writes config.yaml and stores secrets so chat needs zero flags.
PRESET_HOSTS = {
    "ollama": "http://localhost:11434",
    "ollama-cloud": "https://api.ollama.com",
    "vllm": "http://localhost:8000/v1",
    "sglang": "http://localhost:30000/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "vercel": "https://ai-gateway.vercel.sh/v1",
    "openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com/v1",
}


def _print_endpoints(console, title: str = "endpoints") -> None:
    """Table of every known endpoint: key status + remembered model."""
    from rich.table import Table

    tbl = Table(title=title)
    for col in ("endpoint", "key", "model"):
        tbl.add_column(col)
    for ep in known_endpoints():
        tbl.add_row(
            ("[bold]" if ep["active"] else "") + ep["host"],
            f"••••{ep['key'][-4:]}" if ep["key"] else "[dim]none[/]",
            ep["model"] or "[dim](auto)[/]",
        )
    console.print(tbl)


def _print_secrets(console, title: str) -> None:
    """Table of the optional secrets"""
    from rich.table import Table

    tbl = Table(title=title)
    for col in ("name", "status", "env"):
        tbl.add_column(col)
    for name in SECRET_NAMES:
        val = get_secret(name)
        tbl.add_row(name, f"[green]set (••••{val[-4:]})[/]" if val else "[dim]not set[/]", SECRET_ENV.get(name, ""))
    console.print(tbl)


def cmd_setup(args) -> None:
    """Interactive setup wizard: pick endpoint, set key, model, data path, tokens."""
    from rich.console import Console

    console = Console()
    dest = Path(args.config or Path.home() / ".baby-onit" / "config.yaml").expanduser()
    if getattr(args, "reset", False):
        prev = {}
        console.print("[yellow]--reset: starting fresh, all previous values ignored[/]")
    else:
        prev = _load_yaml(dest)
    prev_serving = prev.get("serving") or {}
    if getattr(args, "show", False):  # print config + secrets, no prompts
        cfg = load_config(args.config)
        console.print(f"[bold]config[/] : {args.config or '~/.baby-onit/config.yaml'}")
        for k in (
            "provider",
            "host",
            "model",
            "think",
            "max_tokens",
            "max_chat_iterations",
            "max_context_tokens",
            "temperature",
            "top_p",
        ):
            console.print(f"  {k:<22} {cfg['serving'].get(k, DEFAULTS['serving'].get(k))}")
        console.print(f"  {'data_path':<22} {cfg['data_path']}")
        _print_endpoints(console)
        _print_secrets(console, "secrets")
        return
    d_host = prev_serving.get("host") or DEFAULTS["serving"]["host"]
    d_model, d_path = prev_serving.get("model") or "", prev.get("data_path") or DEFAULTS["data_path"]
    norm_host = normalize_host(d_host, is_ollama_host(d_host))
    d_key = get_secret(f"endpoint_key:{norm_host}")
    console.print(
        "[bold]baby-onit setup[/] — enter a number, paste a URL, enter to keep current, "
        "or 'd' to delete the current endpoint's key + remembered model"
    )
    console.print(
        f"  current: host={d_host}  model={d_model or '(auto)'}  "
        f"key={'••••' + d_key[-4:] if d_key else '(none)'}  dir={d_path}"
    )
    for i, (name, url) in enumerate(PRESET_HOSTS.items(), 1):
        console.print(f"  {i}. {name:<13} {url}")
    _print_endpoints(console)
    choice, presets = input("endpoint: ").strip(), list(PRESET_HOSTS.values())
    if choice == "d":  # delete/clear the current endpoint's configuration
        removed = forget_endpoint(d_host)
        console.print(
            f"  {d_host}: " + (", ".join(f"deleted {r}" for r in removed) if removed else "nothing was stored")
        )
        choice = input("endpoint (enter to keep current, or pick another): ").strip()
    host = (
        d_host
        if not choice
        else (
            presets[int(choice) - 1]
            if choice.isdigit() and 1 <= int(choice) <= len(presets)
            else PRESET_HOSTS.get(choice, choice) or d_host
        )
    )
    if not re.match(r"^https?://", host):
        raise SystemExit(f"{host!r} is not a valid endpoint URL (need http:// or https://)")
    norm = normalize_host(host, is_ollama_host(host))
    if norm != norm_host:  # switched endpoints: prefill that endpoint's remembered model
        d_model = next((e["model"] for e in known_endpoints() if e["host"] == norm and e["model"]), "")
    prev_key = get_secret(f"endpoint_key:{norm}")
    key = getpass.getpass(f"API key (enter to keep {'••••' + prev_key[-4:] if prev_key else 'none'}): ")
    if key:
        console.print(f"  stored in {set_secret(f'endpoint_key:{norm}', key)}")
    elif not prev_key:
        console.print("[yellow]  no API key set — only keyless endpoints will work[/]")
    model = input(f"model [{d_model or 'auto-detect'}]: ").strip() or d_model
    data_path = input(f"data path [{d_path}]: ").strip() or d_path
    cfg = {**prev, "data_path": data_path, "serving": {**prev_serving, "host": host, "model": model}}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False))
    if model:  # remember this endpoint's model for the next setup
        models = _load_models(dest.parent)
        models[norm] = model
        _save_models(models, dest.parent)
    console.print(f"[green]wrote {dest}[/] — run: baby-onit chat")
    _print_secrets(console, "optional keys")
    for name in SECRET_NAMES:
        prev_tok = get_secret(name)
        tok = getpass.getpass(
            f"{name} (enter to keep {'••••' + prev_tok[-4:] if prev_tok else 'none'}, " "'d' to delete): "
        ).strip()
        if tok == "d":
            if prev_tok:
                _keyring(name, "")
                _file_set(name, None)  # clear keychain + fallback file
            console.print(f"  {name}: {'deleted' if prev_tok else 'not set'}")
        elif tok:
            console.print(f"  {name}: stored in {set_secret(name, tok)}")
        else:
            console.print(f"  {name}: {'kept' if prev_tok else 'not set (optional)'}")
    _print_endpoints(console, title="endpoints (key + remembered model)")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, load config, dispatch to setup/chat/run."""
    ap = argparse.ArgumentParser(prog="baby-onit", description="Tiny single-file agent harness distilled from onit.")
    # Defaults are stated in the help text (onit cli.py convention), not via
    # default=, so an absent flag leaves the config value untouched.
    for flag, kw in [
        ("--config", {"help": "path to config.yaml (default: ~/.baby-onit/config.yaml)"}),
        ("--host", {"help": "endpoint URL (default: http://localhost:11434)"}),
        ("--model", {"help": "model id (default: auto-detect from the endpoint)"}),
        ("--data-path", {"help": "working directory for tools (default: ~/baby-sandbox)"}),
        ("--no-think", {"action": "store_true", "help": "disable thinking mode (default: on)"}),
        ("--max-iterations", {"type": int, "help": "cap on agent-loop turns (default: -1 = no cap)"}),
        (
            "--max-context-tokens",
            {
                "type": int,
                "help": "compaction trigger in tokens " "(default: 262144; e.g. 1000000 for a 1M-context model)",
            },
        ),
        (
            "--history-budget-tokens",
            {
                "type": int,
                "help": (
                    "cap on replayed session history "
                    "in tokens (default: 16000; lower it if a provider flakes on large payloads)"
                ),
            },
        ),
    ]:
        ap.add_argument(flag, **kw)
    sub = ap.add_subparsers(dest="cmd")
    p_setup = sub.add_parser("setup", help="configure endpoint + secrets")
    p_setup.add_argument("--show", action="store_true", help="print config + secrets, no prompts")
    p_setup.add_argument("--reset", action="store_true", help="ignore existing config, start fresh")
    p_run = sub.add_parser("run", help="run one task, print the answer, exit")
    p_run.add_argument("task", nargs="+")
    # The same flag after the subcommand too. default=SUPPRESS: when the flag
    # is absent the subparser leaves the namespace alone instead of
    # overwriting the top-level value with its own default.
    for p in (
        sub.add_parser("chat", help="interactive chat"),
        p_run,
        sub.add_parser("doctor", help="smoke-test every known endpoint with a minimal task"),
    ):
        p.add_argument(
            "--max-context-tokens",
            type=int,
            default=argparse.SUPPRESS,
            help="compaction trigger in tokens (default: 262144)",
        )
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    for k, v in (
        ("host", args.host),
        ("model", args.model),
        ("think", False if args.no_think else None),
        ("max_chat_iterations", args.max_iterations),
        ("max_context_tokens", getattr(args, "max_context_tokens", None)),
        ("history_budget_tokens", getattr(args, "history_budget_tokens", None)),
    ):
        if v is not None:
            cfg["serving"][k] = v
    if args.data_path:
        cfg["data_path"] = str(Path(args.data_path).expanduser())
    if args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "chat":
        ui_chat(cfg)
    elif args.cmd == "doctor":
        cmd_doctor(args, cfg)
    elif args.cmd == "run":
        provider = Provider(cfg)
        if not provider.model:
            provider.model = asyncio.run(provider.autodetect_model())
            provider.remember_model()
        turn = TurnUI(None, provider, cfg)
        print(
            _run_turn(
                provider,
                agent_loop(" ".join(args.task), cfg, provider, on_event=turn.event, verbose=cfg.get("verbose")),
            )
        )
        turn.finish()
        if sys.stderr.isatty():
            print(f"─ {turn.footer()}", file=sys.stderr)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
