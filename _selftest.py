#!/usr/bin/env python3
"""baby-onit self-test: registry, tools, and (with a key) one live loop."""
import asyncio, json, os, sys
import baby_onit as b

def tools_offline():
    assert len(b.TOOLS) == 8
    assert b.json_repair("{'a': 1,}") == {"a": 1}
    assert b.normalize_host("https://api.ollama.com/v1", True) == "https://api.ollama.com"
    assert b.normalize_host("http://x:8000", False) == "http://x:8000/v1"
    # claude: rides the OpenAI-compatible dialect; host must normalize to /v1
    # and label as "claude".
    assert b.normalize_host("https://api.anthropic.com/v1", False) == "https://api.anthropic.com/v1"
    assert b._provider_label("https://api.anthropic.com/v1") == "claude"

async def tools_dispatch(tmp):
    b.DATA_PATH = tmp
    await b.dispatch("write_file", {"path": "t.txt", "content": "one two"})
    assert "one two" in await b.dispatch("read_file", {"path": "t.txt"})
    await b.dispatch("edit_file", {"path": "t.txt", "old_string": "one", "new_string": "ONE"})
    assert "ONE two" in await b.dispatch("read_file", {"path": "t.txt"})
    assert "t.txt" in await b.dispatch("bash", {"command": "ls"})
    await b.dispatch("write_file", {"path": "doc.md", "content": "# Doc\n\nThis file explains BM25 ranking and retrieval augmented generation.\n"})
    hits = json.loads(await b.dispatch("local_search", {"query": "BM25 ranking"}))
    assert hits, "local_search found nothing"
    b._call_history.clear()
    for _ in range(5):
        r = await b.dispatch("bash", {"command": "echo hi"})
    assert "repeated 5 times" in r

async def live(host, model, tmp, key_env):
    if not os.environ.get(key_env):
        return f"SKIP (no {key_env})"
    b.DATA_PATH = tmp
    cfg = b.load_config()
    cfg["serving"]["host"] = host
    cfg["data_path"] = tmp
    p = b.Provider(cfg)
    p.model = model
    answer = await b.agent_loop(
        "Use write_file to create go.txt containing exactly: ok. Then read_file "
        "it, and reply with just its contents.", cfg, p)
    return answer[:120]

if __name__ == "__main__":
    tmp = "/tmp/baby_onit_selftest"
    os.makedirs(tmp, exist_ok=True)
    tools_offline()
    asyncio.run(tools_dispatch(tmp))
    print("offline: PASS")
    if len(sys.argv) > 2:
        print("live:", asyncio.run(live(sys.argv[1], sys.argv[2], tmp, sys.argv[3] if len(sys.argv) > 3 else "BABY_ONIT_API_KEY")))
