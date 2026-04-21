#!/usr/bin/env python3
"""
LMS QA Checker
Traverses all activities in a course via the Next button,
checks every component type, and outputs a per-course CSV report.

Usage:
    python lms_qa_checker.py --course-id <ID> [--course-name "My Course"]

Arguments can also be supplied via environment variables:
    COURSE_ID, COURSE_NAME, LMS_USERNAME, LMS_PASSWORD
"""

import argparse
import csv
import os
import re
import sys
import time
import json
import requests
from datetime import date
from urllib.parse import urlparse, urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

# ── Fixed config ───────────────────────────────────────────────────────────────
# All values must be supplied via environment variables or CLI flags.
# Copy .env.example to .env and fill in your details before running locally.
LMS_BASE        = os.environ.get("LMS_BASE", "").rstrip("/")
LOGIN_URL       = f"{LMS_BASE}/login"
REQUEST_TIMEOUT = 10        # seconds for external link checks
PAGE_TIMEOUT    = 45_000    # ms for Playwright page loads
DATE_CHECKED    = str(date.today())

# Credentials — must be supplied via environment variables or GitHub Secrets.
# See .env.example for required variable names.
USERNAME = os.environ.get("LMS_USERNAME", "")
PASSWORD = os.environ.get("LMS_PASSWORD", "")

# ── Runtime config (set by main() after arg parsing) ──────────────────────────
COURSE_ID   = ""
COURSE_NAME = ""
MODULE_NAME = ""
OUTPUT_CSV  = "qa_report.csv"

VIMEO_OEMBED    = "https://vimeo.com/api/oembed.json"

DOCUMENT_EXTS   = {".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"}
DOCUMENT_MIMES  = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

FIELDNAMES = [
    "date_checked", "module_name", "activity_id", "activity_title",
    "component_type", "label", "url", "status_code", "result", "ai_summary", "notes",
]

# ── CSV helper ─────────────────────────────────────────────────────────────────
def row(activity_id, activity_title, component_type, label, url, status_code, result, ai_summary, notes):
    return {
        "date_checked":   DATE_CHECKED,
        "module_name":    MODULE_NAME,
        "activity_id":    activity_id,
        "activity_title": activity_title,
        "component_type": component_type,
        "label":          label,
        "url":            url,
        "status_code":    status_code,
        "result":         result,
        "ai_summary":     ai_summary,
        "notes":          notes,
    }

