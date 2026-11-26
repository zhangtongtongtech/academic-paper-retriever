#!/usr/bin/env python3
"""Search academic sources, export citations, and download open-access PDFs.

Sources: arXiv, PubMed, Semantic Scholar, Google Scholar (optional), and a
DOI/Sci-Hub fallback resolver.

The script intentionally depends only on ``requests``; ``beautifulsoup4`` and
``scholarly`` are optional.  Metadata is never invented: fields that a source
does not provide are left empty.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in broken installs
    sys.stderr.write(
        "Missing dependency: requests\n"
        "Install with: python -m pip install requests beautifulsoup4\n"
    )
    raise SystemExit(3)

try:
    from bs4 import BeautifulSoup
except ImportError:  # Optional; regex extraction still works.
    BeautifulSoup = None  # type: ignore[assignment]


VERSION = "1.1.0"

ARXIV_API = "https://export.arxiv.org/api/query"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
SEMANTIC_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_WORK = "https://api.crossref.org/works/{}"

DEFAULT_SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-net.xyz",
    "https://sci-hub.box",
    "https://sci-hub.red",
    "https://sci-hub.ren",
    "https://sci-hub.ee",
]

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_PDF_BYTES = 200 * 1024 * 1024
MIN_PDF_BYTES = 1024

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


class RetrievalError(Exception):
    """Base class for expected, user-facing errors."""


class ApiError(RetrievalError):
    """A remote service failed or returned an unusable response."""


class DependencyError(RetrievalError):
    """An optional dependency is required for the requested source."""


class CliError(RetrievalError):
    """Invalid arguments or local filesystem failure."""


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    source: str = ""
    published: str = ""
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    pmid: str = ""
    pmcid: str = ""
    paper_id: str = ""
    venue: str = ""
    categories: list[str] = field(default_factory=list)
    pdf_url: str = ""
    full_text_url: str = ""
    url: str = ""
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    fields_of_study: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    citing_papers: list[dict[str, Any]] = field(default_factory=list)
    download: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "title": self.title,
            "authors": self.authors,
            "source": self.source,
            "published": self.published,
            "abstract": self.abstract,
        }
        optional = {
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "paper_id": self.paper_id,
            "venue": self.venue,
            "categories": self.categories,
            "pdf_url": self.pdf_url,
            "full_text_url": self.full_text_url,
            "url": self.url,
            "citation_count": self.citation_count,
            "influential_citation_count": self.influential_citation_count,
            "reference_count": self.reference_count,
            "fields_of_study": self.fields_of_study,
            "publication_types": self.publication_types,
            "references": self.references,
            "citing_papers": self.citing_papers,
            "download": self.download,
            "notes": self.extra,
        }
        for key, value in optional.items():
            if value not in ("", None, [], {}):
                data[key] = value
        return data

    def identity(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        if self.arxiv_id:
            return "arxiv:" + self.arxiv_id
        if self.pmid:
            return "pmid:" + self.pmid
        return "title:" + normalize_title(self.title)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def debug(enabled: bool, message: str) -> None:
    if enabled:
        log("[debug] " + message)


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    if BeautifulSoup is not None:
        text = BeautifulSoup(value, "html.parser").get_text(" ")
    else:
        text = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(html_module.unescape(text))


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    value = html_module.unescape(str(value)).strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    value = value.strip().strip(".,;")
    match = re.search(r"10\.\d{4,9}/\S+", value, flags=re.I)
    if not match:
        return ""
    doi = match.group(0).rstrip(".,;")
    return doi


def is_arxiv_doi(doi: str) -> bool:
    return normalize_doi(doi).lower().startswith("10.48550/arxiv.")


def clean_arxiv_id(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", value)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value)
    return value.strip("/")


def first_author_last_name(authors: Sequence[str]) -> str:
    if not authors:
        return ""
    name = normalize_space(authors[0])
    if not name:
        return ""
    if "," in name:
        return normalize_space(name.split(",", 1)[0])
    parts = name.replace(".", " ").split()
    if not parts:
        return ""
    # PubMed uses "Family INITIALS" while arXiv uses "Given Family".
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]{1,4}", parts[-1] or ""):
        return parts[0]
    return parts[-1]


def year_of(paper: Paper) -> str:
    match = re.search(r"(19|20)\d{2}", paper.published or "")
    return match.group(0) if match else ""


def dedupe(papers: Iterable[Paper]) -> list[Paper]:
    seen: set[str] = set()
    output: list[Paper] = []
    for paper in papers:
        key = paper.identity()
        if key in seen:
            continue
        seen.add(key)
        output.append(paper)
    return output


class HttpClient:
    """Small requests wrapper with bounded retries and polite diagnostics."""

    def __init__(self, timeout: float = 30.0, verbose: bool = False) -> None:
        self.timeout = timeout
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": os.environ.get(
                    "PAPERHUB_USER_AGENT",
                    "academic-paper-retriever/1.0 (academic research tool; "
                    "contact the operator for rate-limit questions)",
                ),
                "Accept": "*/*",
            }
        )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        params = kwargs.pop("params", None)
        headers = kwargs.pop("headers", None)
        stream = kwargs.pop("stream", False)
        allow_redirects = kwargs.pop("allow_redirects", True)
        timeout = kwargs.pop("timeout", self.timeout)
        last_error = ""
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    stream=stream,
                    allow_redirects=allow_redirects,
                    timeout=timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                debug(self.verbose, f"{method} {url} failed: {last_error}")
                dns_failure = (
                    "NameResolutionError" in last_error
                    or "nodename nor servname" in last_error
                    or "Temporary failure in name resolution" in last_error
                )
                if attempt < 3 and not dns_failure:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise ApiError(f"Network error for {url}: {last_error}") from exc

            if response.status_code in RETRYABLE_STATUS and attempt < 3:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    wait = min(float(retry_after), 15.0)
                except (TypeError, ValueError):
                    wait = min(2**attempt, 8)
                debug(
                    self.verbose,
                    f"HTTP {response.status_code}; retrying in {wait:.1f}s",
                )
                response.close()
                time.sleep(wait)
                continue

            debug(self.verbose, f"{method} {url} -> HTTP {response.status_code}")
            return response

        raise ApiError(f"Network error for {url}: {last_error}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)


def _response_error(response: requests.Response, limit: int = 300) -> str:
    text = normalize_space(response.text[:limit]) if response.text else ""
    return f"HTTP {response.status_code}" + (f": {text}" if text else "")


# ---------------------------------------------------------------------------
# Date and query helpers
# ---------------------------------------------------------------------------


def resolve_dates(args: argparse.Namespace) -> tuple[date | None, date | None]:
    try:
        start = date.fromisoformat(args.start_date) if args.start_date else None
        end = date.fromisoformat(args.end_date) if args.end_date else None
    except ValueError as exc:
        raise CliError("Dates must use YYYY-MM-DD.") from exc

    if args.year is not None:
        if args.year < 1500 or args.year > 2200:
            raise CliError("--year must be a four-digit year between 1500 and 2200.")
        if start is None:
            start = date(args.year, 1, 1)
        if end is None:
            end = date(args.year, 12, 31)

    if start and end and start > end:
        raise CliError("--start-date cannot be after --end-date.")
    return start, end


def arxiv_query(args: argparse.Namespace, start: date | None, end: date | None) -> str:
    query = normalize_space(args.query)
    if not query:
        raise CliError("The search query cannot be empty.")
    if re.search(r"\b(AND|OR|ANDNOT)\b|[\w.-]+:", query, flags=re.I):
        core = f"({query})"
    else:
        core = f'all:"{query.replace(chr(34), " ")}"'

    parts = [core]
    if args.author:
        parts.append(f'au:"{args.author.replace(chr(34), " ")}"')
    if args.category:
        categories = [f"cat:{c.strip()}" for c in args.category if c.strip()]
        if categories:
            parts.append("(" + " OR ".join(categories) + ")")
    if start or end:
        lower = start or date(1500, 1, 1)
        upper = end or date(2200, 12, 31)
        parts.append(
            "submittedDate:["
            f"{lower.strftime('%Y%m%d')}0000 TO {upper.strftime('%Y%m%d')}2359]"
        )
    return " AND ".join(parts)


def pubmed_query(args: argparse.Namespace, start: date | None, end: date | None) -> str:
    query = normalize_space(args.query)
    if not query:
        raise CliError("The search query cannot be empty.")
    parts = [f"({query})"]
    if args.author:
        parts.append(f'"{args.author.replace(chr(34), " ")}"[Author]')
    if start or end:
        lower = (start or date(1500, 1, 1)).strftime("%Y/%m/%d")
        upper = (end or date(2200, 12, 31)).strftime("%Y/%m/%d")
        parts.append(f'("{lower}"[Date - Publication] : "{upper}"[Date - Publication])')
    for publication_type in args.publication_type or []:
        cleaned = publication_type.replace('"', " ").strip()
        if cleaned:
            parts.append(f'"{cleaned}"[Publication Type]')
    return " AND ".join(parts)


def semantic_fields(include_references: bool, include_citations: bool) -> str:
    fields = [
        "paperId",
        "title",
        "authors",
        "abstract",
        "year",
        "publicationDate",
        "venue",
        "journal",
        "externalIds",
        "openAccessPdf",
        "citationCount",
        "influentialCitationCount",
        "referenceCount",
        "fieldsOfStudy",
        "publicationTypes",
        "url",
    ]
    if include_references:
        fields.extend(
            [
                "references.title",
                "references.authors",
                "references.year",
                "references.externalIds",
                "references.url",
            ]
        )
    if include_citations:
        fields.extend(
            [
                "citations.title",
                "citations.authors",
                "citations.year",
                "citations.externalIds",
                "citations.url",
            ]
        )
    return ",".join(fields)


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------


def search_arxiv(
    client: HttpClient, args: argparse.Namespace, start: date | None, end: date | None
) -> list[Paper]:
    sort_map = {
        "relevance": "relevance",
        "date": "submittedDate",
        "citations": "relevance",
    }
    params = {
        "search_query": arxiv_query(args, start, end),
        "start": 0,
        "max_results": args.max_results,
        "sortBy": sort_map.get(args.sort_by, "relevance"),
        "sortOrder": "descending",
    }
    response = client.get(ARXIV_API, params=params)
    if response.status_code != 200:
        raise ApiError(f"arXiv API error: {_response_error(response)}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ApiError("arXiv returned an unreadable Atom feed.") from exc

    error_node = root.find("atom:entry/atom:title", NS)
    if error_node is not None and normalize_space(error_node.text) == "Error":
        summary = root.findtext("atom:entry/atom:summary", default="", namespaces=NS)
        raise ApiError(f"arXiv rejected the query: {normalize_space(summary)}")

    papers: list[Paper] = []
    for entry in root.findall("atom:entry", NS):
        title = normalize_space(entry.findtext("atom:title", default="", namespaces=NS))
        if not title:
            continue
        raw_id = normalize_space(
            entry.findtext("atom:id", default="", namespaces=NS)
        )
        arxiv_id = clean_arxiv_id(raw_id)
        pdf_url = ""
        abs_url = raw_id
        for link in entry.findall("atom:link", NS):
            href = link.attrib.get("href", "")
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
            if link.attrib.get("rel") == "alternate":
                abs_url = href
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        doi = normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=NS))
        if not doi and arxiv_id:
            doi = f"10.48550/arXiv.{arxiv_id}"

        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=NS))
            for author in entry.findall("atom:author", NS)
        ]
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", NS)
            if category.attrib.get("term")
        ]
        published = normalize_space(
            entry.findtext("atom:published", default="", namespaces=NS)
        )[:10]
        journal_ref = normalize_space(
            entry.findtext("arxiv:journal_ref", default="", namespaces=NS)
        )
        papers.append(
            Paper(
                title=title,
                authors=[a for a in authors if a],
                source="arXiv",
                published=published,
                abstract=normalize_space(
                    entry.findtext("atom:summary", default="", namespaces=NS)
                ),
                doi=doi,
                arxiv_id=arxiv_id,
                venue=journal_ref,
                categories=list(dict.fromkeys(categories)),
                pdf_url=pdf_url,
                url=abs_url,
            )
        )
    return dedupe(papers)[: args.max_results]


def _pubmed_month(value: str | None) -> str:
    if not value:
        return ""
    raw = normalize_space(value)
    if raw.isdigit():
        number = int(raw)
        return f"{number:02d}" if 1 <= number <= 12 else ""
    months = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    for name, number in months.items():
        if raw.lower().startswith(name):
            return number
    return ""


def _parse_pubmed_date(article: ET.Element) -> str:
    pub_date = article.find(".//JournalIssue/PubDate")
    year = month = day = ""
    if pub_date is not None:
        year = normalize_space(pub_date.findtext("Year"))
        month = _pubmed_month(pub_date.findtext("Month"))
        day = normalize_space(pub_date.findtext("Day"))
        if not year:
            medline = normalize_space(pub_date.findtext("MedlineDate"))
            match = re.search(r"(19|20)\d{2}", medline)
            if match:
                year = match.group(0)
                month_match = re.search(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
                    medline,
                    flags=re.I,
                )
                if month_match:
                    month = _pubmed_month(month_match.group(0))
    if not year:
        article_date = article.find(".//ArticleDate")
        if article_date is not None:
            year = normalize_space(article_date.findtext("Year"))
            month = _pubmed_month(article_date.findtext("Month"))
            day = normalize_space(article_date.findtext("Day"))
    if not year:
        return ""
    parts = [year]
    if month:
        parts.append(month)
        if day and day.isdigit():
            parts.append(f"{int(day):02d}")
    return "-".join(parts)


def search_pubmed(
    client: HttpClient, args: argparse.Namespace, start: date | None, end: date | None
) -> list[Paper]:
    term = pubmed_query(args, start, end)
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": args.max_results,
        "sort": "pub_date" if args.sort_by == "date" else "relevance",
        "tool": "academic-paper-retriever",
    }
    email = os.environ.get("PAPERHUB_EMAIL") or os.environ.get("NCBI_EMAIL")
    if email:
        params["email"] = email

    response = client.get(PUBMED_ESEARCH, params=params)
    if response.status_code != 200:
        raise ApiError(f"PubMed ESearch error: {_response_error(response)}")
    try:
        payload = response.json()
        search_result = payload.get("esearchresult", {})
        ids = search_result.get("idlist", [])
    except (ValueError, AttributeError) as exc:
        raise ApiError("PubMed returned an unreadable ESearch response.") from exc
    if not ids:
        return []

    fetch_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
        "tool": "academic-paper-retriever",
    }
    if email:
        fetch_params["email"] = email
    fetched = client.get(PUBMED_EFETCH, params=fetch_params)
    if fetched.status_code != 200:
        raise ApiError(f"PubMed EFetch error: {_response_error(fetched)}")
    try:
        root = ET.fromstring(fetched.content)
    except ET.ParseError as exc:
        raise ApiError("PubMed returned unreadable XML.") from exc

    papers: list[Paper] = []
    for article in root.findall(".//PubmedArticle"):
        title_node = article.find(".//ArticleTitle")
        title = normalize_space(
            "".join(title_node.itertext()) if title_node is not None else ""
        )
        if not title:
            continue

        authors: list[str] = []
        for author in article.findall(".//AuthorList/Author"):
            last = normalize_space(author.findtext("LastName"))
            initials = normalize_space(author.findtext("Initials"))
            collective = normalize_space(author.findtext("CollectiveName"))
            if last:
                authors.append(f"{last} {initials}".strip())
            elif collective:
                authors.append(collective)

        abstract_parts: list[str] = []
        for abstract in article.findall(".//Abstract/AbstractText"):
            text = normalize_space("".join(abstract.itertext()))
            if not text:
                continue
            label = normalize_space(abstract.attrib.get("Label"))
            abstract_parts.append(f"{label}: {text}" if label else text)
        abstract = normalize_space(" ".join(abstract_parts))

        doi = ""
        pmcid = ""
        # Only the record's own ArticleIdList is authoritative. A PubmedArticle
        # also contains ArticleIdList elements for every cited reference; a
        # ".//" search would silently pick the last reference's DOI/PMCID.
        own_ids = article.findall("PubmedData/ArticleIdList/ArticleId")
        if not own_ids:
            own_ids = article.findall("MedlineCitation/ArticleIdList/ArticleId")
        for article_id in own_ids:
            id_type = (article_id.attrib.get("IdType") or "").lower()
            value = normalize_space(article_id.text)
            if id_type == "doi":
                doi = normalize_doi(value)
            elif id_type == "pmc":
                pmcid = value.upper()
                if pmcid and not pmcid.startswith("PMC"):
                    pmcid = "PMC" + pmcid
        if not doi:
            for location_id in article.findall(".//Article/ELocationID"):
                if (location_id.attrib.get("EIdType") or "").lower() == "doi":
                    doi = normalize_doi(location_id.text)
        pmid = normalize_space(article.findtext(".//MedlineCitation/PMID"))

        journal = normalize_space(article.findtext(".//Journal/ISOAbbreviation"))
        if not journal:
            journal = normalize_space(article.findtext(".//Journal/Title"))
        publication_types = [
            normalize_space(node.text)
            for node in article.findall(".//PublicationTypeList/PublicationType")
            if normalize_space(node.text)
        ]
        published = _parse_pubmed_date(article)
        full_text_url = (
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
            if pmcid and args.full_text
            else ""
        )
        pdf_url = (
            f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else ""
        )
        paper = Paper(
            title=title,
            authors=authors,
            source="PubMed",
            published=published,
            abstract=abstract,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            venue=journal,
            pdf_url=pdf_url,
            full_text_url=full_text_url,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            publication_types=publication_types,
        )
        if doi and not pmcid:
            paper.extra["try_doi_resolution"] = True
        papers.append(paper)

    return dedupe(papers)[: args.max_results]


def _compact_semantic_paper(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    external = item.get("externalIds") or {}
    authors = [
        normalize_space(author.get("name", ""))
        for author in item.get("authors") or []
        if isinstance(author, dict) and normalize_space(author.get("name", ""))
    ]
    data: dict[str, Any] = {
        "title": normalize_space(item.get("title", "")),
        "authors": authors,
        "year": item.get("year"),
        "url": item.get("url", ""),
    }
    if external.get("DOI"):
        data["doi"] = normalize_doi(external.get("DOI"))
    if external.get("ArXiv"):
        data["arxiv_id"] = clean_arxiv_id(external.get("ArXiv"))
    if item.get("paperId"):
        data["paper_id"] = item.get("paperId")
    return {key: value for key, value in data.items() if value not in ("", None, [])}


def _semantic_sort(papers: list[Paper], sort_by: str) -> list[Paper]:
    if sort_by == "citations":
        return sorted(
            papers,
            key=lambda paper: (
                paper.citation_count is not None,
                paper.citation_count or 0,
            ),
            reverse=True,
        )
    if sort_by == "date":
        return sorted(
            papers,
            key=lambda paper: paper.published or "",
            reverse=True,
        )
    return papers


def _author_matches(authors: Sequence[str], query: str) -> bool:
    if not query:
        return True
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", query.lower())
        if token
    ]
    if not tokens:
        return True
    haystack = " ".join(authors).lower()
    return all(token in haystack for token in tokens)


def search_semantic(
    client: HttpClient, args: argparse.Namespace, start: date | None, end: date | None
) -> list[Paper]:
    needs_extra_candidates = bool(
        args.author or args.sort_by in {"date", "citations"}
    )
    requested = args.max_results * 3 if needs_extra_candidates else args.max_results
    limit = max(args.max_results, min(100, requested))
    params: dict[str, Any] = {
        "query": args.query,
        "limit": limit,
        "fields": semantic_fields(args.include_references, args.include_citations),
    }
    if args.min_citations is not None:
        params["minCitationCount"] = args.min_citations
    if args.year and not (args.start_date or args.end_date):
        params["year"] = str(args.year)
    elif start or end:
        lower = (start or date(1500, 1, 1)).isoformat()
        upper = (end or date(2200, 12, 31)).isoformat()
        params["publicationDateOrYear"] = f"{lower}:{upper}"

    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    response = client.get(SEMANTIC_SEARCH, params=params, headers=headers)
    if response.status_code == 429:
        raise ApiError(
            "Semantic Scholar rate limit exceeded. Retry later, reduce "
            "--max-results, or set SEMANTIC_SCHOLAR_API_KEY."
        )
    if response.status_code != 200:
        raise ApiError(f"Semantic Scholar API error: {_response_error(response)}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError("Semantic Scholar returned unreadable JSON.") from exc

    papers: list[Paper] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        title = normalize_space(item.get("title", ""))
        if not title:
            continue
        external = item.get("externalIds") or {}
        authors = [
            normalize_space(author.get("name", ""))
            for author in item.get("authors") or []
            if isinstance(author, dict) and normalize_space(author.get("name", ""))
        ]
        if not _author_matches(authors, args.author):
            continue
        doi = normalize_doi(external.get("DOI"))
        arxiv_id = clean_arxiv_id(external.get("ArXiv") or "")
        pmid = normalize_space(external.get("PubMed") or "")
        pdf_url = ""
        open_access = item.get("openAccessPdf") or {}
        if isinstance(open_access, dict):
            pdf_url = open_access.get("url") or ""
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        journal = item.get("journal") or {}
        venue = (
            normalize_space(journal.get("name", ""))
            if isinstance(journal, dict)
            else ""
        ) or normalize_space(item.get("venue", ""))
        published = normalize_space(item.get("publicationDate", ""))
        if not published and item.get("year"):
            published = str(item.get("year"))
        references = [
            compact
            for compact in (
                _compact_semantic_paper(ref)
                for ref in (item.get("references") or [])
            )
            if compact
        ]
        citing = [
            compact
            for compact in (
                _compact_semantic_paper(ref)
                for ref in (item.get("citations") or [])
            )
            if compact
        ]
        paper = Paper(
            title=title,
            authors=authors,
            source="Semantic Scholar",
            published=published,
            abstract=normalize_space(item.get("abstract", "")),
            doi=doi,
            arxiv_id=arxiv_id,
            pmid=pmid,
            paper_id=normalize_space(item.get("paperId", "")),
            venue=venue,
            pdf_url=pdf_url,
            url=normalize_space(item.get("url", "")),
            citation_count=item.get("citationCount"),
            influential_citation_count=item.get("influentialCitationCount"),
            reference_count=item.get("referenceCount"),
            fields_of_study=[
                normalize_space(field)
                for field in (item.get("fieldsOfStudy") or [])
                if normalize_space(field)
            ],
            publication_types=[
                normalize_space(kind)
                for kind in (item.get("publicationTypes") or [])
                if normalize_space(kind)
            ],
            references=references,
            citing_papers=citing,
        )
        papers.append(paper)

    papers = _semantic_sort(papers, args.sort_by)
    return dedupe(papers)[: args.max_results]


def search_scholar(
    client: HttpClient, args: argparse.Namespace, start: date | None, end: date | None
) -> list[Paper]:
    del client  # scholarly manages its own HTTP session.
    try:
        from scholarly import scholarly  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DependencyError(
            "Google Scholar support requires the optional 'scholarly' package. "
            "Install it with: python -m pip install scholarly. Google Scholar "
            "is rate-limited and may block automated access."
        ) from exc

    target = max(args.max_results, args.max_results * 3 if args.author else 0)
    maximum_scan = min(300, target + 10)
    papers: list[Paper] = []
    scanned = 0
    try:
        results = scholarly.search_pubs(args.query)
        for item in results:
            scanned += 1
            if scanned > maximum_scan:
                break
            bib = item.get("bib") or {}
            title = normalize_space(bib.get("title", ""))
            if not title:
                continue
            author_value = normalize_space(bib.get("author", ""))
            if " and " in author_value:
                authors = [normalize_space(a) for a in author_value.split(" and ")]
            else:
                authors = [normalize_space(a) for a in author_value.split(",")]
            authors = [a for a in authors if a]
            if not _author_matches(authors, args.author):
                continue
            year_value = normalize_space(str(bib.get("pub_year", "")))
            if start and year_value.isdigit() and int(year_value) < start.year:
                continue
            if end and year_value.isdigit() and int(year_value) > end.year:
                continue
            citations = item.get("num_citations")
            try:
                citations = int(citations) if citations is not None else None
            except (TypeError, ValueError):
                citations = None
            if args.min_citations is not None and (citations or 0) < args.min_citations:
                continue
            pdf_url = normalize_space(item.get("eprint_url", ""))
            papers.append(
                Paper(
                    title=title,
                    authors=authors,
                    source="Google Scholar",
                    published=year_value,
                    abstract=normalize_space(bib.get("abstract", "")),
                    venue=normalize_space(bib.get("venue", "")),
                    pdf_url=pdf_url,
                    url=normalize_space(item.get("pub_url", "")),
                    citation_count=citations,
                )
            )
    except Exception as exc:  # scholarly raises broad parsing/network errors
        raise ApiError(
            "Google Scholar search failed (automated access is frequently "
            f"blocked): {type(exc).__name__}: {exc}"
        ) from exc

    if args.sort_by == "citations":
        papers.sort(key=lambda p: p.citation_count or 0, reverse=True)
    elif args.sort_by == "date":
        papers.sort(key=lambda p: p.published, reverse=True)
    return dedupe(papers)[: args.max_results]


def fetch_crossref(client: HttpClient, doi: str, verbose: bool) -> Paper:
    """Fetch DOI metadata from Crossref; never fail the download if this fails."""
    response = client.get(
        CROSSREF_WORK.format(quote(doi, safe="")),
        params={"mailto": os.environ.get("PAPERHUB_EMAIL", "")}
        if os.environ.get("PAPERHUB_EMAIL")
        else None,
    )
    if response.status_code != 200:
        log(f"Warning: Crossref metadata unavailable for {doi} ({_response_error(response)}).")
        return Paper(title=f"DOI {doi}", source="Sci-Hub", doi=doi)

    try:
        message = response.json().get("message") or {}
    except ValueError:
        log(f"Warning: Crossref returned unreadable metadata for {doi}.")
        return Paper(title=f"DOI {doi}", source="Sci-Hub", doi=doi)

    title = ""
    titles = message.get("title") or []
    if isinstance(titles, list) and titles:
        title = strip_markup(str(titles[0]))
    if not title:
        title = f"DOI {doi}"

    authors: list[str] = []
    for author in message.get("author") or []:
        family = normalize_space(author.get("family", ""))
        given = normalize_space(author.get("given", ""))
        name = normalize_space(author.get("name", ""))
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
        elif name:
            authors.append(name)

    published = ""
    for date_key in ("published-print", "published-online", "issued", "created"):
        parts = ((message.get(date_key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            published = str(parts[0])
            if len(parts) > 1 and parts[1]:
                published += f"-{int(parts[1]):02d}"
            if len(parts) > 2 and parts[2]:
                published += f"-{int(parts[2]):02d}"
            break

    pdf_url = ""
    direct_links: list[str] = []
    for link in message.get("link") or []:
        url = normalize_space(link.get("URL", ""))
        content_type = (link.get("content-type") or "").lower()
        if not url:
            continue
        direct_links.append(url)
        if "pdf" in content_type and not pdf_url:
            pdf_url = url

    container = message.get("container-title") or []
    venue = strip_markup(str(container[0])) if container else ""
    paper = Paper(
        title=title,
        authors=authors,
        source="Sci-Hub",
        published=published,
        abstract=strip_markup(message.get("abstract", "")),
        doi=doi,
        venue=venue,
        pdf_url=pdf_url,
        url=normalize_space(message.get("URL", "")) or f"https://doi.org/{doi}",
    )
    paper.extra["metadata_source"] = "Crossref"
    if direct_links:
        paper.extra["crossref_links"] = direct_links
    debug(verbose, f"Crossref metadata loaded for {doi}")
    return paper


# ---------------------------------------------------------------------------
# PDF download and Sci-Hub fallback
# ---------------------------------------------------------------------------


@dataclass
class DownloadOutcome:
    status: str  # downloaded | existing | failed
    file: str = ""
    via: str = ""
    url: str = ""
    mirror: str = ""
    bytes: int = 0
    sha256: str = ""
    error: str = ""
    attempts: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.status}
        for key, value in (
            ("file", self.file),
            ("via", self.via),
            ("url", self.url),
            ("mirror", self.mirror),
            ("bytes", self.bytes),
            ("sha256", self.sha256),
            ("error", self.error),
            ("attempts", self.attempts),
        ):
            if value not in ("", 0, [], None):
                data[key] = value
        return data


def looks_like_pdf(path: Path) -> bool:
    try:
        if path.stat().st_size < MIN_PDF_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def safe_filename(value: str, limit: int = 90) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    return (value[:limit].rstrip("-._") or "paper")


def paper_filename(paper: Paper) -> str:
    last = first_author_last_name(paper.authors) or "anon"
    year = year_of(paper) or "nd"
    title = safe_filename(paper.title, 65)
    identifier = paper.arxiv_id or paper.pmid
    if not identifier and paper.doi:
        identifier = safe_filename(paper.doi.split("/")[-1], 28)
    if not identifier:
        identifier = hashlib.sha1(paper.identity().encode("utf-8")).hexdigest()[:8]
    identifier = safe_filename(identifier, 32)
    return f"{safe_filename(last, 25)}_{year}_{title}_{identifier}.pdf"


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    for index in range(2, 1000):
        candidate = directory / f"{stem}-{index}.pdf"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{int(time.time())}.pdf"


def _write_stream(response: requests.Response, destination: Path) -> tuple[bool, str, int, str]:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if response.status_code != 200:
        error = _response_error(response, 180)
        response.close()
        return False, error, 0, ""
    partial = destination.with_name(destination.name + ".part")
    total = 0
    digest = hashlib.sha256()
    first = b""
    try:
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:1024]
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise ApiError("PDF exceeds the 200 MB safety limit.")
                handle.write(chunk)
                digest.update(chunk)
        if not first:
            return False, "empty response", 0, ""
        if not first.lstrip().startswith(b"%PDF-"):
            hint = "HTML/login page" if b"<html" in first.lower() else "not a PDF"
            return False, f"response was {hint} (content-type: {content_type or 'unknown'})", 0, ""
        if total < MIN_PDF_BYTES:
            return False, f"PDF too small ({total} bytes)", 0, ""
        os.replace(partial, destination)
        return True, "", total, digest.hexdigest()
    finally:
        response.close()
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass


def save_pdf_bytes(data: bytes, destination: Path) -> tuple[bool, str, int, str]:
    """Validate and atomically save a PDF already held in memory."""
    if not data:
        return False, "empty response", 0, ""
    if not data.lstrip().startswith(b"%PDF-"):
        return False, "response was not a PDF", 0, ""
    if len(data) < MIN_PDF_BYTES:
        return False, f"PDF too small ({len(data)} bytes)", 0, ""
    if len(data) > MAX_PDF_BYTES:
        return False, "PDF exceeds the 200 MB safety limit.", 0, ""
    partial = destination.with_name(destination.name + ".part")
    try:
        partial.write_bytes(data)
        os.replace(partial, destination)
    finally:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
    return True, "", len(data), hashlib.sha256(data).hexdigest()


def try_pdf_url(
    client: HttpClient, url: str, destination: Path, referer: str = ""
) -> tuple[bool, str, int, str]:
    headers = {"Accept": "application/pdf,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        response = client.get(url, stream=True, headers=headers)
    except ApiError as exc:
        return False, str(exc), 0, ""
    try:
        return _write_stream(response, destination)
    except (requests.RequestException, ApiError) as exc:
        return False, f"{type(exc).__name__}: {exc}", 0, ""


def extract_pdf_candidates(markup: str, base_url: str) -> list[str]:
    """Extract likely PDF/download URLs from a Sci-Hub page."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        if not raw:
            return
        value = html_module.unescape(str(raw)).strip().strip("\"'")
        value = value.replace("\\/", "/")
        if not value or "\\" in value:
            return
        if value.startswith(("data:", "javascript:", "mailto:", "#")):
            return
        if value.startswith("//"):
            value = "https:" + value
        elif not value.startswith(("http://", "https://")):
            value = urljoin(base_url, value)
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return
        value = urlunparse(parsed._replace(fragment=""))
        if value not in seen:
            seen.add(value)
            candidates.append(value)

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(markup, "html.parser")
        except Exception:
            soup = None
        if soup is not None:
            meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
            if meta and meta.get("content"):
                add(meta["content"])
            for tag in soup.find_all(["iframe", "embed", "object"]):
                for attribute in ("src", "data"):
                    if tag.get(attribute):
                        add(tag[attribute])
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"]
                if ".pdf" in href.lower() or "/download" in href.lower():
                    add(href)

    patterns = [
        r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)""",
        r"""<iframe[^>]+(?:src|data-src)=["']([^"']+)""",
        r"""<embed[^>]+src=["']([^"']+)""",
        r"""<object[^>]+data=["']([^"']+)""",
        r"""(?:location\.href|location)\s*=\s*["']([^"']+)""",
        r"""["'](//[^"']+\.pdf(?:\?[^"']*)?)["']""",
        r"""(https?://[^\s"'<>]+\.pdf(?:\?[^\s"'<>]*)?)""",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, markup, flags=re.I):
            add(match)

    # Prefer direct PDF URLs, but keep download endpoints that may not end in
    # ".pdf" because Sci-Hub often serves them through a redirect.
    def priority(value: str) -> tuple[int, int]:
        lower = value.lower()
        return (
            0 if ".pdf" in lower else 1,
            0 if "sci-hub" in lower else 1,
        )

    return sorted(candidates, key=priority)


