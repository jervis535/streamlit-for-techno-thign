"""FX Forecaster - entry point / router.  Run with:  streamlit run app.py

Pages:  forecast_page.py (the forecaster)  and  subscription_page.py (plans).
Everything is behind login (see auth.py).
"""
from __future__ import annotations

import streamlit as st

from auth import logout_button, require_login
from subscriptions import load as load_subscription

st.set_page_config(page_title="FX Forecaster", page_icon="💱", layout="wide")

# Everything below this line is only reachable by registered, logged-in users.
user = require_login()
sub = load_subscription()

pages = [
    st.Page("forecast_page.py", title="Forecast", icon="📈", default=True),
    st.Page("subscription_page.py", title="Subscription", icon="💳"),
]
current = st.navigation(pages)

with st.sidebar:
    logout_button(user)
    st.caption(f"Plan: **{sub.plan.name}**")
    st.divider()

current.run()