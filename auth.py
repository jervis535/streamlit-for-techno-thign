"""Supabase email/password authentication for Streamlit.

Pages (switched through st.session_state["auth_view"]):
  * login     - default; has a "No account? Register now!" button
  * register  - has an "Already have an account? Log in" button

Usage in app.py:
    from auth import require_login, logout_button
    user = require_login()        # renders login/register and st.stop()s until signed in
    logout_button(user)           # e.g. inside the sidebar

Security notes
  * Only the public *anon* key is used. Never put the service_role key in this app.
  * Each browser session gets its OWN Supabase client (kept in st.session_state).
    A shared/cached client would share one login between all visitors.
  * Passwords are never stored or logged by this app; Supabase hashes them.
"""
from __future__ import annotations

import re

import streamlit as st

MIN_PASSWORD_LEN = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthConfigError(Exception):
    pass


# --------------------------------------------------------------------------
# Supabase plumbing
# --------------------------------------------------------------------------
def _new_client():
    try:
        from supabase import create_client
    except ImportError as e:  # pragma: no cover
        raise AuthConfigError("Install the `supabase` package (pip install -r requirements.txt).") from e
    try:
        cfg = st.secrets["supabase"]
        return create_client(cfg["url"], cfg["anon_key"])
    except (KeyError, FileNotFoundError) as e:
        raise AuthConfigError(
            "Supabase is not configured. Add a [supabase] section with `url` and `anon_key` "
            "to .streamlit/secrets.toml (see secrets.toml.example)."
        ) from e


def _client():
    if "sb_client" not in st.session_state:
        st.session_state["sb_client"] = _new_client()
    return st.session_state["sb_client"]


def _friendly(err: Exception) -> str:
    msg = str(getattr(err, "message", None) or err)
    low = msg.lower()
    if "invalid login" in low or "invalid credentials" in low:
        return "Incorrect email or password."
    if "email not confirmed" in low:
        return "Please confirm your email first. Check your inbox for the confirmation link."
    if "already registered" in low or "already been registered" in low:
        return "An account with this email already exists. Try logging in instead."
    if "rate limit" in low or "too many" in low:
        return "Too many attempts. Please wait a few minutes and try again."
    if "password" in low and ("weak" in low or "least" in low or "short" in low):
        return msg
    return f"Something went wrong: {msg}"


def get_client():
    """The signed-in user's own Supabase client (used for subscription calls)."""
    return _client()


def _set_user(user) -> None:
    st.session_state["auth_user"] = {"id": user.id, "email": user.email}
    # never carry another user's cached plan into this session
    for k in ("sub", "sub_error"):
        st.session_state.pop(k, None)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def sign_in(email: str, password: str) -> str | None:
    """Return an error message, or None on success."""
    try:
        res = _client().auth.sign_in_with_password({"email": email, "password": password})
    except AuthConfigError:
        raise
    except Exception as e:
        return _friendly(e)
    if not getattr(res, "user", None):
        return "Incorrect email or password."
    _set_user(res.user)
    return None


def sign_up(email: str, password: str) -> tuple[str | None, bool]:
    """Return (error, needs_email_confirmation)."""
    try:
        res = _client().auth.sign_up({"email": email, "password": password})
    except AuthConfigError:
        raise
    except Exception as e:
        return _friendly(e), False
    user = getattr(res, "user", None)
    if not user:
        return "Could not create the account. Please try again.", False
    # With "Confirm email" enabled in Supabase there is a user but no session yet.
    # Supabase also returns a user with empty `identities` when the email is already taken.
    if getattr(user, "identities", None) == []:
        return "An account with this email already exists. Try logging in instead.", False
    if getattr(res, "session", None):
        _set_user(user)
        return None, False
    return None, True


def sign_out() -> None:
    try:
        _client().auth.sign_out()
    except Exception:
        pass  # always clear the local session, even if the network call fails
    for k in ("auth_user", "sb_client", "auth_view", "ran", "sub", "sub_error", "sub_flash"):
        st.session_state.pop(k, None)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def _center():
    _, mid, _ = st.columns([1, 1.4, 1])
    return mid


def _goto(view: str) -> None:
    st.session_state["auth_view"] = view
    st.rerun()


def _login_page() -> None:
    with _center():
        st.markdown("## 💱 FX Forecaster")
        st.subheader("Log in")
        if msg := st.session_state.pop("auth_flash", None):
            st.success(msg)
        with st.form("login_form"):
            email = st.text_input("Email", autocomplete="email")
            password = st.text_input("Password", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("Log in", type="primary", width="stretch")
        if submitted:
            email = email.strip().lower()
            if not email or not password:
                st.error("Enter your email and password.")
            else:
                with st.spinner("Logging in..."):
                    err = sign_in(email, password)
                if err:
                    st.error(err)
                else:
                    st.rerun()
        st.divider()
        st.write("No account?")
        if st.button("Register now!", width="stretch"):
            _goto("register")


def _register_page() -> None:
    with _center():
        st.markdown("## 💱 FX Forecaster")
        st.subheader("Create your account")
        with st.form("register_form"):
            email = st.text_input("Email", autocomplete="email")
            password = st.text_input("Password", type="password", autocomplete="new-password",
                                     help=f"At least {MIN_PASSWORD_LEN} characters.")
            confirm = st.text_input("Confirm password", type="password", autocomplete="new-password")
            submitted = st.form_submit_button("Register", type="primary", width="stretch")
        if submitted:
            email = email.strip().lower()
            if not EMAIL_RE.match(email):
                st.error("Enter a valid email address.")
            elif len(password) < MIN_PASSWORD_LEN:
                st.error(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
            elif password != confirm:
                st.error("Passwords do not match.")
            else:
                with st.spinner("Creating account..."):
                    err, needs_confirm = sign_up(email, password)
                if err:
                    st.error(err)
                elif needs_confirm:
                    st.session_state["auth_flash"] = (
                        "Account created! Check your email and click the confirmation link, then log in."
                    )
                    _goto("login")
                else:
                    st.rerun()
        st.divider()
        st.write("Already have an account?")
        if st.button("Back to log in", width="stretch"):
            _goto("login")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def require_login() -> dict:
    """Return the signed-in user ({'id', 'email'}); otherwise show the auth pages and stop."""
    user = st.session_state.get("auth_user")
    if user:
        return user
    try:
        if st.session_state.get("auth_view") == "register":
            _register_page()
        else:
            _login_page()
    except AuthConfigError as e:
        st.error(str(e))
    st.stop()


def logout_button(user: dict) -> None:
    st.caption(f"Signed in as **{user['email']}**")
    if st.button("Log out", width="stretch"):
        sign_out()
        st.rerun()