"""Offline regression tests for the academic paper retriever.

Run from the skill directory:
    python3 -m unittest discover -s tests -v

The tests never contact a live academic API or Sci-Hub; HTTP is faked or served
from 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research  # noqa: E402


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        content: bytes = b"",
        payload: object | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
        url: str = "https://example.test/",
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload

    def close(self) -> None:
        pass


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        return self.responses.pop(0)


class RetrieverTests(unittest.TestCase):
    def test_doi_normalization(self) -> None:
        self.assertEqual(
            research.normalize_doi("https://doi.org/10.1038/s41586-020-2649-2"),
            "10.1038/s41586-020-2649-2",
        )
        self.assertEqual(
            research.normalize_doi("doi:10.1000/xyz."),
            "10.1000/xyz",
        )
        self.assertEqual(research.normalize_doi("not a doi"), "")

    def test_author_and_filename(self) -> None:
        self.assertEqual(research.first_author_last_name(["Morrison AH"]), "Morrison")
        self.assertEqual(research.first_author_last_name(["Julius Berner"]), "Berner")
        self.assertEqual(
            research.first_author_last_name(["Vaswani, Ashish"]), "Vaswani"
        )
        paper = research.Paper(
            title="Immunotherapy and Prevention of Pancreatic Cancer.",
            authors=["Morrison AH", "Byrne KT"],
            published="2018-06",
            pmid="29860986",
        )
        self.assertEqual(
            research.paper_filename(paper),
            "Morrison_2018_Immunotherapy-and-Prevention-of-Pancreatic-Cancer_29860986.pdf",
        )

    def test_citation_key_handles_accents(self) -> None:
        paper = research.Paper(
            title="Déjà vu: A Contextualized Temporal Attention Mechanism",
            authors=["Jibang Wu"],
            published="2020-01-29",
        )
        self.assertEqual(research.citation_keys([paper]), ["wu2020deja"])

    def test_citation_keys_are_unique(self) -> None:
        papers = [
            research.Paper(title="Same title", authors=["Smith J"], published="2024"),
            research.Paper(title="Same title", authors=["Smith J"], published="2024"),
        ]
        keys = research.citation_keys(papers)
        self.assertEqual(keys[0], "smith2024same")
        self.assertEqual(keys[1], "smith2024samea")

    def test_bibtex_and_ris(self) -> None:
        paper = research.Paper(
            title="A & B_100%",
            authors=["Doe, Jane"],
            published="2024-02-03",
            doi="10.1000/example",
            arxiv_id="2401.00001",
            pdf_url="https://arxiv.org/pdf/2401.00001",
        )
        bibtex = research.render_bibtex([paper])
        self.assertIn("@article{doe2024b", bibtex)
        self.assertIn(r"A \& B\_100\%", bibtex)
        self.assertIn("10.1000/example", bibtex)
        ris = research.render_ris([paper])
        self.assertIn("TY  - JOUR", ris)
        self.assertIn("DA  - 2024/02/03", ris)
        self.assertIn("DO  - 10.1000/example", ris)

    def test_json_contains_download_status(self) -> None:
        paper = research.Paper(title="Test", authors=["Doe, Jane"], source="arXiv")
        paper.download = {"status": "downloaded", "file": "papers/test.pdf"}
        payload = json.loads(research.render_json([paper]))
        self.assertEqual(payload[0]["download"]["status"], "downloaded")

    def test_extract_pdf_candidates(self) -> None:
        markup = """
        <html><head>
        <meta name="citation_pdf_url" content="https://sci-hub.se/downloads/a.pdf">
        </head><body>
        <iframe src="//sci.bban.top/pdf/10.1/x.pdf#navpaned=1"></iframe>
        <script>location.href='\\/\\/sci.bban.top\\/pdf\\/10.1\\/y.pdf';</script>
        </body></html>
        """
        candidates = research.extract_pdf_candidates(
            markup, "https://sci-hub.ren/10.1/x"
        )
        self.assertIn("https://sci-hub.se/downloads/a.pdf", candidates)
        self.assertIn("https://sci.bban.top/pdf/10.1/x.pdf", candidates)
        self.assertIn("https://sci.bban.top/pdf/10.1/y.pdf", candidates)
        self.assertNotIn("https://sci-hub.ren/\\/\\/sci.bban.top", candidates)

    def test_pubmed_uses_own_ids_not_reference_ids(self) -> None:
        xml = b"""<?xml version="1.0"?>
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>123456</PMID>
              <Article>
                <ArticleTitle>Test article</ArticleTitle>
                <Abstract><AbstractText Label="BACKGROUND">Body text.</AbstractText></Abstract>
                <Journal>
                  <ISOAbbreviation>J Test</ISOAbbreviation>
                  <JournalIssue><PubDate><Year>2024</Year><Month>Jan</Month><Day>2</Day></PubDate></JournalIssue>
                </Journal>
                <AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author></AuthorList>
                <PublicationTypeList><PublicationType>Review</PublicationType></PublicationTypeList>
              </Article>
            </MedlineCitation>
            <PubmedData>
              <ArticleIdList>
                <ArticleId IdType="doi">10.1000/correct</ArticleId>
                <ArticleId IdType="pmc">12345</ArticleId>
              </ArticleIdList>
            </PubmedData>
            <ReferenceList>
              <Reference><ArticleIdList>
                <ArticleId IdType="doi">10.1000/wrong</ArticleId>
                <ArticleId IdType="pmc">99999</ArticleId>
              </ArticleIdList></Reference>
            </ReferenceList>
          </PubmedArticle>
        </PubmedArticleSet>
        """
        client = FakeClient(
            [
                FakeResponse(payload={"esearchresult": {"idlist": ["123456"]}}),
                FakeResponse(content=xml),
            ]
        )
        args = Namespace(
            query="test",
            author=None,
            publication_type=None,
            max_results=10,
            sort_by="relevance",
            full_text=False,
        )
        papers = research.search_pubmed(client, args, None, None)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].doi, "10.1000/correct")
        self.assertEqual(papers[0].pmcid, "PMC12345")
        self.assertEqual(papers[0].published, "2024-01-02")

    def test_semantic_parsing_and_author_filter(self) -> None:
        payload = {
            "data": [
                {
                    "paperId": "abc",
                    "title": "Graph Networks",
                    "authors": [{"name": "Alice Smith"}, {"name": "Bob Jones"}],
                    "abstract": "Graph abstract",
                    "year": 2022,
                    "publicationDate": "2022-03-01",
                    "venue": "TestConf",
                    "journal": {"name": "Test Journal"},
                    "externalIds": {"DOI": "10.1000/graph", "ArXiv": "2201.00001"},
                    "openAccessPdf": {"url": "https://example.test/graph.pdf"},
                    "citationCount": 42,
                    "influentialCitationCount": 4,
                    "referenceCount": 10,
                    "fieldsOfStudy": ["Computer Science"],
                    "publicationTypes": ["JournalArticle"],
                    "url": "https://semanticscholar.org/paper/abc",
                },
                {
                    "paperId": "def",
                    "title": "Other",
                    "authors": [{"name": "Carol Brown"}],
                    "externalIds": {},
                    "citationCount": 1,
                },
            ]
        }
        client = FakeClient([FakeResponse(payload=payload)])
        args = Namespace(
            query="graph",
            max_results=2,
            author="Alice Smith",
            sort_by="relevance",
            min_citations=None,
            year=None,
            start_date=None,
            end_date=None,
            include_references=False,
            include_citations=False,
        )
        papers = research.search_semantic(client, args, None, None)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].doi, "10.1000/graph")
        self.assertEqual(papers[0].arxiv_id, "2201.00001")
        self.assertEqual(papers[0].citation_count, 42)


class DownloadTests(unittest.TestCase):
    def test_scihub_fallback_against_local_server(self) -> None:
        pdf_bytes = b"%PDF-1.4\n" + b"x" * 2048 + b"\n%%EOF\n"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/paper.pdf"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(pdf_bytes)))
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
                    return
                page = (
                    b"<html><body><iframe src=\"/paper.pdf#navpaned=1\"></iframe>"
                    b"</body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, format, *args):  # noqa: A002
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_mirrors = os.environ.get("PAPERHUB_SCIHUB_MIRRORS")
        try:
            os.environ["PAPERHUB_SCIHUB_MIRRORS"] = (
                f"http://127.0.0.1:{server.server_port}"
            )
            with tempfile.TemporaryDirectory() as directory:
                args = Namespace(
                    output_dir=directory,
                    no_scihub=False,
                    scihub_only=True,
                    delay=0.0,
                    verbose=False,
                )
                client = research.HttpClient(timeout=10.0)
                paper = research.Paper(
                    title="Fallback Test",
                    authors=["Doe, Jane"],
                    published="2024",
                    doi="10.1000/fallback",
                    source="Sci-Hub",
                )
                outcome = research.download_paper(client, paper, args)
                self.assertEqual(outcome.status, "downloaded", outcome.error)
                self.assertEqual(outcome.via, "scihub")
                self.assertTrue(Path(outcome.file).exists())
                self.assertTrue(research.looks_like_pdf(Path(outcome.file)))
                paper.download = outcome.to_dict()
                research.update_manifest([paper], args)
                manifest = json.loads(
                    (Path(directory) / "download_manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(manifest["downloads"][0]["download"]["status"], "downloaded")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if old_mirrors is None:
                os.environ.pop("PAPERHUB_SCIHUB_MIRRORS", None)
            else:
                os.environ["PAPERHUB_SCIHUB_MIRRORS"] = old_mirrors


if __name__ == "__main__":
    unittest.main()