# ── Network checks ─────────────────────────────────────────────────────────────
def check_vimeo(video_id):
    try:
        resp = requests.get(VIMEO_OEMBED,
                            params={"url": f"https://vimeo.com/{video_id}"},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return 200, "PASS", f"Vimeo OK: {resp.json().get('title','')}"
        return resp.status_code, "FAIL", f"Vimeo {resp.status_code}: unavailable/private"
    except requests.exceptions.Timeout:
        return None, "FAIL", "Vimeo oEmbed timed out"
    except Exception as e:
        return None, "FAIL", f"Vimeo error: {e}"

def check_document(url, session=None):
    req = session or requests
    try:
        resp = req.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 LMS-QA-Bot"})
        if resp.status_code == 405:
            resp = req.get(url, timeout=REQUEST_TIMEOUT, stream=True,
                           headers={"User-Agent": "Mozilla/5.0 LMS-QA-Bot"})
        ct  = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        ext = "." + url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
        if resp.status_code == 200:
            ok = ct in DOCUMENT_MIMES or ext in DOCUMENT_EXTS
            return resp.status_code, "PASS" if ok else "WARNING", \
                   f"{'OK' if ok else 'Unexpected content-type'}: {ct or ext}"
        return resp.status_code, "FAIL", f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return None, "FAIL", "Request timed out"
    except requests.exceptions.SSLError as e:
        return None, "FAIL", f"SSL error: {e}"
    except Exception as e:
        return None, "FAIL", f"Request error: {e}"

def check_url(url, session=None, follow_redirects=False):
    """Generic URL reachability check. Returns (status_code, result, notes)."""
    req = session or requests
    try:
        resp = req.get(url, timeout=REQUEST_TIMEOUT,
                       allow_redirects=follow_redirects,
                       headers={"User-Agent": "Mozilla/5.0 LMS-QA-Bot"})
        if follow_redirects:
            if 200 <= resp.status_code < 300:
                return resp.status_code, "PASS", "OK"
            return resp.status_code, "FAIL", f"HTTP {resp.status_code}"
        else:
            if resp.status_code in (301, 302, 303, 307, 308):
                dest = resp.headers.get("Location", "unknown")
                return resp.status_code, "WARNING", f"Redirects to: {dest}"
            if 200 <= resp.status_code < 300:
                return resp.status_code, "PASS", "OK"
            return resp.status_code, "FAIL", f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return None, "FAIL", "Request timed out (10s)"
    except requests.exceptions.SSLError as e:
        return None, "FAIL", f"SSL error: {e}"
    except Exception as e:
        return None, "FAIL", f"Request error: {e}"

# ── Page helpers ───────────────────────────────────────────────────────────────
def wait_for_activity(page, timeout=25_000):
    """Wait until the activity viewer has rendered its content."""
    try:
        page.wait_for_selector(
            '[class*="Viewer"], [class*="viewer"], [class*="nextPrev"], [class*="prevNext"]',
            timeout=timeout
        )
        time.sleep(1.5)
        return True
    except PlaywrightTimeout:
        return False

def detect_ai_summary(page):
    """
    Check for an AI Summary block below the main content.
    Returns PRESENT / MISSING / EMPTY.
    """
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.8)

        result = page.evaluate("""
            () => {
                // Specific class-based selectors
                const specific = [
                    '[class*="ai-summary"]', '[class*="AiSummary"]', '[class*="ai_summary"]',
                    '[id*="ai-summary"]', '[data-testid*="ai-summary"]',
                    '[class*="SummaryCard"]', '[class*="summary-card"]', '[class*="SummaryBlock"]',
                ];
                for (const sel of specific) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const txt = el.innerText.trim();
                        return txt ? 'PRESENT' : 'EMPTY';
                    }
                }

                // Heading-based: find h* with exact text "AI Summary" or "Summary"
                // then check next sibling content
                const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
                for (const h of headings) {
                    const txt = h.innerText.trim().toLowerCase();
                    if (txt === 'ai summary' || txt === 'summary' || txt === 'ai-generated summary') {
                        // Get parent and check for content after the heading
                        const parent = h.parentElement;
                        const parentText = parent ? parent.innerText.replace(h.innerText, '').trim() : '';
                        return parentText.length > 10 ? 'PRESENT' : 'EMPTY';
                    }
                }

                // Strong/bold labeled section
                const bolds = Array.from(document.querySelectorAll('strong, b, [class*="label"], [class*="Label"]'));
                for (const b of bolds) {
                    const txt = b.innerText.trim().toLowerCase();
                    if (txt === 'ai summary' || txt === 'summary') {
                        const next = b.nextElementSibling || b.parentElement?.nextElementSibling;
                        if (next) {
                            const content = next.innerText.trim();
                            return content.length > 10 ? 'PRESENT' : 'EMPTY';
                        }
                    }
                }

                return 'MISSING';
            }
        """)
        return result
    except Exception:
        return "MISSING"

