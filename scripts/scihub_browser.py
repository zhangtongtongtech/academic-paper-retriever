#!/usr/bin/env python3
"""Human-in-the-loop Sci-Hub browser handoff.

When Sci-Hub presents a CAPTCHA / Cloudflare / DDoS-Guard challenge, this
helper opens a visible Chrome window, lets the human complete the challenge,
then detects the PDF and downloads it with the browser's cookies.

It never solves, bypasses, or automates a CAPTCHA.  The browser window is the
handoff point; the human remains the verifier.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import research  # noqa: E402

CHALLENGE_MARKERS = (
    "captcha",
    "just a moment",
    "verify you are human",
    "checking your browser",
    "ddos-guard",
    "cf-chl",
    "attention required",
)

PDF_EXTRACT_JS = r"""
() => {
  const urls = [];
  const meta = document.querySelector('meta[name="citation_pdf_url"]');
  if (meta && meta.content) urls.push(meta.content);

  const nodes = document.querySelectorAll(
    'iframe#pdf, embed#pdf, iframe[src*=".pdf"], embed[src*=".pdf"], ' +
    'iframe[src*="/download"], embed[src*="/download"], object[data*=".pdf"]'
  );
  for (const node of nodes) {
    const value = node.src || node.data;
    if (value) urls.push(value);
  }

  for (const anchor of document.querySelectorAll('a[href]')) {
    const href = anchor.getAttribute('href') || '';
    if (/\.pdf(?:[?#]|$)/i.test(href) || href.includes('/download')) {
      urls.push(anchor.href);
    }
  }
  return urls;
}
"""


class BrowserError(Exception):
    """Expected browser-handoff failure."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def debug(enabled: bool, message: str) -> None:
    if enabled:
        log("[debug] " + message)


def normalize_candidate(raw: str, base_url: str) -> str:
    if not raw:
        return ""
    value = str(raw).strip().strip("\"'")
    if not value or value.startswith(("data:", "javascript:", "mailto:", "#")):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    elif not value.startswith(("http://", "https://")):
        value = urljoin(base_url, value)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunparse(parsed._replace(fragment=""))


def looks_like_challenge(title: str, body: str) -> bool:
    haystack = f"{title}\n{body[:6000]}".lower()
    return any(marker in haystack for marker in CHALLENGE_MARKERS)


def _challenge_kind(page) -> str:
    """Return a challenge kind, or an empty string for a normal page."""
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except Exception:
        body = ""
    haystack = f"{title}\n{body[:6000]}".lower()
    if not looks_like_challenge(title, body):
        return ""
    # A CAPTCHA is worth handing to the human even if the page also carries a
    # DDoS-Guard footer. Pure DDoS-Guard 403 pages are not interactive.
    if "captcha" in haystack or "verify you are human" in haystack:
        return "captcha"
    if "just a moment" in haystack or "cf-chl" in haystack:
        return "cloudflare"
    if "ddos-guard" in haystack:
        return "ddos"
    return "captcha"


def _extract_pdf_url(page) -> str:
    candidates: list[str] = []
    try:
        values = page.evaluate(PDF_EXTRACT_JS)
    except Exception:
        values = []
    if isinstance(values, list):
        candidates.extend(str(value) for value in values if value)

    try:
        if page.evaluate("document.contentType") == "application/pdf":
            candidates.append(page.url)
    except Exception:
        pass

    for frame in page.frames:
        frame_url = getattr(frame, "url", "")
        if ".pdf" in frame_url.lower():
            candidates.append(frame_url)

    for raw in candidates:
        candidate = normalize_candidate(raw, page.url)
        if candidate:
            return candidate
    return ""


def _launch_context(playwright, args):
    profile = Path(args.profile).expanduser()
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserError(f"Cannot create browser profile directory {profile}: {exc}") from exc

    options = {
        "headless": args.headless,
        "accept_downloads": True,
        "viewport": {"width": 1440, "height": 1000},
        "args": [
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if args.executable:
        options["executable_path"] = str(Path(args.executable).expanduser())
    elif args.channel:
        options["channel"] = args.channel

    try:
        return playwright.chromium.launch_persistent_context(str(profile), **options)
    except Exception as exc:
        if options.get("channel"):
            log(
                f"Warning: could not launch browser channel '{options['channel']}' "
                f"({type(exc).__name__}: {exc}); trying bundled Chromium."
            )
            options.pop("channel", None)
            try:
                return playwright.chromium.launch_persistent_context(
                    str(profile), **options
                )
            except Exception as fallback_exc:
                raise BrowserError(
                    "Could not launch Chrome or Chromium. Install Playwright's "
                    "browser with: python -m playwright install chromium. "
                    f"Last error: {type(fallback_exc).__name__}: {fallback_exc}"
                ) from fallback_exc
        raise BrowserError(
            "Could not launch a visible browser. Install Playwright and a browser "
            "with: python -m pip install playwright && python -m playwright install chromium. "
            f"Last error: {type(exc).__name__}: {exc}"
        ) from exc


def _wait_for_pdf(
    page,
    deadline: float,
    poll: float,
    verbose: bool,
    wait_for_human: bool,
) -> str:
    challenge_announced = False
    normal_deadline = min(deadline, time.monotonic() + 6.0)
    previous_kind = "unknown"
    while time.monotonic() < deadline:
        if page.is_closed():
            raise BrowserError("The browser window was closed before a PDF was found.")
        pdf_url = _extract_pdf_url(page)
        if pdf_url:
            debug(verbose, f"detected PDF URL: {pdf_url}")
            return pdf_url

        kind = _challenge_kind(page)
        if kind != previous_kind:
            if not kind:
                # Give the viewer a short grace period after the human solves
                # a challenge and the page becomes a normal PDF viewer.
                normal_deadline = min(deadline, time.monotonic() + 10.0)
            previous_kind = kind
        if kind and not wait_for_human:
            debug(verbose, f"challenge detected ({kind}); deferring this mirror")
            return ""
        if kind and not challenge_announced:
            challenge_announced = True
            page.bring_to_front()
            if kind == "ddos":
                log("  [verification] DDoS-Guard block page; waiting briefly for its JS check.")
            else:
                log("  [verification] This mirror is asking for human verification.")
                log("  [verification] Complete the CAPTCHA/Cloudflare check in the browser.")
                log("  [verification] Waiting; the download continues automatically afterwards.")
        if kind == "ddos" and time.monotonic() >= normal_deadline:
            return ""
        if not kind and time.monotonic() >= normal_deadline:
            return ""

        page.wait_for_timeout(int(poll * 1000))

    return ""


def _download_pdf(context, page, pdf_url: str, destination: Path):
    headers = {"Referer": page.url}
    try:
        response = context.request.get(
            pdf_url,
            headers=headers,
            timeout=120_000,
            fail_on_status_code=False,
        )
        if response.ok:
            data = response.body()
            ok, error, size, digest = research.save_pdf_bytes(data, destination)
            if ok:
                return True, "", size, digest
            log(f"  [browser] PDF URL returned invalid data: {error}")
        else:
            log(f"  [browser] PDF request failed: HTTP {response.status}")
    except Exception as exc:
        log(f"  [browser] PDF request failed: {type(exc).__name__}: {exc}")

    # Last resort: navigate the browser to the PDF URL. Some mirrors only
    # expose the file through the visible browser session.
    try:
        with page.expect_download(timeout=45_000) as download_info:
            page.goto(pdf_url, wait_until="domcontentloaded", timeout=45_000)
        download = download_info.value
        download.save_as(str(destination))
        if research.looks_like_pdf(destination):
            return (
                True,
                "",
                destination.stat().st_size,
                research.hash_file(destination),
            )
        return False, "browser download was not a valid PDF", 0, ""
    except Exception as exc:
        return False, f"browser download failed: {type(exc).__name__}: {exc}", 0, ""


def _save_result(
    context,
    page,
    pdf_url: str,
    destination: Path,
    paper: research.Paper,
    mirror: str,
    args,
) -> bool:
    log(f"  [browser] detected PDF: {pdf_url}")
    ok, error, size, digest = _download_pdf(context, page, pdf_url, destination)
    if not ok:
        log(f"  [browser] download failed: {error}")
        return False

    paper.download = research.DownloadOutcome(
        status="downloaded",
        file=str(destination),
        via="browser",
        url=pdf_url,
        mirror=mirror,
        bytes=size,
        sha256=digest,
    ).to_dict()
    research.update_manifest(
        [paper],
        SimpleNamespace(
            source="scihub-browser",
            query=paper.doi,
            output_dir=str(args.output_dir),
        ),
    )
    log(f"  saved: {destination} ({size:,} bytes)")
    _emit(paper, args.format)
    return True


def _emit(paper: research.Paper, output_format: str) -> None:
    if output_format == "json":
        sys.stdout.write(json.dumps(paper.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return
    download = paper.download or {}
    print(f"Title: {paper.title}")
    print(f"DOI: {paper.doi}")
    print(f"Status: {download.get('status', 'failed')}")
    print(f"File: {download.get('file', '')}")
    print(f"Mirror: {download.get('mirror', '')}")
    print(f"Bytes: {download.get('bytes', 0)}")
    if download.get("sha256"):
        print(f"SHA-256: {download['sha256']}")
    if download.get("error"):
        print(f"Error: {download['error']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scihub_browser.py",
        description=(
            "Open a visible browser for manual Sci-Hub CAPTCHA/Cloudflare "
            "verification, then automatically detect and download the PDF. "
            "This is a human-in-the-loop handoff; it does not solve CAPTCHAs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python scripts/scihub_browser.py "10.1053/j.gastro.2018.03.029"\n'
            '  python scripts/scihub_browser.py "10.1053/j.gastro.2018.03.029" \\\n'
            '    --output-dir papers/ --timeout 300\n'
            "\n"
            "If Chrome is installed, Playwright can use it without downloading\n"
            "a separate Chromium: python -m pip install playwright\n"
            "Otherwise run: python -m playwright install chromium\n"
        ),
    )
    parser.add_argument("doi", help="DOI to fetch.")
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Directory for the PDF and manifest (default: downloads/).",
    )
    parser.add_argument(
        "--mirror",
        action="append",
        help="Override mirror base URL; repeatable. Defaults to the skill's mirror list.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=240.0,
        help="Total human-verification wait in seconds (default: 240).",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=2.0,
        help="Polling interval while waiting for the PDF (default: 2s).",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help="Playwright browser channel (default: chrome; use chromium/msedge if needed).",
    )
    parser.add_argument(
        "--executable",
        help="Path to a specific Chrome/Chromium executable.",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get(
            "PAPERHUB_BROWSER_PROFILE",
            str(Path.home() / ".cache" / "academic-paper-retriever" / "browser-profile"),
        ),
        help="Persistent browser profile directory for cookies/session.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (only useful for local testing; CAPTCHA needs a visible window).",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave the browser open after the attempt; press Enter to close.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Summary output format (default: text).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose diagnostics.")
    parser.add_argument("--version", action="version", version="%(prog)s 1.1.0")
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    doi = research.normalize_doi(args.doi)
    if not doi:
        log("error: a valid DOI is required.")
        return 2
    if args.timeout <= 0 or args.poll <= 0:
        log("error: --timeout and --poll must be positive.")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log(
            "dependency error: browser handoff requires Playwright.\n"
            "Install with: python -m pip install playwright\n"
            "If Google Chrome is installed, no extra browser download is needed.\n"
            "Otherwise run: python -m playwright install chromium"
        )
        return 3

    output_dir = Path(args.output_dir).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"error: cannot create output directory {output_dir}: {exc}")
        return 2

    log("Warning: Sci-Hub is used as a fallback. Use it only where lawful.")
    log("A browser window will open. Complete any human verification manually.")

    try:
        client = research.HttpClient(timeout=30.0, verbose=args.verbose)
        paper = research.fetch_crossref(client, doi, args.verbose)
    except Exception as exc:
        log(f"Warning: Crossref metadata unavailable ({type(exc).__name__}: {exc}).")
        paper = research.Paper(title=f"DOI {doi}", source="Sci-Hub", doi=doi)

    filename = research.paper_filename(paper)
    destination = output_dir / filename
    if research.looks_like_pdf(destination):
        paper.download = {
            "status": "existing",
            "file": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": research.hash_file(destination),
        }
        _emit(paper, args.format)
        return 0

    mirrors = args.mirror or research.scihub_mirrors()
    mirrors = [m.rstrip("/") for m in mirrors if m.strip()]
    deadline = time.monotonic() + args.timeout
    last_error = "no mirror attempted"

    try:
        with sync_playwright() as playwright:
            context = _launch_context(playwright, args)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(45_000)
            challenge_pages: list[tuple[str, str]] = []
            try:
                # Pass 1: look for a PDF that loads without human interaction.
                # CAPTCHA pages are remembered and visited only after the other
                # mirrors have been tried, so a working non-CAPTCHA mirror wins.
                for mirror in mirrors:
                    if not mirror.startswith(("http://", "https://")):
                        mirror = "https://" + mirror
                    page_url = f"{mirror}/{quote(doi, safe='')}"
                    log(f"[browser] opening {page_url}")
                    try:
                        page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception as exc:
                        last_error = f"{mirror}: {type(exc).__name__}: {exc}"
                        log(f"  [browser] navigation failed: {last_error}")
                        continue
                    page.bring_to_front()

                    kind = _challenge_kind(page)
                    if kind in {"captcha", "cloudflare"}:
                        challenge_pages.append((mirror, page.url))
                        last_error = f"{mirror}: human verification required"
                        log("  [verification] challenge detected; deferring until other mirrors are tried.")
                        continue

                    pdf_url = _wait_for_pdf(
                        page, deadline, args.poll, args.verbose, wait_for_human=False
                    )
                    if pdf_url:
                        if _save_result(
                            context, page, pdf_url, destination, paper, mirror, args
                        ):
                            return 0
                        last_error = f"{mirror}: PDF download failed"
                        continue

                    last_error = f"{mirror}: no PDF link found"
                    log("  [browser] no PDF detected; trying next mirror.")
                    if page.is_closed() or time.monotonic() >= deadline:
                        break

                # Pass 2: hand a real CAPTCHA/Cloudflare page to the human.
                if challenge_pages and time.monotonic() < deadline and not page.is_closed():
                    mirror, challenge_url = challenge_pages[0]
                    log(f"[browser] opening deferred verification page: {challenge_url}")
                    try:
                        page.goto(
                            challenge_url,
                            wait_until="domcontentloaded",
                            timeout=45_000,
                        )
                    except Exception as exc:
                        last_error = (
                            f"{mirror}: verification page navigation failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        log(f"  [browser] {last_error}")
                    else:
                        page.bring_to_front()
                        pdf_url = _wait_for_pdf(
                            page,
                            deadline,
                            args.poll,
                            args.verbose,
                            wait_for_human=True,
                        )
                        if pdf_url and _save_result(
                            context, page, pdf_url, destination, paper, mirror, args
                        ):
                            return 0
                        last_error = f"{mirror}: no PDF after human verification"
            finally:
                if args.keep_open:
                    log("Browser left open; press Enter to close it.")
                    try:
                        input()
                    except EOFError:
                        pass
                context.close()
    except BrowserError as exc:
        log(f"browser error: {exc}")
        return 2
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130

    paper.download = research.DownloadOutcome(
        status="failed", error=last_error
    ).to_dict()
    research.update_manifest(
        [paper],
        SimpleNamespace(
            source="scihub-browser",
            query=doi,
            output_dir=str(output_dir),
        ),
    )
    _emit(paper, args.format)
    log(f"All browser attempts failed: {last_error}")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
