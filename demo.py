"""
AI Selenium Test Generator
===========================
A Streamlit tool for the QA team that:

  Step 1 - Crawls a website (SSRF-protected) and extracts UI locators (CSS/XPath)
  Step 2 - Uses an LLM (Groq) to draft a simple test plan (test name + steps)
  Step 3 - Generates ready-to-run Selenium test code in Python (unittest)
  Step 4 - [Advanced] Generates ready-to-run Selenium test code in Java (TestNG)

Setup
-----
Create a file at `.streamlit/secrets.toml` next to this app (this file is
gitignored, so the real password/key never lives in source control):

    [auth]
    password = "your-strong-password-here"

    [groq]
    api_key = "your-groq-api-key-here"

Then install dependencies and run:

    pip install -r requirements.txt
    streamlit run app.py

If `[groq] api_key` is not set in secrets, the app falls back to asking for
the key in the sidebar for that session only.
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

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
# Both models below are CURRENT production models on Groq (verified against
# Groq's docs). The old free models this app used to default to
# (llama-3.1-8b-instant / llama-3.3-70b-versatile) were deprecated by Groq in
# June 2026 - Groq's own migration guide points people to these two instead.
MODEL_OPTIONS = {
    "openai/gpt-oss-120b (best quality)": "openai/gpt-oss-120b",
    "openai/gpt-oss-20b (fastest)": "openai/gpt-oss-20b",
}
MAX_LOGIN_ATTEMPTS = 5
REQUEST_TIMEOUT = 10
CRAWL_DELAY_SECONDS = 0.5

st.set_page_config(page_title="AI Test Generator", page_icon="🔒", layout="wide")

# ---------------------------------------------------------------------------
# AUTH
# Reads the password from st.secrets so it never lives in source control.
# ---------------------------------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "login_attempts" not in st.session_state:
    st.session_state.login_attempts = 0


def get_configured_password():
    """
    Preferred format (see secrets.toml.example):
        [auth]
        password = "..."
    Also accepts a flat `password = "..."` at the top level, in case it was
    pasted into Streamlit Cloud's Secrets box without the [auth] section.
    """
    try:
        return st.secrets["auth"]["password"]
    except Exception:
        pass
    try:
        return st.secrets["password"]
    except Exception:
        return None


def get_secret_groq_key():
    """
    Preferred format (see secrets.toml.example):
        [groq]
        api_key = "..."
    Also accepts a few common flat key names, in case it was pasted into
    Streamlit Cloud's Secrets box without the [groq] section.
    """
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
            "`.streamlit/secrets.toml` before running this app."
        )
        st.stop()

    if st.session_state.login_attempts >= MAX_LOGIN_ATTEMPTS:
        st.error("Too many failed attempts. Restart the app to try again.")
        st.stop()

    pw = st.text_input("Enter Password", type="password")
    if st.button("Unlock"):
        # hmac.compare_digest avoids leaking timing information about the
        # password, unlike a plain == comparison.
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
st.caption("Crawl a site → AI test plan → Python (Selenium) code → Java (TestNG) code")

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Settings")

    model_label = st.selectbox("AI Model", list(MODEL_OPTIONS.keys()))
    selected_model = MODEL_OPTIONS[model_label]

    max_pages = st.slider("Max Pages to Crawl", 5, 12, 8)

    secret_groq_key = get_secret_groq_key()
    if secret_groq_key:
        groq_api_key = secret_groq_key
        st.caption("✅ Groq API key loaded from secrets.toml")
    else:
        groq_api_key = st.text_input(
            "Groq API Key",
            type="password",
            help="Tip: add [groq] api_key = '...' to .streamlit/secrets.toml "
                 "so your team doesn't have to paste this every time.",
        )

    st.divider()
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
            current_url = urljoin(current_url, resp.headers.get("Location", ""))
            continue
        return resp
    raise ValueError("Too many redirects")

# ---------------------------------------------------------------------------
# AI HELPERS
# ---------------------------------------------------------------------------
def ask_ai(prompt, api_key, model):
    if not api_key:
        return None, "No Groq API key provided."
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        return resp.choices[0].message.content, None
    except Exception as e:
        return None, f"AI call failed: {e}"


def clean_code_fences(text):
    """
    Strips a leading/trailing ``` fence (with or without a language tag)
    around a code block. More robust than a naive str.split("```") because
    it only strips the *outer* fence instead of splitting on every backtick
    occurrence in the text.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # drop opening fence (and any language tag on it)
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def locator_lines_for_prompt(locators):
    return "\n".join(
        f"- {loc['Element']} | type: {loc['Type']} | selector: {loc['Selector']} | text: {loc['Text']}"
        for loc in locators
    )


