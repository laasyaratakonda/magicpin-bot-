# Vera Bot — magicpin AI Challenge submission

## Second-pass fixes (found by re-testing against the real dataset, not just re-reading code)

After the first pass, I re-verified everything against the exact scenarios in your original report and the written spec, and found two more real bugs:

1. **Customer slot-pick was broken.** A customer replying *"Yes please book me for Wed 5 Nov, 6pm"* was falling through to the generic digest-fallback and getting back *"Noted. On Dental Cleaning @ ₹299 — want me to update it..."* — nonsensical for a customer confirming a booking. This is exactly the scenario your original report showed passing (`Customer Slot Pick: ✅ Passed`), so it would have been a regression. Fixed with an explicit customer-booking-confirmation branch that runs before the generic fallback and responds with a real confirmation, echoing back the slot text when present. Verified against the exact message from your report, a bare digit choice ("1"), a short affirmative ("Yes that works"), and a longer unrelated customer message starting with "yes" (correctly *not* misclassified as a booking).
2. **`/v1/context` idempotency was wrong.** The spec says "re-posting the same version is a no-op" (200, success) and reserves 409 for a version *older* than what's stored. My code was treating same-version and older-version identically as 409. Fixed to the correct three-way behavior: higher version → replace; equal version → no-op success; lower version → 409 conflict. Added an explicit test for all three cases.
3. Fixed `contact_email` in `/v1/metadata` defaulting to magicpin's own inbox address instead of a placeholder for yours — set the `CONTACT_EMAIL` env var (see below) before you deploy, since this is presumably how they'd reach you.

## What changed since the 55/100 submission

The previous run's report flagged four concrete problems. Each is fixed directly in `bot.py`:

| Feedback | Fix |
|---|---|
| **Specificity 5/10** — bodies not grounded enough | Every trigger kind now has its own template pulling *only* real fields from the pushed context (numbers, dates, offer titles, peer stats). Triggers with placeholder/empty payloads are routed to a separate fallback that grounds in real merchant `performance`/`signals`/`offers` instead of inventing trigger-specific facts. |
| **1 body over 320 chars** | `truncate_body()` enforces a hard 320-char cap on every outgoing body, cutting at the last sentence or word boundary. Verified: 0 over-length bodies across all 30 canonical pairs. |
| **Merchant technical follow-up too generic** | `/v1/reply` now runs a keyword-overlap search (`find_relevant_digest`) over the merchant's category digest/content library before falling back to anything generic. The X-ray/D-speed-film test now correctly surfaces the DCI radiograph compliance item instead of a canned "Noted — want me to go ahead..." line. |
| **STOP handling partial** | Opt-out detection (`OPT_OUT_RE`) now runs first, before anything else, and immediately returns `action: "end"` with no further pitch — including for messages like "Stop messaging me, this is spam" that combine hostility with an opt-out. |

I also fixed a couple of bugs found while testing against the real 30-pair dataset (not just eyeballing the code):
- Auto-reply detection was comparing a message to itself on the very first turn (always "matched"). Fixed to snapshot prior turns before the current one is recorded.
- Placeholder-payload triggers (many of the 100 generated triggers only carry `{"placeholder": true}`) were being fed into kind-specific templates that assumed real fields, producing literal `"None"` in the output. Fixed to route placeholder payloads to the grounded fallback unconditionally.

## Architecture

Single-file FastAPI app (`bot.py`), **no external LLM calls** — a deterministic rule-based composer:
- Per-trigger-kind template functions (`_h_research_digest`, `_h_recall_due`, `_h_perf_dip`, etc.) for the ~28 known kinds in the dataset.
- A grounded fallback for unknown/placeholder kinds that never fabricates numbers.
- A conversation state machine in `/v1/reply` that checks, in order: opt-out → auto-reply → hostility → off-topic → intent-commitment → grounded digest match → grounded generic fallback.
- In-memory storage for context, conversations, and suppression-key dedup (resets on restart — fine for a single evaluation run; swap in Redis/Postgres if you need it to survive restarts).

Determinism means identical input always produces identical output, which is what the brief explicitly asks for, and it also means no API-key management, no latency risk against the 30-second timeout, and no hallucination risk.

## Running locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then push context and hit the endpoints as described in `examples/api-call-examples.md` from the challenge zip.

## Before you deploy — set these

The bot works with defaults, but two `/v1/metadata` fields are placeholders you should override via Render environment variables:
- `TEAM_NAME` (defaults to "Laasya" — confirm or change)
- `CONTACT_EMAIL` (defaults to a placeholder — **set this to your real email**, it's how magicpin would reach you)
- `TEAM_MEMBERS` (comma-separated, defaults to just "Laasya")

## Deploying (Render, since that's what you're already using)

1. Push this folder to a GitHub repo (or your existing `vera-bot` repo).
2. On Render: New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT` (also in the included `Procfile`).
5. Optional environment variables to personalize `/v1/metadata`: `TEAM_NAME`, `TEAM_MEMBERS` (comma-separated), `CONTACT_EMAIL`, `BOT_MODEL`, `BOT_VERSION`.
6. Once live, re-submit the same public URL (`https://vera-bot-md40.onrender.com` or a new one if you redeploy).

**Keep the service awake** — Render's free tier spins down on idle, and a cold start eating into the judge's 30-second timeout budget will look like a broken endpoint. If you're on the free tier, either upgrade or ping `/v1/healthz` every few minutes during the evaluation window.

## Testing before you resubmit

`local_harness.py` (included) pushes the full expanded dataset and replays all 30 canonical test pairs plus the four scenario tests (auto-reply, intent-transition, hostile/opt-out, technical follow-up) against a locally running instance — no LLM key needed, since it checks the same structural rules the judge's `judge_simulator.py` checks (body length, action type, actioning vs. qualifying language). Run it against your own dataset copy to sanity-check before you deploy:

```bash
python3 dataset/generate_dataset.py --seed-dir dataset --out expanded
uvicorn bot:app --host 0.0.0.0 --port 8080 &
python3 local_harness.py
```

Current local results: 29/30 triggers produce a grounded action (the 30th, a Diwali reminder 188 days out, is correctly withheld — that's restraint, not a bug), 0 over-320-char bodies, and all four scenario tests pass.

## On the "95/100" target, honestly

I can't guarantee a specific number, and you shouldn't take one from me or from any tool. Two real constraints:

1. **The judge harness explicitly tests unseen scenarios**, not the 30 canonical pairs — new digest items, metric shifts, and customer contexts injected after submission. Everything above should generalize well because it's grounded in *reading the context object*, not in memorizing these 30 pairs, but I can't run the actual harness myself to confirm a score.
2. **The final score comes from an LLM judge** scoring subjective qualities (tone, "engagement compulsion," category fit) — those have some inherent variance no amount of local testing eliminates.

What I *can* tell you with confidence: the four specific weaknesses from your 55/100 report are now directly fixed and verified against the real dataset, and the message quality is materially more specific and grounded than the previous run's sample outputs.
