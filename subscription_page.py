"""Subscription page: compare plans, subscribe, change plan, cancel."""
from __future__ import annotations

import streamlit as st

from subscriptions import PLAN_ORDER, PLANS, cancel, fmt_date, load, resume, subscribe

sub = load()

st.title("💳 Subscription")

if err := st.session_state.get("sub_error"):
    st.warning(err)
if msg := st.session_state.pop("sub_flash", None):
    st.success(msg)

st.info("Payments are not enabled yet. Choosing a plan activates it immediately and for free (demo mode).")

# ----------------------------------------------------------------------------
# Current plan
# ----------------------------------------------------------------------------
with st.container(border=True):
    a, b = st.columns([2, 1])
    a.markdown(f"**Your plan:** {sub.plan.name}")
    if sub.plan.id == "free":
        a.caption("Upgrade any time to unlock longer forecasts, more history and detailed confidence.")
    elif sub.cancelling:
        a.caption(f"Cancelled. You keep {sub.plan.name} until **{fmt_date(sub.period_end)}**, "
                  "then you move to Free.")
        if b.button("Keep my plan", width="stretch"):
            e = resume()
            if e:
                st.error(e)
            else:
                st.session_state["sub_flash"] = f"Great, your {sub.plan.name} plan will renew as normal."
                st.rerun()
    else:
        a.caption(f"Renews on **{fmt_date(sub.period_end)}** at ${sub.plan.price:.2f}/month.")
        with b.popover("Cancel subscription", width="stretch"):
            st.write(f"You'll keep {sub.plan.name} until {fmt_date(sub.period_end)}, then move to Free.")
            if st.button("Yes, cancel", type="primary", key="confirm_cancel"):
                e = cancel()
                if e:
                    st.error(e)
                else:
                    st.session_state["sub_flash"] = (
                        f"Subscription cancelled. {sub.plan.name} stays active until {fmt_date(sub.period_end)}.")
                    st.rerun()

# ----------------------------------------------------------------------------
# Plan cards
# ----------------------------------------------------------------------------
st.subheader("Plans")
cols = st.columns(len(PLAN_ORDER))
cur_rank = PLAN_ORDER.index(sub.plan.id)

for col, pid in zip(cols, PLAN_ORDER):
    plan = PLANS[pid]
    with col, st.container(border=True):
        st.markdown(f"### {plan.name}")
        st.markdown(f"## {'$0' if plan.price == 0 else f'${plan.price:.2f}'} <small>/month</small>",
                    unsafe_allow_html=True)
        st.caption(plan.tagline)
        for f in plan.features:
            st.markdown(f"- ✅ {f}")
        st.write("")

        if pid == sub.plan.id:
            st.button("Current plan", disabled=True, key=f"btn_{pid}", width="stretch")
        elif pid == "free":
            if sub.cancelling:
                st.button(f"Free from {fmt_date(sub.period_end)}", disabled=True,
                          key=f"btn_{pid}", width="stretch")
            elif st.button("Downgrade to Free", key=f"btn_{pid}", width="stretch"):
                e = cancel()
                if e:
                    st.error(e)
                else:
                    st.session_state["sub_flash"] = (
                        f"You'll move to Free on {fmt_date(sub.period_end)}. Until then you keep {sub.plan.name}.")
                    st.rerun()
        else:
            upgrade = PLAN_ORDER.index(pid) > cur_rank
            label = f"{'Upgrade to' if upgrade else 'Switch to'} {plan.name}"
            if st.button(label, type="primary" if upgrade else "secondary",
                         key=f"btn_{pid}", width="stretch"):
                with st.spinner("Activating..."):
                    e = subscribe(pid)
                if e:
                    st.error(e)
                else:
                    st.session_state["sub_flash"] = f"You're now on {plan.name}. Enjoy!"
                    st.rerun()

st.caption("Plan changes take effect immediately and start a new 1-month period. "
           "Informational only, not financial advice.")