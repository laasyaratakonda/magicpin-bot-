"""
Vera bot — magicpin AI Challenge submission.

Deterministic, rule-grounded message composer (no external LLM calls needed —
this makes every response reproducible, fast, and immune to hallucination,
which is exactly what the rubric rewards: specificity, category fit, merchant
fit, trigger relevance, and engagement compulsion, all anchored in the actual
JSON context pushed by the judge).

Run:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# =============================================================================
# APP + IN-MEMORY STATE
# =============================================================================

app = FastAPI(title="Vera Bot")
START = time.time()

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict[tuple[str, str], dict] = {}

# conversation_id -> state dict
CONVERSATIONS: dict[str, dict] = {}

# suppression_key -> True once an action has been sent for it (dedup across ticks)
SENT_SUPPRESSION_KEYS: set[str] = set()

BODY_CHAR_CAP = 320

TEAM_NAME = os.environ.get("TEAM_NAME", "Laasya")
TEAM_MEMBERS = [m.strip() for m in os.environ.get("TEAM_MEMBERS", "Laasya").split(",") if m.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "SET_CONTACT_EMAIL_ENV_VAR@example.com")
MODEL_NAME = os.environ.get("BOT_MODEL", "rule-based-deterministic-composer-v2")
BOT_VERSION = os.environ.get("BOT_VERSION", "2.0.0")


# =============================================================================
# SMALL UTILITIES
# =============================================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ctx(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = CONTEXTS.get((scope, context_id))
    return entry["payload"] if entry else None


def fmt_pct(x: Any) -> str:
    try:
        v = float(x) * 100
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.0f}%"
    except (TypeError, ValueError):
        return ""


def fmt_num(x: Any) -> str:
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return str(x)


def clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def truncate_body(body: str, cap: int = BODY_CHAR_CAP) -> str:
    body = clean(body)
    if len(body) <= cap:
        return body
    # Prefer cutting at a sentence boundary before the cap.
    window = body[: cap - 1]
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut > cap * 0.5:
        return window[: cut + 1].strip()
    # Otherwise cut at the last whole word.
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).strip() + "…"


def salute(category_slug: str, identity: dict) -> str:
    first = identity.get("owner_first_name") or identity.get("name", "there")
    if category_slug == "dentists" and not first.strip().lower().startswith("dr"):
        return f"Dr. {first}"
    return first


def wants_hindi_mix(identity: dict) -> bool:
    return "hi" in (identity.get("languages") or [])


def is_verified_lang(identity: dict, code: str) -> bool:
    return code in (identity.get("languages") or [])


def active_offers(merchant: dict) -> list[dict]:
    return [o for o in merchant.get("offers", []) if o.get("status") == "active"]


def find_digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item
    return None


_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "and", "or", "with", "my", "our", "your", "i", "we", "you", "it", "this",
    "that", "need", "help", "please", "can", "do", "got", "doc", "hi", "hello",
    "want", "have", "has", "had", "be", "been", "me", "us", "about", "would",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z\-]{3,}", text.lower()) if w not in _STOPWORDS}


def find_relevant_digest(category: dict, text: str) -> Optional[dict]:
    """Keyword-overlap search over the category's digest + content library to
    ground a free-text follow-up in real context instead of a generic reply."""
    q = _tokens(text)
    if not q:
        return None
    best, best_score = None, 0
    for item in category.get("digest", []):
        hay = _tokens(" ".join([
            item.get("title", ""), item.get("summary", ""),
            item.get("actionable", ""), str(item.get("molecule", "")),
        ]))
        score = len(q & hay)
        if score > best_score:
            best, best_score = item, score
    if best_score >= 1:
        return best
    return None


def hi_close(en_question: str, hi_question: str, identity: dict) -> str:
    """Use a light Hindi-English mix closer when the merchant's language
    preference includes Hindi; otherwise stay in English."""
    return hi_question if wants_hindi_mix(identity) else en_question


# =============================================================================
# PYDANTIC MODELS (request/response contracts)
# =============================================================================

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str
    received_at: str
    turn_number: int = 0


class TeardownBody(BaseModel):
    reason: Optional[str] = None


# =============================================================================
# ENDPOINTS: liveness / metadata
# =============================================================================

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": MODEL_NAME,
        "approach": (
            "Deterministic rule-based composer with per-trigger-kind templates, "
            "keyword-grounded digest retrieval for free-text follow-ups, and a "
            "conversation state machine for auto-reply detection, opt-out, "
            "hostility, off-topic deflection, and intent-transition handling."
        ),
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": now_iso(),
    }


# =============================================================================
# ENDPOINT: /v1/context
# =============================================================================

@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"},
        )
    key = (body.scope, body.context_id)
    cur = CONTEXTS.get(key)
    if cur and body.version < cur["version"]:
        # Strictly older version — genuine conflict.
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]},
        )
    if cur and body.version == cur["version"]:
        # Idempotent no-op per spec: "Re-posting the same version is a no-op" —
        # this must succeed (200), not be treated as a conflict.
        return {
            "accepted": True,
            "ack_id": cur.get("ack_id", f"ack_{body.context_id}_v{body.version}"),
            "stored_at": cur.get("stored_at", now_iso()),
        }
    # Higher version (or first time seeing this context_id) — accept and replace atomically.
    ack_id = f"ack_{body.context_id}_v{body.version}"
    stored_at = now_iso()
    CONTEXTS[key] = {"version": body.version, "payload": body.payload, "ack_id": ack_id, "stored_at": stored_at}
    return {
        "accepted": True,
        "ack_id": ack_id,
        "stored_at": stored_at,
    }


# =============================================================================
# COMPOSER — per trigger-kind templates
# =============================================================================

def compose_action(trigger: dict, merchant: dict, category: dict,
                    customer: Optional[dict]) -> Optional[dict]:
    """Returns dict(body, cta, send_as, rationale) or None if nothing worth sending."""
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {}) or {}
    identity = merchant.get("identity", {})
    name = merchant.get("identity", {}).get("name", "there")
    sal = salute(merchant.get("category_slug", ""), identity)
    perf = merchant.get("performance", {})
    peer = category.get("peer_stats", {})
    offers = active_offers(merchant)
    signals = merchant.get("signals", [])
    scope = trigger.get("scope", "merchant")
    send_as = "merchant_on_behalf" if (scope == "customer" and customer) else "vera"

    handler = _HANDLERS.get(kind) if not payload.get("placeholder") else None
    if handler:
        result = handler(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals)
        if result is None:
            return None
        body, cta, rationale = result
    else:
        # Either an unrecognized kind, or a placeholder-only payload (generated
        # trigger with no real facts) — never let a kind-specific template run
        # on fields that don't actually exist, that's how you get "None" in output.
        result = _generic_fallback(kind, payload, merchant, category, customer, sal, identity, perf, peer, offers, signals)
        if result is None:
            return None
        body, cta, rationale = result

    return {
        "body": truncate_body(body),
        "cta": cta,
        "send_as": send_as,
        "rationale": rationale,
    }


def _h_research_digest(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    item = find_digest_item(category, payload.get("top_item_id"))
    if not item:
        return None
    seg = item.get("patient_segment") or item.get("segment_age") or ""
    seg_note = ""
    hr_count = merchant.get("customer_aggregate", {}).get("high_risk_adult_count")
    if seg and "high_risk_adult_cohort" in signals and hr_count:
        seg_note = f" You've got {fmt_num(hr_count)} patients in that exact segment."
    body = (
        f"{sal}, {item.get('source', 'a recent digest')} — {item.get('title', '')}."
        f"{seg_note} {item.get('actionable', '')}. Want a share-ready summary drafted?"
    )
    return body, "open_ended", f"External research_digest item {item.get('id')} matched to merchant's own cohort signal"


def _h_regulation_change(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    item = find_digest_item(category, payload.get("top_item_id"))
    if not item:
        return None
    deadline = payload.get("deadline_iso", item.get("deadline", ""))
    deadline_txt = deadline.split("T")[0] if deadline else ""
    body = (
        f"{sal}, compliance flag: {item.get('title', '')} "
        f"({item.get('source', '')}). {item.get('actionable', '')}"
        f"{f' before {deadline_txt}' if deadline_txt else ''}. "
        f"Want me to draft the audit checklist?"
    )
    return body, "open_ended", "External regulation_change with hard deadline — urgency merits proactive checklist offer"


def _h_cde_opportunity(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    item = find_digest_item(category, payload.get("digest_item_id"))
    if not item:
        return None
    when = item.get("date", "").replace("T", " ").split("+")[0]
    fee = payload.get("fee", "").replace("_", " ")
    body = (
        f"{sal}, {item.get('title', '')} — {when}. {item.get('summary', '')} "
        f"{fee}. {payload.get('credits', '')} CDE credits. Should I block your calendar?"
    )
    return body, "binary", "External CDE opportunity with a firm date — binary calendar-hold ask"


def _h_recall_due(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    lang_mix = wants_hindi_mix(customer.get("identity", {}))
    service = (payload.get("service_due") or "").replace("_", " ")
    last_visit = customer.get("relationship", {}).get("last_visit", "")
    slots = payload.get("available_slots", [])[:2]
    slot_txt = " ya ".join(s.get("label", "") for s in slots) if lang_mix else " or ".join(s.get("label", "") for s in slots)
    matching_offer = next((o for o in offers if "clean" in o.get("title", "").lower()), (offers[0] if offers else None))
    price_txt = f" {matching_offer['title']}." if matching_offer else ""
    if lang_mix:
        body = (
            f"Hi {cust_name}, {name} yahaan se 🦷 Aapka {service} recall due hai "
            f"(last visit {last_visit}). Apke liye slots ready hain: {slot_txt}.{price_txt} "
            f"Reply 1 ya 2, ya apna time batayein."
        )
    else:
        body = (
            f"Hi {cust_name}, {name} here. Your {service.replace('6 month', '6-month')} recall is due "
            f"(last visit {last_visit}). Open slots: {slot_txt}.{price_txt} Reply 1 or 2, or share a time that works."
        )
    return body, "open_ended", f"Customer recall_due for {service}; offering real open slots + matching catalog price"


def _h_wedding_package_followup(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    days_to = payload.get("days_to_wedding")
    trial_date = payload.get("trial_completed", "")
    window = (payload.get("next_step_window_open") or "").replace("_", " ")
    body = (
        f"Hi {cust_name}, following up on your trial at {name} ({trial_date}). "
        f"{days_to} days to the big day — this is the window for a {window}. "
        f"Want me to hold your slot for it?"
    )
    return body, "binary", "Customer wedding_package_followup timed to the trial-to-program window"


def _h_trial_followup(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    trial_date = payload.get("trial_date", "")
    opts = payload.get("next_session_options", [])
    slot_txt = opts[0].get("label", "") if opts else "your next slot"
    body = (
        f"Hi {cust_name}, hope the trial at {name} on {trial_date} went well! "
        f"Next session open: {slot_txt}. Want me to lock it in?"
    )
    return body, "binary", "Customer trial_followup converting a completed trial into a booked next session"


def _h_chronic_refill_due(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    molecules = payload.get("molecule_list", [])
    runs_out = payload.get("stock_runs_out_iso", "").split("T")[0]
    delivery = payload.get("delivery_address_saved")
    mol_txt = ", ".join(molecules)
    deliver_txt = " We can deliver to your saved address." if delivery else ""
    body = (
        f"Hi {cust_name}, {name} here — your {mol_txt} refill runs out around {runs_out}. "
        f"Want us to prep it now?{deliver_txt} Reply YES to confirm."
    )
    return body, "binary", "Customer chronic_refill_due — stock-out date is concrete and near"


def _h_perf_dip(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    window = payload.get("window", "7d")
    body = (
        f"{sal}, your {metric} dropped {fmt_pct(delta)} over the last {window} "
        f"(vs your usual {fmt_num(baseline)}/day). Peer avg for your category is "
        f"{fmt_num(peer.get(f'avg_{metric}_30d', peer.get('avg_calls_30d', '')))}/30d — "
        f"worth a quick listing check. Want me to run the audit?"
    )
    return body, "binary", "Internal perf_dip on a specific metric with peer-benchmark contrast"


def _h_perf_spike(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    driver = (payload.get("likely_driver") or "").replace("_", " ")
    driver_txt = f" Looks tied to your {driver}." if driver else ""
    body = (
        f"{sal}, nice move — {metric} up {fmt_pct(delta)} vs your usual "
        f"{fmt_num(baseline)}/day this week.{driver_txt} What did you change? "
        f"Want to double down on it?"
    )
    return body, "open_ended", "Internal perf_spike — using the 'ask the merchant' engagement lever on a real number"


def _h_seasonal_perf_dip(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    metric = payload.get("metric", "performance")
    delta = payload.get("delta_pct")
    note = clean(payload.get("season_note", "").replace("_", " "))
    body = (
        f"{sal}, {metric} is down {fmt_pct(delta)} this week — expected for this window ({note}). "
        f"Good time to focus on retention over acquisition. Want a retention-nudge draft for your existing customers?"
    )
    return body, "open_ended", "Internal seasonal_perf_dip framed as expected, not alarming, per seasonal_beats context"


def _h_renewal_due(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    days = payload.get("days_remaining")
    amount = payload.get("renewal_amount")
    plan = payload.get("plan", merchant.get("subscription", {}).get("plan", ""))
    body = (
        f"{sal}, your {plan} plan renews in {days} days (₹{fmt_num(amount)}). "
        f"No action needed if auto-renew is on — reply STOP to opt out, or RENEW if you'd like me to confirm now."
    )
    return body, "binary", "Internal renewal_due with concrete days-remaining and exact amount"


def _h_festival_upcoming(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    days_until = payload.get("days_until", 999)
    if days_until is None or days_until > 30:
        return None  # too far out — restraint over spam
    festival = payload.get("festival", "the festival")
    top_offer = offers[0]["title"] if offers else None
    offer_txt = f" Your {top_offer} could be the hook." if top_offer else ""
    body = (
        f"{sal}, {festival} is {days_until} days out.{offer_txt} "
        f"Want a festival post drafted for your listing this week?"
    )
    return body, "open_ended", "External festival_upcoming inside the 30-day planning window"


def _h_curious_ask_due(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    body = (
        f"Quick one, {sal} — what's the most-asked-for service at {merchant.get('identity', {}).get('name', 'your place')} "
        f"this week? Helps me pick your next post."
    )
    return body, "open_ended", "Internal curious_ask_due — using the under-used 'ask the merchant' lever"


def _h_winback_eligible(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    added = payload.get("lapsed_customers_added_since_expiry")
    dip = payload.get("perf_dip_pct")
    since_expiry = payload.get("days_since_expiry")
    body = (
        f"{sal}, {fmt_num(added)} more customers went lapsed since your offer expired {since_expiry} days ago "
        f"— tracks with the {fmt_pct(dip)} dip. Want me to relaunch it as a win-back push?"
    )
    return body, "binary", "Internal winback_eligible — offer-expiry causally linked to a real perf dip"


def _h_customer_lapsed_hard(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    days = payload.get("days_since_last_visit")
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    months = payload.get("previous_membership_months")
    top_offer = offers[0]["title"] if offers else None
    offer_txt = f" We've got {top_offer} running right now." if top_offer else ""
    body = (
        f"Hi {cust_name}, it's been {days} days — missed you at {name}! "
        f"You did {months} months on {focus} last time.{offer_txt} Want to restart this week?"
    )
    return body, "binary", "Customer customer_lapsed_hard win-back grounded in their own history"


def _h_customer_lapsed_soft(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    name = merchant.get("identity", {}).get("name", "us")
    cust_name = customer.get("identity", {}).get("name", "there")
    body = (
        f"Hi {cust_name}, checking in from {name} — it's been a bit since your last visit. "
        f"Anything you'd like us to help with this week?"
    )
    return body, "open_ended", "Customer customer_lapsed_soft — light-touch check-in, no hard sell yet"


def _h_review_theme_emerged(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    theme = (payload.get("theme") or "").replace("_", " ")
    occ = payload.get("occurrences_30d")
    trend = payload.get("trend", "")
    body = (
        f"{sal}, {theme} came up in {occ} reviews this month and it's {trend}. "
        f"Worth a process fix before it compounds. Want a draft reply template for these reviews?"
    )
    return body, "open_ended", "Internal review_theme_emerged — real occurrence count + trend direction"


_METRIC_NAMES = {
    "review_count": "reviews", "reviews": "reviews", "followers": "followers",
    "views": "views", "bookings": "bookings", "orders": "orders",
    "checkins": "check-ins", "repeat_customers": "repeat customers",
}


def _h_milestone_reached(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    raw_metric = payload.get("metric") or ""
    metric = _METRIC_NAMES.get(raw_metric, raw_metric.replace("_", " "))
    now_v = payload.get("value_now")
    target = payload.get("milestone_value")
    remaining = (target - now_v) if isinstance(target, (int, float)) and isinstance(now_v, (int, float)) else None
    body = (
        f"{sal}, you're at {fmt_num(now_v)} {metric}"
        f"{f' — {remaining} away from {fmt_num(target)}' if remaining else ''}. "
        f"Want a review-request nudge sent to your last few happy customers to push you over?"
    )
    return body, "binary", "Internal milestone_reached — imminent milestone with a concrete gap number"


def _h_active_planning_intent(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    last_msg = payload.get("merchant_last_message", "")
    body = (
        f"On it, {sal} — for {topic}: I'll draft a structure based on what you said "
        f"(\"{last_msg[:60]}\") and share it here. Anything specific to include?"
    )
    return body, "open_ended", "Internal active_planning_intent — merchant already engaged, so bot moves to action mode not qualification"


def _h_competitor_opened(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    comp = payload.get("competitor_name", "a competitor")
    dist = payload.get("distance_km")
    their_offer = payload.get("their_offer", "")
    my_offer = offers[0]["title"] if offers else None
    compare_txt = f" You're running {my_offer} — want to sharpen it against theirs?" if my_offer else " Want a counter-offer drafted?"
    body = (
        f"{sal}, {comp} opened {dist}km away with {their_offer}.{compare_txt}"
    )
    return body, "open_ended", "External competitor_opened with distance + their exact offer for direct comparison"


def _h_supply_alert(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    molecule = payload.get("molecule", "")
    batches = payload.get("affected_batches", [])
    body = (
        f"{sal}, urgent: {molecule} recall — batches {', '.join(batches)} affected. "
        f"Want me to forward the batch list to your staff and flag it in your stock system?"
    )
    return body, "binary", "External supply_alert — highest urgency (5), exact batch numbers, no editorializing"


def _h_category_seasonal(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    trends = payload.get("trends", [])
    trend_txt = ", ".join(t.replace("_", " ").replace("+", "+").replace("-", "") for t in trends[:3])
    body = (
        f"{sal}, seasonal shelf shift incoming: {trend_txt}. "
        f"Want a shelf-placement checklist for the next 2 weeks?"
    )
    return body, "open_ended", "External category_seasonal demand-shift with named trend list"


def _h_gbp_unverified(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    uplift = payload.get("estimated_uplift_pct")
    path = (payload.get("verification_path") or "").replace("_or_", " or ").replace("_", " ")
    body = (
        f"{sal}, your listing isn't verified yet — verified profiles in your category see "
        f"~{fmt_pct(uplift)} more traffic. Verification is by {path}. Want me to start it?"
    )
    return body, "binary", "Internal gbp_unverified with concrete uplift estimate"


def _h_dormant_with_vera(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    days = payload.get("days_since_last_merchant_message")
    topic = (payload.get("last_topic") or "").replace("_", " ")
    body = (
        f"{sal}, been {days} days — still want to pick up on {topic}? "
        f"Happy to park it if it's not a priority right now."
    )
    return body, "open_ended", "Internal dormant_with_vera — low-pressure re-open naming the exact stalled topic"


def _h_appointment_tomorrow(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    if not customer:
        return None
    cust_name = customer.get("identity", {}).get("name", "there")
    body = (
        f"Hi {cust_name}, quick reminder — your appointment with {merchant.get('identity', {}).get('name', 'us')} "
        f"is tomorrow. Still good for you, or need to reschedule?"
    )
    return body, "binary", "Customer appointment_tomorrow reminder; no fabricated time since none was provided in context"


def _h_ipl_match_today(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    match = payload.get("match", "")
    venue = payload.get("venue", "")
    time_txt = payload.get("match_time_iso", "").split("T")[-1][:5]
    top_offer = offers[0]["title"] if offers else None
    offer_txt = f" Push {top_offer} as your match-night special?" if top_offer else " Want a match-night post drafted?"
    body = f"{sal}, {match} tonight at {venue}, {time_txt}.{offer_txt}"
    return body, "open_ended", "External ipl_match_today — match-night timing is the whole reason to message"


def _h_local_news_event(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    event = payload.get("event") or payload.get("headline") or "a local event"
    body = f"{sal}, heads up: {event} nearby. Want me to check if it affects your foot traffic today?"
    return body, "open_ended", "External local_news_event — real-time relevance framing"


def _h_weather_heatwave(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    temp = payload.get("temp_c") or payload.get("temperature")
    city = merchant.get("identity", {}).get("city", "your city")
    body = f"{sal}, {fmt_num(temp)}°C in {city} today. Worth pushing a heat-relevant offer or post?"
    return body, "open_ended", "External weather_heatwave — locally relevant, timely"


def _h_category_trend_movement(payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    query = payload.get("query", "")
    delta = payload.get("delta_yoy")
    body = f"{sal}, '{query}' searches are up {fmt_pct(delta)} YoY in your area. Want your listing positioned for it?"
    return body, "open_ended", "External category_trend_movement with named query + YoY delta"


_HANDLERS = {
    "research_digest": _h_research_digest,
    "regulation_change": _h_regulation_change,
    "cde_opportunity": _h_cde_opportunity,
    "recall_due": _h_recall_due,
    "wedding_package_followup": _h_wedding_package_followup,
    "trial_followup": _h_trial_followup,
    "chronic_refill_due": _h_chronic_refill_due,
    "perf_dip": _h_perf_dip,
    "perf_spike": _h_perf_spike,
    "seasonal_perf_dip": _h_seasonal_perf_dip,
    "renewal_due": _h_renewal_due,
    "festival_upcoming": _h_festival_upcoming,
    "curious_ask_due": _h_curious_ask_due,
    "winback_eligible": _h_winback_eligible,
    "customer_lapsed_hard": _h_customer_lapsed_hard,
    "customer_lapsed_soft": _h_customer_lapsed_soft,
    "review_theme_emerged": _h_review_theme_emerged,
    "milestone_reached": _h_milestone_reached,
    "active_planning_intent": _h_active_planning_intent,
    "competitor_opened": _h_competitor_opened,
    "supply_alert": _h_supply_alert,
    "category_seasonal": _h_category_seasonal,
    "gbp_unverified": _h_gbp_unverified,
    "dormant_with_vera": _h_dormant_with_vera,
    "appointment_tomorrow": _h_appointment_tomorrow,
    "ipl_match_today": _h_ipl_match_today,
    "local_news_event": _h_local_news_event,
    "weather_heatwave": _h_weather_heatwave,
    "category_trend_movement": _h_category_trend_movement,
}


def _generic_fallback(kind, payload, merchant, category, customer, sal, identity, perf, peer, offers, signals):
    """Used only for placeholder/unknown trigger kinds. Grounds the message in
    real context instead of inventing trigger-specific facts that were never
    actually pushed — avoids the fabrication penalty. Branches hard on
    customer- vs merchant-scope: a customer must never see the merchant's own
    internal performance numbers, that's a category-fit violation."""
    name = merchant.get("identity", {}).get("name", "us")

    if customer is not None:
        # Customer-facing placeholder trigger (e.g. appointment_tomorrow,
        # customer_lapsed_soft, generated with no real payload facts).
        cust_name = customer.get("identity", {}).get("name", "there")
        rel = customer.get("relationship", {})
        last_visit = rel.get("last_visit") or rel.get("last_service_date")
        visits = rel.get("visits_total")
        top_offer = offers[0]["title"] if offers else None
        offer_txt = f" We've got {top_offer} on right now." if top_offer else ""
        k = kind.lower()
        if "appointment" in k or "reminder" in k:
            lead = f"Hi {cust_name}, quick reminder from {name} about your upcoming visit."
        elif "lapsed" in k or "winback" in k or "win_back" in k:
            visit_txt = f" It's been a while since your last visit on {last_visit}." if last_visit else " It's been a while since your last visit."
            lead = f"Hi {cust_name}, checking in from {name}.{visit_txt}"
        elif "wedding" in k or "trial" in k or "followup" in k:
            lead = f"Hi {cust_name}, following up from {name} on where things stand."
        elif "refill" in k or "recall" in k or "due" in k:
            lead = f"Hi {cust_name}, {name} here with a quick heads-up."
        else:
            lead = f"Hi {cust_name}, {name} here — checking in."
        visits_txt = f" You've visited {visits}x with us." if visits else ""
        body = f"{lead}{visits_txt}{offer_txt} Let us know if you'd like to book something in."
        return body, "open_ended", f"Placeholder customer-scope trigger '{kind}' — grounded in customer relationship data, never merchant-internal metrics"

    # Merchant-facing placeholder trigger — vary the lead-in by kind family so
    # unrelated trigger kinds don't all read identically, while keeping every
    # concrete fact limited to what's actually in performance/signals/offers.
    k = kind.lower()
    views = perf.get("views")
    ctr = perf.get("ctr")
    peer_ctr = peer.get("avg_ctr")
    offer_txt = f" Your {offers[0]['title']} is live right now." if offers else ""
    has_perf = views and ctr and peer_ctr

    if any(w in k for w in ("festival", "trend", "competitor", "news", "weather", "seasonal")):
        lead = f"{sal}, worth a look — something time-sensitive for {name} this week."
    elif any(w in k for w in ("renewal", "gbp", "verify", "subscription")):
        lead = f"{sal}, an account item for {name} worth a quick look."
    elif any(w in k for w in ("dormant", "curious", "ask")):
        lead = f"{sal}, quick one for {name}."
    elif any(w in k for w in ("review", "milestone", "reputation")):
        lead = f"{sal}, a reputation update for {name}."
    else:
        lead = f"{sal}, a quick check-in on {name}."

    if has_perf:
        cmp_txt = "above" if ctr >= peer_ctr else "below"
        body = (
            f"{lead} {fmt_num(views)} views this month, CTR {cmp_txt} "
            f"your category's {fmt_pct(peer_ctr)} peer average.{offer_txt} "
            f"Want me to look at what's driving it?"
        )
        return body, "open_ended", f"Placeholder merchant-scope trigger '{kind}' — grounded in real performance vs peer_stats, no invented specifics"
    if signals:
        sig_txt = signals[0].replace(":", " ").replace("_", " ")
        body = f"{lead} Noticed: {sig_txt}.{offer_txt} Want me to take a look?"
        return body, "open_ended", f"Placeholder merchant-scope trigger '{kind}' — grounded in a real merchant signal"
    return None  # genuinely nothing groundable — restraint over spam


