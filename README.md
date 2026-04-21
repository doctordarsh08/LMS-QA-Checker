# LMS QA Runner

Automated quality-assurance system for online courses hosted on a Learning Management System (LMS). Given a Course ID, it logs in, traverses every activity in the course via browser automation, checks all content components, and produces a structured CSV report — triggered from a web UI or directly from GitHub Actions.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [How It Works](#how-it-works)
- [Content Scope](#content-scope)
- [Component Checks](#component-checks)
- [Output Format](#output-format)
- [Setup](#setup)
- [Running a QA Check](#running-a-qa-check)
- [Web UI](#web-ui)
- [Configuration Reference](#configuration-reference)
- [Tech Stack](#tech-stack)

---

## Features

- **Zero-config course traversal** — provide only a Course ID; the tool auto-discovers the first activity and clicks through the entire course via the Next button
- **Multi-module support** — traverses all modules sequentially in one run
- **Component-aware checking** — different logic per content type (video, document, external link, live class, assignment, knowledge check, recording)
- **Scoped content extraction** — analyses only the visible page content column (breadcrumb → Summary → Notebook), excluding site header, footer, and sidebar
- **AI Summary detection** — reports PRESENT / EMPTY / MISSING for every activity
- **Per-course output files** — `qa_report_<course_id>_<date>.csv`, preventing overwrite across runs
- **Web UI** — single-page interface to trigger runs and download reports without touching the CLI
- **GitHub Pages hosting** — the UI auto-deploys on every push to `main`
- **Artifact upload** — CSV and log are attached to every Actions run for direct download

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Web UI (ui/index.html)                  │
│  Enter Course ID + Name → calls GitHub workflow_dispatch API    │
│  Shows recent runs + download links                             │
└──────────────────────────────┬──────────────────────────────────┘
                               │ triggers
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│               GitHub Actions  (.github/workflows/qa.yml)        │
│  Inputs: course_id, course_name                                 │
│  Secrets: LMS_BASE, LMS_USERNAME, LMS_PASSWORD                 │
│  → runs lms_qa_checker.py                                       │
│  → uploads qa_report_*.csv as downloadable artifact            │
│  → commits report to repo                                       │
└──────────────────────────────┬──────────────────────────────────┘
                               │ executes
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    lms_qa_checker.py                            │
│                                                                 │
│  _parse_args()                                                  │
│      └─ accepts --course-id / --course-name (or env vars)      │
│                                                                 │
│  find_course_start(page, course_id)                             │
│      └─ navigates to course, auto-discovers first activity URL  │
│                                                                 │
│  ┌── Activity loop (Next button traversal, up to 300) ──────┐  │
│  │                                                           │  │
│  │  get_content_scope(page)                                  │  │
│  │      └─ JS: finds main wrapper, strips header/footer/    │  │
│  │             sidebar → returns (html, text, raw_links)    │  │
│  │                                                           │  │
│  │  classify_activity(title, text, html)                     │  │
│  │      └─ Video / Content / Assignment / Live Class / ...  │  │
│  │                                                           │  │
│  │  detect_ai_summary(page)   → PRESENT / EMPTY / MISSING   │  │
│  │  extract_vimeo_ids(html)   → check_vimeo() via oEmbed    │  │
│  │  extract_document_links()  → check_document() via HTTP   │  │
│  │  extract_external_links()  → check_url() via HTTP        │  │
│  │                                                           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  build_summary(rows) → aggregate stats row                      │
│  → write qa_report_<id>_<date>.csv                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
lms-qa-runner/
├── lms_qa_checker.py          # Core QA engine
├── .env.example               # Template for required environment variables
├── .gitignore
│
├── ui/
│   └── index.html             # Self-contained web UI (no build step)
│
└── .github/
    └── workflows/
        ├── qa.yml             # QA run workflow (workflow_dispatch)
        └── pages.yml          # Auto-deploys ui/ to GitHub Pages
```

---

## How It Works

### 1. Course discovery
The script navigates to `https://<LMS>/activity?courseId=<id>` and uses three fallback strategies to find the first activity:
1. Check if the LMS redirects directly to the first activity
2. Scan all links on the course page for `activityId=` parameters
3. Try alternate course-home URL patterns (`/course?courseId=`, `/courses/<id>`)

### 2. Activity traversal
Starting from the first activity, the script clicks the **Next** button on each page, waiting for the URL or page title to change before proceeding. It tracks visited activity IDs to detect and stop at loops (end of course).

### 3. Content scoping
Before extracting any text or links from a page, the script runs a JavaScript snippet that:
- Locates the main content wrapper (`main`, `[role="main"]`, or class-based equivalents)
- Builds an exclusion set covering `header`, `footer`, sidebar, drawer, and navbar elements
- Returns HTML, inner text, and anchor links **only from the content column** — from the breadcrumb row at the top through the Summary and Notebook sections at the bottom

### 4. Component checking
Each activity is classified and checked according to its type. Results are accumulated into rows and written to CSV at the end.

---

## Content Scope

Only content within the **vertical page column** is analysed:

```
━━━━ site header (EXCLUDED) ━━━━━━━━━━━━━━━━━━━━━━━━━━

  Course Title  >  Module  >  Page Name    ← breadcrumb (scope START)

  ┌──────────────────────────────────────┐
  │  Page title                          │
  │  Main content body                   │  ← content viewer
  │  (text, embedded media, links …)     │
  └──────────────────────────────────────┘

  ← Previous          Next →

  ┌──────────────────────────────────────┐
  │  Summary                             │  ← AI Summary card
  └──────────────────────────────────────┘

  ┌──────────────────────────────────────┐
  │  My NoteBook   My Notes   My Todo    │  ← Notebook card (scope END)
  └──────────────────────────────────────┘

━━━━ site footer (EXCLUDED) ━━━━━━━━━━━━━━━━━━━━━━━━━━
```

The left sidebar / course-navigation drawer is also excluded regardless of whether it is open or closed.

---

## Component Checks

| Component type | What is checked |
|---|---|
| **Video** | Vimeo oEmbed API — confirms video is public and accessible |
| **PDF / Document** | HTTP HEAD request — confirms file returns 200 with correct MIME type |
| **External Link** | HTTP GET — confirms reachability, flags redirects as WARNING |
| **Live Class** | Checks for Zoom/Meet/Teams link; flags expired class pages |
| **Recording** | Checks YouTube, Drive, Loom, or Vimeo link reachability |
| **Assignment** | Confirms page loads without login redirect |
| **Knowledge Check** | Confirms question elements are present in the DOM |
| **Content** | Confirms page loaded; records AI Summary status |

**AI Summary** is checked on every activity: `PRESENT` (summary text found), `EMPTY` (block exists but no content), or `MISSING` (no block detected).

---

## Output Format

Each run writes `qa_report_<course_id>_<date>.csv`:

| Column | Description |
|---|---|
| `date_checked` | ISO date the run executed |
| `module_name` | Course name passed at runtime |
| `activity_id` | Base64-encoded activity ID from the LMS URL |
| `activity_title` | Page title extracted from the content area |
| `component_type` | Video / PDF / Document / External Link / Content / … |
| `label` | Link text or activity title for the specific component |
| `url` | URL that was checked |
| `status_code` | HTTP status code returned |
| `result` | `PASS` / `FAIL` / `WARNING` |
| `ai_summary` | `PRESENT` / `EMPTY` / `MISSING` / `N/A` |
| `notes` | Human-readable detail (redirect destination, error message, …) |

The final row is a **SUMMARY** row with aggregate counts (total, pass, fail, warning, AI summary breakdown).

---

## Setup

### Prerequisites
- Python 3.11+
- A QA/test account on the target LMS with student access to the courses you want to check

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/lms-qa-runner.git
cd lms-qa-runner
pip install playwright requests
python -m playwright install chromium --with-deps
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — fill in all required values (see .env.example)
```

For local runs, export the variables before running:
```bash
export LMS_BASE="https://your-lms-domain.com"
export LMS_USERNAME="your-qa-account@example.com"
export LMS_PASSWORD="your-password"
```

### 3. Add GitHub Secrets (for Actions runs)

**Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `LMS_BASE` | Root URL of your LMS (e.g. `https://your-lms-domain.com`) |
| `LMS_USERNAME` | QA account email |
| `LMS_PASSWORD` | QA account password |

### 4. Enable GitHub Pages (for the Web UI)

**Settings → Pages → Source → GitHub Actions**

The `pages.yml` workflow deploys `ui/` automatically on every push to `main`. Your UI will be at:
```
https://YOUR_USERNAME.github.io/lms-qa-runner
```

### 5. Point the Web UI at your repo

Open `ui/index.html` and update the `REPO` constant near the top of the `<script>` block:

```js
const REPO = 'YOUR_USERNAME/lms-qa-runner';
```

---

## Running a QA Check

### Via the Web UI
1. Open `https://YOUR_USERNAME.github.io/lms-qa-runner`
2. Enter the **Course ID** (the `?courseId=` value from any LMS activity URL)
3. Optionally enter a **Course Name** for the report label
4. Paste a **GitHub Personal Access Token** with `repo` + `workflow` scopes
5. Click **Run QA Check**

The run queues immediately. The Recent Runs panel refreshes automatically — click the download icon on any completed run to get the CSV.

### Via GitHub Actions
1. **Actions → LMS QA Check → Run workflow**
2. Fill in `course_id` and `course_name`
3. Click **Run workflow**

The CSV and run log are attached as a downloadable artifact on the run page.

### Via CLI (local)
```bash
python lms_qa_checker.py --course-id "YOUR_COURSE_ID" --course-name "Your Course Name"
```

---

## Web UI

`ui/index.html` is a single self-contained HTML file — no build step, no external runtime dependencies.

**Panels:**

| Panel | Description |
|---|---|
| Start a QA Check | Input form; triggers the GitHub Actions workflow via API |
| Recent Runs | Last 15 runs with status badge, relative time, duration, open-on-GitHub and download-artifact icons |

**Token storage:** When "Remember in this browser" is checked, the PAT is stored in `localStorage`. Use a token scoped only to `repo` + `workflow` on this repository and revoke it when no longer needed.

---

## Configuration Reference

### Environment variables / CLI flags

| Variable | CLI flag | Required | Default | Description |
|---|---|---|---|---|
| `LMS_USERNAME` | — | Yes | — | LMS login email |
| `LMS_PASSWORD` | — | Yes | — | LMS login password |
| `COURSE_ID` | `--course-id` | Yes | — | Base64 course ID from LMS URL |
| `COURSE_NAME` | `--course-name` | Yes | — | Label written into every CSV row |

### Script constants (edit `lms_qa_checker.py` to target a different LMS)

| Constant | Default | Description |
|---|---|---|
| `LMS_BASE` | *(required via env)* | LMS root URL (e.g. `https://your-lms-domain.com`) |
| `REQUEST_TIMEOUT` | `10` | Seconds before an HTTP check times out |
| `PAGE_TIMEOUT` | `45000` | Playwright page-load timeout (ms) |
| `MAX_ACTIVITIES` | `300` | Safety cap on activities per run |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Browser automation | [Playwright](https://playwright.dev/python/) (Chromium, headless) |
| HTTP checks | [Requests](https://docs.python-requests.org/) |
| CI / CD | [GitHub Actions](https://docs.github.com/en/actions) |
| Web UI | Vanilla HTML / CSS / JavaScript (no framework) |
| UI hosting | [GitHub Pages](https://pages.github.com/) |
| Report format | CSV (Python `csv` module) |
| Runtime | Python 3.11 |
