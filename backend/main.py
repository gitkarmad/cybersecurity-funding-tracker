"""
Cybersecurity Research & Funding Tracker — FastAPI backend (v3).

Endpoints:
    GET /api/health
    GET /api/sources
    GET /api/stats
    GET /api/search?keyword=...&source=...&closing_soon=...
    GET /api/grants                (alias of /api/search)
    GET /api/opportunity/{id}

Serves the frontend from ../frontend at /.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cyber-tracker")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# ---------------------------------------------------------------------------
# Cybersecurity vocabulary — weighted
# ---------------------------------------------------------------------------
# strong = unambiguous cybersecurity concepts
# medium = clearly security but could appear in adjacent fields
# weak   = security abbreviations that need a co-occurring term
STRONG_TERMS: List[str] = [
    "cybersecurity", "cyber security", "cyber-security",
    "information security", "computer security", "network security",
    "zero trust", "zero trust architecture",
    "identity and access management", "identity security",
    "privileged access management",
    "cloud security", "infrastructure security",
    "endpoint security", "application security", "web application security",
    "mobile security", "software security", "secure software", "devsecops",
    "security operations center", "security operations",
    "threat detection", "threat intelligence", "threat hunting",
    "incident response", "cyber incident",
    "digital forensics", "cyber forensics",
    "malware", "ransomware", "phishing", "social engineering",
    "cyber attack", "cyber attacks", "cyber threat", "cyber threats",
    "advanced persistent threat",
    "vulnerability management", "vulnerability assessment",
    "penetration testing", "ethical hacking", "security testing",
    "adversary emulation", "red team", "blue team", "purple team",
    "cyber resilience", "cyber risk", "cyber risk management",
    "security architecture", "cyber governance",
    "ai security", "artificial intelligence security",
    "machine learning security", "adversarial machine learning", "secure ai",
    "blockchain security", "iot security", "ics security", "ot security",
    "critical infrastructure security",
    "cybercrime", "cyber investigation",
    "cybersecurity education", "cybersecurity training",
    "cybersecurity capacity building",
    "cyber range", "cybersecurity workforce",
    "information assurance", "data security",
    "access control", "authentication", "authorization",
    "cryptography", "encryption",
]

MEDIUM_TERMS: List[str] = [
    "security analytics", "security monitoring",
    "privacy and security", "privacy-preserving",
    "secure computing", "trusted computing",
    "secure communication", "secure communications",
    "digital forensics readiness",
    "national cyber", "cyber policy", "cyber law", "cyber diplomacy",
    "cyber capacity", "cyber defence", "cyber defense",
]

WEAK_TERMS: List[str] = [
    "iam", "soc", "pam", "apt", "zta", "siem", "ztna", "sase", "edr", "xdr",
]

ALL_TERMS: List[str] = STRONG_TERMS + MEDIUM_TERMS + WEAK_TERMS

# ---------------------------------------------------------------------------
# Funding-type vocabulary
# ---------------------------------------------------------------------------
FUNDING_TYPES = [
    "Research Grant",
    "PhD / Doctoral",
    "Fellowship / Scholarship",
    "International Joint Research",
    "Development Project",
    "Technical Assistance",
    "Capacity Building",
    "Government Programme",
    "Research Collaboration",
    "Cybersecurity Funding Opportunity",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _make_id(source: str, external_id: Optional[str], title: str, url: str) -> str:
    key = f"{source}|{external_id}" if external_id else f"{source}|{_normalize(title)}|{url}"
    return _sha1(key)[:16]

def _parse_iso_date(value: Any) -> Optional[str]:
    """Best-effort: return ISO date (YYYY-MM-DD) or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch millis or seconds
        try:
            ts = float(value)
            if ts > 1e12: ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    # Try common formats
    fmts = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y",
            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
            "%b %d, %Y", "%d %b %Y", "%B %d, %Y")
    # iso with timezone
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        pass
    for f in fmts:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except Exception:
            continue
    return None

def _days_left(deadline: Optional[str]) -> Optional[int]:
    if not deadline:
        return None
    try:
        d = datetime.fromisoformat(deadline).date()
    except Exception:
        return None
    return (d - datetime.now(timezone.utc).date()).days

def _is_open(deadline: Optional[str]) -> bool:
    dl = _days_left(deadline)
    return dl is None or dl >= 0

def _count_terms(text: str, terms: Iterable[str]) -> List[str]:
    if not text:
        return []
    lowered = _normalize(text)
    found: List[str] = []
    for t in terms:
        if " " in t or "-" in t:
            if t in lowered:
                found.append(t)
        else:
            if re.search(rf"\b{re.escape(t)}\b", lowered):
                found.append(t)
    return found

