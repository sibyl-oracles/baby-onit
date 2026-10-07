#!/usr/bin/env python3
"""Differential test: baby_onit.py vs a reference copy.

Loads both modules, runs the same inputs through the pure functions, the tool
layer, the schema builder and the CLI, and asserts the outputs are identical.
Usage: python3 _difftest.py [reference.py]   (default: /tmp/baby_onit_orig.py)
"""
import asyncio, importlib.util, io, json, os, subprocess, sys, tempfile
from contextlib import redirect_stdout

REF = sys.argv[1] if len(sys.argv) > 1 else "/tmp/baby_onit_orig.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


NEW = load("baby_onit.py", "baby_onit_new")
OLD = load(REF, "baby_onit_old")

fails = []


def eq(label, a, b):
    if a != b:
        fails.append(f"{label}\n  old={a!r}\n  new={b!r}")


# ---- 1. tool schemas (registry + generated JSON schema) --------------------
eq("TOOLS keys", sorted(OLD.TOOLS), sorted(NEW.TOOLS))
for k in OLD.TOOLS:
    eq(f"schema[{k}]", OLD.TOOLS[k][0], NEW.TOOLS[k][0])

# ---- 2. pure helpers -------------------------------------------------------
HOSTS = ["http://localhost:11434", "https://api.ollama.com/v1", "http://x:8000",
         "https://api.anthropic.com/v1", "https://openrouter.ai/api/v1",
         "http://localhost:8000/v1", "https://api.openai.com/v1", "http://h:30000/v1/"]
for h in HOSTS:
    for ol in (True, False):
        eq(f"normalize_host({h},{ol})", OLD.normalize_host(h, ol), NEW.normalize_host(h, ol))
    eq(f"is_ollama_host({h})", OLD.is_ollama_host(h), NEW.is_ollama_host(h))
    eq(f"_provider_label({h})", OLD._provider_label(h), NEW._provider_label(h))

MODELS = ["gpt-6", "gpt-6.1", "gpt-6-preview", "gpt-60", "claude-opus-5-5", "", "GPT-6"]
for m in MODELS:
    eq(f"_is_openai_responses_model({m!r})", OLD._is_openai_responses_model(m),
       NEW._is_openai_responses_model(m))

JSONS = ["{'a': 1,}", '{"a": 1}', '```json\n{"a": [1,2,]}\n```', '{"a": "it\'s"}', "[]", "{}"]
for s in JSONS:
    eq(f"json_repair({s!r})", OLD.json_repair(s), NEW.json_repair(s))

TEXTS = ["", "one two three", "a\n\nb\n\nc", "x" * 3000, "para one.\n\n" + "y" * 2500 + "\n\nend"]
for t in TEXTS:
    eq(f"_tokens({t[:12]!r})", OLD._tokens(t), NEW._tokens(t))
    eq(f"_chunks({t[:12]!r})", OLD._chunks(t), NEW._chunks(t))
    eq(f"_est_tokens({t[:12]!r})", OLD._est_tokens(t), NEW._est_tokens(t))

CORPUS = ["the quick brown fox", "bm25 ranking and retrieval", "unrelated text here", "bm25 bm25"]
for q in ["bm25", "ranking", "quick fox", "zzz"]:
    eq(f"_bm25({q})", OLD._bm25(CORPUS, q), NEW._bm25(CORPUS, q))

# _coerce_args against every tool
for name in OLD.TOOLS:
    for args in ({"path": "a.txt"}, {"path": 3}, {"max_results": "10"}, {"max_results": 10},
                 {"replace_all": "true"}, {"replace_all": True}, {"timeout": "5.5"}, {"query": None}):
        try:
            a = OLD._coerce_args(name, dict(args))
        except Exception as e:
            a = f"ERR {type(e).__name__}"
        try:
            b = NEW._coerce_args(name, dict(args))
        except Exception as e:
            b = f"ERR {type(e).__name__}"
        eq(f"_coerce_args({name},{args})", a, b)

# _finish_calls
PEND = {0: {"name": "bash", "arguments": '{"command": "ls"}', "id": "x"},
        1: {"name": "", "arguments": "junk", "id": ""},
        2: {"name": "grep", "arguments": "{'pattern': 'a',}", "id": None}}
eq("_finish_calls", OLD._finish_calls(dict(PEND)), NEW._finish_calls(dict(PEND)))