def build_test_plan_prompt(page, locators):
    return (
        "You are a QA engineer writing a manual test plan for a web page, "
        "before any code is written.\n\n"
        f"Page: {page}\n\n"
        f"Available UI elements on this page:\n{locator_lines_for_prompt(locators)}\n\n"
        "Write the test plan as a checklist. For EACH test case use exactly "
        "this format:\n\n"
        "Test Name: <short descriptive name>\n"
        "Steps:\n"
        "1. <step>\n"
        "2. <step>\n"
        "Expected Result: <what should happen>\n\n"
        "Rules:\n"
        "- Only reference elements listed above. Do not invent elements or fields.\n"
        "- Include the obvious positive-path test(s), plus at least one "
        "negative/validation test if the page has form inputs.\n"
        "- Keep it practical: 3 to 6 test cases is usually enough for one page.\n"
        "- Return only the checklist, no extra commentary or headers."
    )


def build_python_test_prompt(page, locators, test_plan=None):
    plan_section = f"\n\nImplement exactly this approved test plan:\n{test_plan}\n" if test_plan else ""
    return (
        "Generate a Python Selenium test script using unittest and "
        "selenium.webdriver. Use explicit waits (WebDriverWait + "
        "expected_conditions) - never time.sleep. Use webdriver_manager "
        "(ChromeDriverManager) to set up the driver so no manual driver "
        "path is needed. For each element below, `type` tells you which "
        "locator strategy to use: CSS -> By.CSS_SELECTOR, XPath -> By.XPATH. "
        "Only use the locators listed - do not invent elements.\n\n"
        f"Page: {page}\n\nElements:\n{locator_lines_for_prompt(locators)}"
        f"{plan_section}\n"
        "Return only the Python code, no explanation."
    )


def build_java_test_prompt(page, locators, class_name, test_plan=None):
    plan_section = f"\n\nImplement exactly this approved test plan:\n{test_plan}\n" if test_plan else ""
    return (
        "Generate a Selenium WebDriver test class in Java using TestNG for "
        "the page below.\n\n"
        "Requirements:\n"
        "- Package: com.testautomation.tests\n"
        f"- The public class name MUST be exactly: {class_name}\n"
        "- Use WebDriverManager (io.github.bonigarcia.wdm.WebDriverManager) "
        "to set up ChromeDriver - do not hardcode a driver path\n"
        "- Use WebDriverWait with ExpectedConditions for explicit waits - "
        "never Thread.sleep\n"
        "- Use @BeforeClass to start the driver and @AfterClass to quit it\n"
        "- One @Test method per test case\n"
        "- For each element below, `type` tells you which locator strategy "
        "to use: CSS -> By.cssSelector(...), XPath -> By.xpath(...)\n"
        "- Only use the locators listed - do not invent elements\n"
        "- Add short comments explaining each test\n\n"
        f"Page: {page}\n\nElements:\n{locator_lines_for_prompt(locators)}"
        f"{plan_section}\n"
        "Return only the Java code, no explanation."
    )


def sanitize_page_name(page):
    name = page.strip("/").replace("/", "_").replace("-", "_")
    name = re.sub(r"[^0-9a-zA-Z_]", "", name)
    return name or "home"


def to_pascal_case(name):
    parts = re.split(r"[_\s]+", name)
    return "".join(p.capitalize() for p in parts if p) or "Home"


def java_class_name_for_page(page):
    return "Test" + to_pascal_case(sanitize_page_name(page))


def ensure_java_class_name(code, class_name):
    """
    Defensive fix: force the class name in the generated code to exactly
    match the intended class/file name, even if the AI output drifted.
    Java requires the public class name to match the file name exactly,
    otherwise the generated project won't compile.
    """
    fixed, count = re.subn(r"public\s+class\s+\w+", f"public class {class_name}", code, count=1)
    return fixed if count else code


POM_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>com.testautomation</groupId>
    <artifactId>selenium-tests</artifactId>
    <version>1.0.0</version>
    <packaging>jar</packaging>

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
            </plugin>
        </plugins>
    </build>
