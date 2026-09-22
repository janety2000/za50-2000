#!/usr/bin/env python3
import os
import re
import csv
import sys
import io
import time
import base64
import hashlib
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════
def _env_int(name, default):
    """int(os.environ.get(name, default)) crashes if the env var is SET but
    empty (e.g. an unfilled GitHub Actions workflow_dispatch input, which
    arrives as '' rather than being absent). Treat blank as 'not provided'."""
    val = os.environ.get(name, "")
    if val is None or val.strip() == "":
        return default
    return int(val)


BASE_URL     = "https://www.myjobmag.co.za"
START_PAGE   = _env_int("START_PAGE", 21)
END_PAGE     = _env_int("END_PAGE", 2000)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Charset": "utf-8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": BASE_URL + "/",
}
REQUEST_TIMEOUT = 20
FALLBACK_TIMEOUT = 6   # short leash for the Google/LinkedIn company-bio fallback
PROCESSED_IDS_FILE = "processed.csv"
PAGE_STATE_FILE = "current_page.txt"

# Being polite between LIST page fetches (not just between jobs) noticeably
# reduces the odds of tripping Cloudflare's rate-based bot detection, which
# was the likely cause of every page returning 0 jobs in the last run — the
# scraper was hammering /page/N every ~230ms with no pause.
def _env_float(name, default):
    val = os.environ.get(name, "")
    if val is None or val.strip() == "":
        return default
    return float(val)


PAGE_FETCH_DELAY = _env_float("PAGE_FETCH_DELAY", 2.0)

# ── WordPress ─────────────────────────────────────────────────────────────────
# Credentials are ONLY ever read from environment variables / GitHub Secrets.
# Do NOT hardcode real credentials in this file — a previous version of this
# script had a live username + WordPress Application Password committed in
# plaintext here. If that password is still active, revoke/regenerate it now
# in WP Admin → Users → Profile → Application Passwords.
WP_URL      = os.environ.get("WP_BASE_URL", "")
WP_USER     = os.environ.get("WP_USERNAME", "")
WP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

WP_BASE        = WP_URL.rstrip("/")
WP_JOBS_URL    = f"{WP_BASE}/job-listings"
WP_COMPANY_URL = f"{WP_BASE}/companies"
WP_MEDIA_URL   = f"{WP_BASE}/media"

JOB_TYPE_MAPPING = {
    "full-time": "full-time", "full time": "full-time", "fulltime": "full-time",
    "part-time": "part-time", "part time": "part-time", "parttime": "part-time",
    "contract": "contract", "contractor": "contract", "contracting": "contract",
    "temporary": "temporary", "temp": "temporary",
    "freelance": "freelance",
    "internship": "internship", "intern": "internship",
    "volunteer": "volunteer",
}

# ── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers.clear()

_fh = logging.FileHandler("debug.log", encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)

try:
    _utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except AttributeError:
    _utf8_stdout = sys.stdout

_ch = logging.StreamHandler(_utf8_stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_ch)


def require_wp_config():
    missing = [n for n, v in
               [("WP_BASE_URL", WP_URL), ("WP_USERNAME", WP_USER), ("WP_APP_PASSWORD", WP_PASSWORD)]
               if not v]
    if missing:
        raise EnvironmentError(
            f"\n❌ Missing required environment variable(s): {', '.join(missing)}\n"
            f"   Set them as environment variables locally, or as GitHub repo secrets\n"
            f"   named WP_BASE_URL / WP_USERNAME / WP_APP_PASSWORD for Actions runs.\n"
        )


# ════════════════════════════════════════════════════════════════════════════
# HTTP SESSION
# ════════════════════════════════════════════════════════════════════════════
# This site sits behind Cloudflare (you can see cdn-cgi/challenge-platform
# scripts in its pages). A bare requests.Session() can get served a JS
# challenge page instead of real content — which has none of the job-listing
# markup, hence "Found 0 job URLs" on every single page. cloudscraper solves
# Cloudflare's JS challenge automatically; we fall back to plain requests if
# it isn't installed, but log a clear warning so it's obvious why zero
# results might still happen.
try:
    import cloudscraper
    SESSION = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    _USING_CLOUDSCRAPER = True
