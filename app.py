"""
AI Selenium Test Generator (Streamlit)
=======================================
Flow:
  1. Enter a URL + Groq API key -> Start
     -> crawls the site AND auto-extracts locators (no extra click needed)
  2. Tab 1: Generate Test Plan + Test Cases together (one button)
  3. Tab 2: Review the auto-extracted locators (CSS + XPath)
  4. Tab 3: "Advance" -> Generate Selenium Java code (Page Objects + TestNG test)
     -> uses only the locators that are actually relevant to the generated
        test cases, not the full raw crawl dump
  5. Tab 4: Download everything as a ready-to-run Maven project (.zip)

Requirements (pip install):
    streamlit requests beautifulsoup4 pandas groq

Run with:
    streamlit run selenium_test_generator.py
"""

import re
import io
import copy
import zipfile
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from groq import Groq

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
# NOTE: "llama-3.3-70b-versatile" was deprecated by Groq (June 2026).
# openai/gpt-oss-120b is Groq's official recommended replacement:
# same free tier, better/faster, large context window.
GROQ_MODEL = "openai/gpt-oss-120b"

st.set_page_config(page_title="Selenium Test Generator", page_icon="🤖", layout="wide")
st.title("🤖 AI Selenium Test Generator")
st.caption("Crawl a website → Test Plan & Test Cases → Locators → Selenium Java Code → Download")

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Setup")
    groq_api_key = st.text_input("Groq API Key", type="password", placeholder="gsk_...")
    max_pages = st.slider("Max Pages to Crawl", min_value=5, max_value=30, value=15, step=5)
    st.markdown("---")
    st.markdown("**How it works:**")
    st.markdown("1️⃣ Enter API key + URL, click Start")
    st.markdown("2️⃣ Tab 1 → Generate Test Plan + Test Cases")
    st.markdown("3️⃣ Tab 2 → Review Locators (auto-found)")
    st.markdown("4️⃣ Tab 3 → Generate Java Code (uses only test-relevant locators)")
    st.markdown("5️⃣ Tab 4 → Download the project")
    st.markdown("---")
    st.caption(f"Model: `{GROQ_MODEL}`")
    st.caption("Free Groq key: [console.groq.com](https://console.groq.com)")

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────
def default_state():
    """Return a FRESH dict of default session values every time it's called.
    (Previously a single module-level dict was reused, so every reset
    reassigned the *same* mutable {}/[] objects into session_state instead
    of fresh ones — a latent state-corruption bug if anything mutated
    those objects in place.)
    """
    return {
        "crawled_pages": {},   # {url: html}
        "base_url": "",
        "locators": [],        # full pool, auto-filled right after crawl
        "test_plan": "",
        "test_cases": "",
        "java_code": {},       # {filepath: code}
        "crawl_done": False,
    }


for key, value in default_state().items():
    st.session_state.setdefault(key, value)


