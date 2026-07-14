"""
AI Selenium Test Generator (Streamlit)
=======================================
Flow:
  1. Enter a URL -> Start
     -> crawls the site (SSRF-protected) AND auto-extracts locators
  2. Tab 1: Generate Test Plan + Test Cases together (one button)
  3. Tab 2: Review the auto-extracted locators (CSS + XPath)
  4. Tab 3: "Advance" -> Generate Selenium Java code (Page Objects + TestNG),
     grounded in the exact test cases approved in Tab 1
  5. Tab 3: Download everything as a ready-to-run Maven project (.zip)

Setup
-----
Create `.streamlit/secrets.toml` (or paste into Streamlit Cloud's
Settings -> Secrets):

    [auth]
    password = "your-strong-password-here"

    [groq]
    api_key = "your-groq-api-key-here"

Run with:
    pip install -r requirements.txt
    streamlit run app.py
"""

import hmac
import io
import ipaddress
import re
import socket
import time
import zipfile
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from groq import Groq

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
# Both models below are current PRODUCTION models on Groq. Groq deprecated
# llama-3.1-8b-instant / llama-3.3-70b-versatile in June 2026 and points
# people at these two instead. If Groq retires one of these, this is the
# one place to update.
MODEL_OPTIONS = {
    "openai/gpt-oss-120b (best quality)": "openai/gpt-oss-120b",
    "openai/gpt-oss-20b (fastest)": "openai/gpt-oss-20b",
}
MAX_LOGIN_ATTEMPTS = 5
REQUEST_TIMEOUT = 10
CRAWL_DELAY_SECONDS = 0.4
MAX_PAGE_OBJECTS = 6          # how many pages get a Page Object class
ELEMENTS_PER_PAGE_OBJECT = 18  # locators fed into one Page Object prompt
CASE_PAGES_LIMIT = 10          # pages shown to the AI when writing test cases
CASE_ELEMENTS_PER_PAGE = 10    # locators per page shown when writing test cases

st.set_page_config(page_title="AI Test Generator", page_icon="🔒", layout="wide")

# ─────────────────────────────────────────────────────────────
# AUTH — password + Groq key both come from secrets, never hardcoded.
# ─────────────────────────────────────────────────────────────
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "login_attempts" not in st.session_state:
    st.session_state.login_attempts = 0


def get_configured_password():
    """Preferred: [auth] password = "...".  Also accepts a flat `password`
    key in case it was pasted into Streamlit Cloud's Secrets box without
    the [auth] section."""
    try:
        return st.secrets["auth"]["password"]
    except Exception:
        pass
    try:
        return st.secrets["password"]
    except Exception:
        return None


def get_secret_groq_key():
    """Preferred: [groq] api_key = "...".  Also accepts a few common flat
    key names for the same reason as above."""
    try:
        return st.secrets["groq"]["api_key"]
    except Exception:
        pass
    for flat_key in ("GROQ_API_KEY", "groq_api_key", "api_key"):
        try:
            return st.secrets[flat_key]
        except Exception:
            continue
    return None


if not st.session_state.authenticated:
    configured_pw = get_configured_password()
    if configured_pw is None:
        st.error(
            "No password configured. Add `[auth] password = ...` to "
            "`.streamlit/secrets.toml` (or Streamlit Cloud's Settings -> "
            "Secrets) before running this app."
        )
        st.stop()

    if st.session_state.login_attempts >= MAX_LOGIN_ATTEMPTS:
        st.error("Too many failed attempts. Restart the app to try again.")
        st.stop()

    pw = st.text_input("Enter Password", type="password")
    if st.button("Unlock"):
        if hmac.compare_digest(pw, configured_pw):
            st.session_state.authenticated = True
            st.session_state.login_attempts = 0
            st.rerun()
        else:
            st.session_state.login_attempts += 1
            remaining = MAX_LOGIN_ATTEMPTS - st.session_state.login_attempts
            st.error(f"Incorrect password. {remaining} attempt(s) remaining.")
    st.stop()

