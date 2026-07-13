"""
AI Selenium Test Generator - HARDENED VERSION
Fixes: SSRF protection, real auth, error handling, actual test generation, CSV export
"""

import io
import ipaddress
import socket
import time
import zipfile
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from groq import Groq

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GROQ_MODEL = "openai/gpt-oss-120b"  # verified current on Groq as of docs
MAX_LOGIN_ATTEMPTS = 5
REQUEST_TIMEOUT = 10
CRAWL_DELAY_SECONDS = 0.5

st.set_page_config(page_title="AI Test Generator", page_icon="🔒", layout="wide")

# ---------------------------------------------------------------------------
# AUTH
# Uses st.secrets so the real password never lives in source control.
# Add a .streamlit/secrets.toml locally (gitignored) or set the secret in
# your hosting platform:
#   [auth]
#   password = "your-strong-password-here"
# ---------------------------------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "login_attempts" not in st.session_state:
    st.session_state.login_attempts = 0

def get_configured_password():
    try:
        return st.secrets["auth"]["password"]
    except Exception:
        return None

if not st.session_state.authenticated:
    configured_pw = get_configured_password()
    if configured_pw is None:
        st.error(
            "No password configured. Set `[auth] password = ...` in "
            "Streamlit secrets before deploying this app."
        )
        st.stop()

    if st.session_state.login_attempts >= MAX_LOGIN_ATTEMPTS:
        st.error("Too many failed attempts. Restart the app to try again.")
        st.stop()

    pw = st.text_input("Enter Password", type="password")
    if st.button("Unlock"):
        if pw == configured_pw:
            st.session_state.authenticated = True
            st.session_state.login_attempts = 0
            st.rerun()
        else:
            st.session_state.login_attempts += 1
            remaining = MAX_LOGIN_ATTEMPTS - st.session_state.login_attempts
            st.error(f"Incorrect password. {remaining} attempt(s) remaining.")
    st.stop()

st.title("🤖 AI Selenium Test Generator")
st.caption("🔒 Hardened Version — SSRF-protected crawling + AI test generation")

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    groq_api_key = st.text_input("Groq API Key", type="password")
    max_pages = st.slider("Max Pages", 5, 12, 8)
    st.caption("Crawling respects same-origin and blocks internal/private IPs.")

# ---------------------------------------------------------------------------
# SECURITY: SSRF-resistant URL validation
# Resolves DNS and checks the *actual* IP against all private/reserved
# ranges, not just a string prefix match. Also re-validated on every
# redirect hop and every discovered link, not just the initial URL.
# ---------------------------------------------------------------------------
def resolve_all_ips(hostname):
    """Return all IP addresses a hostname resolves to (v4 + v6)."""
    ips = set()
    try:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                infos = socket.getaddrinfo(hostname, None, family)
                for info in infos:
                    ips.add(info[4][0])
            except socket.gaierror:
                continue
    except Exception:
        pass
    return ips

def is_private_or_reserved(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])  # strip zone id for v6
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        return True  # fail closed on anything unparseable

def is_safe_url(url):
    """Validate scheme + resolved IPs. Fails closed on any doubt."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False

    # Block obviously dangerous hostnames outright
    if hostname.lower() in {"localhost", "metadata.google.internal"}:
        return False

    ips = resolve_all_ips(hostname)
    if not ips:
        return False  # couldn't resolve -> don't proceed

    for ip in ips:
        if is_private_or_reserved(ip):
            return False

    return True

def safe_get(url, **kwargs):
    """
    requests.get wrapper that disables automatic redirect-following so we
    can validate every hop ourselves (prevents SSRF via redirect to an
    internal address) and caps the number of manual hops.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    current_url = url
    for _ in range(5):  # max redirect hops
        if not is_safe_url(current_url):
            raise ValueError(f"Blocked unsafe URL: {current_url}")
        resp = requests.get(current_url, allow_redirects=False, **kwargs)
        if resp.is_redirect or resp.is_permanent_redirect:
            next_url = urljoin(current_url, resp.headers.get("Location", ""))
            current_url = next_url
            continue
        return resp
    raise ValueError("Too many redirects")

# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------
def ask_ai(prompt, api_key):
    if not api_key:
        return None, "No Groq API key provided."
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        return resp.choices[0].message.content, None
    except Exception as e:
        return None, f"AI call failed: {e}"

def build_test_prompt(page, locators):
    locator_lines = "\n".join(
        f"- {loc['Element']} | {loc['Identifier']} | text: {loc['Text']}"
        for loc in locators
    )
    return (
        "Generate a Python Selenium test script (using unittest and "
        "selenium.webdriver) that exercises the following page elements. "
        "Use explicit waits (WebDriverWait), sensible test names, and only "
        "the locators listed below. Do not invent elements that aren't "
        f"listed.\n\nPage: {page}\n\nElements:\n{locator_lines}\n\n"
        "Return only the Python code, no explanation."
    )

