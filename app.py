import os
import json
import time
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from difflib import SequenceMatcher
import google.generativeai as genai

app = Flask(__name__)
CORS(app)

RESULTS_FILE = "results.json"

# ── Gemini setup ──────────────────────────────────────────────────────────────
genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r") as f:
            return json.load(f)
    return []

def save_result(entry):
    results = load_results()
    existing = next((i for i, r in enumerate(results) if r.get("url") == entry.get("url")), None)
    if existing is not None:
        results[existing] = entry
    else:
        results.append(entry)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

TARGET_SLUGS = ["about", "contact", "services", "team", "company",
                "who-we-are", "what-we-do", "solutions", "products", "about-us"]

def slug_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def is_relevant_url(url):
    path = urlparse(url).path.lower().strip("/")
    for part in path.split("/"):
        for slug in TARGET_SLUGS:
            if slug_similarity(part, slug) > 0.68:
                return True
    return False

def fetch_html(url, timeout=12):
    candidates = [url]
    if url.startswith("https://"):
        candidates.append(url.replace("https://", "http://"))
    parsed = urlparse(url)
    if not parsed.netloc.startswith("www."):
        candidates.append(url.replace(parsed.netloc, "www." + parsed.netloc))
    for attempt in candidates:
        try:
            r = requests.get(attempt, headers=HEADERS, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r.text
        except Exception:
            pass
    return None

def get_sitemap_urls(base_url):
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        try:
            r = requests.get(urljoin(base_url, path), headers=HEADERS, timeout=8)
            if r.status_code == 200 and "<loc" in r.text:
                soup = BeautifulSoup(r.text, "lxml-xml")
                return [t.text.strip() for t in soup.find_all("loc")][:60]
        except Exception:
            pass
    return []

def extract_internal_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    domain = urlparse(base_url).netloc
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http"):
            if urlparse(href).netloc == domain:
                links.add(href)
        elif href.startswith("/") and not href.startswith("//"):
            links.add(urljoin(base_url, href))
    return list(links)

NOISE = ["nav", "footer", "header", "cookie", "banner",
         "menu", "sidebar", "advertisement", "popup", "modal"]

def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer",
                     "header", "aside", "iframe", "svg", "img", "button", "form"]):
        tag.decompose()
    for pat in NOISE:
        for el in soup.find_all(attrs={"class": re.compile(pat, re.I)}): el.decompose()
        for el in soup.find_all(attrs={"id":    re.compile(pat, re.I)}): el.decompose()
    lines = [l.strip() for l in soup.get_text("\n", strip=True).splitlines() if len(l.strip()) > 2]
    return "\n".join(lines)

def scrape_company(url):
    base = url.rstrip("/")
    parts = []
    home_html = fetch_html(base)
    if home_html:
        parts.append(f"=== HOME ({base}) ===\n{clean_html(home_html)}")
    all_links = get_sitemap_urls(base) or (home_html and extract_internal_links(home_html, base)) or []
    relevant  = [u for u in all_links if is_relevant_url(u)][:5]
    seen = {base}
    for page in relevant:
        if page in seen: continue
        seen.add(page)
        time.sleep(0.5)
        html = fetch_html(page)
        if html:
            cleaned = clean_html(html)
            if len(cleaned) > 100:
                parts.append(f"=== {page} ===\n{cleaned}")
        if len("\n\n".join(parts)) > 10000:
            break
    return "\n\n".join(parts)[:8000]

def ai_enrich(url, scraped_text, website_name=""):
    full_prompt = f"""You are a precise business intelligence extraction engine.
Extract ONLY information explicitly present in the provided text.
NEVER fabricate, infer, or hallucinate contact details, addresses, phone numbers, or email addresses.
Return ONLY valid JSON - no explanation, no markdown fences, no backticks.

Extract structured company information from this scraped website content.

URL: {url}
Website Name Hint: {website_name if website_name else "Not provided"}

SCRAPED CONTENT:
{scraped_text}

Return EXACTLY this JSON schema:
{{
  "website_name": "short brand/site name",
  "company_name": "full official company name",
  "address": "physical address if explicitly found, else empty string",
  "mobile_number": "phone number if explicitly found, else empty string",
  "mail": ["email1@domain.com"],
  "core_service": "primary service or product in 1-2 sentences",
  "target_customer": "who their primary customers are based on content",
  "probable_pain_point": "most likely business pain point their customers face",
  "outreach_opener": "2-3 sentence personalised sales message referencing the actual company name and specific services"
}}

Critical rules:
- mail MUST be a JSON array (use [] if no emails found)
- address and mobile_number must be empty string "" if not found in text
- Do NOT invent any contact details whatsoever
- outreach_opener must mention the real company name and real services"""

    response = gemini_model.generate_content(full_prompt)
    raw = response.text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    result = json.loads(raw)

    # Schema safety net
    defaults = {
        "website_name": "", "company_name": "",
        "address": "", "mobile_number": "", "mail": [],
        "core_service": "", "target_customer": "",
        "probable_pain_point": "", "outreach_opener": ""
    }
    for k, v in defaults.items():
        result.setdefault(k, v)
    if not isinstance(result["mail"], list):
        result["mail"] = [result["mail"]] if result["mail"] else []

    return result

# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.route("/enrich", methods=["POST"])
def enrich():
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "Missing 'url' field"}), 400

    url = data["url"].strip()
    website_name = data.get("website_name", "")
    if not url.startswith("http"):
        url = "https://" + url

    try:
        scraped = scrape_company(url)
        if not scraped or len(scraped) < 50:
            return jsonify({"error": "Could not scrape any content from the URL"}), 422

        result = ai_enrich(url, scraped, website_name)
        result["url"] = url
        if website_name:
            result["website_name"] = result.get("website_name") or website_name

        save_result(result)
        return jsonify(result), 200

    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI response parsing failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/results", methods=["GET"])
def results():
    return jsonify(load_results()), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
