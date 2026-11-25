import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app_example"))
os.environ["LLM_PROXY_API_KEY"] = "sk-e2e-1234"
import litellm
litellm.suppress_debug_info = True
from llm_client import LimitReached, chat

MSG = [{"role": "user", "content": "hi"}]

async def main():
    t = time.monotonic()
    try:
        await chat("alice", "gpt-4o-mini", MSG)
        print("alice: unexpected success")
    except LimitReached as e:
        print(f"alice budget: LimitReached type={e.limit_type} reset_at={e.reset_at} wait_s={e.wait_s:.0f} "
              f"after {time.monotonic()-t:.2f}s (no retries)")

    for i in range(3):
        t = time.monotonic()
        r = await chat("dave", "gpt-4o", MSG)
        print(f"dave call {i+1}: ok in {time.monotonic()-t:.2f}s -> {r.choices[0].message.content!r}")

    t = time.monotonic()
    stream = await chat("dave", "gpt-4o", MSG, stream=True)
    text = "".join([c.choices[0].delta.content or "" async for c in stream])
    print(f"dave stream: ok in {time.monotonic()-t:.2f}s -> {text!r}")

asyncio.run(main())