def get_activity_title(page):
    """Extract the primary title of the current activity."""
    try:
        # Look inside the Viewer/content area specifically
        title = page.evaluate("""
            () => {
                // Try content area headings first
                const contentSelectors = [
                    '[class*="Viewer"] h1', '[class*="Viewer"] h2',
                    '[class*="content"] h1', '[class*="content"] h2',
                    'main h1', 'main h2', 'article h1', 'article h2',
                ];
                const skip = new Set(['Home', 'Campus', 'Inbox', 'Notifications', 'Raise a Ticket']);
                for (const sel of contentSelectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const t = el.innerText.trim();
                        if (t && t.length > 3 && !skip.has(t)) return t;
                    }
                }
                // Fallback: breadcrumb last item
                const crumbs = document.querySelectorAll('[class*="breadcrumb"] a, [class*="Breadcrumb"] a');
                if (crumbs.length > 0) return crumbs[crumbs.length-1].innerText.trim();
                return '';
            }
        """)
        return title.strip()[:200] if title else ""
    except Exception:
        return ""

def get_activity_id_from_url(url):
    m = re.search(r'activityId=([A-Za-z0-9+/=%]+)', url)
    if not m:
        return ""
    aid = m.group(1)
    # URL-decode if needed
    return requests.utils.unquote(aid)

def classify_activity(title, body_text, html):
    """Classify component type from title and page content."""
    title_lower = body_text_lower = html_lower = ""
    try:
        title_lower     = title.lower()
        body_text_lower = body_text.lower()
        html_lower      = html.lower()
    except Exception:
        pass

    # Video: Vimeo iframe present
    if re.search(r'player\.vimeo\.com/video/\d+', html):
        return "Video"

    # Live Class: expired class or live session link
    if re.search(r'class expired|class\s+is\s+live|live\s+class', body_text_lower):
        return "Live Class"
    if re.search(r'zoom\.us/j/|meet\.google\.com/|teams\.microsoft\.com/l/meetup', html_lower):
        return "Live Class"

    # Recording
    if re.search(r'recording|recorded\s+session|class\s+recording', title_lower):
        return "Recording"

    # Knowledge Check
    if re.search(r'knowledge\s+check|quiz|test\s+your\s+knowledge|check\s+your\s+understanding', title_lower):
        return "Knowledge Check"

    # Assignment (graded)
    if re.search(r'\bgraded\s+assignment\b|\bassignment\b', title_lower) and \
       re.search(r'submit|grade|rubric', body_text_lower):
        return "Assignment"

    # Ungraded Assignment
    if re.search(r'ungraded|practice\s+assignment', title_lower):
        return "Ungraded Assignment"

    # Course Project
    if re.search(r'\bproject\b|\bcapstone\b', title_lower) and \
       re.search(r'submit|deliverable', body_text_lower):
        return "Course Project"

    # PDF/Document embedded
    if re.search(r'\.pdf|\.docx|\.pptx|\.xlsx', html_lower):
        return "PDF"

    # Generic content/reading
    return "Content"

def extract_vimeo_ids(html):
    return list(set(re.findall(r'player\.vimeo\.com/video/(\d+)', html)))