def _score(title: str, description: str) -> Tuple[int, List[str], Dict[str, int], bool]:
    """
    Weighted scoring:
        strong:  title +25, desc +6
        medium:  title +18, desc +4
        weak:    title +12, desc +2
    Cap at 100. Requires at least one STRONG term,
    or one MEDIUM + any other term, or two WEAK terms.
    Returns: (score, matched_terms, breakdown, is_cyber)
    """
    t = _normalize(title)
    d = _normalize(description)

    t_strong = _count_terms(t, STRONG_TERMS)
    d_strong = _count_terms(d, STRONG_TERMS)
    t_medium = _count_terms(t, MEDIUM_TERMS)
    d_medium = _count_terms(d, MEDIUM_TERMS)
    t_weak   = _count_terms(t, WEAK_TERMS)
    d_weak   = _count_terms(d, WEAK_TERMS)

    raw = (len(t_strong) * 25 + len(d_strong) * 6 +
           len(t_medium) * 18 + len(d_medium) * 4 +
           len(t_weak)   * 12 + len(d_weak)   * 2)
    score = min(raw, 100)

    matched: List[str] = []
    for group in (t_strong, d_strong, t_medium, d_medium, t_weak, d_weak):
        for term in group:
            if term not in matched:
                matched.append(term)

    is_cyber = bool(t_strong or d_strong) or \
               bool((t_medium or d_medium) and matched) or \
               (len(t_weak) + len(d_weak) >= 2)

    if not is_cyber:
        score = 0

    breakdown = {
        "title_strong": len(t_strong),
        "title_medium": len(t_medium),
        "title_weak":   len(t_weak),
        "desc_strong":  len(d_strong),
        "desc_medium":  len(d_medium),
        "desc_weak":    len(d_weak),
    }
    return score, matched, breakdown, is_cyber

def _classify_funding_type(text: str) -> str:
    t = _normalize(text)
    if any(k in t for k in ("phd", "doctoral", "doctorate", "ph.d")):
        return "PhD / Doctoral"
    if any(k in t for k in ("fellowship", "scholarship")):
        return "Fellowship / Scholarship"
    if any(k in t for k in ("joint research", "international joint", "bilateral research")):
        return "International Joint Research"
    if any(k in t for k in ("capacity building", "capacity-building", "training programme")):
        return "Capacity Building"
    if any(k in t for k in ("technical assistance", " advisory services", "advisory service")):
        return "Technical Assistance"
    if any(k in t for k in ("development project", "infrastructure project", "loan", "credit")):
        return "Development Project"
    if any(k in t for k in ("government programme", "national programme", "national program")):
        return "Government Programme"
    if any(k in t for k in ("research collaboration", "collaborative research")):
        return "Research Collaboration"
    if any(k in t for k in ("grant", "funding", "call for proposals", "call for proposal")):
        return "Research Grant"
    return "Cybersecurity Funding Opportunity"

def _bhutan_relevance(text: str) -> str:
    t = _normalize(text)
    if "bhutan" in t:
        return "Bhutan mentioned"
    if any(k in t for k in ("developing countr", "least developed", "ldc",
                            "low-income countr", "global south", "asean",
                            "south asia", "south asian")):
        return "Developing-country opportunity"
    if any(k in t for k in ("international applicant", "open to international",
                            "worldwide", "global applicant", "any country")):
        return "International applicants"
    if any(k in t for k in ("international partner", "partner institution",
                            "consortium", "co-applicant")):
        return "Potentially relevant"
    return "Eligibility requires verification"

def _is_official(url: str) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    official_suffixes = (
        ".gov", ".gov.uk", ".gov.au", ".govt.nz", ".gc.ca",
        ".europa.eu", ".eac.europa.eu", ".ac.uk", ".ac.jp",
        ".go.jp", ".int", ".un.org", ".worldbank.org",
        ".adb.org", ".jica.go.jp",
    )
    official_hosts = {
        "grants.gov", "nsf.gov", "nih.gov", "energy.gov", "nasa.gov",
        "arc.gov.au", "nhmrc.gov.au", "grants.gov.au",
        "nserc-crsng.gc.ca", "sshrc-crsh.gc.ca", "idrc.ca", "mitacs.ca",
        "dfg.de", "daad.de", "snf.ch", "nwo.nl",
        "ukri.org", "gtr.ukri.org",
        "royalsociety.org", "thebritishacademy.ac.uk",
        "jst.go.jp", "jsps.go.jp", "kakenhi.mext.go.jp",
        "ec.europa.eu", "erc.europa.eu",
        "marie-sklodowska-curie-actions.ec.europa.eu",
        "worldbank.org", "projects.worldbank.org",
        "adb.org",
    }
    return host.endswith(official_suffixes) or host in official_hosts

