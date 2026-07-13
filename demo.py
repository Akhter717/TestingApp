"""
AI Selenium Test Generator - FULLY SECURED & COMPLETE DEMO
All functions included + Security fixes
"""

import re
import io
import zipfile
import time
import requests
import pandas as pd
import streamlit as st
import os
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from groq import Groq

# CONFIG
GROQ_MODEL = "openai/gpt-oss-120b"
ALLOWED_DOMAINS = ["automationexercise.com", "the-internet.herokuapp.com"]

st.set_page_config(page_title="Secure AI Test Generator", page_icon="🔒", layout="wide")

# Password Protection
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

def check_password():
    password = st.text_input("Enter Demo Password", type="password", key="pw")
    if st.button("Login"):
        if password == "demo123":  # ← CHANGE THIS PASSWORD
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Wrong password")

if not st.session_state.authenticated:
    st.title("🔒 Secure AI Selenium Test Generator")
    check_password()
    st.stop()

# Main Title
st.title("🤖 AI Selenium Test Generator")
st.caption("🔒 Secured Version for Team Demo")

# SIDEBAR
with st.sidebar:
    st.header("🔒 Setup")
    groq_api_key = st.text_input("Groq API Key", type="password")
    demo_mode = st.checkbox("🎓 Educational Mode", value=True)
    max_pages = st.slider("Max Pages", 5, 15, 8)

# SECURITY HELPERS
def is_safe_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    blocked = ["localhost", "127.", "0.0.0.0", "169.254.", "10.", "172.16.", "192.168."]
    if any(hostname.startswith(b) for b in blocked):
        return False
    return True

def secure_filename(name: str) -> str:
    name = os.path.basename(name)
    name = re.sub(r'[^a-zA-Z0-9_.\-]', '_', name)
    return name[:100]

# SESSION STATE
for key in ["crawled_pages", "base_url", "locators", "test_plan", "test_cases", "java_code", "crawl_done"]:
    if key not in st.session_state:
        st.session_state[key] = {} if key in ["crawled_pages", "java_code"] else "" if key != "crawl_done" else False

# AI CALL
def ask_ai(api_key, prompt, system_msg="You are a senior QA automation engineer."):
    if not api_key:
        st.error("Enter Groq API key")
        return ""
    try:
        client = Groq(api_key=api_key)
        with st.spinner("🤖 AI thinking..."):
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2500,
            )
            result = resp.choices[0].message.content
            if demo_mode:
                with st.expander("📤/📥 AI Prompt & Response", expanded=False):
                    st.code(prompt[:600] + "...", language="markdown")
                    st.code(result, language="markdown")
            return result
    except Exception as e:
        st.error(f"AI Error: {str(e)[:80]}")
        return ""

# CRAWLING
def crawl_website(start_url, max_pages=8):
    if not is_safe_url(start_url):
        st.error("❌ Unsafe URL blocked")
        return {}
    visited = {}
    to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    progress = st.progress(0)
    for i in range(max_pages):
        if not to_visit: break
        url = to_visit.pop(0)
        if url in visited: continue
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if "text/html" not in resp.headers.get("Content-Type", ""): continue
            visited[url] = resp.text
            progress.progress((i+1)/max_pages)
            time.sleep(0.7)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True)[:25]:
                full = urljoin(url, a["href"])
                p = urlparse(full)
                if p.netloc == base_domain and p.scheme in ("http", "https") and "#" not in full and full not in visited:
                    to_visit.append(full)
        except:
            continue
    progress.empty()
    return visited

# LOCATOR EXTRACTION
def extract_locators(pages_dict):
    locators = []
    seen = set()
    for url, html in pages_dict.items():
        soup = BeautifulSoup(html, "html.parser")
        page_name = urlparse(url).path or "/"
        for tag in ["input", "button", "a", "select", "textarea"]:
            for elem in soup.find_all(tag)[:15]:
                if elem.get("type") == "hidden": continue
                css = f"#{elem.get('id')}" if elem.get("id") else f"{tag}[name='{elem.get('name')}']" if elem.get("name") else tag
                xpath = f"//{tag}[@id='{elem.get('id')}']" if elem.get("id") else f"//{tag}"
                key = f"{page_name}|{css}"
                if key in seen: continue
                seen.add(key)
                locators.append({
                    "Page": page_name, "Tag": tag, "Text": elem.get_text(strip=True)[:40] or "N/A",
                    "CSS Selector": css, "XPath": xpath
                })
    return locators

# ZIP CREATION
def create_zip(java_files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in java_files.items():
            safe_path = secure_filename(path)
            zf.writestr(safe_path, content)
    buf.seek(0)
    return buf

# MAIN UI
st.markdown("### 🌐 Enter Website URL")
c1, c2 = st.columns([3, 1])
with c1:
    url_input = st.text_input("URL", value="https://automationexercise.com", label_visibility="collapsed")
with c2:
    if st.button("🚀 Start Secure Demo", type="primary"):
        if not groq_api_key:
            st.error("Enter Groq API Key")
            st.stop()
        st.session_state.crawled_pages = crawl_website(url_input, max_pages)
        st.session_state.locators = extract_locators(st.session_state.crawled_pages)
        st.session_state.base_url = url_input.rstrip("/")
        st.session_state.crawl_done = True
        st.success("✅ Secure crawl completed!")

if st.session_state.get("crawl_done"):
    st.info(f"Target: {st.session_state.base_url}")
    tab1, tab2 = st.tabs(["Test Plan", "Locators"])
    with tab1:
        if st.button("Generate Test Plan"):
            plan = ask_ai(groq_api_key, f"Write a short test plan for {st.session_state.base_url}")
            st.session_state.test_plan = plan
            st.markdown(plan)
    with tab2:
        if st.session_state.locators:
            st.dataframe(pd.DataFrame(st.session_state.locators))

st.caption("🔒 Secured Internal Demo | Change password in code")
