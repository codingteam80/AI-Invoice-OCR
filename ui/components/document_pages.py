"""Shared PDF preview controls for History details and editing."""
import streamlit as st

def render_document_pages(pages, invoice_id, show_image):
    if not pages:
        return
    options=['All pages']+[f'Page {i+1}' for i in range(len(pages))]
    selected=st.selectbox('PDF page',options,key=f'pdf_page_{invoice_id}') if len(pages)>1 else 'Page 1'
    indices=range(len(pages)) if selected=='All pages' else [int(selected.split()[-1])-1]
    for index in indices:
        st.caption(f'Page {index+1} of {len(pages)}')
        show_image(pages[index])