def _infer_country(text: str, fallback: str) -> str:
    t = _normalize(text)
    table = [
        ("bhutan", "Bhutan"),
        ("japan", "Japan"), ("tokyo", "Japan"),
        ("united kingdom", "United Kingdom"), ("uk ", "United Kingdom"), ("london", "United Kingdom"),
        ("united states", "United States"), ("usa", "United States"),
        ("european union", "European Union"), ("eu ", "European Union"),
        ("germany", "Germany"), ("deutschland", "Germany"),
        ("switzerland", "Switzerland"), ("netherlands", "Netherlands"),
        ("canada", "Canada"), ("australia", "Australia"),
        ("india", "India"), ("china", "China"), ("korea", "Republic of Korea"),
        ("singapore", "Singapore"), ("asean", "ASEAN"),
        ("global south", "Global South"), ("developing", "Developing Countries"),
    ]
    for key, val in table:
        if key in t:
            return val
    return fallback

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Opportunity:
    id: str
    title: str
    description: str
    summary: str
    source: str
    source_method: str
    country: str
    funding_type: str
    opportunity_type: str
    deadline: Optional[str]
    open_date: Optional[str]
    posted_date: Optional[str]
    days_left: Optional[int]
    is_open: bool
    award_amount: Optional[str]
    eligibility: Optional[str]
    tags: List[str]
    url: str
    cybersecurity_relevance: int
    matched_terms: List[str]
    relevance_breakdown: Dict[str, int]
    confidence: str
    bhutan_relevance: str
    official_source: bool
    last_checked: str
    external_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=6.0)
USER_AGENT = "CybersecurityFundingTracker/3.0 (+research)"

