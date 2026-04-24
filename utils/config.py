"""
Unified config — reads exclusively from st.secrets (.streamlit/secrets.toml).
On Streamlit Cloud, paste the same secrets.toml content into the Secrets UI.
"""

import streamlit as st


class _Cfg:
    @property
    def approved_emails(self) -> list[str]:
        return list(st.secrets.get("approved_emails", []))

    @property
    def sheet_id(self) -> str:
        return st.secrets["google_sheets"]["sheet_id"]

    @property
    def service_account_json(self) -> str:
        return st.secrets["google_sheets"]["service_account_json"]

    @property
    def gemini_api_key(self) -> str:
        return st.secrets["gemini"]["api_key"]


cfg = _Cfg()