st.title("🤖 AI Selenium Test Generator")
st.caption("Crawl a website → Test Plan & Test Cases → Locators → Selenium Java Code → Download")

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Setup")

    model_label = st.selectbox("AI Model", list(MODEL_OPTIONS.keys()))
    selected_model = MODEL_OPTIONS[model_label]

    max_pages = st.slider("Max Pages to Crawl", min_value=5, max_value=25, value=15, step=5)

    secret_groq_key = get_secret_groq_key()
    if secret_groq_key:
        groq_api_key = secret_groq_key
        st.caption("✅ Groq API key loaded from secrets")
    else:
        groq_api_key = st.text_input("Groq API Key", type="password", placeholder="gsk_...")

    st.markdown("---")
    st.markdown("**How it works:**")
    st.markdown("1️⃣ Enter URL, click Start")
    st.markdown("2️⃣ Tab 1 → Generate Test Plan + Test Cases")
    st.markdown("3️⃣ Tab 2 → Review Locators (auto-found)")
    st.markdown("4️⃣ Tab 3 → Generate Java Code (grounded in Tab 1's cases) → Download")
    st.markdown("---")
    st.caption(f"Model: `{selected_model}`")
    st.caption("Crawling respects same-origin and blocks internal/private IPs.")

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
DEFAULTS = {
    "crawled_pages": {},   # {url: html}
    "base_url": "",
    "locators": [],        # list of dicts, auto-filled right after crawl
    "test_plan": "",
    "test_cases": "",
    "java_code": {},       # {filepath: code}
    "crawl_done": False,
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)

# ─────────────────────────────────────────────────────────────
# SECURITY: SSRF-resistant URL validation
# Resolves DNS and checks the *actual* IP against all private/reserved
# ranges, not just a string prefix match. Re-validated on every redirect
# hop and every discovered link, not just the initial URL.
# ─────────────────────────────────────────────────────────────
def resolve_all_ips(hostname):
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
        ip = ipaddress.ip_address(ip_str.split("%")[0])
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        return True  # fail closed


