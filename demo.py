"""
AI Selenium Test Generator (Streamlit) - DEMO EDITION
Optimized for team explanation (non-AI experts)
"""

import re
import io
import zipfile
import time
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from groq import Groq

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
GROQ_MODEL = "openai/gpt-oss-120b"

st.set_page_config(page_title="AI Selenium Test Generator - Demo", 
                   page_icon="🤖", layout="wide")
st.title("🤖 AI Selenium Test Generator")
st.caption("Live Demo Version - Built for Team Explanation")

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Setup")
    groq_api_key = st.text_input("Groq API Key", type="password", placeholder="gsk_...")
    
    st.markdown("---")
    st.markdown("**Demo Controls**")
    demo_mode = st.checkbox("Enable Demo Mode (Recommended)", value=True)
    
    if demo_mode:
        st.success("✅ Demo Mode Active - Educational explanations enabled")
    
    max_pages = st.slider("Max Pages to Crawl", min_value=5, max_value=30, value=10, step=5)
    
    st.markdown("---")
    st.markdown("### What is Groq?")
    st.caption("Groq is a very fast AI service. We are using a powerful model (`openai/gpt-oss-120b`) that acts like a smart QA engineer.")
    
    st.markdown("---")
    st.caption("Model: `" + GROQ_MODEL + "`")

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
DEFAULTS = {
    "crawled_pages": {}, "base_url": "", "locators": [],
    "test_plan": "", "test_cases": "", "java_code": {},
    "crawl_done": False, "demo_explain": True
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)

# ─────────────────────────────────────────────────────────────
# EDUCATIONAL AI CALL WRAPPER
# ─────────────────────────────────────────────────────────────
def ask_ai(api_key, prompt, system_msg="You are a senior QA automation engineer."):
    if st.session_state.get("demo_explain", True):
        with st.expander("📤 **Demo**: Prompt Sent to AI", expanded=False):
            st.markdown("**System Instruction:**")
            st.code(system_msg, language="markdown")
            st.markdown("**User Prompt (shortened):**")
            st.code(prompt[:1200] + "..." if len(prompt) > 1200 else prompt, language="markdown")
    
    try:
        client = Groq(api_key=api_key)
        with st.spinner("🤖 AI is thinking... (this may take 10-30 seconds)"):
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system_msg},
                          {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4000,
            )
            result = resp.choices[0].message.content
        
        if st.session_state.get("demo_explain", True):
            with st.expander("📥 **Demo**: AI Response Received", expanded=False):
                st.code(result, language="markdown")
        
        return result
    except Exception as e:
        st.error(f"❌ Groq API error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
# REST OF THE CODE (crawl, locators, generation functions)
# ─────────────────────────────────────────────────────────────
# (Keeping original functions mostly unchanged for brevity)

def crawl_website(start_url, max_pages=10):
    visited = {}
    to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    headers = {"User-Agent": "Mozilla/5.0"}
    progress = st.progress(0, text="Crawling website...")

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited: continue
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue
            visited[url] = resp.text
            progress.progress(len(visited) / max_pages, 
                            text=f"Crawled {len(visited)}/{max_pages}: {url}")
            time.sleep(0.8)  # Polite delay for demo

            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                full = urljoin(url, a["href"])
                p = urlparse(full)
                if (p.netloc == base_domain and p.scheme in ("http", "https") 
                    and "#" not in full and full not in visited and full not in to_visit):
                    to_visit.append(full)
        except Exception:
            continue
    progress.empty()
    return visited

# ... [Keep your original extract_locators, build_locators, etc. functions here] ...

def generate_plan_and_cases(api_key, pages_dict, locators):
    # ... (your original function) ...
    pass  # Replace with your existing implementation

def generate_java_code(api_key, locators, pages_dict):
    # ... (your original function) ...
    pass

def create_zip(java_files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in java_files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return buf

# ─────────────────────────────────────────────────────────────
# MAIN UI + DEMO MODE
# ─────────────────────────────────────────────────────────────
st.markdown("### 🌐 Enter Website URL")

c1, c2 = st.columns([3, 1])
with c1:
    default_url = "https://automationexercise.com"
    url_input = st.text_input("URL", value=default_url, 
                             placeholder="https://automationexercise.com")

with c2:
    if st.button("🚀 Start Full Demo", type="primary", use_container_width=True):
        st.session_state.demo_explain = True
        # Reset and run
        for k in DEFAULTS:
            st.session_state[k] = DEFAULTS[k]
        st.session_state.base_url = url_input.rstrip("/")

        with st.spinner("Step 1: Crawling website..."):
            st.session_state.crawled_pages = crawl_website(url_input, max_pages=max_pages)
        
        with st.spinner("Step 2: Extracting locators..."):
            st.session_state.locators = extract_locators(st.session_state.crawled_pages)  # your function
        
        st.session_state.crawl_done = True
        st.success(f"✅ Demo completed crawl of **{len(st.session_state.crawled_pages)}** pages!")

# ─────────────────────────────────────────────────────────────
# TABS WITH EDUCATIONAL CONTENT
# ─────────────────────────────────────────────────────────────
if st.session_state.crawl_done:
    st.info(f"🌐 **Target Site**: {st.session_state.base_url}")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📋 Step 1: Test Plan & Cases",
        "🎯 Step 2: Locators",
        "☕ Step 3: Java Code",
        "🔍 How AI Works (Learn)"
    ])

    with tab1:
        st.subheader("Step 1: Test Plan & Test Cases")
        if st.button("📋 Generate Test Plan & Test Cases", type="primary"):
            with st.spinner("AI is generating..."):
                plan, cases = generate_plan_and_cases(
                    groq_api_key, st.session_state.crawled_pages, st.session_state.locators
                )
                st.session_state.test_plan = plan
                st.session_state.test_cases = cases
            st.rerun()

        if st.session_state.test_plan:
            st.markdown("#### Test Plan")
            st.markdown(st.session_state.test_plan)

    with tab2:
        st.subheader("Step 2: Auto-Extracted Locators")
        # ... your original locator display code ...

    with tab3:
        st.subheader("Step 3: Generate Selenium Java Code")
        if st.button("⚙️ Generate Java Code", type="primary"):
            with st.spinner("Generating Page Objects + TestNG tests..."):
                result = generate_java_code(
                    groq_api_key, st.session_state.locators, st.session_state.crawled_pages
                )
                if result:
                    st.session_state.java_code = result
            st.rerun()
        
        if st.session_state.java_code:
            st.download_button("📦 Download Full Maven Project (.zip)",
                               data=create_zip(st.session_state.java_code),
                               file_name="selenium-tests-demo.zip",
                               mime="application/zip")

    with tab4:
        st.subheader("🔍 How This AI Tool Works")
        st.markdown("""
        ### Simple Explanation for the Team:

        1. **Crawling** - The app visits the website pages (like you opening links).
        2. **Locators** - Finds buttons, input boxes automatically (CSS & XPath).
        3. **AI Brain** - Sends clear English instructions to Groq's powerful model.
        4. **Output** - AI writes professional test documents and Java code.
        
        **Key Takeaway**: AI is a smart assistant, but human review is still important.
        """)

else:
    st.info("👆 Click **Start Full Demo** to begin the demonstration.")

# Footer
st.caption("Demo Optimized for Team Training • AI Selenium Test Generator")