_CONTENT_SCOPE_JS = """
    () => {
        // Elements that are always out of scope regardless of position
        const EXCLUDE_SELS = [
            'header', 'footer',
            '[class*="Header"]', '[class*="header"]',
            '[class*="Footer"]', '[class*="footer"]',
            '[class*="Sidebar"]', '[class*="sidebar"]',
            '[class*="SideBar"]', '[class*="SideNav"]', '[class*="sidenav"]',
            '[class*="Drawer"]', '[class*="drawer"]',
            '[class*="TopNav"]', '[class*="topNav"]',
            '[class*="NavBar"]',  '[class*="navbar"]',
        ];

        // Build a set of all excluded elements (and their descendants)
        const excluded = new Set();
        for (const sel of EXCLUDE_SELS) {
            for (const el of document.querySelectorAll(sel)) {
                excluded.add(el);
                for (const child of el.querySelectorAll('*')) excluded.add(child);
            }
        }

        const isExcluded = (el) => {
            let node = el;
            while (node) { if (excluded.has(node)) return true; node = node.parentElement; }
            return false;
        };

        // Find the vertical content column: main > [role=main] > body fallback
        const wrapperSels = [
            'main', '[role="main"]',
            '[class*="PageContent"]', '[class*="pageContent"]',
            '[class*="ContentArea"]',  '[class*="contentArea"]',
            '[class*="MainContent"]',  '[class*="mainContent"]',
        ];
        let wrapper = null;
        for (const sel of wrapperSels) {
            const el = document.querySelector(sel);
            if (el && !isExcluded(el)) { wrapper = el; break; }
        }
        if (!wrapper) wrapper = document.body;

        // Collect HTML and text from wrapper, skipping excluded sub-trees
        // Use a TreeWalker over the wrapper to build scoped text
        const walker = document.createTreeWalker(wrapper, NodeFilter.SHOW_ELEMENT, {
            acceptNode: (node) => excluded.has(node)
                ? NodeFilter.FILTER_REJECT   // skip entire sub-tree
                : NodeFilter.FILTER_ACCEPT,
        });
        const scopedEls = [];
        let node = walker.currentNode;
        while (node) { scopedEls.push(node); node = walker.nextNode(); }

        // Scoped HTML = wrapper innerHTML (sidebar/header/footer are outside wrapper anyway;
        // any that leaked inside are skipped at link-collection time via isExcluded)
        const html = wrapper.innerHTML;

        // Scoped text: innerText of wrapper minus excluded nodes
        // Simplest reliable approach: clone, strip excluded, read innerText
        const clone = wrapper.cloneNode(true);
        // Map excluded elements to cloned equivalents by position
        const allOrig  = Array.from(wrapper.querySelectorAll('*'));
        const allClone = Array.from(clone.querySelectorAll('*'));
        allOrig.forEach((el, i) => { if (excluded.has(el) && allClone[i]) allClone[i].remove(); });
        const text = clone.innerText || clone.textContent || '';

        // Scoped links: only anchors inside wrapper that are not excluded
        const links = [];
        const seenHref = new Set();
        for (const a of wrapper.querySelectorAll('a[href]')) {
            if (isExcluded(a)) continue;
            const href = a.getAttribute('href') || '';
            if (href && !seenHref.has(href)) {
                seenHref.add(href);
                links.push({ href, text: (a.innerText || '').trim().substring(0, 80) });
            }
        }

        return { html, text: text.trim(), links };
    }
"""

def get_content_scope(page):
    """
    Return (html, text, raw_links) for the full vertical content column:
    from the breadcrumb row (Course Title > Module > Page Name) through the
    end of the Summary and NoteBook sections.
    Excludes the site header above, the site footer below, and the left sidebar.
    """
    try:
        result = page.evaluate(_CONTENT_SCOPE_JS)
        return (
            result.get('html', ''),
            result.get('text', ''),
            [(l['href'], l['text']) for l in result.get('links', [])],
        )
    except Exception:
        return page.content(), page.inner_text('body'), []


def extract_external_links(base_url, raw_links):
    """Return deduplicated list of (url, text) for external links from scoped raw_links."""
    base_host = urlparse(base_url).netloc
    seen = set()
    links = []
    skip_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
    for href, text in raw_links:
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.hostname or ""
        if not host or base_host in host or host in skip_hosts:
            continue
        if re.match(r'^(localhost|127\.|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)', host):
            continue
        ext = "." + href.rsplit(".", 1)[-1].lower() if "." in href.rsplit("/", 1)[-1] else ""
        if ext in DOCUMENT_EXTS:
            continue  # handled separately
        if full not in seen:
            seen.add(full)
            links.append((full, text or href[:80]))
    return links


def extract_document_links(base_url, raw_links):
    seen = set()
    docs = []
    for href, text in raw_links:
        full = urljoin(base_url, href)
        ext  = "." + href.rsplit(".", 1)[-1].lower() if "." in href.rsplit("/", 1)[-1] else ""
        if ext in DOCUMENT_EXTS and full not in seen:
            seen.add(full)
            docs.append((full, text or href[:80]))
    return docs