def is_safe_url(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname.lower() in {"localhost", "metadata.google.internal"}:
        return False
    ips = resolve_all_ips(hostname)
    if not ips:
        return False
    return not any(is_private_or_reserved(ip) for ip in ips)


def safe_get(url, **kwargs):
    """requests.get wrapper that manually follows redirects so every hop
    can be re-validated (prevents SSRF via redirect to an internal IP)."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs.setdefault("headers", {"User-Agent": "Mozilla/5.0"})
    current_url = url
    for _ in range(5):
        if not is_safe_url(current_url):
            raise ValueError(f"Blocked unsafe URL: {current_url}")
        resp = requests.get(current_url, allow_redirects=False, **kwargs)
        if resp.is_redirect or resp.is_permanent_redirect:
            current_url = urljoin(current_url, resp.headers.get("Location", ""))
            continue
        return resp
    raise ValueError("Too many redirects")


# ─────────────────────────────────────────────────────────────
# GROQ CALL (single place all AI calls go through)
# ─────────────────────────────────────────────────────────────
def ask_ai(api_key, prompt, system_msg="You are a senior QA automation engineer.",
           model=None, max_tokens=2000):
    if not api_key:
        st.error("❌ No Groq API key available. Add it in the sidebar or in secrets.")
        return ""
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=model or MODEL_OPTIONS[next(iter(MODEL_OPTIONS))],
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        msg = str(e).lower()
        if "invalid_api_key" in msg or "authentication" in msg:
            st.error("❌ Invalid Groq API key. Check the sidebar / secrets.")
        elif "rate_limit" in msg:
            st.error("⚠️ Groq rate limit hit. Wait a moment and try again.")
        elif "context_length" in msg or "token" in msg:
            st.error("⚠️ Too much content for the model. Try crawling fewer pages.")
        elif "decommissioned" in msg or "not found" in msg:
            st.error(f"⚠️ Model isn't available on your account. "
                      f"Check https://console.groq.com/docs/models for current model IDs.")
        else:
            st.error(f"❌ Groq API error: {e}")
        return ""


def clean_code_fences(text):
    return re.sub(r"```(?:java|xml)?", "", text).strip()


# ─────────────────────────────────────────────────────────────
# CRAWLING
# ─────────────────────────────────────────────────────────────
def crawl_website(start_url, max_pages=15):
    """Same-origin breadth-first crawl, SSRF-protected."""
    if not is_safe_url(start_url):
        st.error("🚫 Unsafe or unresolvable URL — refusing to crawl.")
        return {}

    visited = {}
    to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    errors = []
    progress = st.progress(0, text="Starting crawl...")

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited or not is_safe_url(url):
            continue
        try:
            resp = safe_get(url)
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue
            visited[url] = resp.text
            progress.progress(len(visited) / max_pages, text=f"Crawling ({len(visited)}/{max_pages}): {url}")
            time.sleep(CRAWL_DELAY_SECONDS)

            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                full = urljoin(url, a["href"]).split("#")[0]
                p = urlparse(full)
                if (p.netloc == base_domain and p.scheme in ("http", "https")
                        and full not in visited and full not in to_visit):
                    to_visit.append(full)
        except requests.exceptions.RequestException as e:
            errors.append(f"{url}: network error ({e})")
        except ValueError as e:
            errors.append(f"{url}: {e}")
        except Exception as e:
            errors.append(f"{url}: unexpected error ({e})")

    progress.empty()
    if errors:
        with st.expander(f"⚠️ {len(errors)} page(s) failed during crawl"):
            for err in errors:
                st.text(err)
    return visited


# ─────────────────────────────────────────────────────────────
# LOCATOR EXTRACTION
# ─────────────────────────────────────────────────────────────
SKIP_FIELD_NAMES = {"csrfmiddlewaretoken", "csrf_token", "_token", "__RequestVerificationToken"}
INTERACTIVE_TAGS = ["input", "button", "a", "select", "textarea", "label"]
BARE_TAGS = set(INTERACTIVE_TAGS)
# Only these characters are safe to drop straight into a CSS class selector.
# Utility-CSS frameworks (Tailwind etc.) produce classes like "hover:bg-red-500"
# or "sm:w-1/2" that are NOT valid CSS identifiers and would silently produce
# a broken selector - those are filtered out here instead.
CSS_IDENT_RE = re.compile(r"^-?[a-zA-Z_][a-zA-Z0-9_-]*$")


def valid_css_classes(classes):
    return [c for c in classes if CSS_IDENT_RE.match(c)]


def build_locators(tag, elem):
    """Build a CSS selector and an XPath for one element, best identifier first."""
    eid = elem.get("id", "").strip()
    ename = elem.get("name", "").strip()
    eclasses = valid_css_classes(elem.get("class", []) or [])
    eplace = elem.get("placeholder", "").strip()
    earia = elem.get("aria-label", "").strip()
    edata_test = elem.get("data-test", "").strip()
    edata_testid = elem.get("data-testid", "").strip()
    etext = elem.get_text(strip=True)[:40]

    # CSS: id > data-test/data-testid > name > aria-label > placeholder > class > bare tag
    if eid:
        css = f"#{eid}"
    elif edata_test:
        css = f"[data-test='{edata_test}']"
    elif edata_testid:
        css = f"[data-testid='{edata_testid}']"
    elif ename:
        css = f"{tag}[name='{ename}']"
    elif earia:
        css = f"[aria-label='{earia}']"
    elif eplace:
        css = f"{tag}[placeholder='{eplace}']"
    elif eclasses:
        css = f"{tag}.{'.'.join(eclasses[:2])}"
    else:
        css = tag  # ambiguous, filtered out later

    # XPath: same priority, with visible text as a fallback for buttons/links
    # (CSS can't select by text, so this is the one case XPath is preferred).
    if eid:
        xpath = f"//{tag}[@id='{eid}']"
    elif edata_test:
        xpath = f"//{tag}[@data-test='{edata_test}']"
    elif edata_testid:
        xpath = f"//{tag}[@data-testid='{edata_testid}']"
    elif ename:
        xpath = f"//{tag}[@name='{ename}']"
    elif earia:
        xpath = f"//{tag}[@aria-label='{earia}']"
    elif eplace:
        xpath = f"//{tag}[@placeholder='{eplace}']"
    elif etext and tag in ("button", "a", "label"):
        safe = etext.replace("'", "\\'")[:30]
        xpath = f"//{tag}[normalize-space()='{safe}']"
    elif eclasses:
        xpath = f"//{tag}[contains(@class,'{eclasses[0]}')]"
    else:
        xpath = f"//{tag}"

    return css, xpath


def extract_locators(pages_dict):
    """Scan every crawled page for interactive elements and build locators."""
    locators = []
    seen = set()

    for url, html in pages_dict.items():
        soup = BeautifulSoup(html, "html.parser")
        page_name = urlparse(url).path or "/"

        for tag in INTERACTIVE_TAGS:
            for elem in soup.find_all(tag)[:20]:
                if elem.get("name", "") in SKIP_FIELD_NAMES or elem.get("type", "") == "hidden":
                    continue  # never turn hidden/CSRF fields into locators

                css, xpath = build_locators(tag, elem)
                if css == tag and xpath == f"//{tag}":
                    continue  # too ambiguous to be useful

                key = f"{page_name}|{tag}|{css}|{xpath}"
                if key in seen:
                    continue
                seen.add(key)

                label = (
                    elem.get_text(strip=True)[:50] or elem.get("placeholder", "")
                    or elem.get("aria-label", "") or elem.get("name", "")
                    or elem.get("id", "") or elem.get("href", "")[:30] or f"<{tag}>"
                )
                locators.append({
                    "Page": page_name,
                    "Tag": tag,
                    "Type": elem.get("type", tag),
                    "Text / Label": label[:40],
                    "CSS Selector": css,
                    "XPath": xpath,
                    "_url": url,  # original URL, used later to build correct page URLs
                })

    return locators


def filter_unique_locators(page_locs):
    """Drop bare-tag / duplicate CSS selectors so the AI never generates
    duplicate @FindBy fields like css="a" or css="button"."""
    seen_css = set()
    filtered = []
    for loc in page_locs:
        if loc["CSS Selector"] in BARE_TAGS:
            continue
        if loc["CSS Selector"] in seen_css:
            continue
        seen_css.add(loc["CSS Selector"])
        filtered.append(loc)
    return filtered


# ─────────────────────────────────────────────────────────────
# PAGE SUMMARY (context fed to the AI)
# Includes simple feature detection (login form, search, etc.) so the AI
# writes a test plan/cases specific to this site instead of generic
# boilerplate - this is the single biggest lever on output quality.
# ─────────────────────────────────────────────────────────────
def build_page_summary(pages_dict, limit=12):
    summary = ""
    for url, html in list(pages_dict.items())[:limit]:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else url

        has_password = bool(soup.find("input", attrs={"type": "password"}))
        has_email = bool(soup.find("input", attrs={"type": "email"})) or bool(
            soup.find("input", attrs={"name": re.compile("email", re.I)})
        )
        has_search = bool(soup.find("input", attrs={"type": "search"})) or bool(
            soup.find("input", attrs={"placeholder": re.compile("search", re.I)})
        )
        features = []
        if has_password:
            features.append("login/password form")
        if has_email and not has_password:
            features.append("email field")
        if has_search:
            features.append("search")
        feature_note = f" | Notable: {', '.join(features)}" if features else ""

        summary += (
            f"\nURL: {url}\nTitle: {title}\n"
            f"Forms: {len(soup.find_all('form'))} | Buttons: {len(soup.find_all('button'))} | "
            f"Inputs: {len(soup.find_all('input'))} | Links: {len(soup.find_all('a', href=True))}"
            f"{feature_note}\n"
        )
    return summary


def fix_testcase_formatting(raw_text):
    """Force every TC_* field onto its own line (models sometimes merge them)."""
    fields = ["**TC_ID:**", "**Summary:**", "**Page:**", "**Prerequisites:**",
              "**Test Steps:**", "**Expected Result:**", "**Priority:**", "**Type:**"]
    result = raw_text
    for field in fields:
        result = re.sub(rf"(?<!\n)\s*({re.escape(field)})", r"\n\n\1", result)
    result = re.sub(r"(?<!\n)(---)", r"\n\n\1\n\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ─────────────────────────────────────────────────────────────
# STEP 1: TEST PLAN + TEST CASES (combined into one call/button)
# ─────────────────────────────────────────────────────────────
def generate_plan_and_cases(api_key, pages_dict, locators, model):
    base_url = st.session_state.base_url or list(pages_dict.keys())[0]
    summary = build_page_summary(pages_dict)

    # Real crawled URLs, so the AI can't invent a "Page" that doesn't exist.
    crawled_urls = "\n".join(f"- {u}" for u in list(pages_dict.keys())[:CASE_PAGES_LIMIT])

    # Group locators by page (not a flat list) so cases are grounded to the
    # right elements on the right page, instead of mixing elements from
    # different pages into one test case.
    by_page = {}
    for l in locators:
        by_page.setdefault(l["Page"], []).append(l)

    loc_info = ""
    for page, locs in list(by_page.items())[:CASE_PAGES_LIMIT]:
        clean = filter_unique_locators(locs)[:CASE_ELEMENTS_PER_PAGE]
        if not clean:
            continue
        rows = "\n".join(f"    [{l['Tag']}] '{l['Text / Label']}' | CSS: {l['CSS Selector']}" for l in clean)
        loc_info += f"\n  Page {page}:\n{rows}\n"
    if not loc_info:
        loc_info = "No elements extracted yet — base cases on page structure above."

    plan_prompt = f"""
You are a senior QA engineer. Write a short, formal TEST PLAN for this website.
This is a high-level strategy document — do NOT write individual test cases here.

Website: {base_url}
Pages crawled: {len(pages_dict)}
Elements found: {len(locators)}

Pages (including detected features like login forms, search, etc.):
{summary}

Sections (use exactly these headers):
## 1. Introduction
## 2. Scope of Testing
## 3. Test Objectives
## 4. Testing Types Covered
## 5. Test Environment
## 6. Entry & Exit Criteria
## 7. Risks & Mitigation
## 8. Deliverables

Reference the specific features detected above (e.g. call out login testing
explicitly if a login form was found) instead of writing generic boilerplate.
Keep every section short and specific to this website.
"""
    plan = ask_ai(api_key, plan_prompt, "You are a senior QA engineer writing a formal test plan.",
                  model=model, max_tokens=2000)

    cases_prompt = f"""
You are a senior QA engineer. Write at least 12 structured test cases for this website.

Base URL: {base_url}

Actual crawled pages — the "Page" field in every test case MUST be copied
exactly from this list. Never invent or guess a URL:
{crawled_urls}

Elements per page:
{loc_info}

Cover: Functional UI, Form Validation, Navigation/Links, and Login/Auth (if
a login form was detected above). Include at least one negative/validation
test (e.g. submitting a form with a required field empty or invalid) for
every form found.

FORMATTING RULES (follow exactly):
- Each field on its own line, one blank line between fields
- Put --- on its own line before each test case
- "Page" must be copied exactly from the crawled pages list above
- "Prerequisites" is a bullet list

Format each test case EXACTLY like this:

---

**TC_ID:** TC_001

**Summary:** one sentence

**Page:** (one of the exact URLs listed above)

**Prerequisites:**
- Browser is open and internet is available

**Test Steps:**
1. Step one
2. Step two

**Expected Result:** what should happen

**Priority:** High

**Type:** Functional

Repeat this exact structure for all 12+ cases. Never merge two fields on one line.
"""
    cases = ask_ai(api_key, cases_prompt,
                    "You are a senior QA engineer. Put every test case field on its own line, "
                    "never merge two fields onto one line, and never invent a URL.",
                    model=model, max_tokens=4000)
    return plan, cases


# ─────────────────────────────────────────────────────────────
# STEP 2: SELENIUM JAVA CODE ("Advance" step)
# The TestNG class is grounded in the exact test cases from Step 1, instead
# of a fixed, arbitrary list of test methods unrelated to what Tab 1 wrote -
# this is what makes the "manual" and "automated" tests actually match.
# ─────────────────────────────────────────────────────────────
def generate_java_code(api_key, locators, pages_dict, model, test_cases_text=""):
    if not pages_dict:
        st.error("❌ No crawled pages. Cannot generate Java code.")
        return {}

    base_url = st.session_state.base_url or list(pages_dict.keys())[0].rstrip("/")
    java_files = {}

    # Group locators by page, and remember each page's real crawled URL
    pages, page_url_lookup = {}, {}
    for loc in locators:
        page_key = loc["Page"].strip("/").replace("/", "_").replace("-", "_") or "home"
        page_url_lookup.setdefault(page_key, loc.get("_url", base_url))
        pages.setdefault(page_key, []).append(loc)

    # Prioritize pages with the most usable locators (forms, login, checkout
    # pages usually have the most) rather than an arbitrary "first 4 crawled" -
    # this stops important pages like login from being silently dropped.
    ranked_pages = sorted(
        pages.items(), key=lambda kv: len(filter_unique_locators(kv[1])), reverse=True
    )[:MAX_PAGE_OBJECTS]

    # ---- Page Object classes ----
    for page_name, page_locs in ranked_pages:
        class_name = "".join(w.capitalize() for w in re.split(r"[_\-\s]+", page_name) if w) + "Page"
        full_page_url = page_url_lookup.get(page_name, base_url)
        clean_locs = filter_unique_locators(page_locs)[:ELEMENTS_PER_PAGE_OBJECT]
        if not clean_locs:
            continue

        elements_info = "\n".join(
            f'  [{l["Tag"]}] label="{l["Text / Label"]}" CSS="{l["CSS Selector"]}" XPath="{l["XPath"]}"'
            for l in clean_locs
        )

        prompt = f"""
Generate a complete Selenium Java Page Object Model class named {class_name}.

Real page URL (use ONLY this, never example.com): {full_page_url}

Elements on this page:
{elements_info}

Requirements:
- Package: pages
- Import: config.BaseConfig
- public static final String PAGE_URL = "{full_page_url}";
- Use @FindBy annotations only (no By.* inside methods)
- Prefer CSS; use XPath only when CSS is a bare tag
- Constructor takes WebDriver, calls PageFactory.initElements(driver, this)
- navigateTo(WebDriver driver) does driver.get(PAGE_URL)
- One action method per element: clickX(), enterX(String text), getXText()
- NEVER use a bare-tag @FindBy (css = "a", "button", "input")
- NEVER duplicate a @FindBy locator — keep the first occurrence only
- Include all imports
- Return ONLY Java code, no markdown fences, no explanation
"""
        code = ask_ai(api_key, prompt, "You are a Selenium Java expert. Return only clean Java code.",
                      model=model, max_tokens=2500)
        if code:
            java_files[f"src/main/java/pages/{class_name}.java"] = clean_code_fences(code)

    # ---- TestNG test class, grounded in the Tab 1 test cases ----
    page_classes = ["".join(w.capitalize() for w in re.split(r"[_\-\s]+", p) if w) + "Page" for p, _ in ranked_pages]
    sample = "\n".join(
        f'  [{l["Tag"]}] "{l["Text / Label"]}" CSS: {l["CSS Selector"]} | XPath: {l["XPath"]}'
        for l in filter_unique_locators(locators)[:20]
    )

    test_prompt = f"""
Generate a complete Selenium Java TestNG test class named WebAppTest that
AUTOMATES the approved test cases below — one @Test method per test case.
Do not invent extra scenarios and do not skip any test case listed. Name
each method after its TC_ID plus a short summary, e.g.
testTC001LoginWithValidCredentials. Base each method's steps and assertions
on that exact test case's Test Steps and Expected Result.

Approved test cases:
{test_cases_text or "(No manual test cases were generated in Tab 1 yet — "
                     "write 6 reasonable smoke tests covering navigation, "
                     "forms, and buttons instead.)"}

Base URL (use only this or its subpages, never example.com): {base_url}
Page Objects available: {", ".join(page_classes)}
Key elements:
{sample}

Requirements:
- Package: tests
- Import: config.BaseConfig, org.testng.Assert (NOT org.testng.asserts.Assert), java.time.Duration
- public static final String BASE_URL = "{base_url}";
- @BeforeClass: WebDriverManager.chromedriver().setup(); new ChromeDriver(); maximize window;
  implicit wait 10s; driver.get(BASE_URL); instantiate all page objects
- @AfterClass: driver.quit()
- Use a @DataProvider for any test case that implies multiple inputs (e.g. valid/invalid login)
- Navigation/click assertions must use the locators/page objects provided, never By.cssSelector("a")
- Every driver.get() must use BASE_URL or a real subpath from the page objects above, never a fake URL
- Return ONLY Java code, no markdown fences, no explanation
"""
    test_code = ask_ai(api_key, test_prompt,
                        "You are a Selenium TestNG expert. Return only clean Java code. "
                        "Never use bare CSS selectors like By.cssSelector('a').",
                        model=model, max_tokens=6000)
    if test_code:
        java_files["src/test/java/tests/WebAppTest.java"] = clean_code_fences(test_code)

    # ---- Static support files ----
    java_files["src/main/java/config/BaseConfig.java"] = f"""package config;

/** Central config. Target site: {base_url} */
public class BaseConfig {{
    public static final String BASE_URL = "{base_url}";
    public static final int    TIMEOUT  = 10;
    public static final String BROWSER  = "chrome";
}}
"""

    java_files["pom.xml"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.selenium.tests</groupId>
  <artifactId>ai-generated-tests</artifactId>
  <version>1.0-SNAPSHOT</version>
  <!-- Target site: {base_url} -->
  <properties>
    <maven.compiler.source>11</maven.compiler.source>
    <maven.compiler.target>11</maven.compiler.target>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.seleniumhq.selenium</groupId>
      <artifactId>selenium-java</artifactId>
      <version>4.24.0</version>
    </dependency>
    <dependency>
      <groupId>org.testng</groupId>
      <artifactId>testng</artifactId>
      <version>7.10.2</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>io.github.bonigarcia</groupId>
      <artifactId>webdrivermanager</artifactId>
      <version>5.9.2</version>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
        <configuration>
          <suiteXmlFiles><suiteXmlFile>testng.xml</suiteXmlFile></suiteXmlFiles>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>"""

    java_files["testng.xml"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE suite SYSTEM "https://testng.org/testng-1.0.dtd">
<!-- Auto-generated for: {base_url} -->
<suite name="AI Generated Suite" verbose="1">
  <test name="WebApp Tests">
    <classes><class name="tests.WebAppTest"/></classes>
  </test>
</suite>"""

    return java_files


def create_zip(java_files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in java_files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────────
# MAIN UI — URL INPUT + START
# ─────────────────────────────────────────────────────────────
st.markdown("### 🌐 Enter Website URL")
c1, c2 = st.columns([3, 1])
with c1:
    url_input = st.text_input("URL", placeholder="https://automationexercise.com",
                               label_visibility="collapsed")
with c2:
    start_btn = st.button("🚀 Start", use_container_width=True, type="primary")

if start_btn:
    if not groq_api_key:
        st.error("❌ No Groq API key available. Add it in the sidebar or in secrets.")
        st.stop()
    if not url_input.startswith("http"):
        st.error("❌ Please enter a valid URL starting with http:// or https://")
        st.stop()

    for k, v in DEFAULTS.items():
        st.session_state[k] = v
    st.session_state.base_url = url_input.rstrip("/")

    with st.spinner("🔍 Crawling website..."):
        st.session_state.crawled_pages = crawl_website(url_input, max_pages=max_pages)

    if st.session_state.crawled_pages:
        with st.spinner("🎯 Auto-extracting locators..."):
            st.session_state.locators = extract_locators(st.session_state.crawled_pages)
        st.session_state.crawl_done = True
        st.success(
            f"✅ Crawled **{len(st.session_state.crawled_pages)} pages** and found "
            f"**{len(st.session_state.locators)} elements**. Continue in the tabs below."
        )
    else:
        st.warning("No pages were crawled. The URL may be unreachable, or was blocked by the SSRF guard.")

# ─────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────
if st.session_state.crawl_done:
    st.info(f"🌐 Target: **{st.session_state.base_url}**")

    tab1, tab2, tab3 = st.tabs([
        "📋 Step 1: Test Plan & Test Cases",
        "🎯 Step 2: Locators",
        "☕ Step 3: Java Code & Download",
    ])

    # ── TAB 1: TEST PLAN + TEST CASES ──────────────────────────
    with tab1:
        st.subheader("📋 Test Plan & Test Cases")

        with st.expander("🌐 Crawled Pages"):
            for i, u in enumerate(st.session_state.crawled_pages.keys(), 1):
                st.write(f"{i}. {u}")

        if st.session_state.test_plan or st.session_state.test_cases:
            st.markdown("#### Test Plan")
            st.markdown(st.session_state.test_plan)
            st.markdown("---")
            st.markdown("#### Test Cases (Manual)")
            st.markdown(fix_testcase_formatting(st.session_state.test_cases))

            if st.button("🔄 Regenerate"):
                st.session_state.test_plan = ""
                st.session_state.test_cases = ""
                st.rerun()
        else:
            st.info("One click generates both the Test Plan and the individual Test Cases.")
            if st.button("📋 Generate Test Plan & Test Cases", type="primary"):
                with st.spinner("AI is writing the test plan and test cases..."):
                    plan, cases = generate_plan_and_cases(
                        groq_api_key, st.session_state.crawled_pages,
                        st.session_state.locators, selected_model,
                    )
                    st.session_state.test_plan = plan
                    st.session_state.test_cases = cases
                st.rerun()

    # ── TAB 2: LOCATORS (auto-extracted, view only) ────────────
    with tab2:
        st.subheader("🎯 Element Locators — XPath & CSS")
        st.caption("Auto-extracted right after crawling.")

        locs = st.session_state.locators
        if not locs:
            st.warning(
                "⚠️ No locators found. The site may render via JavaScript (this crawler only "
                "sees the initial HTML). Try **https://the-internet.herokuapp.com** or "
                "**https://automationexercise.com** to test the flow."
            )
        else:
            clean_count = len(filter_unique_locators(locs))
            st.success(f"✅ Found **{len(locs)} elements**.")
            if clean_count < len(locs):
                st.info(
                    f"ℹ️ **{clean_count} unique elements** will be used for Java code "
                    f"({len(locs) - clean_count} ambiguous/duplicate ones are skipped)."
                )

            df = pd.DataFrame(locs)
            pages_opt = ["All Pages"] + sorted(df["Page"].unique().tolist())
            sel = st.selectbox("Filter by Page", pages_opt)
            filtered = df if sel == "All Pages" else df[df["Page"] == sel]
            st.dataframe(
                filtered[["Page", "Tag", "Type", "Text / Label", "CSS Selector", "XPath"]],
                use_container_width=True, height=400,
            )
            st.caption(f"Showing {len(filtered)} of {len(locs)} elements")

            st.download_button(
                "⬇️ Download locators as CSV",
                data=df.drop(columns=["_url"]).to_csv(index=False).encode("utf-8"),
                file_name="locators.csv",
                mime="text/csv",
            )

            if st.button("🔄 Re-extract Locators"):
                st.session_state.locators = extract_locators(st.session_state.crawled_pages)
                st.session_state.java_code = {}
                st.rerun()

    # ── TAB 3: JAVA CODE ("Advance" step) + DOWNLOAD, combined ──
    with tab3:
        st.subheader("☕ Selenium Java Code")
        st.caption("Page Object classes + TestNG test class + config + pom.xml + testng.xml")

        if not st.session_state.locators:
            st.warning("⚠️ No locators available. Crawl a site with more interactive elements.")

        elif not st.session_state.java_code:
            clean_count = len(filter_unique_locators(st.session_state.locators))
            st.info(f"Ready to generate from **{clean_count} unique elements**.")
            if not st.session_state.test_cases:
                st.caption("💡 Tip: generate Test Cases in Tab 1 first so the Java tests map "
                           "1-to-1 to your approved manual test cases. You can still generate "
                           "code directly from locators without one.")
            if st.button("⚙️ Advance: Generate Java Code", type="primary"):
                with st.spinner("AI is generating Selenium Java code... (~30-60s)"):
                    result = generate_java_code(
                        groq_api_key, st.session_state.locators, st.session_state.crawled_pages,
                        selected_model, st.session_state.test_cases,
                    )
                    if result:
                        st.session_state.java_code = result
                st.rerun()

        else:
            st.success(f"✅ {len(st.session_state.java_code)} files generated for "
                       f"**{st.session_state.base_url}**")
            if st.button("🔄 Regenerate Code"):
                st.session_state.java_code = {}
                st.rerun()

            st.markdown("---")
            st.markdown("### 📥 Download")

            c1, c2 = st.columns(2)
            with c1:
                st.download_button(
                    "📦 Download Full Maven Project (.zip)",
                    data=create_zip(st.session_state.java_code),
                    file_name="selenium-tests.zip",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                )
            with c2:
                test_code = next(
                    (c for f, c in st.session_state.java_code.items() if "WebAppTest.java" in f), ""
                )
                if test_code:
                    st.download_button(
                        "☕ Download WebAppTest.java only",
                        data=test_code,
                        file_name="WebAppTest.java",
                        mime="text/plain",
                        use_container_width=True,
                    )

            st.markdown("---")
            st.markdown("**📋 Individual Files**")
            for fname, code in st.session_state.java_code.items():
                lang = "java" if fname.endswith(".java") else "xml"
                with st.expander(f"📄 {fname}"):
                    st.code(code, language=lang)

            st.markdown("---")
            st.markdown("### 🚀 How to Run")
            st.code(f"""\
# 1. Unzip
unzip selenium-tests.zip

# 2. Open in IntelliJ / Eclipse as a Maven project

# 3. Run all tests (target: {st.session_state.base_url})
mvn test

# 4. Run one test class
mvn -Dtest=WebAppTest test
""", language="bash")

else:
    st.info("👆 Enter a URL above and click **Start** to begin.")

st.caption("Internal Demo Only")
