"""Scan and preview pages before sending them through the existing upload flow."""
import streamlit as st
from services.scanner_service import ScannerError, find_console, list_scanners, scan_page, prepare_scans
from services.audit_service import log_action
from config.settings import settings


def render_scanner_input(locked: bool) -> list[tuple[str, bytes]] | None:
    st.caption('Scan from the flatbed connected to this app’s Windows PC. Place one page on the glass before each scan.')
    try:
        find_console()
    except ScannerError as exc:
        st.info(str(exc))
        st.caption('Install the Canon ScanGear driver for the LiDE 300, then test one scan in NAPS2. See SCANNER_PRINT_SETUP.md.')
        return None
    driver = st.selectbox('Scanner driver', ['twain', 'wia', 'escl'], format_func=lambda x: {'twain': 'TWAIN — Canon LiDE 300', 'wia': 'WIA — Windows scanner', 'escl': 'eSCL — compatible network scanner'}[x], disabled=locked)
    if st.button('Find / refresh scanners', disabled=locked):
        try:
            with st.spinner('Finding scanners…'):
                st.session_state['scan_devices_' + driver] = list_scanners(driver)
        except ScannerError as exc:
            st.error(str(exc))
    devices = st.session_state.get('scan_devices_' + driver, [])
    device = st.selectbox('Scanner', devices, disabled=locked) if devices else None
    if not devices:
        st.caption('Click Find / refresh scanners. If none appear, check the driver and connection or choose another driver type.')
    a, b, c = st.columns(3)
    dpi = a.selectbox('Resolution (DPI)', [150, 300, 600], index=1, disabled=locked)
    color = b.selectbox('Scan color', ['color', 'gray'], disabled=locked)
    size = c.selectbox('Paper size', ['a4', 'letter'], disabled=locked)
    pages = st.session_state.setdefault('scan_pages', [])
    if st.button('Scan page', disabled=locked or not device or len(pages) >= 20):
        try:
            with st.spinner('Scanning… Please leave the scanner connected.'):
                result = scan_page(device, driver, dpi, color, size)
            if sum(len(data) for _, data in pages) + len(result[1]) > settings.MAX_UPLOAD_MB * 1024 * 1024:
                st.error('The pending scans exceed the upload limit. Process these pages before scanning more.')
            else:
                pages.append(result)
                log_action('SCAN', 'upload', None, f'Scanned page with {device} at {dpi} DPI')
        except ScannerError as exc:
            st.error(str(exc))
    if pages:
        st.caption(f'{len(pages)} page(s) ready. Up to 20 pages per scan batch. Discard an unwanted page and scan it again.')
        index = st.selectbox('Preview page', range(len(pages)), format_func=lambda i: f'Page {i + 1}', disabled=locked)
        st.image(pages[index][1], width=500)
        left, right = st.columns(2)
        if left.button('Discard previewed page', disabled=locked):
            pages.pop(index)
            st.rerun()
        if right.button('Clear scanned pages', disabled=locked):
            st.session_state['scan_pages'] = []
            st.rerun()
        combine = st.radio('Process scanned pages as', ['Separate invoices', 'One multi-page PDF'], disabled=locked) == 'One multi-page PDF'
        if st.button('Process scanned invoices', type='primary', disabled=locked):
            try:
                prepared = prepare_scans(pages, combine)
                if any(len(data) > settings.MAX_UPLOAD_MB * 1024 * 1024 for _, data in prepared):
                    st.error('The combined PDF exceeds the upload limit. Process fewer pages together.')
                else:
                    st.session_state['scan_pages'] = []
                    return prepared
            except Exception as exc:
                st.error(f'Could not prepare scanned pages: {exc}')
    return None
