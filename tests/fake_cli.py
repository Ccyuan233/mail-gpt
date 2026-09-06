"""Local subprocess fixture. No network, authentication, or real model calls."""
import json
from pathlib import Path
import sys
import uuid

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

args = sys.argv[1:]
prompt = json.loads(sys.stdin.read())
sid = args[args.index("resume") + 1] if "resume" in args else str(uuid.uuid4())
answer = "Fake answer to: " + prompt["body"]
Path(args[args.index("--output-last-message") + 1]).write_text(answer, encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": sid}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
