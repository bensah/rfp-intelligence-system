"""Streamlit Cloud entry shim.

Streamlit Community Cloud pinned this app's "Main file path" to ``Home.py``
when it was first deployed, and the dashboard offers no way to change it. The
real router lives in ``App.py`` (see its docstring). This shim just executes
App.py so Cloud's ``streamlit run Home.py`` boots the app unchanged.

`runpy.run_path` (not ``import App``) is used deliberately so App.py's
module-level code re-executes on every Streamlit rerun. No Streamlit calls
happen here, so ``st.set_page_config`` in App.py stays the first command.

Local dev can still use ``streamlit run App.py`` directly (see README).
"""
import os
import runpy

runpy.run_path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "App.py"),
    run_name="__main__",
)
