"""SDS Management -- entry point.

Two pages: bulk uploading many SDS documents at once (no review step), and
a searchable repository for looking up ones already saved. See
bulk_upload.py and repository.py. Both build on the shared pipeline in
extraction_pipeline.py.
"""

import streamlit as st

import bulk_upload
import repository

st.set_page_config(page_title="SDS Management", layout="wide")

pages = [
    st.Page(bulk_upload.render, title="Bulk Upload", icon="📦", url_path="bulk-upload", default=True),
    st.Page(repository.render, title="SDS Repository", icon="🗂️", url_path="repository"),
]

st.navigation(pages).run()
