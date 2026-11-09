"""text2md pairwise judge: terra prompt v1b.1 (gpt-6.1-terra, OpenAI Responses API, reasoning effort low).

Given one user request and two text2md Markdown briefs for it (md_x, md_y), predict which brief gives the better final
image, without rendering. The model answers strict JSON {reason, p_A_better, p_B_better, p_similar}.

margin = P(md_y better) - P(md_x better), in [-1, 1]; |margin| is the confidence. By default every pair is judged in
BOTH A/B orders and the two margins are averaged (this is how the thresholds in the README were measured); pass
orders=1 for one call per pair, where md5(pid) % 2 fixes which md is shown as A.

usage (CLI):  OPENAI_API_KEY=... python terra_judge.py pairs.jsonl out.jsonl [--orders 2] [--concurrency 24] [--threshold 0.8]
input jsonl:  {"pid", "prompt", "aspect_ratio"?, "md_x", "md_y"}   (pid unique)
output jsonl: {"pid", "margin", "margins", "probs", "reasons", "usage", "model"}; margin > 0 means md_y is better. Resumable.
"""
import argparse, asyncio, hashlib, json, os, random, time
import httpx

MODEL = "gpt-6.1-terra"
EFFORT = "low"
PRICE = (1.0, 0.10, 5.0)  # USD per 1M tokens: input, cached input, output (online; the Batch API is half)
TRANSIENT = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524}  # 52x: Cloudflare edge errors, transient