except ImportError:
    SESSION = requests.Session()
    _USING_CLOUDSCRAPER = False

SESSION.headers.update(HEADERS)


def warm_up_session():
    """Hit the homepage once before scraping so any Cloudflare clearance
    cookies get set, and so we look like a normal visitor arriving on the
    site rather than jumping straight to /page/1001."""
    if not _USING_CLOUDSCRAPER:
        logger.warning("cloudscraper is not installed — install it (pip install cloudscraper) "
                        "if pages keep coming back with 0 jobs; this site uses Cloudflare.")
    try:
        resp = SESSION.get(BASE_URL, timeout=REQUEST_TIMEOUT)
        logger.info(f"Warm-up request to {BASE_URL} → HTTP {resp.status_code}, {len(resp.text)} bytes")
        if _looks_blocked(resp.text):
            logger.warning("Warm-up response looks like a Cloudflare challenge page. "
                            "Scraping is likely to fail until this clears.")
    except Exception as e:
        logger.error(f"Warm-up request failed: {e}")


def _looks_blocked(html: str) -> bool:
    markers = ("Just a moment", "cf-browser-verification", "cf_chl_opt",
               "Attention Required! | Cloudflare", "challenges.cloudflare.com")
    return any(m in html for m in markers)


def wp_headers() -> dict:
    token = base64.b64encode(f"{WP_USER}:{WP_PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


# ════════════════════════════════════════════════════════════════════════════
# SANITIZATION
# ════════════════════════════════════════════════════════════════════════════
_MOJIBAKE = [
    ("Â", ""), ("â€™", "'"), ("â€œ", '"'), ("â€\x9d", '"'), ("â€", '"'),
    ("â€¢", "•"), ("Ã©", "é"), ("Ã ", "à"), ("Ã¨", "è"), ("Ã¯", "ï"),
    ("\u00a0", " "), ("\u200b", ""), ("\ufeff", ""),
]


def sanitize(value: str) -> str:
    if not isinstance(value, str):
        return value
    text = value
    for pattern, repl in _MOJIBAKE:
        text = text.replace(pattern, repl)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    text = re.sub(r"^N/A$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bN/A\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalise_job_type(raw: str) -> str:
    return JOB_TYPE_MAPPING.get((raw or "").lower().strip(), "full-time")


def add_three_months(posted_date: datetime) -> str:
    month = posted_date.month - 1 + 3
    year = posted_date.year + month // 12
    month = month % 12 + 1
    day = min(posted_date.day, 28)
    return datetime(year, month, day).strftime("%Y-%m-%d")


def make_job_id(job_url: str) -> str:
    return hashlib.md5(job_url.encode()).hexdigest()[:16]


# ════════════════════════════════════════════════════════════════════════════
# DEDUP TRACKER
# ════════════════════════════════════════════════════════════════════════════
def _init_tracker():
    if not os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Job ID", "Job URL", "Job Title", "Status", "Timestamp", "Page", "Job Number"]
            )


def load_processed_ids() -> set:
    _init_tracker()
    with open(PROCESSED_IDS_FILE, newline="", encoding="utf-8") as f:
        return {row["Job ID"] for row in csv.DictReader(f)}


def mark_processed(job_id: str, job_url: str, title: str, status: str,
                    page_num=None, job_number=None):
    _init_tracker()
    with open(PROCESSED_IDS_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            job_id, job_url, title, status, datetime.now().isoformat(),
            page_num if page_num is not None else "",
            job_number if job_number is not None else "",
        ])


# ════════════════════════════════════════════════════════════════════════════
# PAGE PROGRESS TRACKER
# ════════════════════════════════════════════════════════════════════════════
def load_current_page() -> int:
    if os.path.exists(PAGE_STATE_FILE):
        try:
            with open(PAGE_STATE_FILE, encoding="utf-8") as f:
                saved = int(f.read().strip())
            if saved < START_PAGE or saved > END_PAGE:
                logger.info(f"📄 Saved page {saved} is outside range "
                            f"[{START_PAGE}, {END_PAGE}] — starting fresh at START_PAGE={START_PAGE}.")
                return START_PAGE
            return saved
        except (ValueError, OSError) as e:
            logger.warning(f"Could not read {PAGE_STATE_FILE} ({e}) — using START_PAGE.")
    return START_PAGE