def click_next_button(page):
    """Click the Next button. Returns True if page content/URL changed."""
    url_before   = page.url
    title_before = get_activity_title(page)

    clicked = page.evaluate("""
        () => {
            // Strategy 1: prevNextButton divs — second one is "Next"
            const btns = Array.from(document.querySelectorAll('[class*="prevNextButton"],[class*="PrevNextButton"]'));
            for (const btn of btns) {
                const txt = btn.innerText.trim();
                if (txt === 'Next' || (txt.includes('Next') && !txt.includes('Previous') && txt.length < 20)) {
                    btn.click();
                    return 'prevNextButton: ' + txt;
                }
            }
            // Strategy 2: any element whose sole text is "Next"
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const hits = [];
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.trim() === 'Next') hits.push(node);
            }
            if (hits.length > 0) {
                // Use the last one (rightmost/bottom-most in DOM)
                const parent = hits[hits.length - 1].parentElement;
                if (parent) { parent.click(); return 'text-node parent: ' + parent.tagName + '.' + parent.className; }
            }
            return null;
        }
    """)

    if not clicked:
        return False

    # Wait for URL or title to change
    try:
        page.wait_for_function(
            f"""() => {{
                const newUrl = document.location.href;
                return newUrl !== {json.dumps(url_before)};
            }}""",
            timeout=8_000
        )
    except PlaywrightTimeout:
        pass

    time.sleep(1.5)
    wait_for_activity(page, 12_000)

    url_after   = page.url
    title_after = get_activity_title(page)
    changed = url_after != url_before or title_after != title_before
    print(f"    [Next] {clicked} | url_chg={url_after != url_before} | title_chg={title_after != title_before}")
    return changed

# ── Login ──────────────────────────────────────────────────────────────────────
def login(page):
    print("→ Logging in …")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except (PlaywrightTimeout, PlaywrightError):
        page.goto(LOGIN_URL, wait_until="load", timeout=PAGE_TIMEOUT)

    page.wait_for_selector("input[type='email'],input[type='password'],input[name='email']",
                           timeout=15_000)

    for sel in ["input[name='email']","input[type='email']","#email","input[placeholder*='mail' i]"]:
        if page.query_selector(sel):
            page.fill(sel, USERNAME); break
    for sel in ["input[type='password']","#password"]:
        if page.query_selector(sel):
            page.fill(sel, PASSWORD); break

    for sel in ["button[type='submit']","button:has-text('Login')","button:has-text('Sign in')"]:
        if page.query_selector(sel):
            page.click(sel); break
    else:
        page.keyboard.press("Enter")

    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
    except PlaywrightTimeout:
        pass
    time.sleep(2)

    ok = "/login" not in page.url
    print(f"  Login {'OK' if ok else 'FAILED'} | URL: {page.url}")
    return ok

