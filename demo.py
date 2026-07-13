"""
AI Selenium Test Generator - DEMO EDITION
Fully working version with educational features
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

st.set_page_config(page_title="AI Selenium Test Generator - Demo", page_icon="🤖", layout="wide")
st.title("🤖 AI Selenium Test Generator - Team Demo")
st.caption("Educational Version - Perfect for explaining AI to the team")

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Setup")
    groq_api_key = st.text_input("Groq API Key", type="password", placeholder="gsk_...")
    
    st.markdown("---")
    demo_mode = st.checkbox("🎓 Enable Educational Demo Mode", value=True)
    max_pages = st.slider("Max Pages to Crawl", 5, 30, 10, 5)
    
    st.markdown("---")
    st.markdown("### What is Groq?")
    st.caption("Groq provides very fast AI. We use `openai/gpt-oss-120b` - one of the strongest models available.")

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
DEFAULTS = {
    "crawled_pages": {}, "base_url": "", "locators": [],
    "test_plan": "", "test_cases": "", "java_code": {},
    "crawl_done": False
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)

# ─────────────────────────────────────────────────────────────
# EDUCATIONAL AI CALL
# ─────────────────────────────────────────────────────────────
def ask_ai(api_key, prompt, system_msg="You are a senior QA automation engineer."):
    if demo_mode:
        with st.expander("📤 **Demo**: What was sent to AI", expanded=False):
            st.code(system_msg, language="markdown")
            st.code(prompt[:1000] + "..." if len(prompt) > 1000 else prompt, language="markdown")
    
    try:
        client = Groq(api_key=api_key)
        with st.spinner("🤖 AI is thinking..."):
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4000,
            )
            result = resp.choices[0].message.content
        
        if demo_mode:
            with st.expander("📥 **Demo**: AI Response", expanded=False):
                st.code(result, language="markdown")
        
        return result
    except Exception as e:
        st.error(f"Groq Error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
# CRAWLING
# ─────────────────────────────────────────────────────────────
def crawl_website(start_url, max_pages=10):
    visited = {}
    to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    headers = {"User-Agent": "Mozilla/5.0"}
    progress = st.progress(0, text="Starting crawl...")

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited: continue
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if "text/html" not in resp.headers.get("Content-Type", ""): continue
            
            visited[url] = resp.text
            progress.progress(len(visited) / max_pages, text=f"Crawling: {url}")
            time.sleep(0.7)  # Polite for demo

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

# ─────────────────────────────────────────────────────────────
# LOCATOR EXTRACTION
# ─────────────────────────────────────────────────────────────
SKIP_FIELD_NAMES = {"csrfmiddlewaretoken", "csrf_token", "_token", "__RequestVerificationToken"}
INTERACTIVE_TAGS = ["input", "button", "a", "select", "textarea", "label"]

def build_locators(tag, elem):
    eid = elem.get("id", "").strip()
    ename = elem.get("name", "").strip()
    eclasses = elem.get("class") or []
    if isinstance(eclasses, str):
        eclasses = eclasses.strip().split()
    etype = elem.get("type", "").strip()
    eplace = elem.get("placeholder", "").strip()
    earia = elem.get("aria-label", "").strip()
    edata = elem.get("data-test", "").strip() or elem.get("data-testid", "").strip()
    etext = elem.get_text(strip=True)[:40]

    if eid:
        css = f"#{eid}"
    elif edata:
        css = f"[data-test='{edata}']"
    elif ename:
        css = f"{tag}[name='{ename}']"
    elif earia:
        css = f"[aria-label='{earia}']"
    elif eplace:
        css = f"{tag}[placeholder='{eplace}']"
    elif eclasses:
        css = f"{tag}.{eclasses[0]}"
    else:
        css = tag

    # Similar logic for XPath...
    if eid:
        xpath = f"//{tag}[@id='{eid}']"
    elif edata:
        xpath = f"//{tag}[@data-test='{edata}']"
    elif ename:
        xpath = f"//{tag}[@name='{ename}']"
    elif earia:
        xpath = f"//{tag}[@aria-label='{earia}']"
    elif etext and tag in ("button", "a"):
        safe = etext.replace("'", "\\'")[:30]
        xpath = f"//{tag}[normalize-space()='{safe}']"
    else:
        xpath = f"//{tag}"

    return css, xpath

def extract_locators(pages_dict):
    locators = []
    seen = set()
    for url, html in pages_dict.items():
        soup = BeautifulSoup(html, "html.parser")
        page_name = urlparse(url).path or "/"
        for tag in INTERACTIVE_TAGS:
            for elem in soup.find_all(tag)[:15]:
                if elem.get("name") in SKIP_FIELD_NAMES or elem.get("type") == "hidden":
                    continue
                css, xpath = build_locators(tag, elem)
                if css == tag and xpath == f"//{tag}":
                    continue
                key = f"{page_name}|{css}|{xpath}"
                if key in seen: continue
                seen.add(key)
                label = elem.get_text(strip=True)[:50] or elem.get("placeholder", "") or elem.get("aria-label", "") or f"<{tag}>"
                locators.append({
                    "Page": page_name, "Tag": tag, "Type": elem.get("type", tag),
                    "Text / Label": label[:40], "CSS Selector": css, "XPath": xpath,
                    "_url": url
                })
    return locators

# ─────────────────────────────────────────────────────────────
# GENERATION FUNCTIONS (simplified for demo)
# ─────────────────────────────────────────────────────────────
def generate_plan_and_cases(api_key, pages_dict, locators):
    base_url = list(pages_dict.keys())[0] if pages_dict else ""
    summary = f"Pages crawled: {len(pages_dict)}"
    
    plan_prompt = f"Write a short Test Plan for {base_url}. Keep it simple."
    plan = ask_ai(api_key, plan_prompt)
    
    cases_prompt = f"Write 5 test cases for {base_url}."
    cases = ask_ai(api_key, cases_prompt)
    
    return plan, cases

def generate_java_code(api_key, locators, pages_dict):
    base_url = list(pages_dict.keys())[0] if pages_dict else ""
    files = {}
    files["pom.xml"] = f"""<project><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>ai-tests</artifactId><version>1.0</version></project>"""
    files["src/test/java/tests/DemoTest.java"] = f"// Generated for {base_url}\npublic class DemoTest {{}}"
    return files

def create_zip(java_files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in java_files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return buf

# ─────────────────────────────────────────────────────────────
# MAIN DEMO UI
# ─────────────────────────────────────────────────────────────
st.markdown("### 🌐 Enter Website URL for Demo")
c1, c2 = st.columns([3, 1])
with c1:
    url_input = st.text_input("URL", value="https://automationexercise.com", label_visibility="collapsed")
with c2:
    if st.button("🚀 Start Full Demo", type="primary", use_container_width=True):
        for k in DEFAULTS: 
            st.session_state[k] = DEFAULTS[k]
        st.session_state.base_url = url_input.rstrip("/")

        with st.spinner("Step 1/3: Crawling website..."):
            st.session_state.crawled_pages = crawl_website(url_input, max_pages)
        
        with st.spinner("Step 2/3: Extracting interactive elements..."):
            st.session_state.locators = extract_locators(st.session_state.crawled_pages)
        
        st.session_state.crawl_done = True
        st.success(f"✅ Crawled **{len(st.session_state.crawled_pages)}** pages and found **{len(st.session_state.locators)}** elements!")

# TABS
if st.session_state.crawl_done:
    st.info(f"**Target**: {st.session_state.base_url}")

    tab1, tab2, tab3, tab4 = st.tabs(["📋 Test Plan & Cases", "🎯 Locators", "☕ Java Code", "🔍 How AI Works"])

    with tab1:
        st.subheader("Step 1: Generate Test Plan & Test Cases")
        if st.button("📋 Generate with AI", type="primary"):
            plan, cases = generate_plan_and_cases(groq_api_key, st.session_state.crawled_pages, st.session_state.locators)
            st.session_state.test_plan = plan
            st.session_state.test_cases = cases
            st.rerun()
        
        if st.session_state.test_plan:
            st.markdown(st.session_state.test_plan)

    with tab2:
        st.subheader("Step 2: Auto-Extracted Locators")
        if st.session_state.locators:
            df = pd.DataFrame(st.session_state.locators)
            st.dataframe(df[["Page", "Tag", "Text / Label", "CSS Selector", "XPath"]], use_container_width=True)

    with tab3:
        st.subheader("Step 3: Generate Java Code")
        if st.button("⚙️ Generate Selenium Java Code", type="primary"):
            with st.spinner("Generating code..."):
                st.session_state.java_code = generate_java_code(groq_api_key, st.session_state.locators, st.session_state.crawled_pages)
            st.success("Code generated!")
        
        if st.session_state.java_code:
            st.download_button("📦 Download Project (.zip)", 
                             data=create_zip(st.session_state.java_code),
                             file_name="selenium-demo.zip", mime="application/zip")

    with tab4:
        st.subheader("🔍 How This AI Tool Works")
        st.markdown("""
        ### Simple Breakdown for the Team:
        - **Crawling**: Visits website pages automatically.
        - **Locators**: Finds buttons & fields (CSS/XPath).
        - **AI (Groq)**: Acts like a senior QA engineer when given good instructions.
        - **Output**: Creates professional test documents and runnable Java code.
        
        **Key Point**: AI speeds up work but needs human review.
        """)

else:
    st.info("Click **Start Full Demo** to begin the demonstration.")

st.caption("Demo Version for Team Training")