def save_current_page(next_page: int):
    with open(PAGE_STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(next_page))


# ════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ════════════════════════════════════════════════════════════════════════════
def get_soup(url: str, timeout: int = REQUEST_TIMEOUT) -> BeautifulSoup:
    resp = SESSION.get(url, timeout=timeout)
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def text_of(soup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""


# ════════════════════════════════════════════════════════════════════════════
# SCRAPING
# ════════════════════════════════════════════════════════════════════════════
# Primary selector matches the title link inside each job card. A couple of
# looser fallbacks are tried (still scoped to the main <ul class="job-list">
# so we don't accidentally sweep up the sidebar's "Popular Jobs" / "Featured
# Jobs" links, which also point at /job/... URLs) in case the site tweaks
# its markup on some pages/templates.
LIST_SELECTORS = [
    "ul.job-list li.job-list-li li.mag-b > h2 > a",
    "ul.job-list li.job-list-li h2 a[href]",
    "ul.job-list li.job-list-li a[href^='/job/']",
    "ul.job-list li.job-list-li a[href^='/jobs/']",
]


def _extract_list_anchors(soup):
    for sel in LIST_SELECTORS:
        anchors = soup.select(sel)
        if anchors:
            return anchors, sel
    return [], None


def scrape_job_list_page(page_num: int) -> list:
    url = f"{BASE_URL}/page/{page_num}"
    logger.info(f"Fetching page {page_num}: {url}")
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as e:
        logger.error(f"Error fetching page {page_num}: {e}")
        return []

    if resp.status_code != 200:
        logger.warning(f"Page {page_num}: HTTP {resp.status_code} (len={len(html)}).")

    if _looks_blocked(html):
        logger.warning(f"Page {page_num}: response looks like a Cloudflare challenge page "
                        f"(len={len(html)}) — this page will yield 0 jobs until that clears.")

    soup = BeautifulSoup(html, "html.parser")
    anchors, matched_selector = _extract_list_anchors(soup)

    urls = []
    seen = set()
    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        full = BASE_URL + href if href.startswith("/") else href
        if full not in seen:
            seen.add(full)
            urls.append(full)

    if not urls:
        logger.warning(
            f"Page {page_num}: 0 job URLs found. HTTP={resp.status_code}, len={len(html)}. "
            f"First 200 chars: {html[:200]!r}"
        )
    else:
        logger.info(f"Found {len(urls)} job URLs on page {page_num} (selector: {matched_selector!r})")

    return urls


def scrape_company_details(company_url: str) -> dict:
    company = {
        "name": "", "logo": "", "industry": "", "founded": "",
        "type": "", "website": "", "address": "", "details": "",
    }
    if not company_url:
        return company
    try:
        soup = get_soup(company_url)
        company["name"] = text_of(soup, "#wrap-comp-jobs > div.company-jobs > h1").replace("Recruitment", "").strip()
        logo_el = soup.select_one("#wrap-comp-jobs > div.company-jobs > div.company-logo > img")
        if logo_el and logo_el.get("src"):
            src = logo_el["src"]
            company["logo"] = BASE_URL + src if src.startswith("/") else src
        company["industry"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(1) > span.comp-info-desc")
        company["founded"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(2) > span.comp-info-desc")
        company["type"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(3) > span.comp-info-desc")
        company["website"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(4) > span.comp-info-desc")
        company["address"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.company-details-right > ul > li:nth-of-type(5) > span.comp-info-desc")
        company["details"] = text_of(
            soup, "#wrap-comp-jobs > div.company-jobs > div.mag-b.fl-r.ts-13.tc-b6.bm-b-35")
        logger.info(f"Company scraped: {company['name']}")
    except Exception as e:
        logger.error(f"Company fetch failed for {company_url}: {e}")
    return company


def _extract_date_pair(soup) -> tuple:
    posted_str, deadline_str = "", ""
    date_lis = soup.select("div.read-date-sec div.read-date-sec-li")
    if len(date_lis) >= 1:
        li = date_lis[0]
        b = li.find("b")
        if b:
            b.extract()
        posted_str = li.get_text(strip=True)
    if len(date_lis) >= 2:
        li = date_lis[1]
        b = li.find("b")
        if b:
            b.extract()
        deadline_str = li.get_text(strip=True)
    return posted_str, deadline_str


def _extract_company_url(soup) -> str:
    a = soup.select_one("li.job-industry a[href^='/jobs-at/']")
    if a and a.get("href"):
        href = a["href"]
        return BASE_URL + href if href.startswith("/") else href
    for a in soup.select("#printable > a"):
        href = a.get("href")
        if href:
            return BASE_URL + href if href.startswith("/") else href
    return ""


def _extract_application(soup) -> str:
    app_block = soup.select_one("#printable > div.mag-b.bm-b-30")
    if not app_block:
        h2 = soup.select_one("#application-method")
        app_block = h2.find_next_sibling("div") if h2 else None

    if not app_block:
        return ""

    text = app_block.get_text(" ", strip=True)
    email_match = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}", text)
    if email_match:
        return email_match.group(0)

    link = app_block.select_one("a")
    if link and link.get("href"):
        href = link["href"]
        return BASE_URL + href if href.startswith("/") else href

    return ""


def scrape_job_details(job_url: str) -> list:
    soup = get_soup(job_url)

    job_headings = soup.select("#printable > h2.mag-b")
    if not job_headings:
        h2 = soup.select_one("h2.mag-b")
        if h2:
            job_headings = [h2]

    subjob_blocks = []
    for h2 in job_headings:
        link_el = h2.select_one("a")
        if link_el and link_el.get("href"):
            title = link_el.get_text(strip=True)
            href = link_el["href"]
            subjob_url = BASE_URL + href if href.startswith("/") else href
        else:
            span_el = h2.select_one("span.subjob-title")
            title = span_el.get_text(strip=True) if span_el else h2.get_text(strip=True)
            subjob_url = job_url
        title = title.replace("Method of Application", "").strip()
        if not title:
            continue

        key_info_ul = h2.find_next_sibling("ul", class_="job-key-info")
        details_div = h2.find_next_sibling("div", class_="job-details")

        def kv(label):
            if not key_info_ul:
                return ""
            for li in key_info_ul.select("li"):
                t = li.select_one("span.jkey-title")
                if t and t.get_text(strip=True) == label:
                    info = li.select_one("span.jkey-info")
                    return info.get_text(strip=True) if info else ""
            return ""

        subjob_blocks.append({
            "title": title,
            "url": subjob_url,
            "job_type": kv("Job Type"),
            "qualifications": kv("Qualification"),
            "experience": kv("Experience"),
            "location": kv("Location"),
            "field": kv("Job Field"),
            "salary": kv("Salary Range"),
            "description": details_div.get_text("\n", strip=True) if details_div else "",
        })

    if not subjob_blocks:
        logger.warning(f"No job blocks found on page — skipping: {job_url}")
        return []

    date_posted_str, deadline_raw = _extract_date_pair(soup)
    date_posted = None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            date_posted = datetime.strptime(date_posted_str, fmt)
            break
        except ValueError:
            continue
    if date_posted is None:
        logger.warning(f"Invalid/unparseable date '{date_posted_str}' — skipping page: {job_url}")
        return []

    estimated_deadline = add_three_months(date_posted)
    deadline = deadline_raw.strip()
    if not deadline or deadline.lower() == "not specified":
        deadline = estimated_deadline

    application = _extract_application(soup)
    company_url = _extract_company_url(soup)
    company = scrape_company_details(company_url)
    if not company["details"] and company["name"]:
        company["details"] = search_company_details_fallback(company["name"])
    if not application:
        application = company["website"]

    jobs = []
    for blk in subjob_blocks:
        jobs.append({
            "job_title": sanitize(blk["title"]),
            "job_type": sanitize(blk["job_type"]),
            "job_qualifications": sanitize(blk["qualifications"]),
            "job_experience": sanitize(blk["experience"]),
            "job_location": sanitize(blk["location"]),
            "job_field": sanitize(blk["field"]),
            "date_posted": sanitize(date_posted_str),
            "deadline": sanitize(deadline),
            "job_description": sanitize(blk["description"]),
            "application": sanitize(application),
            "company_url": sanitize(company_url),
            "company_name": sanitize(company["name"]),
            "company_logo": sanitize(company["logo"]),
            "company_industry": sanitize(company["industry"]),
            "company_founded": sanitize(company["founded"]),
            "company_type": sanitize(company["type"]),
            "company_website": sanitize(company["website"]),
            "company_address": sanitize(company["address"]),
            "company_details": sanitize(company["details"]),
            "job_url": sanitize(blk["url"]),
            "estimated_deadline": sanitize(estimated_deadline),
            "salary_range": sanitize(blk["salary"]),
        })
    return jobs


def search_company_details_fallback(company_name: str) -> str:
    try:
        url = "https://www.google.com/search?q=" + requests.utils.quote(company_name + " company about")
        soup = get_soup(url, timeout=FALLBACK_TIMEOUT)
        snippet = (soup.select_one("div.BNeawe") or
                   soup.select_one("span.aCOpRe") or
                   soup.select_one("div.VwiC3b"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
    except Exception as e:
        logger.error(f"Google fallback failed for {company_name}: {e}")
    try:
        slug = re.sub(r"[^a-z0-9-]", "-", company_name.lower())
        url = f"https://www.linkedin.com/company/{slug}"
        soup = get_soup(url, timeout=FALLBACK_TIMEOUT)
        snippet = (soup.select_one("p.core-section-container__info") or
                   soup.select_one("section.summary p"))
        if snippet and len(snippet.get_text(strip=True)) > 20:
            return snippet.get_text(strip=True)
        meta = soup.find("meta", {"name": "description"})
        if meta and meta.get("content") and len(meta["content"]) > 20:
            return meta["content"]
    except Exception as e:
        logger.error(f"LinkedIn fallback failed for {company_name}: {e}")
    return ""


# ════════════════════════════════════════════════════════════════════════════
# WORDPRESS
# ════════════════════════════════════════════════════════════════════════════
def upload_logo(logo_url: str):
    if not logo_url or not logo_url.startswith("http"):
        return None
    ext = logo_url.lower().rsplit(".", 1)[-1]
    if ext not in ("png", "jpg", "jpeg", "webp"):
        return None
    try:
        img = SESSION.get(logo_url, timeout=15)
        img.raise_for_status()
        headers = wp_headers()
        headers["Content-Disposition"] = f"attachment; filename={logo_url.split('/')[-1]}"
        headers["Content-Type"] = img.headers.get("content-type", "image/jpeg")
        r = SESSION.post(WP_MEDIA_URL, headers=headers, data=img.content,
                          auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        return r.json().get("id")
    except Exception as e:
        logger.error(f"Logo upload error: {e}")
        return None


_term_cache = {}


def get_or_create_term(taxonomy_url: str, name: str):
    if not name or not name.strip():
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    cache_key = (taxonomy_url, slug)
    if cache_key in _term_cache:
        return _term_cache[cache_key]

    try:
        r = SESSION.get(f"{taxonomy_url}?slug={slug}", headers=wp_headers(), timeout=10)
        terms = r.json()
        if isinstance(terms, list) and terms:
            _term_cache[cache_key] = terms[0]["id"]
            return terms[0]["id"]
    except Exception:
        pass
    try:
        r = SESSION.post(taxonomy_url, json={"name": name, "slug": slug},
                          headers=wp_headers(), auth=(WP_USER, WP_PASSWORD), timeout=10)
        term_id = r.json().get("id")
        _term_cache[cache_key] = term_id
        return term_id
    except Exception as e:
        logger.error(f"Term create error '{name}': {e}")
        return None


def ensure_job_type_terms():
    for jt_label in ["Full Time", "Part Time", "Contract", "Temporary", "Freelance", "Internship", "Volunteer"]:
        get_or_create_term(f"{WP_BASE}/job_listing_type", jt_label)


def save_company(job: dict):
    name = job["company_name"]
    if not name:
        return None
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    try:
        r = SESSION.get(f"{WP_COMPANY_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Company exists: {name}")
            return posts[0]["id"]
    except Exception:
        pass

    attachment_id = upload_logo(job["company_logo"])
    payload = {
        "title": name,
        "content": job["company_details"],
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_company_name": name,
            "_company_logo": str(attachment_id) if attachment_id else "",
            "_company_industry": job["company_industry"],
            "_company_website": job["company_website"],
        },
    }
    try:
        r = SESSION.post(WP_COMPANY_URL, json=payload, headers=wp_headers(),
                          auth=(WP_USER, WP_PASSWORD), timeout=20)
        r.raise_for_status()
        post = r.json()
        logger.info(f"✅ Company posted: {name} → ID {post.get('id')}")
        return post.get("id")
    except Exception as e:
        logger.error(f"Company post error '{name}': {e}")
        return None


def save_job(job: dict):
    title       = job["job_title"]
    description = job["job_description"]
    location    = job["job_location"] or "Nigeria"
    job_type_s  = normalise_job_type(job["job_type"])
    company     = job["company_name"]
    application = job["application"]
    deadline    = job["deadline"] or job["estimated_deadline"]

    is_email = bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", application))
    is_url_v = bool(re.match(r"^https?://[^\s]+$", application))
    if not (is_email or is_url_v):
        application = ""

    slug = re.sub(r"[^a-z0-9-]", "-", title.lower())[:80]
    try:
        r = SESSION.get(f"{WP_JOBS_URL}?slug={slug}", headers=wp_headers(), timeout=10)
        posts = r.json()
        if isinstance(posts, list) and posts:
            logger.info(f"⏭ Job already on WP: {title}")
            return posts[0]["id"], posts[0].get("link")
    except Exception:
        pass

    attachment_id     = upload_logo(job["company_logo"])
    region_term_id    = get_or_create_term(f"{WP_BASE}/job_listing_region", location)
    job_type_term_id  = get_or_create_term(f"{WP_BASE}/job_listing_type", job_type_s.replace("-", " ").title())

    payload = {
        "title": title,
        "content": description,
        "status": "publish",
        "featured_media": attachment_id or 0,
        "meta": {
            "_job_title":          title,
            "_job_location":       location,
            "_job_type":           job_type_s,
            "_job_description":    description,
            "_application":        application,
            "_job_expires":        deadline,
            "_company_name":       company,
            "_company_website":    job["company_website"],
            "_company_logo":       str(attachment_id) if attachment_id else "",
            "_company_industry":   job["company_industry"],
            "_company_address":    job["company_address"],
            "_company_founded":    job["company_founded"],
            "_company_type":       job["company_type"],
            "_job_qualifications": job["job_qualifications"],
            "_job_experiences":    job["job_experience"],
            "_job_field":          job["job_field"],
            "_job_source_url":     job["job_url"],
            "_job_salary":         job["salary_range"],
        },
    }
    if region_term_id:
        payload["job_listing_region"] = [region_term_id]
    if job_type_term_id:
        payload["job_listing_type"] = [job_type_term_id]

    for attempt in range(3):
        try:
            r = SESSION.post(WP_JOBS_URL, json=payload, headers=wp_headers(),
                              auth=(WP_USER, WP_PASSWORD), timeout=25)
            r.raise_for_status()
            post = r.json()
            logger.info(f"✅ Job posted: '{title}' → WP ID {post.get('id')}")
            return post.get("id"), post.get("link")
        except Exception as e:
            logger.error(f"Job post attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None, None


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
def run():
    require_wp_config()
    warm_up_session()
    ensure_job_type_terms()
    processed_ids = load_processed_ids()
    logger.info(f"📋 {len(processed_ids)} jobs already in tracker.")

    page_num = load_current_page()
    posted = skipped = failed = 0
    pages_done = 0
    consecutive_empty_pages = 0

    logger.info(f"🚀 Starting bounded run from page {page_num} up to END_PAGE={END_PAGE} "
                f"(will stop automatically after that page — no wraparound). "
                f"Press Ctrl+C to stop early — progress is saved after every page.")

    try:
        while page_num <= END_PAGE:
            job_urls = scrape_job_list_page(page_num)

            if not job_urls:
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 5:
                    logger.error(
                        f"⚠️ {consecutive_empty_pages} consecutive pages with 0 jobs found. "
                        f"This almost always means requests are being blocked/challenged "
                        f"(see warnings above) rather than the pages genuinely being empty. "
                        f"Stopping early so you don't burn through the whole page range for nothing — "
                        f"re-run once the block clears; progress has been saved."
                    )
                    save_current_page(page_num)
                    break
            else:
                consecutive_empty_pages = 0

            for j, job_url in enumerate(job_urls, start=1):
                logger.info(f"── Page {page_num} | Job {j}/{len(job_urls)}: {job_url}")

                if make_job_id(job_url) in processed_ids:
                    logger.info("⏭ SKIP (pre-check) — already processed, not re-fetching.")
                    skipped += 1
                    continue

                try:
                    subjobs = scrape_job_details(job_url)
                except Exception as e:
                    logger.error(f"Error scraping job {job_url}: {e}")
                    failed += 1
                    continue

                if not subjobs:
                    logger.info("⏭ SKIP — no parsable job blocks on this page.")
                    skipped += 1
                    continue

                for job in subjobs:
                    job_id = make_job_id(job["job_url"])

                    if job_id in processed_ids:
                        logger.info(f"⏭ SKIP — already processed: {job['job_title']}")
                        skipped += 1
                        continue

                    if not job["job_title"] or not job["job_description"]:
                        logger.info("⏭ SKIP — missing title/description.")
                        mark_processed(job_id, job["job_url"], "", "skipped_incomplete",
                                        page_num, j)
                        processed_ids.add(job_id)
                        skipped += 1
                        continue

                    if job["company_name"]:
                        save_company(job)

                    post_id, post_url = save_job(job)
                    if post_id:
                        mark_processed(job_id, job["job_url"], job["job_title"],
                                        f"posted|wp_id={post_id}|{post_url or ''}",
                                        page_num, j)
                        posted += 1
                        logger.info(f"✅ SUCCESS — '{job['job_title']}' → WP ID={post_id} 🔗 {post_url}")
                    else:
                        mark_processed(job_id, job["job_url"], job["job_title"], "wp_post_failed",
                                        page_num, j)
                        failed += 1
                        logger.info(f"❌ WordPress post failed: {job['job_title']}")

                    processed_ids.add(job_id)

                time.sleep(1)  # be polite to the source site between job detail fetches

            pages_done += 1
            save_current_page(page_num + 1)
            logger.info(f"📊 Running totals — pages done: {pages_done} | "
                        f"posted: {posted} | skipped: {skipped} | failed: {failed}")

            page_num += 1
            time.sleep(PAGE_FETCH_DELAY)  # pause between listing pages too

        logger.info(f"\n{'#'*60}")
        logger.info(f" ✅ FINISHED — reached END_PAGE={END_PAGE} ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
        logger.info(f" 📄 Pages scraped : {pages_done} (pages {START_PAGE}–{END_PAGE})")
        logger.info(f" ✅ Posted  : {posted}")
        logger.info(f" ⏭ Skipped : {skipped}")
        logger.info(f" ❌ Failed  : {failed}")
        logger.info(f"{'#'*60}")

    except KeyboardInterrupt:
        logger.info(f"\n{'#'*60}")
        logger.info(f" STOPPED BY USER ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
        logger.info(f" 📄 Last page completed : {page_num - 1}")
        logger.info(f" ▶️  Resumes next at     : {page_num} (re-run to continue)")
        logger.info(f" ✅ Posted  : {posted}")
        logger.info(f" ⏭ Skipped : {skipped}")
        logger.info(f" ❌ Failed  : {failed}")
        logger.info(f"{'#'*60}")


if __name__ == "__main__":
    logger.info(f"🚀 MyjobMag scraper — bounded run, pages {START_PAGE}–{END_PAGE} — starting…")
    run()
    logger.info("✅ Done.")