# ── Per-activity QA ────────────────────────────────────────────────────────────
def check_activity(page, http_session, rows):
    url              = page.url
    aid              = get_activity_id_from_url(url)
    html, body_tx, raw_links = get_content_scope(page)
    title            = get_activity_title(page)
    ctype            = classify_activity(title, body_tx, html)

    print(f"  ▶ [{aid}] {title!r} → {ctype}")

    # ── AI Summary ──────────────────────────────────────────────────────────
    ai_sum = detect_ai_summary(page)
    if ctype in ("Live Class", "External Link"):
        ai_sum = "N/A"

    # ── Vimeo Videos ────────────────────────────────────────────────────────
    vimeo_ids = extract_vimeo_ids(html)
    if vimeo_ids:
        for vid in vimeo_ids:
            sc, res, notes = check_vimeo(vid)
            rows.append(row(aid, title, "Video", title, f"https://vimeo.com/{vid}",
                            sc, res, ai_sum, notes))
    elif ctype == "Video":
        rows.append(row(aid, title, "Video", title, url, None, "FAIL", ai_sum,
                        "Classified as Video but no Vimeo iframe found"))

    # ── Documents / PDFs ────────────────────────────────────────────────────
    for doc_url, doc_label in extract_document_links(url, raw_links):
        ext   = "." + doc_url.rsplit(".", 1)[-1].lower()
        dtype = "PDF" if ext == ".pdf" else "Document"
        sc, res, notes = check_document(doc_url, http_session)
        rows.append(row(aid, title, dtype, doc_label, doc_url, sc, res, ai_sum, notes))

    # ── External Links ───────────────────────────────────────────────────────
    for ext_url, ext_label in extract_external_links(url, raw_links):
        sc, res, notes = check_url(ext_url)
        rows.append(row(aid, title, "External Link", ext_label, ext_url, sc, res, "N/A", notes))

    # ── Type-specific checks ─────────────────────────────────────────────────
    if ctype in ("Assignment", "Ungraded Assignment", "Course Project"):
        page_ok = "/login" not in url
        rows.append(row(aid, title, ctype, title, url, 200 if page_ok else 302,
                        "PASS" if page_ok else "FAIL",
                        ai_sum,
                        "Page loaded OK" if page_ok else "Redirected to login"))

    elif ctype == "Knowledge Check":
        q_found = bool(page.query_selector(
            '[class*="question"],[class*="Question"],[class*="quiz"],[class*="Quiz"],fieldset'))
        rows.append(row(aid, title, "Knowledge Check", title, url, 200,
                        "PASS" if q_found else "WARNING", ai_sum,
                        "Questions visible" if q_found else "No question elements detected — verify manually"))

    elif ctype == "Live Class":
        # Find live session link
        live_link = page.evaluate("""
            () => {
                const patterns = ['zoom.us', 'meet.google', 'teams.microsoft', 'webex'];
                for (const p of patterns) {
                    const a = document.querySelector('a[href*="' + p + '"]');
                    if (a) return a.href;
                }
                return null;
            }
        """)
        if live_link:
            sc, res, notes = check_url(live_link, follow_redirects=True)
            notes += " | Live class — verify session is scheduled"
        else:
            # Check if it's an expired class page
            if re.search(r'class expired', body_tx.lower()):
                sc, res, notes = 200, "WARNING", "Live class page — Class Expired label detected"
            else:
                sc, res, notes = 200, "WARNING", "No live session link found — verify manually"
        rows.append(row(aid, title, "Live Class", title, live_link or url, sc, res, "N/A", notes))

    elif ctype == "Recording":
        if vimeo_ids:
            pass  # already handled in Vimeo block above (re-labeled as Recording)
        else:
            rec_link = page.evaluate("""
                () => {
                    const sels = ['a[href*="youtube.com"]','a[href*="youtu.be"]',
                                  'a[href*="drive.google.com"]','a[href*="loom.com"]',
                                  'iframe[src*="youtube"]'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) return el.getAttribute('href') || el.getAttribute('src');
                    }
                    return null;
                }
            """)
            if rec_link:
                sc, res, notes = check_url(rec_link, follow_redirects=True)
                rows.append(row(aid, title, "Recording", title, rec_link, sc, res, ai_sum, notes))
            else:
                rows.append(row(aid, title, "Recording", title, url, None, "FAIL", ai_sum,
                                "No recording link found"))

    elif ctype == "Content" and not vimeo_ids:
        # Generic content page — just record successful load
        rows.append(row(aid, title, "Content", title, url, 200, "PASS", ai_sum, "Page loaded OK"))

    # If video but also classified under another type, add a pass for it
    if vimeo_ids and ctype not in ("Video", "Recording"):
        pass  # already added above