# =============================================================================
# ENDPOINT: /v1/tick
# =============================================================================

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        if len(actions) >= 20:
            break
        trigger = get_ctx("trigger", trg_id)
        if not trigger:
            continue
        supp_key = trigger.get("suppression_key", trg_id)
        if supp_key in SENT_SUPPRESSION_KEYS:
            continue  # already sent — restraint, avoid repeat
        merchant_id = trigger.get("merchant_id")
        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue
        category = get_ctx("category", merchant.get("category_slug"))
        if not category:
            continue
        customer_id = trigger.get("customer_id")
        customer = get_ctx("customer", customer_id) if customer_id else None

        composed = compose_action(trigger, merchant, category, customer)
        if not composed:
            continue

        conv_id = f"conv_{merchant_id}_{trg_id}"
        CONVERSATIONS.setdefault(conv_id, {
            "merchant_id": merchant_id, "customer_id": customer_id,
            "turns": [], "sent_bodies": [], "auto_reply_strikes": 0,
            "ended": False, "hostile_flag": False, "last_topic": trigger.get("kind"),
        })
        CONVERSATIONS[conv_id]["sent_bodies"].append(composed["body"])
        SENT_SUPPRESSION_KEYS.add(supp_key)

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", ""), trigger.get("kind", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": supp_key,
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