# Prompt v1b.1, verbatim as run (the repeated sentence at the end of the photo / illustration item is part of it).
PROMPT = """You compare two Markdown design briefs, A and B, written for the same user request. Each brief is the only input to an image generation pipeline: a layout model turns it into objects and text, then an image model renders it. The pipeline never sees the request. Decide which brief will produce the better final image, as judged by a person with good visual taste who also checks the image against the request.

What matters, roughly in this order:
1. Requirements kept: every explicit requirement of the request (subjects, counts, attributes, required text, exclusions, format) is present and unchanged.
2. Text on the image: the words to render are exactly right (wording, spelling, numbers, one language, no stray characters from another writing system), and the amount of text suits the format.
3. Coherence: no contradictions or physically impossible combinations; the main subject is described concretely and plausibly.
4. Design quality: a clear layout and focal point, and a fitting style, palette and composition; added details support the request.

Expanding the request is the brief's job: added composition, lighting, props and styling are good when they fit. Length, richness and amount of detail are never good or bad by themselves; do not prefer a brief for being shorter or longer.

Answer similar unless one brief will clearly give the better image. Small differences in wording, ordering or amount of detail are similar. Only pick a brief when you are confident that a careful viewer would agree.

# Avoid these wrong preferences
- Length: do not prefer the shorter brief. A longer brief is not more likely to fail just because it has more elements, more text or more detail; judge whether those additions are correct and fit the request. Before answering, ask yourself: if the two briefs were the same length, would I still pick the same one? If not, answer similar.
- Plainness: do not reward a brief for being plain, generic or safe. A richer composition with props, context and styling is better when it fits the request and keeps the requested subject in charge.
- Photos, paintings and illustrations with little or no text: most of the image quality comes from how the subject and scene are depicted. Prefer the brief whose main subject, pose, anatomy, materials, lighting and spatial arrangement are concrete, physically plausible and coherent. Differences in wording, ordering or optional props are similar. Two valid artistic interpretations of the same request are similar. Differences in wording, ordering or optional props are similar.
- Judge for a human viewer: do not favour a brief because it reads like a generic image-generation recipe (stacks of quality words such as cinematic, 8k, ultra-detailed, masterpiece). Concrete visual decisions matter; buzzwords do not.

# How to answer
First write reason in Simplified Chinese: at most 60 characters naming the difference that decides the answer (or why they are similar). Then give three probabilities that sum to 1: p_A_better, p_B_better, p_similar, meaning how likely a careful viewer comparing the two final images would say A is clearly better, B is clearly better, or neither. Use p_similar for everything you are not confident about."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reason", "p_A_better", "p_B_better", "p_similar"],
          "properties": {"reason": {"type": "string"}, "p_A_better": {"type": "number"}, "p_B_better": {"type": "number"},
                         "p_similar": {"type": "number"}}}


def user_text(prompt, ar, a, b):
    ar = f"\nTarget aspect ratio: {ar}" if ar else ""
    return f"# User request\n{prompt}{ar}\n\n# Brief A\n{a}\n\n# Brief B\n{b}\n"


def request_body(prompt, ar, md_a, md_b):
    """Responses API body for one comparison (md_a shown as Brief A); also usable as a Batch API request line body."""
    return {"model": MODEL, "reasoning": {"effort": EFFORT},
            "input": [{"role": "developer", "content": PROMPT}, {"role": "user", "content": user_text(prompt, ar, md_a, md_b)}],
            "text": {"format": {"type": "json_schema", "name": "pair", "strict": True, "schema": SCHEMA}}}


def parse_body(body):
    """Responses API body -> normalised {A_better, B_better, similar} and the reason."""
    t = json.loads("".join(c.get("text", "") for o in body["output"] if o.get("type") == "message" for c in o["content"]))
    p = {"A_better": t["p_A_better"], "B_better": t["p_B_better"], "similar": t["p_similar"]}
    p = {k: max(0.0, float(v)) for k, v in p.items()}; s = sum(p.values()) or 1.0
    return {k: v / s for k, v in p.items()}, t["reason"]


def order_of(pid):  # 0: md_x is shown as A, 1: md_y is shown as A
    return int(hashlib.md5(pid.encode()).hexdigest(), 16) % 2


def margin(probs, order):  # + means md_y is better
    m = probs["B_better"] - probs["A_better"]
    return round(m if order == 0 else -m, 6)


async def _one(client, sem, headers, pair, order, attempts=6):
    a, b = (pair["md_x"], pair["md_y"]) if order == 0 else (pair["md_y"], pair["md_x"])
    payload = request_body(pair["prompt"], pair.get("aspect_ratio"), a, b)
    err = None
    for att in range(attempts):
        async with sem:
            try:
                r = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
                if r.status_code == 200:
                    body = r.json(); probs, reason = parse_body(body)
                    return {"order": order, "probs": probs, "margin": margin(probs, order), "reason": reason,
                            "usage": body.get("usage") or {}, "model": body.get("model")}
                err = f"http_{r.status_code}"
                if r.status_code not in TRANSIENT:
                    break
            except (httpx.HTTPError, ValueError, KeyError, TypeError, asyncio.TimeoutError) as e:
                err = type(e).__name__
        await asyncio.sleep(min(60, 2 ** att + random.random()))
    return {"order": order, "error": err}


async def judge(client, sem, headers, pair, orders=2):
    """One pair -> one output row. orders=2: both A/B orders, margin = mean of the two."""
    os_ = [0, 1] if orders == 2 else [order_of(pair["pid"])]
    res = await asyncio.gather(*[_one(client, sem, headers, pair, o) for o in os_])
    bad = [r["error"] for r in res if "error" in r]
    if bad:
        return {"pid": pair["pid"], "error": bad[0]}
    ms = [r["margin"] for r in res]
    usage = {"input_tokens": sum(r["usage"].get("input_tokens", 0) for r in res),
             "cached_tokens": sum((r["usage"].get("input_tokens_details") or {}).get("cached_tokens", 0) for r in res),
             "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in res)}
    return {"pid": pair["pid"], "margin": round(sum(ms) / len(ms), 6), "margins": ms, "orders": os_,
            "probs": [r["probs"] for r in res], "reasons": [r["reason"] for r in res], "usage": usage, "model": res[0]["model"]}


async def run(pairs, out_path, orders=2, concurrency=24):
    """Judge every pair not already in out_path (resumable); appends one JSON row per pair."""
    done = {d["pid"] for d in map(json.loads, open(out_path)) if "error" not in d} if os.path.exists(out_path) else set()
    todo = [p for p in pairs if p["pid"] not in done]
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    sem = asyncio.Semaphore(concurrency); t0 = time.time(); n = errs = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        with open(out_path, "a") as f:
            for fut in asyncio.as_completed([judge(client, sem, headers, p, orders) for p in todo]):
                r = await fut; f.write(json.dumps(r, ensure_ascii=False) + "\n"); f.flush(); n += 1; errs += "error" in r
                if n % 200 == 0 or n == len(todo):
                    print(f"{n}/{len(todo)} done, {errs} errors, {time.time() - t0:.0f}s", flush=True)


def cost(rows):
    u = [d["usage"] for d in rows if "error" not in d]
    tin = sum(x["input_tokens"] for x in u); tc = sum(x["cached_tokens"] for x in u); tout = sum(x["output_tokens"] for x in u)
    return {"pairs": len(u), "input": tin, "cached": tc, "output": tout, "usd": ((tin - tc) * PRICE[0] + tc * PRICE[1] + tout * PRICE[2]) / 1e6}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("pairs"); ap.add_argument("out")
    ap.add_argument("--orders", type=int, default=2, choices=[1, 2]); ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--threshold", type=float, default=0.8)
    a = ap.parse_args()
    pairs = [json.loads(l) for l in open(a.pairs) if l.strip()]
    asyncio.run(run(pairs, a.out, a.orders, a.concurrency))
    want = {p["pid"] for p in pairs}
    rows = [d for d in map(json.loads, open(a.out)) if "error" not in d and d["pid"] in want]
    kept = [d for d in rows if abs(d["margin"]) >= a.threshold]
    c = cost(rows)
    print(f"{len(rows)}/{len(pairs)} pairs judged; |margin| >= {a.threshold}: {len(kept)} ({len(kept) / max(len(rows), 1):.1%}), "
          f"{sum(d['margin'] > 0 for d in kept)} pick md_y, {sum(d['margin'] < 0 for d in kept)} pick md_x")
    print(f"tokens: input {c['input']:,} (cached {c['cached']:,}), output {c['output']:,}; ~ ${c['usd']:.2f} online")


if __name__ == "__main__":
    main()