def scihub_mirrors() -> list[str]:
    configured = os.environ.get("PAPERHUB_SCIHUB_MIRRORS", "").strip()
    if configured:
        mirrors = [m.strip().rstrip("/") for m in configured.split(",") if m.strip()]
    else:
        mirrors = list(DEFAULT_SCIHUB_MIRRORS)
    normalized = []
    for mirror in mirrors:
        if not mirror.startswith(("http://", "https://")):
            mirror = "https://" + mirror
        normalized.append(mirror.rstrip("/"))
    return normalized


def fetch_from_scihub(
    client: HttpClient, paper: Paper, destination: Path, delay: float, verbose: bool
) -> DownloadOutcome:
    doi = normalize_doi(paper.doi)
    attempts: list[dict[str, str]] = []
    if not doi:
        return DownloadOutcome(
            status="failed", error="No DOI available for Sci-Hub fallback.", attempts=attempts
        )
    if is_arxiv_doi(doi):
        return DownloadOutcome(
            status="failed",
            error="arXiv DOIs are not hosted by Sci-Hub; the direct arXiv PDF failed.",
            attempts=attempts,
        )

    mirrors = scihub_mirrors()
    for mirror in mirrors:
        page_url = f"{mirror}/{quote(doi, safe='')}"
        log(f"  [scihub] trying {page_url}")
        try:
            page = client.get(page_url, headers={"Accept": "text/html,*/*"})
        except ApiError as exc:
            attempts.append({"url": page_url, "error": str(exc)})
            continue
        if page.status_code != 200:
            attempts.append({"url": page_url, "error": _response_error(page, 160)})
            page.close()
            continue
        if page.content[:5].lstrip().startswith(b"%PDF-"):
            ok, error, size, digest = save_pdf_bytes(page.content, destination)
            page_url_final = page.url
            page.close()
            attempt = {"url": page_url_final}
            if not ok:
                attempt["error"] = error
            attempts.append(attempt)
            if ok:
                return DownloadOutcome(
                    status="downloaded",
                    file=str(destination),
                    via="scihub",
                    url=page_url_final,
                    mirror=mirror,
                    bytes=size,
                    sha256=digest,
                    attempts=attempts,
                )
            continue
        markup = page.text
        page_url_final = page.url
        page.close()
        lowered = markup.lower()
        if "just a moment" in lowered or "cf-chl" in lowered or "captcha" in lowered:
            attempts.append(
                {"url": page_url_final, "error": "blocked by CAPTCHA/Cloudflare challenge"}
            )
            continue
        candidates = extract_pdf_candidates(markup, page_url_final)
        if not candidates:
            attempts.append(
                {"url": page_url_final, "error": "no PDF/download link found in page"}
            )
            continue
        for candidate in candidates[:8]:
            debug(verbose, f"scihub candidate: {candidate}")
            ok, error, size, digest = try_pdf_url(
                client, candidate, destination, referer=page_url_final
            )
            attempt: dict[str, str] = {"url": candidate}
            if not ok:
                attempt["error"] = error
            attempts.append(attempt)
            if ok:
                return DownloadOutcome(
                    status="downloaded",
                    file=str(destination),
                    via="scihub",
                    url=candidate,
                    mirror=mirror,
                    bytes=size,
                    sha256=digest,
                    attempts=attempts,
                )
        if delay > 0:
            time.sleep(min(delay, 3.0))

    last_error = attempts[-1]["error"] if attempts else "all mirrors failed"
    challenge_seen = any(
        "CAPTCHA" in entry.get("error", "")
        or "Cloudflare" in entry.get("error", "")
        or "DDoS-Guard" in entry.get("error", "")
        for entry in attempts
    )
    if challenge_seen:
        helper = Path(__file__).with_name("scihub_browser.py")
        log(
            "Tip: a mirror presented a CAPTCHA/anti-bot challenge. For a visible "
            f'browser handoff, run: python "{helper}" "{doi}" --output-dir <directory>'
        )
    return DownloadOutcome(
        status="failed",
        error=f"Sci-Hub fallback failed ({len(mirrors)} mirrors); last error: {last_error}",
        attempts=attempts,
    )