# ---------------------------------------------------------------------------
# CRAWLER
# ---------------------------------------------------------------------------
def crawl_website(url, max_pages, status_placeholder=None):
    if not is_safe_url(url):
        st.error("Unsafe or unresolvable URL — refusing to crawl.")
        return {}

    visited = {}
    to_visit = [url]
    base = urlparse(url).netloc
    errors = []

    while to_visit and len(visited) < max_pages:
        current = to_visit.pop(0)
        if current in visited:
            continue
        if not is_safe_url(current):
            continue  # re-validate every discovered link, not just the seed

        if status_placeholder:
            status_placeholder.text(f"Fetching ({len(visited)+1}/{max_pages}): {current}")

        try:
            resp = safe_get(current)
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                continue  # skip non-HTML responses (PDFs, images, etc.)
            visited[current] = resp.text
            time.sleep(CRAWL_DELAY_SECONDS)

            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                link = urljoin(current, a["href"])
                if urlparse(link).netloc == base and link not in visited:
                    to_visit.append(link)
        except requests.exceptions.RequestException as e:
            errors.append(f"{current}: network error ({e})")
        except ValueError as e:
            errors.append(f"{current}: {e}")
        except Exception as e:
            errors.append(f"{current}: unexpected error ({e})")

    if errors:
        with st.expander(f"⚠️ {len(errors)} page(s) failed during crawl"):
            for err in errors:
                st.text(err)

    return visited

# ---------------------------------------------------------------------------
# LOCATOR EXTRACTION
# ---------------------------------------------------------------------------
def extract_locators(pages):
    locators = []
    seen = set()
    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        page = urlparse(url).path or "home"
        for tag in ["input", "button", "a", "select", "textarea"]:
            for el in soup.find_all(tag):
                if el.get("type") == "hidden":
                    continue
                ident = ""
                if el.get("id"):
                    ident = f"#{el['id']}"
                elif el.get("name"):
                    ident = f"{tag}[name='{el['name']}']"
                elif el.get("placeholder"):
                    ident = f"{tag}[placeholder='{el.get('placeholder')}']"
                elif el.get("data-test") or el.get("data-testid"):
                    ident = f"[data-test='{el.get('data-test') or el.get('data-testid')}']"
                else:
                    text = el.get_text(strip=True)
                    if text and len(text) < 40 and tag in ["button", "a"]:
                        # Store as an XPath so it's actually usable by Selenium
                        safe_text = text.replace("'", "\\'")
                        ident = f"xpath://{tag}[normalize-space(text())='{safe_text}']"
                    else:
                        continue
                key = f"{page}|{ident}"
                if key in seen:
                    continue
                seen.add(key)
                locators.append({
                    "Page": page,
                    "Element": tag,
                    "Identifier": ident,
                    "Text": el.get_text(strip=True)[:50] or "-",
                })
    return locators

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
url = st.text_input("Website URL", "https://automationexercise.com")

if st.button("🚀 Crawl & Extract Locators"):
    status = st.empty()
    pages = crawl_website(url, max_pages, status_placeholder=status)
    status.empty()
    locs = extract_locators(pages)
    st.session_state.locators = locs
    st.session_state.pages_crawled = list(pages.keys())
    if locs:
        st.success(f"✅ Extracted **{len(locs)}** locators from {len(pages)} page(s)")
    else:
        st.warning("No locators found. The site may block automated requests, "
                    "or no pages were reachable.")

if "locators" in st.session_state and st.session_state.locators:
    st.subheader("Detected Locators")
    df = pd.DataFrame(st.session_state.locators)
    st.dataframe(df, use_container_width=True)

    st.download_button(
        "⬇️ Download locators as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="locators.csv",
        mime="text/csv",
    )

    st.divider()
    st.subheader("Generate Selenium Tests")
    pages_available = sorted(set(df["Page"]))
    selected_pages = st.multiselect(
        "Select pages to generate tests for", pages_available, default=pages_available[:1]
    )

    if st.button("🧪 Generate Selenium Test Scripts"):
        if not groq_api_key:
            st.error("Enter a Groq API key in the sidebar first.")
        elif not selected_pages:
            st.error("Select at least one page.")
        else:
            generated = {}
            progress = st.progress(0.0)
            for i, page in enumerate(selected_pages):
                page_locators = [l for l in st.session_state.locators if l["Page"] == page]
                prompt = build_test_prompt(page, page_locators)
                code, err = ask_ai(prompt, groq_api_key)
                if err:
                    st.error(f"{page}: {err}")
                else:
                    cleaned = code.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("```")[1]
                        if cleaned.startswith("python"):
                            cleaned = cleaned[len("python"):]
                    generated[page] = cleaned.strip()
                progress.progress((i + 1) / len(selected_pages))
            progress.empty()

            if generated:
                st.session_state.generated_tests = generated
                st.success(f"Generated tests for {len(generated)} page(s).")

if st.session_state.get("generated_tests"):
    st.subheader("Generated Test Scripts")
    for page, code in st.session_state.generated_tests.items():
        with st.expander(f"test_{page.strip('/').replace('/', '_') or 'home'}.py"):
            st.code(code, language="python")

    # Bundle all generated scripts into a single downloadable zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for page, code in st.session_state.generated_tests.items():
            filename = f"test_{page.strip('/').replace('/', '_') or 'home'}.py"
            zf.writestr(filename, code)
    zip_buffer.seek(0)

    st.download_button(
        "⬇️ Download all tests as .zip",
        data=zip_buffer,
        file_name="selenium_tests.zip",
        mime="application/zip",
    )

if "locators" not in st.session_state:
    st.info("Click the button above to start")

st.caption("Internal Demo Only")
