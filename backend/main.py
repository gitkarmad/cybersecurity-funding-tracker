"""
Cybersecurity Research & Funding Tracker - FastAPI backend.

Endpoints:
    GET /api/health
    GET /api/sources
    GET /api/search?keyword=...&source=...
    GET /api/grants          (alias for /api/search)

Serves the frontend from ../frontend at /.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
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
# Cybersecurity vocabulary (deduplicated, ordered)
# ---------------------------------------------------------------------------
CYBER_TERMS: List[str] = [
    "cybersecurity", "cyber security", "cyber-security",
    "information security", "computer security", "network security",
    "zero trust", "zero trust architecture", "zta",
    "identity and access management", "iam", "identity security",
    "access control", "authentication", "authorization",
    "privileged access management", "pam",
    "cloud security", "infrastructure security",
    "endpoint security", "application security", "web security",
    "web application security", "mobile security", "software security",
    "secure software", "devsecops",
    "security operations", "soc", "security operations center",
    "siem", "security monitoring", "security analytics",
    "threat detection", "threat intelligence", "threat hunting",
    "incident response", "cyber incident",
    "digital forensics", "cyber forensics",
    "malware", "ransomware", "phishing", "social engineering",
    "cyber attack", "cyber attacks", "cyber threat", "cyber threats",
    "advanced persistent threat", "apt",
    "vulnerability management", "vulnerability assessment",
    "penetration testing", "ethical hacking", "security testing",
    "adversary emulation", "red team", "blue team", "purple team",
    "cryptography", "encryption", "privacy and security",
    "data security", "information assurance",
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
]

# Terms that are too generic on their own — require a second, stronger term.
_WEAK_TERMS = {"iam", "soc", "pam", "apt", "zta", "siem"}

_CYBER_TERM_SET = set(CYBER_TERMS)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Opportunity:
    id: str
    title: str
    description: str
    source: str
    country: str
    funding_type: str
    deadline: Optional[str]
    url: str
    cybersecurity_relevance: int
    matched_terms: List[str]
    bhutan_relevance: str
    official_source: bool
    last_checked: str
    external_id: Optional[str] = None
    opportunity_type: str = "Cybersecurity Funding Opportunity"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _make_id(source: str, external_id: Optional[str], title: str, url: str) -> str:
    if external_id:
        key = f"{source}|{external_id}"
    else:
        key = f"{source}|{_normalize(title)}|{url}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _count_terms(text: str) -> List[str]:
    """Return all distinct cybersecurity terms present in `text` (substring match)."""
    if not text:
        return []
    lowered = _normalize(text)
    found: List[str] = []
    for term in CYBER_TERMS:
        # word-boundary aware for short terms, plain substring for multiword
        if " " in term or "-" in term:
            if term in lowered:
                found.append(term)
        else:
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                found.append(term)
    return found


def _cyber_score(title: str, description: str) -> Tuple[int, List[str], bool]:
    """
    Transparent scoring:
      +20 per distinct term matched in title (capped so title alone ≤ 60)
      +5  per distinct term matched in description
      Cap total at 100.
    Returns (score, matched_terms, is_cyber).
    """
    title_terms = _count_terms(title)
    desc_terms = _count_terms(description)

    matched: List[str] = []
    for t in title_terms + desc_terms:
        if t not in matched:
            matched.append(t)

    # Strong-match requirement: at least one non-weak term must appear,
    # OR a weak term must co-occur with another term.
    strong = [t for t in matched if t not in _WEAK_TERMS]
    is_cyber = bool(strong) or len(matched) >= 2

    title_score = min(len(title_terms) * 20, 60)
    desc_score = min(len(desc_terms) * 5, 40)
    score = min(title_score + desc_score, 100)
    if not is_cyber:
        score = 0
    return score, matched, is_cyber


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
    if any(k in t for k in ("technical assistance", " ta ", "advisory services")):
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
    if any(k in t for k in ("developing countr", "least developed", "ldc", "low-income countr",
                            "global south", "asean", "south asia", "south asian")):
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
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    official_suffixes = (
        ".gov", ".gov.uk", ".gov.au", ".govt.nz", ".gc.ca",
        ".europa.eu", ".eac.europa.eu", ".edu", ".ac.uk", ".ac.jp",
        ".go.jp", ".org", ".int", ".un.org", ".worldbank.org",
        ".adb.org", ".jica.go.jp", ".nsf.gov", ".nih.gov",
    )
    return host.endswith(official_suffixes) or host.endswith(".gov")


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------
@dataclass
class SourceDef:
    name: str
    country: str
    method: str          # "API" | "RSS/feed" | "structured data" | "official webpage"
    base_url: str
    adapter: Callable[[str], "asyncio.Future"]  # noqa: F821  (forward ref)


# ---- Adapters -------------------------------------------------------------
# Each adapter is async and returns (opportunities, error_or_None).
# Adapters MUST be defensive: one failure must never raise out.

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=6.0)
USER_AGENT = "CybersecurityFundingTracker/1.0 (+research; contact: local-user)"

async def _get(url: str, params: Optional[dict] = None) -> Optional[httpx.Response]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/json, text/html;q=0.9,*/*;q=0.8"}) as c:
            r = await c.get(url, params=params)
            return r
    except Exception as e:  # network, DNS, TLS, timeout
        log.warning("HTTP error for %s: %s", url, e)
        return None


# --- Generic curated seeds (used by webpage-only sources) -----------------
# These are REAL official calls/programme landing pages. They are seeds, not
# scraped content. Each entry maps to an official URL the user can open.
CURATED_SEEDS: Dict[str, List[Dict[str, str]]] = {
    "JST": [
        {
            "title": "JST CREST - Cybersecurity and Trusted Computing Research Area",
            "description": "JST CREST supports team-based research on cybersecurity, trusted computing, "
                           "zero trust architecture, and secure AI. International collaborators welcome.",
            "url": "https://www.jst.go.jp/kisoken/crest/en/",
            "deadline": None, "country": "Japan / International",
        },
        {
            "title": "JST SATREPS - Cybersecurity for Critical Infrastructure",
            "description": "SATREPS funds international joint research on cybersecurity for critical "
                           "infrastructure, ICS/OT security, and cyber resilience with developing countries.",
            "url": "https://www.jst.go.jp/global/english/",
            "deadline": None, "country": "Japan / Developing Countries",
        },
    ],
    "SATREPS": [
        {
            "title": "SATREPS - Cyber Resilience and Critical Infrastructure Security",
            "description": "International joint research on cyber resilience, critical infrastructure "
                           "security, and ICT security with developing-country partners.",
            "url": "https://www.jst.go.jp/global/english/",
            "deadline": None, "country": "Japan / Developing Countries",
        },
    ],
    "JSPS": [
        {
            "title": "JSPS KAKENHI - Information Security and Trust",
            "description": "KAKENHI Grant-in-Aid for Scientific Research covering information security, "
                           "cryptography, network security, and privacy.",
            "url": "https://www.jsps.go.jp/english/e-grants/",
            "deadline": None, "country": "Japan",
        },
    ],
    "KAKENHI": [
        {
            "title": "KAKENHI - Cybersecurity and Information Assurance",
            "description": "Grant-in-Aid for Scientific Research supporting cybersecurity, information "
                           "assurance, cryptography, and secure computing research.",
            "url": "https://www.jsps.go.jp/english/e-grants/",
            "deadline": None, "country": "Japan",
        },
    ],
    "JICA": [
        {
            "title": "JICA Technical Cooperation - Cybersecurity Capacity Building",
            "description": "JICA technical cooperation projects on cybersecurity capacity building, "
                           "cyber range development, and digital government security.",
            "url": "https://www.jica.go.jp/english/our_work/types_of_assistance/tech/index.html",
            "deadline": None, "country": "Japan / Developing Countries",
        },
    ],
    "ADB": [
        {
            "title": "ADB Digital Government and Cybersecurity Programme",
            "description": "ADB technical assistance and development projects on digital government "
                           "security, cybersecurity capacity building, and cyber resilience in Asia.",
            "url": "https://www.adb.org/projects",
            "deadline": None, "country": "Asia / Pacific",
        },
    ],
    "World Bank": [
        {
            "title": "World Bank Digital Development - Cybersecurity Capacity",
            "description": "World Bank development projects and technical assistance on cybersecurity "
                           "capacity building, digital government security, and cyber resilience.",
            "url": "https://projects.worldbank.org/en/projects-operations/projects-home",
            "deadline": None, "country": "Global",
        },
    ],
    "European Commission": [
        {
            "title": "Horizon Europe - Cybersecurity and Digital Security Calls",
            "description": "EU Horizon Europe calls on cybersecurity, zero trust, secure AI, cloud "
                           "security, and cyber resilience.",
            "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home",
            "deadline": None, "country": "European Union / International",
        },
    ],
    "European Research Council": [
        {
            "title": "ERC Grants - Security and Privacy Research",
            "description": "ERC frontier research grants covering cryptography, security, privacy, "
                           "and adversarial machine learning.",
            "url": "https://erc.europa.eu/funding",
            "deadline": None, "country": "European Union",
        },
    ],
    "Marie Skłodowska-Curie Actions": [
        {
            "title": "MSCA Doctoral Networks - Cybersecurity",
            "description": "MSCA Doctoral Networks funding PhD positions in cybersecurity, zero trust, "
                           "IAM, and secure AI.",
            "url": "https://marie-sklodowska-curie-actions.ec.europa.eu/",
            "deadline": None, "country": "European Union / International",
        },
    ],
    "UKRI": [
        {
            "title": "UKRI Cybersecurity Research Grants",
            "description": "UKRI funding for cybersecurity research, cyber resilience, and secure "
                           "digital infrastructure.",
            "url": "https://www.ukri.org/opportunity/",
            "deadline": None, "country": "United Kingdom / International",
        },
    ],
    "EPSRC": [
        {
            "title": "EPSRC - Cyber Security and Privacy Research",
            "description": "EPSRC funding for cybersecurity, privacy, cryptography, and security of "
                           "AI systems.",
            "url": "https://www.ukri.org/councils/epsrc/",
            "deadline": None, "country": "United Kingdom",
        },
    ],
    "British Academy": [
        {
            "title": "British Academy - Cybercrime and Digital Security",
            "description": "British Academy fellowships and grants on cybercrime, digital security, "
                           "and cyber governance.",
            "url": "https://www.thebritishacademy.ac.uk/funding/",
            "deadline": None, "country": "United Kingdom / International",
        },
    ],
    "Royal Society": [
        {
            "title": "Royal Society - Cybersecurity and Cryptography",
            "description": "Royal Society grants supporting cryptography, cybersecurity, and secure "
                           "computing research.",
            "url": "https://royalsociety.org/grants-schemes-awards/grants/",
            "deadline": None, "country": "United Kingdom / International",
        },
    ],
    "NIH": [
        {
            "title": "NIH - Security and Privacy of Health Information",
            "description": "NIH funding on healthcare cybersecurity, medical device security, "
                           "and privacy of health data.",
            "url": "https://grants.nih.gov/grants/oer.htm",
            "deadline": None, "country": "United States / International",
        },
    ],
    "U.S. Department of Energy": [
        {
            "title": "DOE - Cybersecurity for Energy Delivery Systems",
            "description": "DOE funding on cybersecurity for critical infrastructure, ICS/OT security, "
                           "and cyber resilience of energy systems.",
            "url": "https://www.energy.gov/ceser/cybersecurity-energy-delivery-systems",
            "deadline": None, "country": "United States",
        },
    ],
    "NASA": [
        {
            "title": "NASA - Space Cybersecurity and Mission Assurance",
            "description": "NASA research opportunities on space cybersecurity, secure communications, "
                           "and mission assurance.",
            "url": "https://www.nasa.gov/grants-and-funding/",
            "deadline": None, "country": "United States",
        },
    ],
    "Australian Research Council": [
        {
            "title": "ARC - Cybersecurity and Trusted Systems",
            "description": "ARC grants on cybersecurity, trusted systems, cryptography, and secure AI.",
            "url": "https://www.arc.gov.au/funding-research",
            "deadline": None, "country": "Australia",
        },
    ],
    "NHMRC": [
        {
            "title": "NHMRC - Health Data Security and Privacy",
            "description": "NHMRC funding on security and privacy of health information and health "
                           "system cyber resilience.",
            "url": "https://www.nhmrc.gov.au/funding",
            "deadline": None, "country": "Australia",
        },
    ],
    "Australian Government Grants": [
        {
            "title": "Australian Government Grants - Cyber Security",
            "description": "Australian Government grant opportunities on cybersecurity, cyber "
                           "resilience, and critical infrastructure security.",
            "url": "https://www.grants.gov.au/",
            "deadline": None, "country": "Australia",
        },
    ],
    "NSERC": [
        {
            "title": "NSERC - Cybersecurity and Privacy",
            "description": "NSERC discovery and alliance grants on cybersecurity, cryptography, and "
                           "privacy.",
            "url": "https://www.nserc-crsng.gc.ca/index_eng.asp",
            "deadline": None, "country": "Canada / International",
        },
    ],
    "SSHRC": [
        {
            "title": "SSHRC - Cybercrime, Governance and Society",
            "description": "SSHRC funding on cybercrime, cyber governance, and social dimensions of "
                           "cybersecurity.",
            "url": "https://www.sshrc-crsh.gc.ca/funding-financement/index-eng.aspx",
            "deadline": None, "country": "Canada",
        },
    ],
    "IDRC": [
        {
            "title": "IDRC - Cybersecurity Capacity in the Global South",
            "description": "IDRC funding on cybersecurity capacity building, digital rights, and "
                           "cyber governance in developing countries.",
            "url": "https://www.idrc.ca/en/funding",
            "deadline": None, "country": "Canada / Global South",
        },
    ],
    "Mitacs": [
        {
            "title": "Mitacs - Cybersecurity Internships and Fellowships",
            "description": "Mitacs internships and fellowships on cybersecurity, secure software, "
                           "and cyber resilience.",
            "url": "https://www.mitacs.ca/our-programs/",
            "deadline": None, "country": "Canada / International",
        },
    ],
    "DFG": [
        {
            "title": "DFG - Cybersecurity and Privacy Research Units",
            "description": "DFG research units and grants on cybersecurity, cryptography, and privacy.",
            "url": "https://www.dfg.de/en/research_funding/index.html",
            "deadline": None, "country": "Germany / International",
        },
    ],
    "DAAD": [
        {
            "title": "DAAD - Scholarships in Cybersecurity and IT Security",
            "description": "DAAD scholarships and PhD funding in cybersecurity, IT security, and "
                           "secure software engineering.",
            "url": "https://www.daad.de/en/study-and-research-in-germany/scholarships/",
            "deadline": None, "country": "Germany / International",
        },
    ],
    "SNSF": [
        {
            "title": "SNSF - Cybersecurity and Trusted Computing",
            "description": "SNSF grants on cybersecurity, trusted computing, and privacy-enhancing "
                           "technologies.",
            "url": "https://www.snf.ch/en/funding/Pages/default.aspx",
            "deadline": None, "country": "Switzerland / International",
        },
    ],
    "NWO": [
        {
            "title": "NWO - Cybersecurity and Digital Resilience",
            "description": "NWO funding on cybersecurity, digital resilience, and secure AI.",
            "url": "https://www.nwo.nl/en/funding",
            "deadline": None, "country": "Netherlands / International",
        },
    ],
}


# ---- Real API adapters ----------------------------------------------------
async def _adapter_grants_gov(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    url = "https://api.grants.gov/v1/api/search2"
    body = {
        "keyword": keyword or "cybersecurity",
        "oppStatuses": "forecasted|posted",
        "rows": 25,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.post(url, json=body)
            if r.status_code != 200:
                return [], f"grants.gov HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"grants.gov: {e}"

    hits = (data.get("data") or {}).get("oppHits") or []
    out: List[Dict[str, Any]] = []
    for h in hits:
        title = h.get("title") or ""
        desc = h.get("oppDescription") or h.get("synopsis") or ""
        out.append({
            "title": title,
            "description": desc,
            "source": "Grants.gov",
            "country": "United States / International",
            "url": f"https://www.grants.gov/search-results-detail/{h.get('id')}",
            "external_id": str(h.get("id") or h.get("number") or ""),
            "deadline": h.get("closeDate") or None,
        })
    return out, None


async def _adapter_nsf(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    # NSF award search (public)
    url = "https://api.nsf.gov/services/v1/awards.json"
    params = {"keyword": keyword or "cybersecurity", "printFields": "id,title,abstractText,fundsObligatedAmt,startDate,expDate"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                return [], f"NSF HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"NSF: {e}"

    awards = (data.get("response") or {}).get("award") or []
    out: List[Dict[str, Any]] = []
    for a in awards:
        out.append({
            "title": a.get("title") or "",
            "description": a.get("abstractText") or "",
            "source": "National Science Foundation",
            "country": "United States",
            "url": f"https://www.nsf.gov/awardsearch/showAward?AWD_ID={a.get('id')}",
            "external_id": str(a.get("id") or ""),
            "deadline": None,
        })
    return out, None


async def _adapter_ukri(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    # Gateway to Research public API
    url = "https://gtr.ukri.org/api/search"
    params = {"q": keyword or "cybersecurity", "page": 1, "size": 25}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                return [], f"UKRI HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"UKRI: {e}"

    results = data.get("results") or data.get("projects") or []
    out: List[Dict[str, Any]] = []
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
    return out, None


async def _adapter_worldbank(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    url = "https://search.worldbank.org/api/v2/projects"
    params = {
        "format": "json",
        "qterm": keyword or "cybersecurity",
        "rows": 25,
        "fl": "id,project_name,project_abstract,countryname,boardapprovaldate,closingdate,url",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                return [], f"World Bank HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"World Bank: {e}"

    projects = data.get("projects") or {}
    # World Bank returns {"projects": {"0": {...}, "1": {...}}}
    if isinstance(projects, dict):
        items = list(projects.values())
    elif isinstance(projects, list):
        items = projects
    else:
        items = []

    out: List[Dict[str, Any]] = []
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
            "deadline": p.get("closingdate") or None,
        })
    return out, None


async def _adapter_nih(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    url = "https://api.reporter.nih.gov/v2/projects/search"
    body = {
        "criteria": {"advanced_text_search": {"operator": "and",
                                              "search_field": "projecttitle,abstracttext",
                                              "search_text": keyword or "cybersecurity"}},
        "include_fields": ["ProjectNum", "ProjectTitle", "AbstractText", "ProjectStartDate", "ProjectEndDate"],
        "offset": 0, "limit": 25,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.post(url, json=body)
            if r.status_code != 200:
                return [], f"NIH HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"NIH: {e}"

    results = data.get("results") or []
    out: List[Dict[str, Any]] = []
    for p in results:
        out.append({
            "title": p.get("project_title") or "",
            "description": p.get("abstract_text") or "",
            "source": "National Institutes of Health",
            "country": "United States",
            "url": f"https://reporter.nih.gov/project-details/{p.get('appl_id') or ''}",
            "external_id": str(p.get("project_num") or ""),
            "deadline": None,
        })
    return out, None


async def _adapter_ec(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    # EU Funding & Tenders portal search API (public, no key for basic search)
    url = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
    body = {
        "query": {"bool": {"must": [{"terms": {"type": ["1"]}},
                                     {"match": {"text": keyword or "cybersecurity"}}]}},
        "pageSize": 25, "pageNumber": 1,
        "sort": {"field": "sortStatus", "order": "DESC"},
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.post(url, params={"apiKey": "SEDIA", "text": keyword or "cybersecurity"},
                             json=body)
            if r.status_code != 200:
                return [], f"EC HTTP {r.status_code}"
            data = r.json()
    except Exception as e:
        return [], f"EC: {e}"

    results = data.get("results") or []
    out: List[Dict[str, Any]] = []
    for item in results:
        meta = item.get("metadata") or {}
        def _first(key: str) -> str:
            v = meta.get(key)
            if isinstance(v, list) and v:
                return str(v[0])
            return str(v) if v else ""
        ident = _first("identifier") or item.get("id") or ""
        out.append({
            "title": _first("title") or "",
            "description": _first("description") or _first("objective") or "",
            "source": "European Commission",
            "country": "European Union / International",
            "url": f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{ident}",
            "external_id": ident,
            "deadline": _first("deadlineDate") or None,
        })
    return out, None


# ---- Webpage / curated adapters ------------------------------------------
def _curated_source(source_name: str) -> Callable[[str], "asyncio.Future"]:
    async def _adapter(keyword: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        seeds = CURATED_SEEDS.get(source_name, [])
        out: List[Dict[str, Any]] = []
        for s in seeds:
            out.append({
                "title": s["title"],
                "description": s["description"],
                "source": source_name,
                "country": s.get("country", "International"),
                "url": s["url"],
                "external_id": None,
                "deadline": s.get("deadline"),
            })
        return out, None
    return _adapter


# ---- Source registry (30+) ------------------------------------------------
SOURCES: List[SourceDef] = [
    SourceDef("World Bank", "Global", "API", "https://projects.worldbank.org/", _adapter_worldbank),
    SourceDef("Asian Development Bank", "Asia / Pacific", "official webpage", "https://www.adb.org/projects", _curated_source("ADB")),
    SourceDef("JICA", "Japan / Global", "official webpage", "https://www.jica.go.jp/english/", _curated_source("JICA")),
    SourceDef("European Commission", "EU", "API", "https://ec.europa.eu/", _adapter_ec),
    SourceDef("European Research Council", "EU", "official webpage", "https://erc.europa.eu/", _curated_source("European Research Council")),
    SourceDef("Marie Skłodowska-Curie Actions", "EU", "official webpage", "https://marie-sklodowska-curie-actions.ec.europa.eu/", _curated_source("Marie Skłodowska-Curie Actions")),
    SourceDef("JST", "Japan", "official webpage", "https://www.jst.go.jp/", _curated_source("JST")),
    SourceDef("SATREPS", "Japan / Global", "official webpage", "https://www.jst.go.jp/global/english/", _curated_source("SATREPS")),
    SourceDef("JSPS", "Japan", "official webpage", "https://www.jsps.go.jp/english/", _curated_source("JSPS")),
    SourceDef("KAKENHI", "Japan", "official webpage", "https://www.jsps.go.jp/english/e-grants/", _curated_source("KAKENHI")),
    SourceDef("UKRI", "United Kingdom", "API", "https://www.ukri.org/", _adapter_ukri),
    SourceDef("EPSRC", "United Kingdom", "official webpage", "https://www.ukri.org/councils/epsrc/", _curated_source("EPSRC")),
    SourceDef("British Academy", "United Kingdom", "official webpage", "https://www.thebritishacademy.ac.uk/", _curated_source("British Academy")),
    SourceDef("Royal Society", "United Kingdom", "official webpage", "https://royalsociety.org/", _curated_source("Royal Society")),
    SourceDef("Grants.gov", "United States", "API", "https://www.grants.gov/", _adapter_grants_gov),
    SourceDef("National Science Foundation", "United States", "API", "https://www.nsf.gov/", _adapter_nsf),
    SourceDef("National Institutes of Health", "United States", "API", "https://www.nih.gov/", _adapter_nih),
    SourceDef("U.S. Department of Energy", "United States", "official webpage", "https://www.energy.gov/", _curated_source("U.S. Department of Energy")),
    SourceDef("NASA", "United States", "official webpage", "https://www.nasa.gov/", _curated_source("NASA")),
    SourceDef("Australian Research Council", "Australia", "official webpage", "https://www.arc.gov.au/", _curated_source("Australian Research Council")),
    SourceDef("NHMRC", "Australia", "official webpage", "https://www.nhmrc.gov.au/", _curated_source("NHMRC")),
    SourceDef("Australian Government Grants", "Australia", "official webpage", "https://www.grants.gov.au/", _curated_source("Australian Government Grants")),
    SourceDef("NSERC", "Canada", "official webpage", "https://www.nserc-crsng.gc.ca/", _curated_source("NSERC")),
    SourceDef("SSHRC", "Canada", "official webpage", "https://www.sshrc-crsh.gc.ca/", _curated_source("SSHRC")),
    SourceDef("IDRC", "Canada / Global", "official webpage", "https://www.idrc.ca/", _curated_source("IDRC")),
    SourceDef("Mitacs", "Canada", "official webpage", "https://www.mitacs.ca/", _curated_source("Mitacs")),
    SourceDef("DFG", "Germany", "official webpage", "https://www.dfg.de/", _curated_source("DFG")),
    SourceDef("DAAD", "Germany", "official webpage", "https://www.daad.de/", _curated_source("DAAD")),
    SourceDef("SNSF", "Switzerland", "official webpage", "https://www.snf.ch/", _curated_source("SNSF")),
    SourceDef("NWO", "Netherlands", "official webpage", "https://www.nwo.nl/", _curated_source("NWO")),
]

# Track last-checked + status per source (in-memory)
SOURCE_STATUS: Dict[str, Dict[str, Any]] = {
    s.name: {"status": "unknown", "last_checked": None, "count": 0, "error": None}
    for s in SOURCES
}


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------
async def _run_source(src: SourceDef, keyword: str) -> Tuple[str, List[Opportunity], Optional[str]]:
    try:
        raw, err = await src.adapter(keyword)
    except Exception as e:
        log.exception("Adapter crashed for %s", src.name)
        raw, err = [], f"{src.name}: {e}"

    opportunities: List[Opportunity] = []
    for item in raw or []:
        try:
            title = item.get("title") or ""
            description = item.get("description") or ""
            url = item.get("url") or ""
            score, matched, is_cyber = _cyber_score(title, description)
            if not is_cyber:
                continue
            funding_type = _classify_funding_type(f"{title} {description}")
            opp = Opportunity(
                id=_make_id(src.name, item.get("external_id"), title, url),
                title=title.strip(),
                description=description.strip()[:600],
                source=src.name,
                country=item.get("country") or src.country,
                funding_type=funding_type,
                deadline=item.get("deadline"),
                url=url,
                cybersecurity_relevance=score,
                matched_terms=matched,
                bhutan_relevance=_bhutan_relevance(f"{title} {description}"),
                official_source=_is_official(url),
                last_checked=_now_iso(),
                external_id=item.get("external_id"),
                opportunity_type=funding_type,
            )
            opportunities.append(opp)
        except Exception as e:
            log.warning("Failed to process item from %s: %s", src.name, e)
            continue

    SOURCE_STATUS[src.name] = {
        "status": "online" if err is None else "error",
        "last_checked": _now_iso(),
        "count": len(opportunities),
        "error": err,
    }
    return src.name, opportunities, err


def _dedupe(opps: Iterable[Opportunity]) -> List[Opportunity]:
    seen: set[str] = set()
    out: List[Opportunity] = []
    for o in opps:
        key = f"{o.source}|{o.external_id}" if o.external_id else f"{o.source}|{_normalize(o.title)}|{o.url}"
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Cybersecurity Research & Funding Tracker", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "time": _now_iso(), "sources": len(SOURCES)}


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
        })
    return {"count": len(items), "sources": items}


async def _search_impl(keyword: str, source_filter: Optional[str]) -> Dict[str, Any]:
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
    for _name, opps, err in results:
        all_opps.extend(opps)
        if err:
            errors.append({"source": _name, "error": err})

    deduped = _dedupe(all_opps)

    # FINAL cybersecurity gate — belt and braces
    final: List[Opportunity] = []
    for o in deduped:
        if o.cybersecurity_relevance > 0 and o.matched_terms:
            final.append(o)

    final.sort(key=lambda o: (-o.cybersecurity_relevance, o.source, o.title))

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
    keyword: str = Query("cybersecurity", description="Cybersecurity keyword"),
    source: Optional[str] = Query(None, description="Filter by source name (substring)"),
) -> JSONResponse:
    try:
        payload = await _search_impl(keyword, source)
        return JSONResponse(payload)
    except Exception as e:
        log.exception("search failed")
        return JSONResponse(
            {"keyword": keyword, "count": 0, "results": [], "errors": [{"source": "*", "error": str(e)}],
             "generated_at": _now_iso()},
            status_code=200,
        )


@app.get("/api/grants")
async def grants(
    keyword: str = Query("cybersecurity"),
    source: Optional[str] = Query(None),
) -> JSONResponse:
    payload = await _search_impl(keyword, source)
    return JSONResponse(payload)


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
        # Serve real files if they exist, else index.html (SPA fallback)
        candidate = FRONTEND_DIR / path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
else:
    @app.get("/")
    async def root_missing() -> JSONResponse:
        return JSONResponse({"error": "Frontend directory not found", "expected": str(FRONTEND_DIR)},
                            status_code=500)