</project>
"""

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
            status_placeholder.text(f"Fetching ({len(visited) + 1}/{max_pages}): {current}")

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
                if urlparse(link).netloc == base and link not in visited and link not in to_visit:
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
#
# Priority per element (most stable/intentional first):
#   1. data-test / data-testid attribute  -> built specifically for testing
#   2. id                                  -> usually unique on the page
#   3. name                                -> common on form fields
#   4. placeholder                         -> common on inputs
#   5. aria-label                          -> common on buttons/icons
#   6. visible text (buttons/links only)   -> XPath fallback
# CSS is used whenever possible; XPath is only used as the last resort for
# text-only buttons/links, since CSS can't select by visible text.
# ---------------------------------------------------------------------------
def extract_locators(pages):
    locators = []
    seen = set()
    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        page = urlparse(url).path or "/"
        for tag in ["input", "button", "a", "select", "textarea"]:
            for el in soup.find_all(tag):
                if el.get("type") == "hidden":
                    continue

                loc_type, selector = None, None

                if el.get("data-test"):
                    loc_type, selector = "CSS", f"[data-test='{el['data-test']}']"
                elif el.get("data-testid"):
                    loc_type, selector = "CSS", f"[data-testid='{el['data-testid']}']"
                elif el.get("id"):
                    loc_type, selector = "CSS", f"#{el['id']}"
                elif el.get("name"):
                    loc_type, selector = "CSS", f"{tag}[name='{el['name']}']"
                elif el.get("placeholder"):
                    loc_type, selector = "CSS", f"{tag}[placeholder='{el['placeholder']}']"
                elif el.get("aria-label"):
                    loc_type, selector = "CSS", f"{tag}[aria-label='{el['aria-label']}']"
                else:
                    text = el.get_text(strip=True)
                    if text and len(text) < 40 and tag in ("button", "a"):
                        # normalize-space(.) (not text()) also matches text
                        # inside nested tags, e.g. <button><span>Submit</span></button>
                        safe_text = text.replace("'", "\\'")
                        loc_type, selector = "XPath", f"//{tag}[normalize-space(.)='{safe_text}']"
                    else:
                        continue

                key = f"{page}|{selector}"
                if key in seen:
                    continue
                seen.add(key)
                locators.append({
                    "Page": page,
                    "Element": tag,
                    "Type": loc_type,
                    "Selector": selector,
                    "Text": el.get_text(strip=True)[:50] or "-",
                })
    return locators

# ---------------------------------------------------------------------------
# SESSION STATE DEFAULTS
# ---------------------------------------------------------------------------
for key, default in {
    "locators": [],
    "pages_crawled": [],
    "test_plans": {},
    "generated_python": {},
    "generated_java": {},
    "java_class_names": {},
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# STEP 1: CRAWL & EXTRACT LOCATORS
# ---------------------------------------------------------------------------
st.header("Step 1 · Crawl & extract locators")
url = st.text_input("Website URL", "https://automationexercise.com")

if st.button("🚀 Crawl & Extract Locators"):
    status = st.empty()
    pages = crawl_website(url, max_pages, status_placeholder=status)
    status.empty()
    locs = extract_locators(pages)
    st.session_state.locators = locs
    st.session_state.pages_crawled = list(pages.keys())
    # Starting a new crawl invalidates anything generated for the old site.
    st.session_state.test_plans = {}
    st.session_state.generated_python = {}
    st.session_state.generated_java = {}
    st.session_state.java_class_names = {}
    if locs:
        st.success(f"✅ Extracted **{len(locs)}** locators from {len(pages)} page(s)")
    else:
        st.warning("No locators found. The site may block automated requests, "
                    "or no pages were reachable.")

if st.session_state.locators:
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
    pages_available = sorted(set(df["Page"]))
    selected_pages = st.multiselect(
        "Pages to work with (used in Steps 2-4 below)",
        pages_available,
        default=pages_available[:1],
    )

    # -----------------------------------------------------------------
    # STEP 2: AI TEST PLAN
    # -----------------------------------------------------------------
    st.header("Step 2 · Generate a test plan")
    st.caption("A plain checklist of test cases (name + steps), grounded only in the locators found above.")

    if st.button("📝 Generate Test Plan"):
        if not groq_api_key:
            st.error("Enter a Groq API key in the sidebar first.")
        elif not selected_pages:
            st.error("Select at least one page above.")
        else:
            progress = st.progress(0.0)
            for i, page in enumerate(selected_pages):
                page_locators = [l for l in st.session_state.locators if l["Page"] == page]
                prompt = build_test_plan_prompt(page, page_locators)
                plan, err = ask_ai(prompt, groq_api_key, selected_model)
                if err:
                    st.error(f"{page}: {err}")
                else:
                    st.session_state.test_plans[page] = plan.strip()
                progress.progress((i + 1) / len(selected_pages))
            progress.empty()
            if st.session_state.test_plans:
                st.success(f"Generated a test plan for {len(st.session_state.test_plans)} page(s).")

    if st.session_state.test_plans:
        for page, plan in st.session_state.test_plans.items():
            with st.expander(f"Test plan: {page}"):
                st.markdown(plan)

        combined_plan = "\n\n".join(
            f"# {page}\n\n{plan}" for page, plan in st.session_state.test_plans.items()
        )
        st.download_button(
            "⬇️ Download test plan (Markdown)",
            data=combined_plan.encode("utf-8"),
            file_name="test_plan.md",
            mime="text/markdown",
        )

    # -----------------------------------------------------------------
    # STEP 3: PYTHON SELENIUM CODE
    # -----------------------------------------------------------------
    st.header("Step 3 · Generate Python Selenium code")
    if not st.session_state.test_plans:
        st.caption("Tip: generate a test plan in Step 2 first for better results. "
                    "You can still generate code directly from locators without one.")

    if st.button("🐍 Generate Python (Selenium) Tests"):
        if not groq_api_key:
            st.error("Enter a Groq API key in the sidebar first.")
        elif not selected_pages:
            st.error("Select at least one page above.")
        else:
            progress = st.progress(0.0)
            for i, page in enumerate(selected_pages):
                page_locators = [l for l in st.session_state.locators if l["Page"] == page]
                plan = st.session_state.test_plans.get(page)
                prompt = build_python_test_prompt(page, page_locators, plan)
                code, err = ask_ai(prompt, groq_api_key, selected_model)
                if err:
                    st.error(f"{page}: {err}")
                else:
                    st.session_state.generated_python[page] = clean_code_fences(code)
                progress.progress((i + 1) / len(selected_pages))
            progress.empty()
            if st.session_state.generated_python:
                st.success(f"Generated Python tests for {len(st.session_state.generated_python)} page(s).")

    if st.session_state.generated_python:
        for page, code in st.session_state.generated_python.items():
            filename = f"test_{sanitize_page_name(page)}.py"
            with st.expander(filename):
                st.code(code, language="python")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for page, code in st.session_state.generated_python.items():
                zf.writestr(f"test_{sanitize_page_name(page)}.py", code)
        zip_buffer.seek(0)
        st.download_button(
            "⬇️ Download all Python tests as .zip",
            data=zip_buffer,
            file_name="selenium_python_tests.zip",
            mime="application/zip",
        )

    # -----------------------------------------------------------------
    # STEP 4 (ADVANCED): JAVA SELENIUM (TESTNG) CODE
    # -----------------------------------------------------------------
    with st.expander("⚙️ Step 4 (Advanced) · Generate Java Selenium (TestNG) code", expanded=False):
        st.caption("Produces a ready-to-run Maven project: pom.xml + one TestNG test class per page.")

        if st.button("☕ Generate Java (TestNG) Tests"):
            if not groq_api_key:
                st.error("Enter a Groq API key in the sidebar first.")
            elif not selected_pages:
                st.error("Select at least one page above.")
            else:
                progress = st.progress(0.0)
                for i, page in enumerate(selected_pages):
                    page_locators = [l for l in st.session_state.locators if l["Page"] == page]
                    plan = st.session_state.test_plans.get(page)
                    class_name = java_class_name_for_page(page)
                    prompt = build_java_test_prompt(page, page_locators, class_name, plan)
                    code, err = ask_ai(prompt, groq_api_key, selected_model)
                    if err:
                        st.error(f"{page}: {err}")
                    else:
                        clean_code = clean_code_fences(code)
                        clean_code = ensure_java_class_name(clean_code, class_name)
                        st.session_state.generated_java[page] = clean_code
                        st.session_state.java_class_names[page] = class_name
                    progress.progress((i + 1) / len(selected_pages))
                progress.empty()
                if st.session_state.generated_java:
                    st.success(f"Generated Java tests for {len(st.session_state.generated_java)} page(s).")

        if st.session_state.generated_java:
            for page, code in st.session_state.generated_java.items():
                class_name = st.session_state.java_class_names[page]
                with st.expander(f"{class_name}.java"):
                    st.code(code, language="java")

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("pom.xml", POM_XML_TEMPLATE)
                for page, code in st.session_state.generated_java.items():
                    class_name = st.session_state.java_class_names[page]
                    path = f"src/test/java/com/testautomation/tests/{class_name}.java"
                    zf.writestr(path, code)
            zip_buffer.seek(0)
            st.download_button(
                "⬇️ Download Maven project as .zip",
                data=zip_buffer,
                file_name="selenium_java_tests.zip",
                mime="application/zip",
                help="Unzip, then run `mvn test` inside the folder.",
            )

if not st.session_state.locators:
    st.info("Click the button above to start")

st.caption("Internal Demo Only")
