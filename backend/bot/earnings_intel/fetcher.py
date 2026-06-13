"""Stage-19 — EDGAR earnings-release fetcher.

For every 8-K with item 2.02 (results of operations) we have a cached
``edgar_filings`` row carrying the accession number + primary document
filename. The actual press release is usually Exhibit 99.1, accessible
at::

  https://www.sec.gov/Archives/edgar/data/{cik}/{acc-no-no-dashes}/

The directory lists files; we look for ``ex99*.htm`` / ``ex_99-1.htm``
patterns and grab the first match. Content is HTML; we strip tags to
get plain text for the extractor.

No new credentials — uses the same ``TB_SEC_USER_AGENT`` env var.
Tolerant: any missing piece returns ``None`` so callers fall through.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from backend.bot.data.edgar import EdgarClient

logger = logging.getLogger(__name__)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_EX99_RE = re.compile(r"(?i)(ex.?99[\-_.]?[12]?|exhibit.?99[\-_.]?[12]?).*\.htm")


def _strip_html(html: str) -> str:
    """Reduce HTML to plain text for the heuristic / Claude extractors.
    Preserves paragraph breaks so quote extraction still works."""
    if not html:
        return ""
    # Replace block tags with newlines before stripping.
    html = re.sub(r"</?(p|div|br|li|h\d|tr)[^>]*>", "\n", html, flags=re.I)
    text = _HTML_TAG_RE.sub(" ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&#8217;", "'").replace("&#8220;", '"')
              .replace("&#8221;", '"').replace("&#8211;", "-")
              .replace("&#8212;", "—").replace("&rsquo;", "'")
              .replace("&ldquo;", '"').replace("&rdquo;", '"'))
    # Collapse whitespace but keep newlines for sentence splitting.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _accession_path(acc: str) -> str:
    """Accession numbers in URLs strip the dashes."""
    return acc.replace("-", "")


def fetch_release(*, cik: str, accession_number: str,
                     primary_document: Optional[str] = None,
                     client: Optional[EdgarClient] = None
                     ) -> Optional[Tuple[str, str]]:
    """Fetch the press-release plaintext for an 8-K filing.

    Returns ``(text, exhibit_url)`` on success, ``None`` when the exhibit
    can't be resolved or the SEC user agent isn't configured.
    """
    cl = client or EdgarClient()
    if not cl.available:
        return None
    if not cik or not accession_number:
        return None

    cik_clean = str(cik).lstrip("0") or "0"
    acc_path = _accession_path(accession_number)
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_path}"

    # Step 1: hit the directory index to list files.
    try:
        idx_raw = cl._getter(f"{base}/", user_agent=cl._ua())
        idx_html = idx_raw.decode("utf-8", errors="ignore")
    except Exception:
        logger.debug("EDGAR directory fetch failed for %s/%s",
                       cik, accession_number, exc_info=True)
        return None

    # Find first match of ex99*.htm in the directory listing.
    matches = _EX99_RE.findall(idx_html)
    exhibit_url: Optional[str] = None
    if matches:
        # The regex returned the prefix; find the actual filename via a
        # broader search on the directory body.
        full_matches = re.findall(
            r'href="([^"]*ex.?99[\-_.]?\d*[^"]*\.htm)"', idx_html, flags=re.I)
        if full_matches:
            exhibit_url = full_matches[0]
            if not exhibit_url.startswith("http"):
                exhibit_url = (f"https://www.sec.gov{exhibit_url}"
                                  if exhibit_url.startswith("/")
                                  else f"{base}/{exhibit_url}")

    # Fallback to the primary document if no exhibit found
    if exhibit_url is None and primary_document:
        exhibit_url = f"{base}/{primary_document}"

    if exhibit_url is None:
        return None

    try:
        raw = cl._getter(exhibit_url, user_agent=cl._ua())
        html = raw.decode("utf-8", errors="ignore")
    except Exception:
        logger.debug("EDGAR exhibit fetch failed for %s", exhibit_url,
                       exc_info=True)
        return None
    return _strip_html(html), exhibit_url