async def _get_json(url: str, *, params: Optional[dict] = None,
                    method: str = "GET", json_body: Optional[dict] = None
                    ) -> Tuple[Optional[Any], Optional[str], int]:
    """Return (payload, error, latency_ms)."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*;q=0.8"},
        ) as c:
            if method.upper() == "POST":
                r = await c.post(url, json=json_body or {}, params=params)
            else:
                r = await c.get(url, params=params)
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}", ms
        try:
            return r.json(), None, ms
        except Exception:
            return None, "invalid JSON", ms
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return None, f"{type(e).__name__}", ms

# ---------------------------------------------------------------------------
# Curated seed data (official programme landing pages — not scraped)
# ---------------------------------------------------------------------------
CURATED_SEEDS: Dict[str, List[Dict[str, Any]]] = {
    "JST": [
        {"title": "JST CREST — Cybersecurity and Trusted Computing",
         "description": "Team-based research on cybersecurity, trusted computing, zero trust architecture and secure AI. International collaborators welcome.",
         "url": "https://www.jst.go.jp/kisoken/crest/en/", "country": "Japan",
         "funding_type": "Research Grant"},
        {"title": "JST SATREPS — Cybersecurity for Critical Infrastructure",
         "description": "International joint research on cybersecurity for critical infrastructure, ICS/OT security and cyber resilience with developing-country partners.",
         "url": "https://www.jst.go.jp/global/english/", "country": "Japan / Developing Countries",
         "funding_type": "International Joint Research"},
    ],
    "SATREPS": [
        {"title": "SATREPS — Cyber Resilience and Critical Infrastructure Security",
         "description": "Joint research on cyber resilience, critical infrastructure security and ICT security with developing-country partners.",
         "url": "https://www.jst.go.jp/global/english/", "country": "Japan / Developing Countries",
         "funding_type": "International Joint Research"},
    ],
    "JSPS": [
        {"title": "JSPS KAKENHI — Information Security and Trust",
         "description": "Grant-in-Aid for Scientific Research covering information security, cryptography, network security and privacy.",
         "url": "https://www.jsps.go.jp/english/e-grants/", "country": "Japan",
         "funding_type": "Research Grant"},
    ],
    "KAKENHI": [
        {"title": "KAKENHI — Cybersecurity and Information Assurance",
         "description": "Grant-in-Aid supporting cybersecurity, information assurance, cryptography and secure computing research.",
         "url": "https://www.jsps.go.jp/english/e-grants/", "country": "Japan",
         "funding_type": "Research Grant"},
    ],
    "JICA": [
        {"title": "JICA Technical Cooperation — Cybersecurity Capacity Building",
         "description": "Technical cooperation on cybersecurity capacity building, cyber range development and digital government security.",
         "url": "https://www.jica.go.jp/english/our_work/types_of_assistance/tech/index.html",
         "country": "Japan / Developing Countries", "funding_type": "Technical Assistance"},
    ],
    "ADB": [
        {"title": "ADB Digital Government and Cybersecurity Programme",
         "description": "Technical assistance and development projects on digital government security, cybersecurity capacity building and cyber resilience in Asia.",
         "url": "https://www.adb.org/projects", "country": "Asia / Pacific",
         "funding_type": "Development Project"},
    ],
    "European Research Council": [
        {"title": "ERC Grants — Security and Privacy Research",
         "description": "Frontier research grants covering cryptography, security, privacy and adversarial machine learning.",
         "url": "https://erc.europa.eu/funding", "country": "European Union",
         "funding_type": "Research Grant"},
    ],
    "Marie Skłodowska-Curie Actions": [
        {"title": "MSCA Doctoral Networks — Cybersecurity",
         "description": "Doctoral Networks funding PhD positions in cybersecurity, zero trust, IAM and secure AI.",
         "url": "https://marie-sklodowska-curie-actions.ec.europa.eu/",
         "country": "European Union", "funding_type": "PhD / Doctoral"},
    ],
    "EPSRC": [
        {"title": "EPSRC — Cyber Security and Privacy Research",
         "description": "Funding for cybersecurity, privacy, cryptography and security of AI systems.",
         "url": "https://www.ukri.org/councils/epsrc/", "country": "United Kingdom",
         "funding_type": "Research Grant"},
    ],
    "British Academy": [
        {"title": "British Academy — Cybercrime and Digital Security",
         "description": "Fellowships and grants on cybercrime, digital security and cyber governance.",
         "url": "https://www.thebritishacademy.ac.uk/funding/", "country": "United Kingdom",
         "funding_type": "Fellowship / Scholarship"},
    ],
    "Royal Society": [
        {"title": "Royal Society — Cybersecurity and Cryptography",
         "description": "Grants supporting cryptography, cybersecurity and secure computing research.",
         "url": "https://royalsociety.org/grants-schemes-awards/grants/",
         "country": "United Kingdom", "funding_type": "Research Grant"},
    ],
    "U.S. Department of Energy": [
        {"title": "DOE — Cybersecurity for Energy Delivery Systems",
         "description": "Funding on cybersecurity for critical infrastructure, ICS/OT security and cyber resilience of energy systems.",
         "url": "https://www.energy.gov/ceser/cybersecurity-energy-delivery-systems",
         "country": "United States", "funding_type": "Research Grant"},
    ],
    "NASA": [
        {"title": "NASA — Space Cybersecurity and Mission Assurance",
         "description": "Research opportunities on space cybersecurity, secure communications and mission assurance.",
         "url": "https://www.nasa.gov/grants-and-funding/",
         "country": "United States", "funding_type": "Research Grant"},
    ],
    "Australian Research Council": [
        {"title": "ARC — Cybersecurity and Trusted Systems",
         "description": "Grants on cybersecurity, trusted systems, cryptography and secure AI.",
         "url": "https://www.arc.gov.au/funding-research", "country": "Australia",
         "funding_type": "Research Grant"},
    ],
    "NHMRC": [
        {"title": "NHMRC — Health Data Security and Privacy",
         "description": "Funding on security and privacy of health information and health system cyber resilience.",
         "url": "https://www.nhmrc.gov.au/funding", "country": "Australia",
         "funding_type": "Research Grant"},
    ],
    "Australian Government Grants": [
        {"title": "Australian Government Grants — Cyber Security",
         "description": "Grant opportunities on cybersecurity, cyber resilience and critical infrastructure security.",
         "url": "https://www.grants.gov.au/", "country": "Australia",
         "funding_type": "Research Grant"},
    ],
    "NSERC": [
        {"title": "NSERC — Cybersecurity and Privacy",
         "description": "Discovery and alliance grants on cybersecurity, cryptography and privacy.",
         "url": "https://www.nserc-crsng.gc.ca/index_eng.asp", "country": "Canada",
         "funding_type": "Research Grant"},
    ],
    "SSHRC": [
        {"title": "SSHRC — Cybercrime, Governance and Society",
         "description": "Funding on cybercrime, cyber governance and social dimensions of cybersecurity.",
         "url": "https://www.sshrc-crsh.gc.ca/funding-financement/index-eng.aspx",
         "country": "Canada", "funding_type": "Research Grant"},
    ],
    "IDRC": [
        {"title": "IDRC — Cybersecurity Capacity in the Global South",
         "description": "Funding on cybersecurity capacity building, digital rights and cyber governance in developing countries.",
         "url": "https://www.idrc.ca/en/funding", "country": "Canada / Global South",
         "funding_type": "Capacity Building"},
    ],
    "Mitacs": [
        {"title": "Mitacs — Cybersecurity Internships and Fellowships",
         "description": "Internships and fellowships on cybersecurity, secure software and cyber resilience.",
         "url": "https://www.mitacs.ca/our-programs/", "country": "Canada",
         "funding_type": "Fellowship / Scholarship"},
    ],
    "DFG": [
        {"title": "DFG — Cybersecurity and Privacy Research Units",
         "description": "Research units and grants on cybersecurity, cryptography and privacy.",
         "url": "https://www.dfg.de/en/research_funding/index.html", "country": "Germany",
         "funding_type": "Research Grant"},
    ],
    "DAAD": [
        {"title": "DAAD — Scholarships in Cybersecurity and IT Security",
         "description": "Scholarships and PhD funding in cybersecurity, IT security and secure software engineering.",
         "url": "https://www.daad.de/en/study-and-research-in-germany/scholarships/",
         "country": "Germany", "funding_type": "Fellowship / Scholarship"},
    ],
    "SNSF": [
        {"title": "SNSF — Cybersecurity and Trusted Computing",
         "description": "Grants on cybersecurity, trusted computing and privacy-enhancing technologies.",
         "url": "https://www.snf.ch/en/funding/Pages/default.aspx",
         "country": "Switzerland", "funding_type": "Research Grant"},
    ],
    "NWO": [
        {"title": "NWO — Cybersecurity and Digital Resilience",
         "description": "Funding on cybersecurity, digital resilience and secure AI.",
         "url": "https://www.nwo.nl/en/funding", "country": "Netherlands",
         "funding_type": "Research Grant"},
    ],
}

# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
AdapterResult = Tuple[List[Dict[str, Any]], Optional[str], int]  # rows, error, latency_ms

async def _adapter_grants_gov(keyword: str) -> AdapterResult:
    url = "https://api.grants.gov/v1/api/search2"
    body = {"keyword": keyword or "cybersecurity",
            "oppStatuses": "forecasted|posted", "rows": 25}
    data, err, ms = await _get_json(url, method="POST", json_body=body)
    if err or not data:
        return [], err or "no data", ms
    hits = (data.get("data") or {}).get("oppHits") or []
    out = []
    for h in hits:
        out.append({
            "title": h.get("title") or "",
            "description": h.get("oppDescription") or h.get("synopsis") or "",
            "source": "Grants.gov",
            "country": "United States",
            "url": f"https://www.grants.gov/search-results-detail/{h.get('id')}",
            "external_id": str(h.get("id") or h.get("number") or ""),
            "deadline": _parse_iso_date(h.get("closeDate")),
            "open_date": _parse_iso_date(h.get("openDate")),
            "posted_date": _parse_iso_date(h.get("postDate") or h.get("postingDate")),
            "award_amount": h.get("awardCeiling") or h.get("estimatedFunding"),
            "eligibility": h.get("applicantEligibilityDesc"),
        })
    return out, None, ms

async def _adapter_nsf(keyword: str) -> AdapterResult:
    url = "https://api.nsf.gov/services/v1/awards.json"
    params = {"keyword": keyword or "cybersecurity",
              "printFields": "id,title,abstractText,startDate,expDate,fundsObligatedAmt,awardeeName"}
    data, err, ms = await _get_json(url, params=params)
    if err or not data:
        return [], err or "no data", ms
    awards = (data.get("response") or {}).get("award") or []
    out = []
    for a in awards:
        out.append({
            "title": a.get("title") or "",
            "description": a.get("abstractText") or "",
            "source": "National Science Foundation",
            "country": "United States",
            "url": f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={a.get('id')}",
            "external_id": str(a.get("id") or ""),
            "deadline": _parse_iso_date(a.get("expDate")),
            "open_date": _parse_iso_date(a.get("startDate")),
            "award_amount": a.get("fundsObligatedAmt"),
        })
    return out, None, ms

async def _adapter_nih(keyword: str) -> AdapterResult:
    url = "https://api.reporter.nih.gov/v2/projects/search"
    body = {
        "criteria": {"advanced_text_search": {
            "operator": "and",
            "search_field": "projecttitle,abstracttext",
            "search_text": keyword or "cybersecurity"}},
        "include_fields": ["ProjectNum", "ProjectTitle", "AbstractText",
                           "ProjectStartDate", "ProjectEndDate", "AwardAmount"],
        "offset": 0, "limit": 25,
    }
    data, err, ms = await _get_json(url, method="POST", json_body=body)
    if err or not data:
        return [], err or "no data", ms
    results = data.get("results") or []
    out = []
    for p in results:
        out.append({
            "title": p.get("project_title") or "",
            "description": p.get("abstract_text") or "",
            "source": "National Institutes of Health",
            "country": "United States",
            "url": f"https://reporter.nih.gov/project-details/{p.get('appl_id') or ''}",
            "external_id": str(p.get("project_num") or ""),
            "deadline": _parse_iso_date(p.get("project_end_date")),
            "open_date": _parse_iso_date(p.get("project_start_date")),
            "award_amount": p.get("award_amount"),
        })
    return out, None, ms

async def _adapter_ukri(keyword: str) -> AdapterResult:
    url = "https://gtr.ukri.org/api/search"
    params = {"q": keyword or "cybersecurity", "page": 1, "size": 25}
    data, err, ms = await _get_json(url, params=params)
    if err or not data:
        return [], err or "no data", ms
    results = data.get("results") or data.get("projects") or []
    out = []
    for p in results:
        pid = p.get("id") or p.get("projectId")
        out.append({
            "title": p.get("title") or "",
            "description": p.get("abstractText") or p.get("description") or "",
            "source": "UKRI",
            "country": "United Kingdom",
            "url": f"https://gtr.ukri.org/projects?ref={pid}",
            "external_id": str(pid or ""),
            "deadline": None,
        })
    return out, None, ms

async def _adapter_worldbank(keyword: str) -> AdapterResult:
    url = "https://search.worldbank.org/api/v2/projects"
    params = {
        "format": "json",
        "qterm": keyword or "cybersecurity",
        "rows": 25,
        "fl": "id,project_name,project_abstract,countryname,boardapprovaldate,closingdate,url",
    }
    data, err, ms = await _get_json(url, params=params)
    if err or not data:
        return [], err or "no data", ms
    projects = data.get("projects") or {}
    items = list(projects.values()) if isinstance(projects, dict) else \
            projects if isinstance(projects, list) else []
    out = []
    for p in items:
        if not isinstance(p, dict):
            continue
        abstract = p.get("project_abstract")
        if isinstance(abstract, dict):
            abstract = abstract.get("cdata") or ""
        out.append({
            "title": p.get("project_name") or "",
            "description": abstract or "",
            "source": "World Bank",
            "country": p.get("countryname") or "Global",
            "url": p.get("url") or "https://projects.worldbank.org/",
            "external_id": str(p.get("id") or ""),
            "deadline": _parse_iso_date(p.get("closingdate")),
            "open_date": _parse_iso_date(p.get("boardapprovaldate")),
        })
    return out, None, ms

async def _adapter_ec(keyword: str) -> AdapterResult:
    url = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    params = {"apiKey": "SEDIA", "text": keyword or "cybersecurity"}
    body = {
        "query": {"bool": {"must": [
            {"terms": {"type": ["1"]}},
            {"match": {"text": keyword or "cybersecurity"}},
        ]}},
        "pageSize": 25, "pageNumber": 1,
        "sort": {"field": "sortStatus", "order": "DESC"},
    }
    data, err, ms = await _get_json(url, params=params, method="POST", json_body=body)
    if err or not data:
        return [], err or "no data", ms
    results = data.get("results") or []
    out = []
    for item in results:
        meta = item.get("metadata") or {}
        def first(k: str) -> str:
            v = meta.get(k)
            if isinstance(v, list) and v:
                return str(v[0])
            return str(v) if v else ""
        ident = first("identifier") or item.get("id") or ""
        out.append({
            "title": first("title") or "",
            "description": first("description") or first("objective") or "",
            "source": "European Commission",
            "country": "European Union",
            "url": f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{ident}",
            "external_id": ident,
            "deadline": _parse_iso_date(first("deadlineDate")),
            "open_date": _parse_iso_date(first("startDate")),
        })
    return out, None, ms

async def _adapter_arxiv(keyword: str) -> AdapterResult:
    """arXiv (real, public API). Used to surface recent cyber research."""
    url = "http://export.arxiv.org/api/query"
    q = f'all:"{keyword or "cybersecurity"}"'
    params = {"search_query": q, "start": 0, "max_results": 15}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(url, params=params)
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}", ms
        text = r.text
    except Exception as e:
        return [], f"{type(e).__name__}", int((time.perf_counter() - t0) * 1000)

    # Light regex parsing (avoids extra XML dep)
    entries = re.findall(r"<entry>(.*?)</entry>", text, flags=re.S)
    out = []
    for e in entries[:15]:
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", e, flags=re.S)
            if not m:
                return ""
            return re.sub(r"\s+", " ", m.group(1)).strip()
        title = grab("title")
        summary = grab("summary")
        link_m = re.search(r'<id>(.*?)</id>', e, flags=re.S)
        link = link_m.group(1).strip() if link_m else ""
        pub = grab("published")
        out.append({
            "title": title,
            "description": summary,
            "source": "arXiv (cyber preprints)",
            "country": "International",
            "url": link,
            "external_id": link,
            "deadline": None,
            "posted_date": _parse_iso_date(pub),
        })
    return out, None, ms

def _curated_source(source_name: str) -> Callable[[str], Any]:
    async def _adapter(keyword: str) -> AdapterResult:
        t0 = time.perf_counter()
        seeds = CURATED_SEEDS.get(source_name, [])
        out = []
        for s in seeds:
            out.append({
                "title": s["title"],
                "description": s["description"],
                "source": source_name,
                "country": s.get("country", "International"),
                "url": s["url"],
                "external_id": None,
                "deadline": None,
                "funding_type": s.get("funding_type"),
            })
        return out, None, int((time.perf_counter() - t0) * 1000)
    return _adapter

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------
@dataclass
class SourceDef:
    name: str
    country: str
    method: str
    base_url: str
    adapter: Callable[[str], Any]

SOURCES: List[SourceDef] = [
    # Real APIs
    SourceDef("World Bank", "Global", "API", "https://projects.worldbank.org/", _adapter_worldbank),
    SourceDef("European Commission", "European Union", "API", "https://ec.europa.eu/", _adapter_ec),
    SourceDef("UKRI", "United Kingdom", "API", "https://www.ukri.org/", _adapter_ukri),
    SourceDef("Grants.gov", "United States", "API", "https://www.grants.gov/", _adapter_grants_gov),
    SourceDef("National Science Foundation", "United States", "API", "https://www.nsf.gov/", _adapter_nsf),
    SourceDef("National Institutes of Health", "United States", "API", "https://www.nih.gov/", _adapter_nih),
    SourceDef("arXiv (cyber preprints)", "International", "API", "https://arxiv.org/", _adapter_arxiv),

    # Official webpage — curated seeds
    SourceDef("Asian Development Bank", "Asia / Pacific", "official webpage", "https://www.adb.org/projects", _curated_source("ADB")),
    SourceDef("JICA", "Japan / Global", "official webpage", "https://www.jica.go.jp/english/", _curated_source("JICA")),
    SourceDef("European Research Council", "European Union", "official webpage", "https://erc.europa.eu/", _curated_source("European Research Council")),
    SourceDef("Marie Skłodowska-Curie Actions", "European Union", "official webpage", "https://marie-sklodowska-curie-actions.ec.europa.eu/", _curated_source("Marie Skłodowska-Curie Actions")),
    SourceDef("JST", "Japan", "official webpage", "https://www.jst.go.jp/", _curated_source("JST")),
    SourceDef("SATREPS", "Japan / Global", "official webpage", "https://www.jst.go.jp/global/english/", _curated_source("SATREPS")),
    SourceDef("JSPS", "Japan", "official webpage", "https://www.jsps.go.jp/english/", _curated_source("JSPS")),
    SourceDef("KAKENHI", "Japan", "official webpage", "https://www.jsps.go.jp/english/e-grants/", _curated_source("KAKENHI")),
    SourceDef("EPSRC", "United Kingdom", "official webpage", "https://www.ukri.org/councils/epsrc/", _curated_source("EPSRC")),
    SourceDef("British Academy", "United Kingdom", "official webpage", "https://www.thebritishacademy.ac.uk/", _curated_source("British Academy")),
    SourceDef("Royal Society", "United Kingdom", "official webpage", "https://royalsociety.org/", _curated_source("Royal Society")),
    SourceDef("U.S. Department of Energy", "United States", "official webpage", "https://www.energy.gov/", _curated_source("U.S. Department of Energy")),
    SourceDef("NASA", "United States", "official webpage", "https://www.nasa.gov/", _curated_source("NASA")),
    SourceDef("Australian Research Council", "Australia", "official webpage", "https://www.arc.gov.au/", _curated_source("Australian Research Council")),
    SourceDef("NHMRC", "Australia", "official webpage", "https://www.nhmrc.gov.au/", _curated_source("NHMRC")),
    SourceDef("Australian Government Grants", "Australia", "official webpage", "https://www.grants.gov.au/", _curated_source("Australian Government Grants")),
    SourceDef("NSERC", "Canada", "official webpage", "https://www.nserc-crsng.gc.ca/", _curated_source("NSERC")),
    SourceDef("SSHRC", "Canada", "official webpage", "https://www.sshrc-crsh.gc.ca/", _curated_source("SSHRC")),
    SourceDef("IDRC", "Canada / Global South", "official webpage", "https://www.idrc.ca/", _curated_source("IDRC")),
    SourceDef("Mitacs", "Canada", "official webpage", "https://www.mitacs.ca/", _curated_source("Mitacs")),
    SourceDef("DFG", "Germany", "official webpage", "https://www.dfg.de/", _curated_source("DFG")),
    SourceDef("DAAD", "Germany", "official webpage", "https://www.daad.de/", _curated_source("DAAD")),
    SourceDef("SNSF", "Switzerland", "official webpage", "https://www.snf.ch/", _curated_source("SNSF")),
    SourceDef("NWO", "Netherlands", "official webpage", "https://www.nwo.nl/", _curated_source("NWO")),
]

SOURCE_STATUS: Dict[str, Dict[str, Any]] = {
    s.name: {"status": "unknown", "last_checked": None, "count": 0,
             "error": None, "latency_ms": None}
    for s in SOURCES
}

# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------
def _summarize(description: str, maxlen: int = 220) -> str:
    if not description:
        return ""
    d = re.sub(r"\s+", " ", description).strip()
    return d if len(d) <= maxlen else d[: maxlen - 1].rstrip() + "…"

def _build_opportunity(src: SourceDef, item: Dict[str, Any]) -> Optional[Opportunity]:
    try:
        title = item.get("title") or ""
        description = item.get("description") or ""
        url = item.get("url") or ""
        score, matched, breakdown, is_cyber = _score(title, description)
        if not is_cyber:
            return None

        funding_type = item.get("funding_type") or _classify_funding_type(f"{title} {description}")
        country = item.get("country") or _infer_country(f"{title} {description}", src.country)
        deadline = _parse_iso_date(item.get("deadline"))
        open_date = _parse_iso_date(item.get("open_date"))
        posted_date = _parse_iso_date(item.get("posted_date"))
        dleft = _days_left(deadline)
        tags = sorted(set(matched))[:8]

        confidence = "high" if score >= 60 else "medium" if score >= 30 else "low"

        return Opportunity(
            id=_make_id(src.name, item.get("external_id"), title, url),
            title=title.strip() or "(untitled)",
            description=description.strip()[:1200],
            summary=_summarize(description),
            source=src.name,
            source_method=src.method,
            country=country,
            funding_type=funding_type,
            opportunity_type=funding_type,
            deadline=deadline,
            open_date=open_date,
            posted_date=posted_date,
            days_left=dleft,
            is_open=(dleft is None or dleft >= 0),
            award_amount=str(item.get("award_amount")) if item.get("award_amount") else None,
            eligibility=item.get("eligibility"),
            tags=tags,
            url=url,
            cybersecurity_relevance=score,
            matched_terms=matched,
            relevance_breakdown=breakdown,
            confidence=confidence,
            bhutan_relevance=_bhutan_relevance(f"{title} {description}"),
            official_source=_is_official(url),
            last_checked=_now_iso(),
            external_id=item.get("external_id"),
        )
    except Exception as e:
        log.warning("Failed to build opportunity from %s: %s", src.name, e)
        return None

async def _run_source(src: SourceDef, keyword: str) -> Tuple[str, List[Opportunity], Optional[str], int]:
    t0 = time.perf_counter()
    try:
        raw, err, ms = await src.adapter(keyword)
    except Exception as e:
        log.exception("Adapter crashed for %s", src.name)
        raw, err, ms = [], f"{type(e).__name__}", int((time.perf_counter() - t0) * 1000)

    opps: List[Opportunity] = []
    for item in raw or []:
        o = _build_opportunity(src, item)
        if o is not None:
            opps.append(o)

    SOURCE_STATUS[src.name] = {
        "status": "online" if err is None else "error",
        "last_checked": _now_iso(),
        "count": len(opps),
        "error": err,
        "latency_ms": ms,
    }
    return src.name, opps, err, ms

def _dedupe(opps: Iterable[Opportunity]) -> List[Opportunity]:
    seen: set = set()
    out: List[Opportunity] = []
    for o in opps:
        key = f"{o.source}|{o.external_id}" if o.external_id else \
              f"{o.source}|{_normalize(o.title)}|{o.url}"
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Cybersecurity Research & Funding Tracker", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_opportunity_cache: Dict[str, Opportunity] = {}

# ------- endpoints -------
@app.get("/api/health")
async def health() -> Dict[str, Any]:
    online = sum(1 for s in SOURCE_STATUS.values() if s["status"] == "online")
    return {"status": "ok", "time": _now_iso(),
            "sources": len(SOURCES), "online": online}

@app.get("/api/sources")
async def sources() -> Dict[str, Any]:
    items = []
    for s in SOURCES:
        st = SOURCE_STATUS.get(s.name, {})
        items.append({
            "name": s.name,
            "country": s.country,
            "method": s.method,
            "base_url": s.base_url,
            "status": st.get("status", "unknown"),
            "last_checked": st.get("last_checked"),
            "count": st.get("count", 0),
            "error": st.get("error"),
            "latency_ms": st.get("latency_ms"),
        })
    return {"count": len(items), "sources": items}

@app.get("/api/stats")
async def stats() -> Dict[str, Any]:
    total = sum(st.get("count", 0) for st in SOURCE_STATUS.values())
    online = sum(1 for st in SOURCE_STATUS.values() if st["status"] == "online")
    return {
        "sources_total": len(SOURCES),
        "sources_online": online,
        "opportunities_cached": total,
        "generated_at": _now_iso(),
    }

@app.get("/api/opportunity/{opp_id}")
async def get_opportunity(opp_id: str) -> JSONResponse:
    o = _opportunity_cache.get(opp_id)
    if not o:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return JSONResponse(o.to_dict())

async def _search_impl(keyword: str, source_filter: Optional[str],
                       closing_soon: bool = False) -> Dict[str, Any]:
    keyword = (keyword or "cybersecurity").strip()
    selected: List[SourceDef] = SOURCES
    if source_filter:
        wanted = source_filter.strip().lower()
        selected = [s for s in SOURCES if wanted in s.name.lower()]

    results = await asyncio.gather(
        *[_run_source(s, keyword) for s in selected],
        return_exceptions=False,
    )

    all_opps: List[Opportunity] = []
    errors: List[Dict[str, str]] = []
    for name, opps, err, _ms in results:
        all_opps.extend(opps)
        if err:
            errors.append({"source": name, "error": err})

    deduped = _dedupe(all_opps)

    final: List[Opportunity] = []
    for o in deduped:
        if o.cybersecurity_relevance <= 0 or not o.matched_terms:
            continue
        if closing_soon:
            if o.days_left is None or not (0 <= o.days_left <= 30):
                continue
        final.append(o)
        _opportunity_cache[o.id] = o

    final.sort(key=lambda o: (-o.cybersecurity_relevance,
                              o.days_left if o.days_left is not None else 10**9,
                              o.source, o.title))

    return {
        "keyword": keyword,
        "count": len(final),
        "sources_queried": len(selected),
        "sources_total": len(SOURCES),
        "results": [o.to_dict() for o in final],
        "errors": errors,
        "generated_at": _now_iso(),
    }

@app.get("/api/search")
async def search(
    keyword: str = Query("cybersecurity"),
    source: Optional[str] = Query(None),
    closing_soon: bool = Query(False),
) -> JSONResponse:
    try:
        return JSONResponse(await _search_impl(keyword, source, closing_soon))
    except Exception as e:
        log.exception("search failed")
        return JSONResponse(
            {"keyword": keyword, "count": 0, "results": [],
             "errors": [{"source": "*", "error": type(e).__name__}],
             "generated_at": _now_iso()},
            status_code=200,
        )

@app.get("/api/grants")
async def grants(keyword: str = Query("cybersecurity"),
                 source: Optional[str] = Query(None),
                 closing_soon: bool = Query(False)) -> JSONResponse:
    return JSONResponse(await _search_impl(keyword, source, closing_soon))

# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        candidate = FRONTEND_DIR / path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
else:
    @app.get("/")
    async def root_missing() -> JSONResponse:
        return JSONResponse({"error": "Frontend directory not found",
                             "expected": str(FRONTEND_DIR)}, status_code=500)