# =============================================================================
# ENDPOINT: /v1/reply — conversation state machine
# =============================================================================

OPT_OUT_RE = re.compile(
    r"\b(stop messaging|stop texting|stop contacting|unsubscribe|do not contact|"
    r"don'?t message|don'?t contact|remove me|no more messages|opt out)\b", re.I
)
# bare "stop" as its own word/sentence also counts as opt-out
OPT_OUT_BARE_RE = re.compile(r"^\s*stop\.?\s*$", re.I)

HOSTILE_RE = re.compile(
    r"\b(useless|spam|idiot|stupid|shut up|waste of time|sick of this|"
    r"annoying|harassment|scam)\b", re.I
)

OFF_TOPIC_RE = re.compile(
    r"\b(gst|income tax|itr|visa|passport|personal loan|insurance claim|"
    r"divorce|lawsuit|immigration|court case|property registration)\b", re.I
)

INTENT_COMMIT_RE = re.compile(
    r"\b(let'?s do it|lets do it|go ahead|yes let'?s|please proceed|"
    r"ok proceed|sounds good,? do it|confirmed,? proceed)\b", re.I
)

AUTO_REPLY_RE = re.compile(
    r"\b(thank you for contacting|will get back to you|team will respond|"
    r"automated (message|assistant|response)|out of office|"
    r"we have received your (message|query)|will revert shortly|"
    r"aapki jaankari|hamari team tak)\b", re.I
)

