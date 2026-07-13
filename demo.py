"""
AI Selenium Test Generator - FULLY SECURED DEMO
For internal team use only
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

# ─────────────────────────────────────────────────────────────
# CONFIG & SECURITY SETTINGS
# ─────────────────────────────────────────────────────────────
GROQ_MODEL = "openai/gpt-oss-120b"
ALLOWED_DOMAINS = ["automationexercise.com", "the-internet.herokuapp.com", "example.com"]  # Add more as needed

st.set_page_config(page_title="Secure AI Test Generator", page_icon="🔒", layout="wide")

# Simple password protection
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

def check_password():
    password = st.text_input("Enter Demo Password", type="password")
    if st.button("Login"):
        if password == "demo123":   # Change this password!
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")

if not st.session_state.authenticated:
    st.title("🔒 Secure AI Selenium Test Generator")
    check_password()
    st.stop()

# Main App
st.title("🤖 AI Selenium Test Generator")
st.caption("🔒 Secured Internal Demo Version")

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔒 Secure Setup")
    groq_api_key = st.text_input("Groq API Key", type="password", placeholder="gsk_...")
    
    st.markdown("---")
    demo_mode = st.checkbox("🎓 Educational Mode", value=True)
    max_pages = st.slider("Max Pages", 5, 15, 8)
    
    st.caption("Only whitelisted domains are allowed by default.")

# ─────────────────────────────────────────────────────────────
# SECURITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def is_safe_url(url: str) -> bool:
    """Strong SSRF protection"""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False
    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    
    # Block private & reserved IPs
    blocked = ["localhost", "127.", "0.0.0.0", "::1", "169.254.", "10.", "172.16.", "192.168."]
    if any(hostname.startswith(b) for b in blocked):
        return False
    
    # Optional: Enforce whitelist
    if ALLOWED_DOMAINS and not any(d in hostname for d in ALLOWED_DOMAINS):
        st.warning("Domain not in whitelist. Contact admin.")
        return False
    return True

def secure_filename(filename: str) -> str:
    """Prevent directory traversal"""
    filename = os.path.basename(filename)
    filename = re.sub(r'[^a-zA-Z0-9_.\-]', '_', filename)
    return filename[:120]

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
for key in ["crawled_pages", "base_url", "locators", "test_plan", "test_cases", "java_code", "crawl_done"]:
    if key not in st.session_state:
        st.session_state[key] = {} if key in ["crawled_pages", "java_code"] else "" if "plan" in key or "cases" in key else False

# ─────────────────────────────────────────────────────────────
# AI CALL (Educational)
# ─────────────────────────────────────────────────────────────
def ask_ai(api_key: str, prompt: str, system_msg: str = "You are a senior QA automation engineer."):
    if not api_key:
        st.error("Please provide Groq API key")
        return ""
    try:
        client = Groq(api_key=api_key)
        with st.spinner("🤖 AI is thinking..."):
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2500,
            )
            result = resp.choices[0].message.content
            
            if demo_mode:
                with st.expander("📤 Prompt / 📥 Response (Educational)", expanded=False):
                    st.code(prompt[:700] + "...", language="markdown")
                    st.code(result, language="markdown")
            return result
    except Exception as e:
        st.error(f"AI Error: {str(e)[:100]}")
        return ""

# Add your crawl_website, extract_locators, generate functions here 
# (use the improved versions from previous messages)

# ─────────────────────────────────────────────────────────────
# MAIN UI
# ─────────────────────────────────────────────────────────────
st.markdown("### 🌐 Enter Website URL")

c1, c2 = st.columns([3, 1])
with c1:
    url_input = st.text_input("URL", value="https://automationexercise.com")

with c2:
    if st.button("🚀 Start Secure Crawl", type="primary"):
        if not groq_api_key:
            st.error("Enter Groq API Key")
            st.stop()
        if not is_safe_url(url_input):
            st.error("❌ URL blocked for security reasons.")
            st.stop()

        # Reset state
        st.session_state.crawled_pages = {}
        st.session_state.locators = []
        st.session_state.java_code = {}
        st.session_state.base_url = url_input.rstrip("/")
        
        with st.spinner("Securely crawling..."):
            st.session_state.crawled_pages = crawl_website(url_input, max_pages)  # your function
            st.session_state.locators = extract_locators(st.session_state.crawled_pages)
        st.session_state.crawl_done = True
        st.success("✅ Secure crawl completed successfully!")

# Add your tabs here (Test Plan, Locators, Java Code, How AI Works)

st.caption("🔒 Internal Use Only | Password Protected")
