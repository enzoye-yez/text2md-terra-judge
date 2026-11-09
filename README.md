# text2md terra judge (prompt v1b.1)

One file, one job: given a user request and two text2md Markdown briefs for it, predict which brief gives the better
final image (text2md md → Text2JSON → J2I), without rendering anything.

- Model `gpt-6.1-terra`, OpenAI Responses API, reasoning effort `low`, strict JSON `{reason, p_A_better, p_B_better, p_similar}`.
- Prompt **v1b.1**, verbatim as run: [`PROMPT.md`](PROMPT.md) (generated from `terra_judge.py`).
- `margin = P(md_y better) − P(md_x better)` ∈ [−1, 1]; `|margin|` is the confidence.
- Default: each pair is judged in **both A/B orders** and the two margins are averaged, which removes position bias.
  This is the setting the numbers below were measured with. `--orders 1` makes one call per pair (A/B side fixed by
  `md5(pid) % 2`): half the cost, noisier.

## Run

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...          # never commit it
python terra_judge.py examples/pairs.jsonl out.jsonl            # --orders 2 (default) --concurrency 24 --threshold 0.8
python evaluate.py out.jsonl examples/labels.jsonl              # coverage / precision against labels, no API calls
```

Input, one pair per line: `{"pid", "prompt", "aspect_ratio"?, "md_x", "md_y"}` (`pid` unique). Output, one row per pair:
`{"pid", "margin", "margins", "orders", "probs", "reasons", "usage", "model"}`; `margin > 0` means `md_y` is better.
Re-running with the same output file only judges the pairs that are missing or failed.

From Python: `terra_judge.request_body(prompt, aspect_ratio, md_a, md_b)` gives the request body (also usable as the
`body` of a Batch API line, at half the price), `terra_judge.parse_body(response)` reads the answer.

## How far to trust it

Measured on Aran's 27B text2md rollouts (8 mds per query, every md rendered and scored with track-specific PRISM
image scores), both orders, mean |margin|. Tuned on a DEV half of the queries, checked on the TEST half; PRISM 700 / VQA
were never used for tuning.

| mean \|margin\| | image agrees with the judge | big wrong calls (image prefers the other md by ≥ 1) |
|---|---|---|
| ≥ 0.8 | **0.85–0.86** | 6–7% |
| 0.7–0.8 | 0.65 | 11% |
| 0.6–0.7 | 0.61 | 15% |
| < 0.6 | 0.51–0.59 | ~10% |

Only **≥ 0.8** is reliable; below that, treat the pair as "similar". Most pairs of two decent mds land below 0.8.

Known weak spot: long requests (≥ 100 words, e.g. PRISM long_text). On the 27B data the confident calls on those agree
with the image 0.77 (84 calls) against 0.83 on shorter requests.

## Cost

About 2–4k input tokens per call (the ~1.2k-token developer message is cached after the first calls) and ~100–200
output tokens. At the prices in `PRICE` (USD per 1M tokens: input 1.0, cached 0.10, output 5.0) that is roughly
$0.002–0.004 per call online, two calls per pair with `--orders 2`; the Batch API halves it.

## Origin

Extracted from the DPO data pipeline in https://github.com/enzoye-yez/text2md-pairwise-judge, where v1b.1 decides the
DPO pairs (`dpo_pick/`, 10k pairs on 2026-11-07). v1b.1 = v1b plus one sentence in the photo / illustration item
("Two valid artistic interpretations of the same request are similar."); the sentence before it is repeated, exactly
as it was run. The request body built here is byte-identical to `pairwise_judge.request_body(..., version="v1b.1")`.