def direct_candidates(paper: Paper) -> list[str]:
    urls: list[str] = []

    def add(value: str) -> None:
        value = normalize_space(value)
        if value and value not in urls:
            urls.append(value)

    add(paper.pdf_url)
    if paper.arxiv_id:
        clean_id = clean_arxiv_id(paper.arxiv_id)
        add(f"https://arxiv.org/pdf/{clean_id}")
        add(f"https://export.arxiv.org/pdf/{clean_id}")
    if paper.pmcid:
        add(f"https://europepmc.org/articles/{paper.pmcid}?pdf=render")
        add(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{paper.pmcid}/pdf/")
    for link in paper.extra.get("crossref_links") or []:
        if isinstance(link, str) and link and link not in urls:
            urls.append(link)
    if paper.extra.get("try_doi_resolution") and paper.doi:
        add(f"https://doi.org/{quote(paper.doi, safe='/')}")
    return urls


def download_paper(
    client: HttpClient, paper: Paper, args: argparse.Namespace
) -> DownloadOutcome:
    output_dir = Path(args.output_dir).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(f"Cannot create output directory {output_dir}: {exc}") from exc

    filename = paper_filename(paper)
    destination = output_dir / filename
    if looks_like_pdf(destination):
        return DownloadOutcome(
            status="existing",
            file=str(destination),
            bytes=destination.stat().st_size,
            sha256=hash_file(destination),
        )
    if destination.exists():
        destination = _unique_path(output_dir, filename)
    destination = _unique_path(output_dir, destination.name)

    attempts: list[dict[str, str]] = []
    if not args.scihub_only:
        for url in direct_candidates(paper):
            log(f"  [direct] trying {url}")
            ok, error, size, digest = try_pdf_url(client, url, destination)
            attempts.append({"url": url, "error": "" if ok else error})
            if ok:
                return DownloadOutcome(
                    status="downloaded",
                    file=str(destination),
                    via="direct",
                    url=url,
                    bytes=size,
                    sha256=digest,
                    attempts=attempts,
                )

    if args.no_scihub:
        error = attempts[-1]["error"] if attempts else "No direct PDF URL available."
        return DownloadOutcome(
            status="failed",
            error=f"Direct download failed ({error}); Sci-Hub fallback disabled.",
            attempts=attempts,
        )

    if not paper.doi:
        error = attempts[-1]["error"] if attempts else "No direct PDF URL available."
        return DownloadOutcome(
            status="failed",
            error=f"Direct download failed ({error}); no DOI for Sci-Hub fallback.",
            attempts=attempts,
        )

    log("  [fallback] direct download failed; trying Sci-Hub mirrors")
    log("  [note] use the fallback only where you are authorized to access the paper.")
    outcome = fetch_from_scihub(client, paper, destination, args.delay, args.verbose)
    outcome.attempts = attempts + outcome.attempts
    return outcome


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_all(
    client: HttpClient, papers: list[Paper], args: argparse.Namespace
) -> tuple[int, int]:
    downloaded = 0
    existing = 0
    for index, paper in enumerate(papers, start=1):
        title = paper.title if len(paper.title) <= 100 else paper.title[:97] + "..."
        log(f"[{index}/{len(papers)}] {title}")
        try:
            outcome = download_paper(client, paper, args)
        except CliError:
            raise
        except Exception as exc:  # keep other papers going
            outcome = DownloadOutcome(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        paper.download = outcome.to_dict()
        if outcome.status == "downloaded":
            downloaded += 1
            log(f"  saved: {outcome.file} ({outcome.bytes:,} bytes, via {outcome.via})")
        elif outcome.status == "existing":
            existing += 1
            log(f"  already present: {outcome.file}")
        else:
            log(f"  FAILED: {outcome.error}")

        if index < len(papers) and args.delay > 0 and outcome.status == "downloaded":
            time.sleep(min(args.delay, 3.0))

    try:
        update_manifest(papers, args)
    except CliError as exc:
        log(f"Warning: PDFs were saved but the manifest could not be written: {exc}")
    log(
        f"Download summary: {downloaded} downloaded, {existing} already present, "
        f"{len(papers) - downloaded - existing} failed."
    )
    return downloaded, existing


def update_manifest(papers: list[Paper], args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).expanduser()
    manifest_path = output_dir / "download_manifest.json"
    existing: list[dict[str, Any]] = []
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload.get("downloads"), list):
                existing = payload["downloads"]
        except (OSError, ValueError):
            existing = []

    current: list[dict[str, Any]] = []
    for paper in papers:
        if not paper.download:
            continue
        record: dict[str, Any] = {
            "title": paper.title,
            "authors": paper.authors,
            "published": paper.published,
            "source": paper.source,
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
            "pmid": paper.pmid,
            "download": paper.download,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if paper.download.get("file"):
            record["file"] = paper.download["file"]
        current.append(record)

    merged: list[dict[str, Any]] = []
    current_keys = {
        (record.get("doi") or record.get("arxiv_id") or record.get("title") or "").lower()
        for record in current
    }
    for record in existing:
        key = (
            record.get("doi") or record.get("arxiv_id") or record.get("title") or ""
        ).lower()
        if key not in current_keys:
            merged.append(record)
    merged.extend(current)
    merged = merged[-500:]

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": getattr(args, "source", ""),
        "query": getattr(args, "query", ""),
        "downloads": merged,
    }
    try:
        tmp = manifest_path.with_name(manifest_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, manifest_path)
    except OSError as exc:
        raise CliError(f"Cannot write download manifest {manifest_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Rendering: text, JSON, BibTeX, RIS, Markdown
# ---------------------------------------------------------------------------


def author_text(authors: Sequence[str], limit: int = 8) -> str:
    if not authors:
        return "Unknown"
    shown = list(authors[:limit])
    suffix = "; et al." if len(authors) > limit else ""
    return "; ".join(shown) + suffix


def render_text(papers: list[Paper], source: str, query: str) -> str:
    lines = [
        f"Search Results: {len(papers)} papers found",
        f"Source: {source}",
        f"Query: {query}",
        "",
    ]
    for index, paper in enumerate(papers, start=1):
        lines.append(f"{index}. {paper.title}")
        lines.append(f"   Authors: {author_text(paper.authors)}")
        if paper.published:
            lines.append(f"   Published: {paper.published}")
        if paper.arxiv_id:
            lines.append(f"   arXiv ID: {paper.arxiv_id}")
        if paper.pmid:
            lines.append(f"   PMID: {paper.pmid}")
        if paper.pmcid:
            lines.append(f"   PMCID: {paper.pmcid}")
        if paper.paper_id and not paper.arxiv_id:
            lines.append(f"   Paper ID: {paper.paper_id}")
        if paper.doi:
            lines.append(f"   DOI: {paper.doi}")
        if paper.venue:
            lines.append(f"   Journal: {paper.venue}")
        if paper.categories:
            lines.append(f"   Categories: {', '.join(paper.categories)}")
        if paper.publication_types:
            lines.append(f"   Publication Types: {', '.join(paper.publication_types)}")
        if paper.citation_count is not None:
            lines.append(f"   Citations: {paper.citation_count}")
        if paper.influential_citation_count is not None:
            lines.append(f"   Influential Citations: {paper.influential_citation_count}")
        if paper.fields_of_study:
            lines.append(f"   Fields: {', '.join(paper.fields_of_study)}")
        if paper.abstract:
            lines.append(f"   Abstract: {paper.abstract}")
        if paper.full_text_url:
            lines.append(f"   Full Text: {paper.full_text_url}")
        if paper.pdf_url:
            lines.append(f"   PDF: {paper.pdf_url}")
        elif paper.url:
            lines.append(f"   URL: {paper.url}")
        if paper.references:
            lines.append(f"   References included: {len(paper.references)}")
        if paper.citing_papers:
            lines.append(f"   Citing papers included: {len(paper.citing_papers)}")
        if paper.download:
            status = paper.download.get("status")
            if status in {"downloaded", "existing"}:
                via = paper.download.get("via")
                via_text = f" via {via}" if via else ""
                lines.append(f"   File: {paper.download.get('file')}{via_text}")
            else:
                lines.append(f"   Download: FAILED — {paper.download.get('error', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(papers: list[Paper], source: str, query: str) -> str:
    lines = [
        f"# Search Results: {len(papers)} papers found",
        "",
        f"**Source:** {source}",
        "",
        f"**Query:** {query}",
        "",
    ]
    for index, paper in enumerate(papers, start=1):
        lines.extend([f"## {index}. {paper.title}", ""])
        lines.append(f"**Authors:** {author_text(paper.authors)}")
        lines.append("")
        meta: list[tuple[str, str]] = [
            ("Published", paper.published),
            ("arXiv ID", paper.arxiv_id),
            ("PMID", paper.pmid),
            ("DOI", paper.doi),
            ("Journal", paper.venue),
            ("Categories", ", ".join(paper.categories)),
            ("Citations", str(paper.citation_count) if paper.citation_count is not None else ""),
            ("Fields", ", ".join(paper.fields_of_study)),
        ]
        for label, value in meta:
            if value:
                lines.extend([f"**{label}:** {value}", ""])
        if paper.abstract:
            lines.extend([f"**Abstract:** {paper.abstract}", ""])
        if paper.full_text_url:
            lines.extend([f"**Full Text:** [Open]({paper.full_text_url})", ""])
        if paper.pdf_url:
            lines.extend([f"**PDF:** [Download]({paper.pdf_url})", ""])
        if paper.download:
            if paper.download.get("status") in {"downloaded", "existing"}:
                lines.extend([f"**File:** `{paper.download.get('file')}`", ""])
            else:
                lines.extend(
                    [f"**Download:** FAILED — {paper.download.get('error', '')}", ""]
                )
    return "\n".join(lines).rstrip() + "\n"


def render_json(papers: list[Paper], with_keys: bool = False) -> str:
    if not with_keys:
        payload = [paper.to_dict() for paper in papers]
    else:
        keys = citation_keys(papers)
        payload = []
        for paper, key in zip(papers, keys):
            item = paper.to_dict()
            item["citation_key"] = key
            payload.append(item)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def latex_escape(value: str) -> str:
    value = (value or "").replace("{", r"\{").replace("}", r"\}")
    value = value.replace("\\", r"\textbackslash{}")
    for char, replacement in (
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ):
        value = value.replace(char, replacement)
    return value


def bibtex_author(name: str) -> str:
    name = normalize_space(name)
    if "," in name or not name:
        return name
    match = re.match(r"^(.+?)\s+([A-Z]{1,3})$", name)
    if match:
        return f"{match.group(1)}, {match.group(2)}"
    return name


def citation_keys(papers: Sequence[Paper]) -> list[str]:
    used: dict[str, int] = {}
    keys: list[str] = []
    stop = {
        "a",
        "an",
        "the",
        "on",
        "of",
        "for",
        "and",
        "to",
        "in",
        "with",
        "from",
        "using",
        "via",
        "toward",
        "towards",
        "is",
        "are",
    }
    for paper in papers:
        last = first_author_last_name(paper.authors)
        last = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", last).encode("ascii", "ignore").decode().lower())
        last = last or "anon"
        year = year_of(paper) or "nd"
        ascii_title = (
            unicodedata.normalize("NFKD", paper.title or "")
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        words = re.findall(r"[A-Za-z0-9]+", ascii_title)
        word = next((w.lower() for w in words if w.lower() not in stop), "paper")
        base = f"{last}{year}{word}"
        count = used.get(base, 0)
        if count == 0:
            key = base
        else:
            suffix = ""
            number = count - 1
            while True:
                suffix = chr(ord("a") + number % 26) + suffix
                number = number // 26 - 1
                if number < 0:
                    break
            key = base + suffix
        used[base] = count + 1
        keys.append(key)
    return keys


def render_bibtex(papers: list[Paper]) -> str:
    keys = citation_keys(papers)
    entries: list[str] = []
    for paper, key in zip(papers, keys):
        fields: list[tuple[str, str]] = []
        fields.append(("title", latex_escape(paper.title or "Untitled")))
        if paper.authors:
            fields.append(
                (
                    "author",
                    " and ".join(latex_escape(bibtex_author(a)) for a in paper.authors),
                )
            )
        if paper.arxiv_id:
            fields.append(("journal", f"arXiv preprint arXiv:{latex_escape(paper.arxiv_id)}"))
        elif paper.venue:
            fields.append(("journal", latex_escape(paper.venue)))
        year = year_of(paper)
        if year:
            fields.append(("year", year))
        if paper.doi:
            fields.append(("doi", latex_escape(paper.doi)))
        link = paper.url or paper.pdf_url
        if link:
            fields.append(("url", latex_escape(link)))
        if paper.arxiv_id:
            fields.append(("eprint", latex_escape(paper.arxiv_id)))
            fields.append(("archivePrefix", "arXiv"))
            if paper.categories:
                fields.append(("primaryClass", latex_escape(paper.categories[0])))
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
        entries.append(f"@article{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def render_ris(papers: list[Paper]) -> str:
    entries: list[str] = []
    for paper in papers:
        lines = ["TY  - JOUR", f"TI  - {paper.title or 'Untitled'}"]
        lines.extend(f"AU  - {author}" for author in paper.authors)
        year = year_of(paper)
        if year:
            lines.append(f"PY  - {year}")
        if paper.published:
            lines.append(f"DA  - {paper.published.replace('-', '/')}")
        if paper.arxiv_id:
            lines.append(f"JO  - arXiv preprint")
            lines.append(f"VL  - arXiv:{paper.arxiv_id}")
        elif paper.venue:
            lines.append(f"JO  - {paper.venue}")
        if paper.doi:
            lines.append(f"DO  - {paper.doi}")
        if paper.abstract:
            lines.append(f"AB  - {normalize_space(paper.abstract)}")
        if paper.url:
            lines.append(f"UR  - {paper.url}")
        elif paper.pdf_url:
            lines.append(f"UR  - {paper.pdf_url}")
        lines.append("ER  - ")
        entries.append("\n".join(lines))
    return "\n\n".join(entries) + ("\n" if entries else "")


def render_citations(papers: list[Paper], fmt: str) -> str:
    if fmt == "bibtex":
        return render_bibtex(papers)
    if fmt == "ris":
        return render_ris(papers)
    if fmt == "json":
        return render_json(papers, with_keys=True)
    raise CliError(f"Unsupported citation format: {fmt}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py",
        description=(
            "Search arXiv, PubMed, Semantic Scholar, or Google Scholar; export "
            "BibTeX/RIS/JSON/Markdown; download PDFs with an automatic Sci-Hub "
            "fallback when a direct open-access PDF is unavailable."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python scripts/research.py arxiv "deep learning" --download --output-dir papers/\n'
            '  python scripts/research.py arxiv "transformers" --year 2023 --max-results 5\n'
            '  python scripts/research.py pubmed "cancer immunotherapy" --full-text\n'
            '  python scripts/research.py semantic "BERT" --min-citations 100 --include-references\n'
            '  python scripts/research.py scholar "graph neural networks" --max-results 10\n'
            '  python scripts/research.py scihub "10.1038/s41586-020-2649-2" --download\n'
            '  python scripts/research.py arxiv "NLP" --format bibtex --output refs.bib\n'
            "\n"
            "Legal: use Sci-Hub fallback only where you are authorized to access "
            "the paper. Prefer arXiv, PubMed Central, and other open-access copies.\n"
        ),
    )
    parser.add_argument(
        "source",
        metavar="SOURCE",
        help="arxiv | pubmed | semantic | scholar | scihub (aliases: semanticscholar, google-scholar)",
    )
    parser.add_argument("query", help="Search query, or a DOI when SOURCE is scihub.")
    parser.add_argument(
        "-n",
        "--max-results",
        type=int,
        default=10,
        help="Maximum results (1-100, default: 10).",
    )
    parser.add_argument(
        "-f",
        "--format",
        dest="output_format",
        choices=["text", "json", "bibtex", "ris", "markdown"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument("-o", "--output", help="Write results to this file.")
    parser.add_argument(
        "--sort-by",
        choices=["relevance", "date", "citations"],
        default="relevance",
        help="Sort order (citation sorting is native on Semantic Scholar).",
    )
    parser.add_argument("--year", type=int, help="Filter to one publication year.")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD).")
    parser.add_argument("--author", help="Author filter (best effort on Semantic Scholar/Scholar).")
    parser.add_argument("--min-citations", type=int, help="Minimum citation count.")
    parser.add_argument(
        "--category",
        action="append",
        help="arXiv category, e.g. cs.LG. Repeat for OR-matching categories.",
    )
    parser.add_argument(
        "--publication-type",
        action="append",
        help="PubMed publication type, e.g. 'Clinical Trial'. Repeatable.",
    )
    parser.add_argument(
        "--full-text",
        action="store_true",
        help="Include PubMed Central full-text links when available.",
    )
    parser.add_argument(
        "--include-references",
        action="store_true",
        help="Include reference lists (Semantic Scholar only).",
    )
    parser.add_argument(
        "--include-citations",
        action="store_true",
        help="Include citing-paper lists (Semantic Scholar only).",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download PDFs, trying direct/open-access URLs first, then Sci-Hub.",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Directory for downloaded PDFs and manifest (default: downloads/).",
    )
    parser.add_argument(
        "--citations",
        action="store_true",
        help="Export citation records for the results instead of the normal format.",
    )
    parser.add_argument(
        "--citation-format",
        choices=["bibtex", "ris", "json"],
        default="bibtex",
        help="Citation format used with --citations (default: bibtex).",
    )
    parser.add_argument("--no-scihub", action="store_true", help="Disable Sci-Hub fallback.")
    parser.add_argument(
        "--scihub-only",
        action="store_true",
        help="Skip direct URLs and use Sci-Hub mirrors immediately (requires --download).",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Polite delay in seconds between downloads/mirrors (default: 1.0).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose diagnostics on stderr.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def canonical_source(value: str) -> str:
    aliases = {
        "arxiv": "arxiv",
        "pubmed": "pubmed",
        "semantic": "semantic",
        "semanticscholar": "semantic",
        "semantic-scholar": "semantic",
        "scholar": "scholar",
        "google-scholar": "scholar",
        "google_scholar": "scholar",
        "scihub": "scihub",
        "sci-hub": "scihub",
    }
    source = aliases.get((value or "").strip().lower())
    if not source:
        raise CliError(
            "Unknown SOURCE. Use arxiv, pubmed, semantic, scholar, or scihub."
        )
    return source


def validate_args(args: argparse.Namespace) -> tuple[str, date | None, date | None]:
    args.source = canonical_source(args.source)
    if not 1 <= args.max_results <= 100:
        raise CliError("--max-results must be between 1 and 100.")
    if args.min_citations is not None and args.min_citations < 0:
        raise CliError("--min-citations cannot be negative.")
    if args.timeout <= 0:
        raise CliError("--timeout must be positive.")
    if args.delay < 0:
        raise CliError("--delay cannot be negative.")
    if args.no_scihub and args.scihub_only:
        raise CliError("--no-scihub and --scihub-only are mutually exclusive.")
    if args.source == "scihub":
        args.query = normalize_doi(args.query)
        if not args.query:
            raise CliError("SOURCE=scihub requires a valid DOI as the query.")
        if args.no_scihub:
            raise CliError("SOURCE=scihub cannot be combined with --no-scihub.")
        args.download = True
    if args.scihub_only and not args.download:
        raise CliError("--scihub-only requires --download.")
    if args.download and not normalize_space(args.output_dir):
        raise CliError("--output-dir cannot be empty.")
    start, end = resolve_dates(args)
    return args.source, start, end


def warn_unsupported(args: argparse.Namespace, source: str) -> None:
    if source in {"arxiv", "pubmed"} and args.sort_by == "citations":
        log("Warning: citation sorting is unavailable for this source; using relevance/date.")
    if source in {"arxiv", "pubmed"} and args.min_citations is not None:
        log("Warning: --min-citations is ignored because this source does not expose citation counts.")
    if source != "pubmed" and args.publication_type:
        log("Warning: --publication-type only applies to PubMed.")
    if source != "arxiv" and args.category:
        log("Warning: --category only applies to arXiv.")
    if source != "pubmed" and args.full_text:
        log("Warning: --full-text only affects PubMed results.")
    if source != "semantic" and (args.include_references or args.include_citations):
        log("Warning: reference/citing-paper expansion is only implemented for Semantic Scholar.")
    if source == "scholar" and args.download:
        log("Warning: Google Scholar has no reliable PDF/DOI field; downloads will often fail.")
    if source == "scihub":
        log("Warning: source=scihub uses a Sci-Hub fallback. Use it only where lawful.")


def dispatch(
    client: HttpClient,
    source: str,
    args: argparse.Namespace,
    start: date | None,
    end: date | None,
) -> list[Paper]:
    if source == "arxiv":
        return search_arxiv(client, args, start, end)
    if source == "pubmed":
        return search_pubmed(client, args, start, end)
    if source == "semantic":
        return search_semantic(client, args, start, end)
    if source == "scholar":
        return search_scholar(client, args, start, end)
    if source == "scihub":
        return [fetch_crossref(client, args.query, args.verbose)]
    raise CliError(f"Unsupported source: {source}")


def write_output(path_value: str, content: str) -> None:
    path = Path(path_value).expanduser()
    if path.exists() and path.is_dir():
        raise CliError(f"--output points to a directory: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise CliError(f"Cannot write {path}: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source, start, end = validate_args(args)
        warn_unsupported(args, source)
        client = HttpClient(timeout=args.timeout, verbose=args.verbose)
        papers = dispatch(client, source, args, start, end)
        papers = dedupe(papers)
        if not papers:
            log("No results found. Try broader terms or remove restrictive filters.")

        exit_code = 0
        downloaded = existing = 0
        if args.download and papers:
            downloaded, existing = download_all(client, papers, args)
            if downloaded + existing == 0:
                exit_code = 4

        if args.citations:
            content = render_citations(papers, args.citation_format)
        elif args.output_format == "json":
            content = render_json(papers)
        elif args.output_format == "bibtex":
            content = render_bibtex(papers)
        elif args.output_format == "ris":
            content = render_ris(papers)
        elif args.output_format == "markdown":
            content = render_markdown(papers, source, args.query)
        else:
            content = render_text(papers, source, args.query)

        if args.output:
            write_output(args.output, content)
            log(f"Results saved to {args.output}")
        else:
            sys.stdout.write(content)
            if content and not content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        return exit_code
    except CliError as exc:
        log(f"error: {exc}")
        return 2
    except DependencyError as exc:
        log(f"dependency error: {exc}")
        return 3
    except ApiError as exc:
        log(f"API error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