# ── Summary ────────────────────────────────────────────────────────────────────
def build_summary(rows):
    from collections import defaultdict
    total    = len(rows)
    passes   = sum(1 for r in rows if r["result"] == "PASS")
    fails    = sum(1 for r in rows if r["result"] == "FAIL")
    warnings = sum(1 for r in rows if r["result"] == "WARNING")

    ai_present = sum(1 for r in rows if r["ai_summary"] == "PRESENT")
    ai_missing = sum(1 for r in rows if r["ai_summary"] == "MISSING")
    ai_empty   = sum(1 for r in rows if r["ai_summary"] == "EMPTY")

    by_type_pass  = defaultdict(int)
    by_type_total = defaultdict(int)
    for r in rows:
        ct = r["component_type"]
        by_type_total[ct] += 1
        if r["result"] == "PASS":
            by_type_pass[ct] += 1
    all_fail_types = [ct for ct, tot in by_type_total.items()
                      if tot > 0 and by_type_pass[ct] == 0]

    notes = (
        f"Total: {total} | PASS: {passes} | FAIL: {fails} | WARNING: {warnings} | "
        f"AI PRESENT: {ai_present} | AI MISSING: {ai_missing} | AI EMPTY: {ai_empty}"
    )
    if all_fail_types:
        notes += f" | 100% FAIL types: {', '.join(all_fail_types)}"

    return [row("SUMMARY", "", "", "", "",
                f"Total={total}", f"PASS={passes} FAIL={fails} WARN={warnings}",
                f"PRESENT={ai_present} MISSING={ai_missing} EMPTY={ai_empty}",
                notes)]

