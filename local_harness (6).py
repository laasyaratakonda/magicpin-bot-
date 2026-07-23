import json, sys, time
from pathlib import Path
from urllib import request as urlreq, error as urlerr

BASE = "http://localhost:8080"
EXP = Path("expanded")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urlreq.Request(BASE + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        resp = urlreq.urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read().decode())
    except urlerr.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_error": str(e)}


def load_all():
    cats, merchants, customers, triggers = {}, {}, {}, {}
    for f in (EXP / "categories").glob("*.json"):
        d = json.load(open(f)); cats[d["slug"]] = d
    for f in (EXP / "merchants").glob("*.json"):
        d = json.load(open(f)); merchants[d["merchant_id"]] = d
    for f in (EXP / "customers").glob("*.json"):
        d = json.load(open(f)); customers[d["customer_id"]] = d
    for f in (EXP / "triggers").glob("*.json"):
        d = json.load(open(f)); triggers[d["id"]] = d
    return cats, merchants, customers, triggers


def main():
    cats, merchants, customers, triggers = load_all()
    print(f"Loaded {len(cats)} categories, {len(merchants)} merchants, {len(customers)} customers, {len(triggers)} triggers")

    # push all context (warmup)
    for slug, c in cats.items():
        status, r = call("POST", "/v1/context", {"scope": "category", "context_id": slug, "version": 1, "payload": c, "delivered_at": "2026-07-17T00:00:00Z"})
        assert r.get("accepted"), r
    for mid, m in merchants.items():
        status, r = call("POST", "/v1/context", {"scope": "merchant", "context_id": mid, "version": 1, "payload": m, "delivered_at": "2026-07-17T00:00:00Z"})
        assert r.get("accepted"), r
    for cid, c in customers.items():
        status, r = call("POST", "/v1/context", {"scope": "customer", "context_id": cid, "version": 1, "payload": c, "delivered_at": "2026-07-17T00:00:00Z"})
        assert r.get("accepted"), r
    for tid, t in triggers.items():
        status, r = call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1, "payload": t, "delivered_at": "2026-07-17T00:00:00Z"})
        assert r.get("accepted"), r
    print("All base context pushed OK.")

    # idempotency check: re-post same version -> should be no-op accepted True (per spec re-posting same version = no-op but still 200);
    # our impl treats version>=cur as stale -> returns 409. Let's verify semantics with a higher version instead.
    status, r = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 2, "payload": cats["dentists"], "delivered_at": "2026-07-17T01:00:00Z"})
    print("version bump (1->2) ->", status, r.get("accepted"))
    status, r = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 2, "payload": cats["dentists"], "delivered_at": "2026-07-17T01:00:00Z"})
    print("re-post SAME version (2==2) -> expect 200 accepted=True (no-op):", status, r)
    assert status == 200 and r.get("accepted") is True, "same-version repost must be a no-op success, not a conflict"
    status, r = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 1, "payload": cats["dentists"], "delivered_at": "2026-07-17T01:00:00Z"})
    print("re-post LOWER version (1<2) -> expect 409:", status, r)
    assert status == 409, "lower version must be rejected as stale"
    print("Idempotency semantics PASS")

    status, hz = call("GET", "/v1/healthz")
    print("healthz:", hz)

    # Run through 30 canonical test pairs via /v1/tick
    test_pairs = json.load(open(EXP / "test_pairs.json"))["pairs"]
    trig_ids = [p["trigger_id"] for p in test_pairs]

    results = []
    for i in range(0, len(trig_ids), 5):
        batch = trig_ids[i:i+5]
        status, r = call("POST", "/v1/tick", {"now": "2026-07-17T10:00:00Z", "available_triggers": batch})
        actions = r.get("actions", [])
        results.extend(actions)

    print(f"\n=== TICK RESULTS: {len(results)} actions from {len(trig_ids)} triggers ===")
    over_len = 0
    for a in results:
        blen = len(a["body"])
        flag = " <<< OVER 320" if blen > 320 else ""
        if blen > 320:
            over_len += 1
        print(f"[{a['trigger_id'][:35]:35}] len={blen:3}{flag}  cta={a['cta']:12} send_as={a['send_as']}")
        print(f"    {a['body']}")
    print(f"\nTotal actions: {len(results)} / {len(trig_ids)} triggers (rest = restraint / no groundable content)")
    print(f"Over-320-char bodies: {over_len}")

    # Save submission.jsonl style output
    with open("submission_preview.jsonl", "w") as f:
        for idx, p in enumerate(test_pairs):
            match = next((a for a in results if a["trigger_id"] == p["trigger_id"]), None)
            line = {
                "test_id": p["test_id"],
                "body": match["body"] if match else "",
                "cta": match["cta"] if match else "none",
                "send_as": match["send_as"] if match else "vera",
                "suppression_key": match["suppression_key"] if match else "",
                "rationale": match["rationale"] if match else "No groundable content for this (merchant, trigger) pair — restrained from sending rather than fabricating.",
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    print("\nWrote submission_preview.jsonl")

    # ===== SCENARIO TESTS (mirroring judge_simulator.py) =====
    mid = list(merchants.keys())[0]

    print("\n=== AUTO-REPLY TEST ===")
    auto_msg = "Thank you for contacting us! Our team will respond shortly."
    for i in range(1, 5):
        status, r = call("POST", "/v1/reply", {
            "conversation_id": f"conv_auto_test", "merchant_id": mid, "customer_id": None,
            "from_role": "merchant", "message": auto_msg, "received_at": "2026-07-17T10:00:00Z", "turn_number": i + 1
        })
        print(f"Turn {i}: action={r.get('action')} body={r.get('body','')[:70]!r}")
        if r.get("action") == "end":
            print("PASS: bot ended on auto-reply")
            break
    else:
        print("FAIL: never ended")

    print("\n=== INTENT TRANSITION TEST ===")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_intent_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Ok lets do it. Whats next?", "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    body = r.get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    print(f"action={r.get('action')} body={r.get('body')!r}")
    if any(w in body for w in actioning) and not any(w in body for w in qualifying):
        print("PASS: correctly switched to action mode")
    else:
        print("FAIL / UNCLEAR")

    print("\n=== HOSTILE TEST ===")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_hostile_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.", "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    print(f"action={r.get('action')} body={r.get('body','')!r}")
    if r.get("action") == "end":
        print("PASS: correctly ended on hostile+opt-out message")
    elif r.get("action") == "send" and any(w in r.get("body","").lower() for w in ["sorry","apolog","won't"]):
        print("PASS: apologized gracefully")
    else:
        print("FAIL")

    print("\n=== OFF-TOPIC TEST (after pure hostility, no opt-out) ===")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_offtopic_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "You are so useless and annoying.", "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    print(f"Turn1 (hostile, no optout): action={r.get('action')} body={r.get('body','')!r}")
    status, r2 = call("POST", "/v1/reply", {
        "conversation_id": "conv_offtopic_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Anyway, can you also help me file my GST?", "received_at": "2026-07-17T10:05:00Z", "turn_number": 3
    })
    print(f"Turn2 (off-topic): action={r2.get('action')} body={r2.get('body','')!r}")

    print("\n=== TECHNICAL FOLLOW-UP TEST (X-ray / D-speed) ===")
    # push a fresh conversation tied to the dentist merchant so digest lookup works
    dentist_mid = "m_001_drmeera_dentist_delhi"
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_tech_test", "merchant_id": dentist_mid, "customer_id": None,
        "from_role": "merchant", "message": "Got it doc - need help auditing my X-ray setup. We have an old D-speed film unit.",
        "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    print(f"action={r.get('action')} body={r.get('body')!r}")
    print(f"rationale={r.get('rationale')}")

    print("\n=== CUSTOMER SLOT-PICK TEST ===")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_slotpick_test", "merchant_id": dentist_mid, "customer_id": "c_001_priya",
        "from_role": "customer", "message": "Yes please book me for Wed 5 Nov, 6pm.",
        "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    print(f"action={r.get('action')} body={r.get('body')!r}")
    if r.get("action") == "send" and "confirm" in r.get("body", "").lower():
        print("PASS: booking confirmed correctly, not misrouted to generic offer question")
    else:
        print("FAIL")

    print("\n=== STOP TEST (bare) ===")
    status, r = call("POST", "/v1/reply", {
        "conversation_id": "conv_stop_test", "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "STOP", "received_at": "2026-07-17T10:00:00Z", "turn_number": 2
    })
    print(f"action={r.get('action')}")
    assert r.get("action") == "end"
    print("PASS")


if __name__ == "__main__":
    main()
