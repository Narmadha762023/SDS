"""SDS Management -- entry point.

Three pages: uploading/extracting one SDS at a time with review, bulk
uploading many at once with no review step, and a searchable repository
for looking up ones already saved. See upload_page.py, bulk_upload.py, and
repository.py.
"""

import streamlit as st

import bulk_upload
import repository
import upload_page

st.set_page_config(page_title="SDS Management", layout="wide")

pages = [
    st.Page(upload_page.render, title="Upload & Extract", icon="📤", url_path="upload", default=True),
    st.Page(bulk_upload.render, title="Bulk Upload", icon="📦", url_path="bulk-upload"),
    st.Page(repository.render, title="SDS Repository", icon="🗂️", url_path="repository"),
]

st.navigation(pages).run()
