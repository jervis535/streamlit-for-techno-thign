"""Plans, limits and subscription state.

The plan *catalog* (prices, limits) lives here in code. The *user's plan* lives in the
Supabase `subscriptions` table (see supabase/schema.sql). Users can read their own row
but can only change it through the database functions subscribe/cancel/resume.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import streamlit as st

from auth import get_client


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price: float  # per month, USD
    tagline: str
    max_horizon_days: int  # trading days
    max_history_days: int  # calendar days of history
    model_settings: bool
    confidence_details: bool
    csv_export: bool
    news: bool
    features: tuple[str, ...]


PLANS: dict[str, Plan] = {
    "free": Plan(
        "free", "Free", 0.0, "Try it out", 10, 365, False, False, False, False,
        ("Forecasts up to 2 weeks ahead", "1 year of rate history", "Convert now / wait verdict",
         "Overall confidence score"),
    ),
    "plus": Plan(
        "plus", "Plus", 4.99, "For regular transfers", 42, 1095, True, True, False, False,
        ("Forecasts up to 2 months ahead", "3 years of rate history", "Adjustable model settings",
         "Full confidence breakdown and backtest"),
    ),
    "pro": Plan(
        "pro", "Pro", 9.99, "For serious timing", 63, 1825, True, True, True, True,
        ("Forecasts up to 3 months ahead", "5 years of rate history", "Adjustable model settings",
         "Full confidence breakdown and backtest", "Download forecast as CSV",
         "Currency news: central banks and headlines"),
    ),
}
PLAN_ORDER = list(PLANS)


@dataclass
class Subscription:
    plan: Plan
    period_end: datetime | None = None
    cancelling: bool = False  # paid plan that will drop to Free at period_end


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _from_row(row: dict | None) -> Subscription:
    """Turn a DB row into the *effective* subscription. An expired period means Free."""
    if row and row.get("plan") in PLANS:
        end = _parse_ts(row["current_period_end"])
        if end > datetime.now(timezone.utc):
            return Subscription(PLANS[row["plan"]], end, bool(row.get("cancel_at_period_end")))
    return Subscription(PLANS["free"])


def load(force: bool = False) -> Subscription:
    """Current user's subscription, cached in the session until it changes."""
    if not force and "sub" in st.session_state:
        return st.session_state["sub"]
    user = st.session_state.get("auth_user")
    sub, err = Subscription(PLANS["free"]), None
    if user:
        try:
            res = (get_client().table("subscriptions").select("*")
                   .eq("user_id", user["id"]).limit(1).execute())
            sub = _from_row(res.data[0] if res.data else None)
        except Exception as e:
            err = _friendly(e)  # fall back to Free so the app stays usable
    st.session_state["sub"] = sub
    st.session_state["sub_error"] = err
    return sub


# --------------------------------------------------------------------------
# Writing (through database functions only)
# --------------------------------------------------------------------------
def _friendly(e: Exception) -> str:
    msg = str(getattr(e, "message", None) or e)
    low = msg.lower()
    if "could not find the function" in low or "does not exist" in low or "schema cache" in low:
        return "Subscriptions are not set up in the database yet. Run supabase/schema.sql in the Supabase SQL editor."
    if "jwt" in low or "not authenticated" in low:
        return "Your session expired. Please log out and log in again."
    return f"Something went wrong: {msg}"


def _rpc(name: str, params: dict | None = None):
    """Return (result, error)."""
    try:
        res = get_client().rpc(name, params or {}).execute()
    except Exception as e:
        return None, _friendly(e)
    st.session_state.pop("sub", None)  # force a reload on next access
    return res.data, None


def subscribe(plan_id: str) -> str | None:
    if plan_id not in PLANS or plan_id == "free":
        return "Invalid plan."
    # TODO(payments): take payment here (e.g. Stripe Checkout) and only then activate the plan.
    _, err = _rpc("subscribe_to_plan", {"p_plan": plan_id})
    return err


def cancel() -> str | None:
    _, err = _rpc("cancel_subscription")
    return err


def resume() -> str | None:
    _, err = _rpc("resume_subscription")
    return err


def fmt_date(d: datetime | None) -> str:
    return d.strftime("%d %b %Y") if d else ""