# ── Course discovery ───────────────────────────────────────────────────────────
def find_course_start(page, course_id):
    """
    Return the URL of the first activity for *course_id*.

    Strategy 1 — bare activity URL: the LMS may redirect straight to the
                  first activity when no activityId is given.
    Strategy 2 — parse any link on that page whose href contains both
                  courseId=<id> and activityId=.
    Strategy 3 — navigate to common course-home patterns and repeat link scan.
    """
    base_candidate = f"{LMS_BASE}/activity?courseId={course_id}"

    def _first_activity_link(pg, cid):
        return pg.evaluate(
            f"""() => {{
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const a of links) {{
                    const h = a.getAttribute('href') || '';
                    if (h.includes('activityId=') && h.includes({json.dumps(cid)}))
                        return a.href;
                }}
                // fallback: any activityId link on the page
                for (const a of links) {{
                    const h = a.getAttribute('href') || '';
                    if (h.includes('activityId=')) return a.href;
                }}
                return null;
            }}"""
        )

    # Strategy 1
    print(f"  → Navigating to course: {base_candidate}")
    try:
        page.goto(base_candidate, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        time.sleep(2)
        if "activityId=" in page.url:
            print(f"  → Redirected to first activity: {page.url}")
            return page.url
        link = _first_activity_link(page, course_id)
        if link:
            print(f"  → Found first activity link on course page: {link}")
            return link
    except Exception as e:
        print(f"  WARNING strategy-1: {e}")

    # Strategy 2 — alternate course URL patterns
    for tmpl in [
        f"{LMS_BASE}/course?courseId={course_id}",
        f"{LMS_BASE}/courses/{course_id}",
        f"{LMS_BASE}/learn?courseId={course_id}",
    ]:
        try:
            page.goto(tmpl, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            time.sleep(2)
            if "activityId=" in page.url:
                return page.url
            link = _first_activity_link(page, course_id)
            if link:
                print(f"  → Found first activity via {tmpl}: {link}")
                return link
        except Exception as e:
            print(f"  WARNING strategy-2 ({tmpl}): {e}")

    print("  ERROR: Could not auto-discover first activity. "
          "Please verify the course ID or pass a full MODULE_URL.")
    return None


def _parse_args():
    p = argparse.ArgumentParser(description="LMS QA Checker")
    p.add_argument("--course-id",
                   default=os.environ.get("COURSE_ID", ""),
                   help="Base64-encoded course ID from the LMS URL (env: COURSE_ID)")
    p.add_argument("--course-name",
                   default=os.environ.get("COURSE_NAME", ""),
                   help="Human-readable course name used as the report label (env: COURSE_NAME)")
    return p.parse_args()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    global COURSE_ID, COURSE_NAME, MODULE_NAME, OUTPUT_CSV

    args = _parse_args()
    COURSE_ID   = args.course_id
    COURSE_NAME = args.course_name
    MODULE_NAME = COURSE_NAME

    # Derive a filesystem-safe output filename: qa_report_<id>_<date>.csv
    safe_id    = re.sub(r"[^A-Za-z0-9_-]", "_", COURSE_ID)[:24]
    OUTPUT_CSV = f"qa_report_{safe_id}_{DATE_CHECKED}.csv"

    missing = [k for k, v in {
        "LMS_BASE":     LMS_BASE,
        "LMS_USERNAME": USERNAME,
        "LMS_PASSWORD": PASSWORD,
        "COURSE_ID":    COURSE_ID,
    }.items() if not v]
    if missing:
        print("ERROR: The following required environment variables are not set:")
        for m in missing:
            print(f"         {m}")
        print("       Copy .env.example to .env, fill in your details, and re-run.")
        sys.exit(1)

    print(f"Course ID   : {COURSE_ID}")
    print(f"Course name : {COURSE_NAME}")
    print(f"Output file : {OUTPUT_CSV}")

    rows = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        # Login
        if not login(page):
            print("ERROR: Login failed. Exiting.")
            browser.close()
            sys.exit(1)

        # Discover first activity for this course
        print(f"\n→ Finding course start …")
        time.sleep(2)   # let SPA stabilise after login redirect
        start_url = find_course_start(page, COURSE_ID)
        if not start_url:
            print("ERROR: Could not find course start URL. Exiting.")
            browser.close()
            sys.exit(1)

        # Navigate to first activity (find_course_start may have already landed there)
        if page.url != start_url:
            try:
                page.goto(start_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            except (PlaywrightTimeout, PlaywrightError) as e:
                print(f"  WARNING: goto error ({e}), retrying with load …")
                time.sleep(3)
                page.goto(start_url, wait_until="load", timeout=PAGE_TIMEOUT)

        if not wait_for_activity(page, 25_000):
            print("WARNING: Activity content may not have fully rendered.")

        # Build requests session with LMS cookies
        http_session = requests.Session()
        http_session.headers["User-Agent"] = "Mozilla/5.0 LMS-QA-Bot"
        for cookie in ctx.cookies():
            http_session.cookies.set(
                cookie["name"], cookie["value"],
                domain=cookie.get("domain", ""), path=cookie.get("path", "/")
            )

        # Traverse all activities via Next button
        seen_aids = set()
        activity_count = 0
        MAX_ACTIVITIES = 300  # raised to cover large multi-module courses

        for i in range(MAX_ACTIVITIES):
            aid = get_activity_id_from_url(page.url)

            if aid in seen_aids:
                print(f"\n[{i}] Already visited activity {aid!r}. Stopping.")
                break
            seen_aids.add(aid)
            activity_count += 1

            print(f"\n[{activity_count}] Activity {aid!r}")
            check_activity(page, http_session, rows)

            # Try to go to next activity
            if not click_next_button(page):
                print("  No Next button or content unchanged. End of module.")
                break

        browser.close()

    # Write CSV
    all_rows = rows + build_summary(rows)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(all_rows)

    passes   = sum(1 for r in rows if r["result"] == "PASS")
    fails    = sum(1 for r in rows if r["result"] == "FAIL")
    warnings = sum(1 for r in rows if r["result"] == "WARNING")
    print(f"\n✓ Saved {OUTPUT_CSV}  ({len(rows)} component rows + summary)")
    print(f"  PASS={passes}  FAIL={fails}  WARNING={warnings}")
    print(f"  Activities visited: {activity_count}")


if __name__ == "__main__":
    main()