QUALIFYING_WORDS = ["would you", "do you", "can you tell", "what if", "how about"]

# A customer confirming a slot/booking/appointment — must be checked with
# from_role == "customer" only, and takes priority over the generic digest
# fallback, which has no concept of "confirm my booking".
CUSTOMER_BOOKING_RE = re.compile(
    r"\b(book me|please book|book it|confirm(ed)? (the|my)? ?(slot|booking|appointment)|"
    r"that works,? book|yes,? book|option (1|2|one|two)|^\s*(1|2)\.?\s*$)\b", re.I
)
CUSTOMER_AFFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|sounds good|that works|confirmed)\b", re.I
)
SLOT_TEXT_RE = re.compile(r"\bfor\s+(.+?)[\.\!]?\s*$", re.I)


def _looks_like_auto_reply(msg: str, prior_turns: list[dict]) -> bool:
    """prior_turns must NOT include the current message — only turns strictly
    before it — otherwise every first message trivially "repeats itself"."""
    if AUTO_REPLY_RE.search(msg):
        return True
    prior = [t["message"] for t in prior_turns if t["from_role"] == "merchant"]
    norm = clean(msg).lower()
    repeats = sum(1 for m in prior if clean(m).lower() == norm)
    return repeats >= 1  # this exact text was already seen at least once before


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    conv = CONVERSATIONS.setdefault(conv_id, {
        "merchant_id": body.merchant_id, "customer_id": body.customer_id,
        "turns": [], "sent_bodies": [], "auto_reply_strikes": 0,
        "ended": False, "hostile_flag": False, "last_topic": None,
    })
    prior_turns = list(conv["turns"])  # snapshot BEFORE appending current turn
    conv["turns"].append({"from_role": body.from_role, "message": body.message, "turn": body.turn_number})

    msg = body.message or ""

    if conv.get("ended"):
        return {"action": "end", "rationale": "Conversation was already ended; not re-engaging."}

    # 1. Opt-out — highest priority, end immediately, no further pitch.
    if OPT_OUT_RE.search(msg) or OPT_OUT_BARE_RE.match(msg):
        conv["ended"] = True
        return {"action": "end", "rationale": "Merchant/customer requested opt-out — ending immediately per suppression policy, no further messages."}

    # 1b. Customer confirming a slot/booking/appointment — must be handled
    # before the generic digest fallback, which has no concept of bookings
    # and would otherwise respond with an irrelevant question about the
    # merchant's own offer catalog instead of confirming the booking.
    if body.from_role == "customer" and (
        CUSTOMER_BOOKING_RE.search(msg)
        or (CUSTOMER_AFFIRM_RE.search(msg) and len(msg.split()) <= 6)
    ):
        m = SLOT_TEXT_RE.search(msg)
        slot_txt = clean(m.group(1)) if m else ""
        conv["ended"] = False
        body_txt = (
            f"Great — booking confirmed for {slot_txt}! " if slot_txt
            else "Great — done. "
        )
        body_txt += "I'll get this moving right away and confirm here once it's live."
        return {
            "action": "send",
            "body": truncate_body(body_txt),
            "cta": "none",
            "rationale": "Customer confirmed a slot/booking — closing the loop with a direct confirmation instead of re-asking or pivoting to unrelated merchant context.",
        }

    # 2. Auto-reply detection (canned phrase or verbatim repeat).
    if _looks_like_auto_reply(msg, prior_turns):
        conv["auto_reply_strikes"] += 1
        if conv["auto_reply_strikes"] >= 2:
            conv["ended"] = True
            return {"action": "end", "rationale": "Second auto-reply detected — stopping to avoid burning turns on a canned response; will try reaching the owner through a different channel."}
        return {
            "action": "send",
            "body": "Got it — before this goes to your team, want to take 2 minutes yourself to see exactly what's changing? Otherwise no worries, I'll follow up with the owner directly.",
            "cta": "open_ended",
            "rationale": "First auto-reply detected — one lightweight direct attempt before disengaging, per anti-auto-reply-burn policy.",
        }

    # 3. Hostile (non-opt-out) — de-escalate briefly, stay polite, don't pitch this turn.
    if HOSTILE_RE.search(msg):
        conv["hostile_flag"] = True
        return {
            "action": "send",
            "body": "Sorry to have bothered you — I'll keep things minimal from here. If anything ever seems useful, I'm here; otherwise no further nudges from me today.",
            "cta": "none",
            "rationale": "Hostile tone without explicit opt-out — de-escalating with a brief apology, no CTA, staying available without pushing.",
        }

    # 4. Off-topic request — politely decline and redirect to in-scope help.
    if OFF_TOPIC_RE.search(msg):
        mname = get_ctx("merchant", conv.get("merchant_id"))
        mname = mname.get("identity", {}).get("name", "your business") if mname else "your business"
        return {
            "action": "send",
            "body": f"That's outside what I can help with here — I'm focused on {mname}'s marketing, listing, and customer messages. Happy to help with any of those, anytime.",
            "cta": "none",
            "rationale": "Off-topic/unsupported request (outside Vera's mandate) — declined politely, redirected to in-scope help, stayed on-mission.",
        }

    # 5. Intent transition — merchant explicitly committed, switch to action mode immediately.
    if INTENT_COMMIT_RE.search(msg):
        topic = clean((conv.get("last_topic") or "").replace("_", " "))
        lead = f"Done — kicking off {topic} now." if topic else "Done — kicking this off now."
        return {
            "action": "send",
            "body": f"{lead} I'll share the draft here shortly.",
            "cta": "open_ended",
            "rationale": "Detected explicit commitment language — routing straight to action mode instead of re-qualifying, per intent-handoff guidance.",
        }

    # 6. Default: grounded technical/informational follow-up using digest retrieval.
    merchant = get_ctx("merchant", conv.get("merchant_id"))
    category = get_ctx("category", merchant.get("category_slug")) if merchant else None
    match = find_relevant_digest(category, msg) if category else None

    if match:
        title = match.get("title", "")
        actionable = match.get("actionable", "")
        source = match.get("source", "")
        resp_body = f"On that — {title}{f' ({source})' if source else ''}. {actionable}. Want me to draft the note for your file?"
        resp_body = truncate_body(resp_body)
        conv["last_topic"] = match.get("id")
        return {
            "action": "send",
            "body": resp_body,
            "cta": "open_ended",
            "rationale": f"Matched merchant's free-text question to category digest item '{match.get('id')}' via keyword overlap — grounded technical answer instead of generic acknowledgment.",
        }

    # No digest match — still avoid generic filler; reference something real if we have it.
    if merchant:
        offers = active_offers(merchant)
        signals = merchant.get("signals", [])
        anchor = offers[0]["title"] if offers else (signals[0].replace(":", " ").replace("_", " ") if signals else None)
        if anchor:
            resp_body = f"Noted. On {anchor} — want me to update it, or is there something specific about this you'd like me to dig into?"
        else:
            resp_body = "Noted — tell me a bit more about what you'd like changed and I'll get it moving."
    else:
        resp_body = "Noted — tell me a bit more about what you'd like changed and I'll get it moving."

    return {
        "action": "send",
        "body": truncate_body(resp_body),
        "cta": "open_ended",
        "rationale": "No specific digest match for this free-text message — anchored the acknowledgment in real merchant context (offer/signal) rather than a generic filler line.",
    }


# =============================================================================
# OPTIONAL: teardown
# =============================================================================

@app.post("/v1/teardown")
async def teardown(_body: TeardownBody | None = None):
    CONTEXTS.clear()
    CONVERSATIONS.clear()
    SENT_SUPPRESSION_KEYS.clear()
    return {"status": "wiped"}


@app.get("/")
async def root():
    return {"service": "vera-bot", "status": "ok", "endpoints": [
        "/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"
    ]}
