"""Root-level Streamlit entry point.

Running ``streamlit run ui/app.py`` causes Streamlit to auto-discover
every ``.py`` file in ``ui/pages/`` as a navigable page, which conflicts
with the explicit ``st.navigation()`` router defined in ``ui/app.py``.

This thin wrapper lives at the repository root — where no ``pages/``
subdirectory exists — so auto-discovery finds nothing and
``st.navigation()`` has sole control over page routing.

Usage (local):
    streamlit run streamlit_app.py

Docker (Dockerfile.ui):
    CMD ["streamlit", "run", "streamlit_app.py", ...]
"""

from ui.app import main

main()