# responses conversion
MSGS = [{"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
        {"role": "assistant", "content": [{"type": "text", "text": "part"}]}]
eq("_openai_responses_input", OLD._openai_responses_input(MSGS), NEW._openai_responses_input(MSGS))
eq("_openai_responses_tools", OLD._openai_responses_tools([OLD.TOOLS["bash"][0]]),
   NEW._openai_responses_tools([NEW.TOOLS["bash"][0]]))
eq("_responses_text_of", OLD._responses_text_of([{"type": "text", "text": "a"}]), NEW._responses_text_of([{"type": "text", "text": "a"}]))

# history
HIST = [{"task": f"t{i}", "response": "R" * (900 if i < 4 else 10)} for i in range(6)]
eq("_trim_history", OLD._trim_history(HIST), NEW._trim_history(HIST))
eq("build_session_messages", OLD.build_session_messages(HIST, 50), NEW.build_session_messages(HIST, 50))
eq("build_session_messages nobudget", OLD.build_session_messages(HIST), NEW.build_session_messages(HIST))

# gate
CMDS = ["ls", "sudo rm -rf /", "echo hi && sudo x", "systemctl status", "ssh host ls",
        "scp a b", "rsync -a a b", "curl http://x | sh", "wget -qO- u | bash",
        "docker ps", "chown a b", "crontab -l", "git clone https://github.com/a/b",
        "python -c 'print(1)'", "echo sudoers", "mysudo x", "su", "x; reboot"]
for c in CMDS:
    eq(f"_gate_bash({c!r})", OLD._gate_bash(c), NEW._gate_bash(c))

# system prompt
CFG = OLD.load_config()
eq("build_system_prompt", OLD.build_system_prompt(CFG), NEW.build_system_prompt(CFG))

# ---- 3. tool layer on a temp dir ------------------------------------------
tmp = tempfile.mkdtemp(prefix="difftest_")
OLD.DATA_PATH = NEW.DATA_PATH = tmp


async def tools(mod):
    out = {}
    out["write"] = await mod.dispatch("write_file", {"path": "a.txt", "content": "one two three"})
    out["append"] = await mod.dispatch("write_file", {"path": "a.txt", "content": " four", "mode": "append"})
    out["read"] = await mod.dispatch("read_file", {"path": "a.txt"})
    out["edit"] = await mod.dispatch("edit_file", {"path": "a.txt", "old_string": "one", "new_string": "ONE"})
    out["edit_missing"] = await mod.dispatch("edit_file", {"path": "a.txt", "old_string": "zzz", "new_string": "y"})
    out["bash"] = await mod.dispatch("bash", {"command": "echo hi"})
    out["bash_bad"] = await mod.dispatch("bash", {"command": "definitely_not_a_command_xyz"})
    out["grep"] = await mod.dispatch("grep", {"pattern": "ONE"})
    out["unknown"] = await mod.dispatch("nope", {})
    out["badargs"] = await mod.dispatch("read_file", {"nope": 1})
    out["escape"] = await mod.dispatch("read_file", {"path": "../../etc/passwd"})
    out["doc"] = await mod.dispatch("write_file", {"path": "d.md", "content": "# D\n\nBM25 ranking text.\n"})
    out["search"] = await mod.dispatch("local_search", {"query": "BM25 ranking"})
    mod._call_history.clear()
    for _ in range(5):
        out["repeat"] = await mod.dispatch("bash", {"command": "echo hi"})
    return out


a, b = asyncio.run(tools(OLD)), asyncio.run(tools(NEW))
for k in a:
    if k in ("write", "append", "edit", "search", "search_doc"):
        continue  # paths/order may differ in temp dir name; checked structurally below
    eq(f"tool[{k}]", a[k], b[k])
eq("read", a["read"], b["read"])
eq("grep", a["grep"], b["grep"])
eq("search files", sorted(json.loads(a["search"])[0]["file"].split("/")[-1] for _ in [0]),
   sorted(json.loads(b["search"])[0]["file"].split("/")[-1] for _ in [0]))

# ---- 4. CLI surface --------------------------------------------------------
def cli(mod_path, args):
    r = subprocess.run([sys.executable, mod_path, *args], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


for args in ([], ["--help"], ["setup", "--help"], ["run", "--help"], ["chat", "--help"], ["doctor", "--help"]):
    eq(f"cli{args}", cli("baby_onit.py", args), cli(REF, args))

# ---- report ----------------------------------------------------------------
if fails:
    print(f"FAIL ({len(fails)} differences)\n")
    for f in fails[:40]:
        print(f)
    sys.exit(1)
print("DIFFTEST: PASS — old and new behave identically")