# ─────────────────────────────────────────────────────────────
# GROQ CALL (single place all AI calls go through)
# ─────────────────────────────────────────────────────────────
def ask_ai(api_key, prompt, system_msg="You are a senior QA automation engineer."):
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        return resp.choices[0].message.content
    except Exception as e:
        msg = str(e).lower()
        if "invalid_api_key" in msg or "authentication" in msg:
            st.error("❌ Invalid Groq API key. Check the sidebar.")
        elif "rate_limit" in msg:
            st.error("⚠️ Groq rate limit hit. Wait a moment and try again.")
        elif "context_length" in msg or "token" in msg:
            st.error("⚠️ Too much content for the model. Try crawling fewer pages.")
        elif "decommissioned" in msg or "not found" in msg:
            st.error(f"⚠️ Model `{GROQ_MODEL}` isn't available on your account. "
                      f"Check https://console.groq.com/docs/models for current model IDs.")
        else:
            st.error(f"❌ Groq API error: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# CRAWLING
# ─────────────────────────────────────────────────────────────
def crawl_website(start_url, max_pages=15):
    """Simple breadth-first crawl, same-domain links only."""
    visited = {}
    to_visit = [start_url]
    base_domain = urlparse(start_url).netloc
    headers = {"User-Agent": "Mozilla/5.0"}
    progress = st.progress(0, text="Starting crawl...")

    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if "text/html" not in resp.headers.get("Content-Type", ""):
                continue
            visited[url] = resp.text
            progress.progress(len(visited) / max_pages, text=f"Crawling ({len(visited)}): {url}")

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
# NOTE: "label" was removed — labels aren't actionable Selenium targets and
# were just adding noise to an already-huge locator pool.
INTERACTIVE_TAGS = ["input", "button", "a", "select", "textarea"]
BARE_TAGS = set(INTERACTIVE_TAGS)

# Cap how many raw elements we even look at per tag/page. This alone cuts
# the raw pool size dramatically on large sites (was 20 per tag per page).
MAX_ELEMENTS_PER_TAG_PER_PAGE = 12


def _esc(val):
    """Escape single quotes so we never emit a broken CSS/XPath attribute
    selector like [aria-label='Don't show again'] (previously only XPath
    text values were escaped, not attribute values in either CSS or XPath).
    """
    return (val or "").replace("'", "\\'")


def build_locators(tag, elem):
    """Build a CSS selector and an XPath for one element, best identifier first."""
    eid = elem.get("id", "").strip()
    ename = elem.get("name", "").strip()
    eclasses = elem.get("class", [])
    eplace = elem.get("placeholder", "").strip()
    earia = elem.get("aria-label", "").strip()
    edata = elem.get("data-test", "").strip()
    etext = elem.get_text(strip=True)[:40]

    # CSS: id > data-test > name > aria-label > placeholder > class > bare tag
    if eid:
        css = f"#{eid}"
    elif edata:
        css = f"[data-test='{_esc(edata)}']"
    elif ename:
        css = f"{tag}[name='{_esc(ename)}']"
    elif earia:
        css = f"[aria-label='{_esc(earia)}']"
    elif eplace:
        css = f"{tag}[placeholder='{_esc(eplace)}']"
    elif eclasses:
        css = f"{tag}.{'.'.join(eclasses[:2])}"
    else:
        css = tag  # ambiguous, filtered out later

    # XPath: same priority, with visible text as a fallback for buttons/links
    if eid:
        xpath = f"//{tag}[@id='{_esc(eid)}']"
    elif edata:
        xpath = f"//{tag}[@data-test='{_esc(edata)}']"
    elif ename:
        xpath = f"//{tag}[@name='{_esc(ename)}']"
    elif earia:
        xpath = f"//{tag}[@aria-label='{_esc(earia)}']"
    elif eplace:
        xpath = f"//{tag}[@placeholder='{_esc(eplace)}']"
    elif etext and tag in ("button", "a"):
        safe = _esc(etext)[:30]
        xpath = f"//{tag}[normalize-space()='{safe}']"
    elif eclasses:
        xpath = f"//{tag}[contains(@class,'{_esc(eclasses[0])}')]"
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
            for elem in soup.find_all(tag)[:MAX_ELEMENTS_PER_TAG_PER_PAGE]:
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


def get_relevant_locators(all_locators, test_cases_text, max_per_page=8, max_total=40):
    """Narrow the huge raw locator pool down to the elements that are
    actually exercised by the generated test cases, so Java code generation
    (and the resulting Page Objects) stay focused instead of dumping every
    element found anywhere on the site.

    Scoring is intentionally simple/deterministic (no extra AI call):
      +2 if the element's page path is mentioned in the test case text
      +1 for each word in the element's label/placeholder/name that shows
         up in the test case text
      +1 baseline boost for form controls (input/select/textarea/button),
         since those are what test steps actually interact with far more
         often than plain nav links
    Falls back to the deduped pool (capped) if no test cases exist yet.
    """
    unique_pool = filter_unique_locators(all_locators)

    if not test_cases_text or not unique_pool:
        return unique_pool[:max_total]

    text_lower = test_cases_text.lower()
    scored = []
    for loc in unique_pool:
        label = (loc.get("Text / Label") or "").lower()
        page = (loc.get("Page") or "").lower()
        score = 0

        if page and page != "/" and page in text_lower:
            score += 2

        for word in re.findall(r"[a-zA-Z]{3,}", label):
            if word in text_lower:
                score += 1

        if loc["Tag"] in ("input", "select", "textarea", "button"):
            score += 1

        scored.append((score, loc))

    scored.sort(key=lambda x: x[0], reverse=True)

    result = []
    per_page_count = {}
    for score, loc in scored:
        p = loc["Page"]
        if per_page_count.get(p, 0) >= max_per_page:
            continue
        result.append(loc)
        per_page_count[p] = per_page_count.get(p, 0) + 1
        if len(result) >= max_total:
            break

    return result


# ─────────────────────────────────────────────────────────────
# PAGE SUMMARY (context fed to the AI)
# ─────────────────────────────────────────────────────────────
def build_page_summary(pages_dict, limit=12):
    summary = ""
    for url, html in list(pages_dict.items())[:limit]:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title else url
        summary += (
            f"\nURL: {url}\nTitle: {title}\n"
            f"Forms: {len(soup.find_all('form'))} | Buttons: {len(soup.find_all('button'))} | "
            f"Inputs: {len(soup.find_all('input'))} | Links: {len(soup.find_all('a', href=True))}\n"
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
def generate_plan_and_cases(api_key, pages_dict, locators):
    base_url = st.session_state.base_url or list(pages_dict.keys())[0]
    summary = build_page_summary(pages_dict)
    loc_info = "\n".join(
        f"  [{l['Tag']}] '{l['Text / Label']}' | CSS: {l['CSS Selector']} | Page: {l['Page']}"
        for l in locators[:30]
    ) or "No elements extracted yet — base cases on page structure above."

    plan_prompt = f"""
You are a senior QA engineer. Write a short, formal TEST PLAN for this website.
This is a high-level strategy document — do NOT write individual test cases here.

Website: {base_url}
Pages crawled: {len(pages_dict)}
Elements found: {len(locators)}

Pages:
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

Keep every section short and specific to this website.
"""
    plan = ask_ai(api_key, plan_prompt, "You are a senior QA engineer writing a formal test plan.")

    cases_prompt = f"""
You are a senior QA engineer. Write at least 12 structured test cases for this website.

Base URL: {base_url}
Pages:
{summary}
Interactive elements:
{loc_info}

Cover: Functional UI, Form Validation, Navigation/Links, and Login/Auth (if a login form exists).

FORMATTING RULES (follow exactly):
- Each field on its own line, one blank line between fields
- Put --- on its own line before each test case
- "Page" must use the full URL, based on {base_url}
- "Prerequisites" is a bullet list

Format each test case EXACTLY like this:

---

**TC_ID:** TC_001

**Summary:** one sentence

**Page:** {base_url}/page-path

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
                    "never merge two fields onto one line.")
    return plan, cases


# ─────────────────────────────────────────────────────────────
# STEP 2: SELENIUM JAVA CODE ("Advance" step)
# ─────────────────────────────────────────────────────────────
def generate_java_code(api_key, locators, pages_dict):
    if not pages_dict:
        st.error("❌ No crawled pages. Cannot generate Java code.")
        return {}
    if not locators:
        st.error("❌ No relevant locators to build code from.")
        return {}

    base_url = st.session_state.base_url or list(pages_dict.keys())[0].rstrip("/")
    java_files = {}

    # Group locators by page, and remember each page's real crawled URL
    pages, page_url_lookup = {}, {}
    for loc in locators:
        page_key = loc["Page"].strip("/").replace("/", "_").replace("-", "_") or "home"
        page_url_lookup.setdefault(page_key, loc.get("_url", base_url))
        pages.setdefault(page_key, []).append(loc)

    # ---- Page Object classes (max 4 pages) ----
    for page_name, page_locs in list(pages.items())[:4]:
        class_name = "".join(w.capitalize() for w in re.split(r"[_\-\s]+", page_name) if w) + "Page"
        full_page_url = page_url_lookup.get(page_name, base_url)
        clean_locs = filter_unique_locators(page_locs)[:12]
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
        code = ask_ai(api_key, prompt, "You are a Selenium Java expert. Return only clean Java code.")
        if code:
            java_files[f"src/main/java/pages/{class_name}.java"] = re.sub(r"```(?:java)?|```", "", code).strip()

    # ---- TestNG test class ----
    page_classes = [
        "".join(w.capitalize() for w in re.split(r"[_\-\s]+", p) if w) + "Page"
        for p in list(pages.keys())[:4]
    ]
    sample = "\n".join(
        f'  [{l["Tag"]}] "{l["Text / Label"]}" CSS: {l["CSS Selector"]} | XPath: {l["XPath"]}'
        for l in filter_unique_locators(locators)[:20]
    )

    test_prompt = f"""
Generate a complete Selenium Java TestNG test class named WebAppTest.

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
- Exactly 6 @Test methods: testPageTitle, testNavigationLinks (XPath text locators only,
  never By.cssSelector("a")), testFormInputs, testButtonClicks (XPath text locators only),
  testDataDriven (with @DataProvider "loginData", 2 rows of sample email/password),
  testElementsVisible (isDisplayed() checks)
- Every driver.get() must use BASE_URL or a subpath, never a fake URL
- Return ONLY Java code, no markdown fences, no explanation
"""
    test_code = ask_ai(api_key, test_prompt,
                        "You are a Selenium TestNG expert. Return only clean Java code. "
                        "Never use bare CSS selectors like By.cssSelector('a').")
    if test_code:
        java_files["src/test/java/tests/WebAppTest.java"] = re.sub(r"```(?:java)?|```", "", test_code).strip()

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
  <dependencies>
    <dependency>
      <groupId>org.seleniumhq.selenium</groupId>
      <artifactId>selenium-java</artifactId>
      <version>4.21.0</version>
    </dependency>
    <dependency>
      <groupId>org.testng</groupId>
      <artifactId>testng</artifactId>
      <version>7.9.0</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>io.github.bonigarcia</groupId>
      <artifactId>webdrivermanager</artifactId>
      <version>5.8.0</version>
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
        st.error("❌ Please enter your Groq API key in the sidebar.")
        st.stop()
    if not url_input.startswith("http"):
        st.error("❌ Please enter a valid URL starting with http:// or https://")
        st.stop()

    for k, v in default_state().items():
        st.session_state[k] = v
    st.session_state.base_url = url_input.rstrip("/")

    with st.spinner("🔍 Crawling website..."):
        st.session_state.crawled_pages = crawl_website(url_input, max_pages=max_pages)

    with st.spinner("🎯 Auto-extracting locators..."):
        st.session_state.locators = extract_locators(st.session_state.crawled_pages)

    st.session_state.crawl_done = True
    st.success(
        f"✅ Crawled **{len(st.session_state.crawled_pages)} pages** and found "
        f"**{len(st.session_state.locators)} elements**. Continue in the tabs below."
    )

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
            st.markdown("#### Test Cases")
            st.markdown(fix_testcase_formatting(st.session_state.test_cases))

            if st.button("🔄 Regenerate"):
                st.session_state.test_plan = ""
                st.session_state.test_cases = ""
                st.session_state.java_code = {}  # stale code was built from old test cases
                st.rerun()
        else:
            st.info("One click generates both the Test Plan and the individual Test Cases.")
            if st.button("📋 Generate Test Plan & Test Cases", type="primary"):
                with st.spinner("AI is writing the test plan and test cases..."):
                    plan, cases = generate_plan_and_cases(
                        groq_api_key, st.session_state.crawled_pages, st.session_state.locators
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
                "⚠️ No locators found. The site may render via JavaScript. "
                "Try **https://the-internet.herokuapp.com** or **https://automationbookstore.dev**."
            )
        else:
            clean_count = len(filter_unique_locators(locs))
            st.success(f"✅ Found **{len(locs)} elements** in the raw crawl.")

            if st.session_state.test_cases:
                relevant = get_relevant_locators(locs, st.session_state.test_cases)
                st.info(
                    f"🎯 Based on your generated test cases, **{len(relevant)} elements** "
                    f"are actually relevant and will be used for Java code generation "
                    f"(down from {clean_count} unique / {len(locs)} raw)."
                )
                show_relevant_only = st.toggle("Show only test-relevant elements", value=True)
            else:
                show_relevant_only = False
                st.caption(
                    "ℹ️ Generate Test Plan & Test Cases in Tab 1 first — this list will then "
                    "narrow down to only the elements your test cases actually use."
                )

            display_locs = (
                get_relevant_locators(locs, st.session_state.test_cases)
                if show_relevant_only else locs
            )

            df = pd.DataFrame(display_locs)
            pages_opt = ["All Pages"] + sorted(df["Page"].unique().tolist())
            sel = st.selectbox("Filter by Page", pages_opt)
            filtered = df if sel == "All Pages" else df[df["Page"] == sel]
            st.dataframe(
                filtered[["Page", "Tag", "Type", "Text / Label", "CSS Selector", "XPath"]],
                use_container_width=True, height=400,
            )
            st.caption(f"Showing {len(filtered)} of {len(display_locs)} elements")

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

        elif not st.session_state.test_cases:
            st.warning(
                "⚠️ Generate the Test Plan & Test Cases in Tab 1 first — Java code is generated "
                "only from the elements your test cases actually reference."
            )

        elif not st.session_state.java_code:
            relevant_locators = get_relevant_locators(
                st.session_state.locators, st.session_state.test_cases
            )
            st.info(
                f"Ready to generate from **{len(relevant_locators)}** test-relevant elements "
                f"(filtered from {len(st.session_state.locators)} raw elements found on the site)."
            )
            if st.button("⚙️ Advance: Generate Java Code", type="primary"):
                with st.spinner("AI is generating Selenium Java code... (~30s)"):
                    result = generate_java_code(
                        groq_api_key, relevant_locators, st.session_state.crawled_pages
                    )
                    if result:
                        st.session_state.java_code = result
                st.rerun()

        else:
            # Code already generated -> show regenerate option + download section together
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
