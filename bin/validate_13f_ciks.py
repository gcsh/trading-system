#!/usr/bin/env python3
"""MITS Phase 11.2 — validate the 13F-fund CIK roster against SEC EDGAR.

For each CIK in ``backend/bot/data/watched_funds.json``:

  1. GET ``https://data.sec.gov/submissions/CIK<cik>.json`` (10-digit
     zero-padded).
  2. Confirm the response is real JSON (a 403/HTML "not found" page
     gets caught here — that's what the operator brief called
     "phantom CIKs").
  3. Walk ``filings.recent.form`` for any ``13F-HR`` or ``13F-HR/A``
     entries in the last ``--lookback-years`` years.
  4. Optionally consult the ``filings.files`` continuation index for
     funds with deep history (Berkshire, Renaissance, etc.) whose
     13F-HR rows have aged past the ``recent`` window — Vanguard etc.
     file so much that the most recent 1000 entries don't reach back
     a full 5y of 13F-HR filings.

Writes back:

  * Marks every validated fund with ``validated_at`` (UTC ISO) and the
    most recent ``latest_13f_filing`` date.
  * Drops the ``funds`` entries whose CIK responded 403 / empty /
    contains no 13F-HR in the lookback window.
  * Appends VERIFIED institutional filers from the operator-supplied
    seed list when they survive validation and aren't already in the
    roster (deduped by CIK).
  * Renames the version field so the loader sees a fresh signature.

The script is idempotent: running it twice produces the same output as
running it once. The output is written atomically (tempfile + rename)
so a half-failed run never leaves a corrupted JSON on disk.

Usage:
  AWS_PROFILE=trading-bot python bin/validate_13f_ciks.py \
      --in backend/bot/data/watched_funds.json \
      --out backend/bot/data/watched_funds.json \
      --lookback-years 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("validate_13f")


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# Seed list: institutional 13F filers the operator told us to verify.
# Each entry is (cik_unpadded_or_padded, name, category). We zero-pad
# in code, so passing either form is safe.
OPERATOR_SEED: List[Dict[str, str]] = [
    # Quant funds
    {"cik": "0001423053", "name": "Citadel Advisors LLC",
        "category": "multistrat_quant"},
    {"cik": "0001179392", "name": "Two Sigma Investments LP",
        "category": "quant"},
    {"cik": "0001037389", "name": "Renaissance Technologies LLC",
        "category": "quant"},
    {"cik": "0001009207", "name": "D. E. Shaw & Co Inc",
        "category": "quant"},
    {"cik": "0001273087", "name": "Millennium Management LLC",
        "category": "multistrat"},
    {"cik": "0001167557", "name": "AQR Capital Management LLC",
        "category": "factor_quant"},
    {"cik": "0001045810", "name": "Acadian Asset Management LLC",
        "category": "quant"},
    {"cik": "0001135730", "name": "Numeric Investors LLC",
        "category": "factor_quant"},

    # Traditional
    {"cik": "0001067983", "name": "Berkshire Hathaway Inc",
        "category": "value_conglomerate"},
    {"cik": "0001350694", "name": "Bridgewater Associates LP",
        "category": "macro_quant"},
    {"cik": "0001167483", "name": "Tiger Global Management LLC",
        "category": "growth_long_short"},
    {"cik": "0001135730", "name": "Coatue Management LLC",
        "category": "growth_long_short"},
    {"cik": "0001061165", "name": "Lone Pine Capital LLC",
        "category": "tmt_long_short"},
    {"cik": "0001103804", "name": "Viking Global Investors LP",
        "category": "tmt_long_short"},
    {"cik": "0001336528", "name": "Pershing Square Capital Management LP",
        "category": "activist"},
    {"cik": "0001040273", "name": "Third Point LLC",
        "category": "event_driven"},
    {"cik": "0001048445", "name": "Elliott Investment Management LP",
        "category": "activist_event_driven"},
    {"cik": "0001079114", "name": "Greenlight Capital Inc",
        "category": "value_activist"},
    {"cik": "0001159159", "name": "JANA Partners LLC",
        "category": "activist"},
    {"cik": "0001162148", "name": "Glenview Capital Management LLC",
        "category": "long_short"},
    {"cik": "0001000275", "name": "Maverick Capital Ltd",
        "category": "tmt_long_short"},
    {"cik": "0001142787", "name": "ValueAct Capital Management LP",
        "category": "activist_value"},

    # Passive giants
    {"cik": "0001364742", "name": "BlackRock Inc",
        "category": "passive_giant"},
    {"cik": "0000102909", "name": "Vanguard Group Inc",
        "category": "passive_giant"},
    {"cik": "0000093751", "name": "State Street Corp",
        "category": "passive_giant"},
    {"cik": "0000315066", "name": "FMR LLC (Fidelity)",
        "category": "long_only_active"},
    {"cik": "0001113169", "name": "T Rowe Price Group Inc",
        "category": "long_only_active"},
    {"cik": "0000902219", "name": "Wellington Management Group LLP",
        "category": "long_only_active"},
    {"cik": "0000891836", "name": "Capital Research Global Investors",
        "category": "long_only_active"},
    {"cik": "0000916012", "name": "Northern Trust Corp",
        "category": "passive_giant"},
    {"cik": "0000354204", "name": "Dimensional Fund Advisors LP",
        "category": "factor_quant"},

    # Big-bank asset managers
    {"cik": "0000947010", "name": "JPMorgan Chase & Co (Asset Management)",
        "category": "bank_asset_manager"},
    {"cik": "0000895421", "name": "Morgan Stanley Investment Management",
        "category": "bank_asset_manager"},
    {"cik": "0000886982", "name": "Goldman Sachs Group Inc (Asset Management)",
        "category": "bank_asset_manager"},
    {"cik": "0001403878", "name": "UBS Asset Management Americas Inc",
        "category": "bank_asset_manager"},
    {"cik": "0001141391", "name": "Allianz Global Investors GmbH",
        "category": "bank_asset_manager"},
    {"cik": "0001390777", "name": "BNY Mellon Corp",
        "category": "bank_asset_manager"},
    {"cik": "0000914208", "name": "Invesco Ltd",
        "category": "long_only_active"},
    {"cik": "0001017386", "name": "Franklin Resources Inc",
        "category": "long_only_active"},
    {"cik": "0000350797", "name": "Eaton Vance Management",
        "category": "long_only_active"},

    # PE / alternatives
    {"cik": "0001393818", "name": "Blackstone Inc",
        "category": "alternatives_giant"},
    {"cik": "0001404912", "name": "KKR & Co Inc",
        "category": "alternatives_giant"},
    {"cik": "0001411494", "name": "Apollo Global Management Inc",
        "category": "alternatives_giant"},
    {"cik": "0001527166", "name": "Carlyle Group Inc",
        "category": "alternatives_giant"},
    {"cik": "0001613103", "name": "TPG Inc",
        "category": "alternatives_giant"},
    {"cik": "0001001288", "name": "Brookfield Asset Management Inc",
        "category": "real_assets"},
]


@dataclass
class ValidationResult:
    cik: str
    name: str
    category: Optional[str]
    valid: bool
    latest_13f_filing: Optional[str] = None
    filing_count_in_window: int = 0
    reason: Optional[str] = None


def _zero_pad_cik(value: str) -> str:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    if not digits:
        return ""
    return digits.zfill(10)


def _user_agent() -> str:
    # Reuse TB_SEC_USER_AGENT if set so we don't trigger SEC's "anonymous
    # caller" lockout. Fall back to a clearly-identified operator string
    # if the env var isn't set (local laptop runs).
    return os.getenv(
        "TB_SEC_USER_AGENT",
        "trading-bot validator/1.0 srikant.parimi@gmail.com",
    )


def _http_get_json(url: str, *, timeout: float = 30.0,
                          retries: int = 3) -> Optional[Dict[str, Any]]:
    """SEC-throttled GET with retries. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": _user_agent(),
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("HTTP error on %s: %s", url, exc)
            time.sleep(1.0 + attempt)
            continue
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                # Some "200 but HTML" pages come back when the CIK
                # doesn't exist — treat as None.
                return None
        if resp.status_code in (429, 502, 503):
            # Back off and retry.
            time.sleep(1.0 + attempt * 2)
            continue
        # 403 / 404 → permanent failure.
        logger.info("SEC returned %s for %s", resp.status_code, url)
        return None
    return None


def _has_recent_13f(submissions: Dict[str, Any],
                          cutoff: datetime) -> tuple[bool, Optional[str], int]:
    """Walk ``filings.recent.form`` + ``filings.files`` for 13F-HR
    rows after ``cutoff``. Returns (has_recent, latest_iso, count_in_window)."""
    if not submissions:
        return False, None, 0

    forms_recent = (
        ((submissions.get("filings") or {}).get("recent") or {}).get("form") or []
    )
    dates_recent = (
        ((submissions.get("filings") or {}).get("recent") or {}).get("filingDate")
        or []
    )

    latest: Optional[datetime] = None
    count = 0
    for form, fd in zip(forms_recent, dates_recent):
        if not isinstance(form, str):
            continue
        if not form.startswith("13F-HR"):
            continue
        try:
            d = datetime.strptime(fd, "%Y-%m-%d")
        except Exception:
            continue
        if d < cutoff:
            continue
        count += 1
        if latest is None or d > latest:
            latest = d

    # Some prolific filers exceed the ~1000-entry recent window. Walk
    # the continuation files only if we found NO recent 13F-HRs — for
    # quarterly filers this is cheap (4/year × 5y = 20 entries fit
    # easily in the recent window).
    if count == 0:
        files = (
            (submissions.get("filings") or {}).get("files") or []
        )
        for f in files[:3]:  # cap to avoid SEC's per-IP limits
            name = f.get("name")
            if not name:
                continue
            url = f"https://data.sec.gov/submissions/{name}"
            cont = _http_get_json(url)
            if not cont:
                continue
            forms_c = cont.get("form") or []
            dates_c = cont.get("filingDate") or []
            for form, fd in zip(forms_c, dates_c):
                if not isinstance(form, str):
                    continue
                if not form.startswith("13F-HR"):
                    continue
                try:
                    d = datetime.strptime(fd, "%Y-%m-%d")
                except Exception:
                    continue
                if d < cutoff:
                    continue
                count += 1
                if latest is None or d > latest:
                    latest = d
            if count > 0:
                break

    return (count > 0, latest.date().isoformat() if latest else None, count)


def validate_one(cik: str, name: str, category: Optional[str],
                       *, cutoff: datetime) -> ValidationResult:
    padded = _zero_pad_cik(cik)
    if not padded:
        return ValidationResult(
            cik=cik, name=name, category=category,
            valid=False, reason="empty_cik",
        )
    url = SEC_SUBMISSIONS_URL.format(cik=padded)
    submissions = _http_get_json(url)
    if not submissions:
        return ValidationResult(
            cik=padded, name=name, category=category,
            valid=False, reason="submissions_unavailable",
        )
    has_recent, latest, count = _has_recent_13f(submissions, cutoff)
    if not has_recent:
        return ValidationResult(
            cik=padded, name=name, category=category,
            valid=False, reason="no_13f_hr_in_window",
        )
    return ValidationResult(
        cik=padded,
        name=name,
        category=category,
        valid=True,
        latest_13f_filing=latest,
        filing_count_in_window=count,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input_path",
                              default="backend/bot/data/watched_funds.json")
    parser.add_argument("--out", dest="output_path",
                              default="backend/bot/data/watched_funds.json")
    parser.add_argument("--lookback-years", type=int, default=5)
    parser.add_argument("--rate-sleep", type=float, default=0.15,
                              help="seconds between SEC calls (token-bucket safety)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    input_path = pathlib.Path(args.input_path)
    if not input_path.exists():
        logger.error("input file not found: %s", input_path)
        return 2
    with input_path.open() as fh:
        roster = json.load(fh)

    cutoff = datetime.utcnow() - timedelta(days=args.lookback_years * 366)

    # Build a unique-by-CIK set: existing roster, then operator seed
    # (operator seed wins on conflict so the validator brief overrides
    # any drift in the in-repo JSON).
    by_cik: Dict[str, Dict[str, Any]] = {}
    for entry in (roster.get("funds") or []):
        padded = _zero_pad_cik(entry.get("cik"))
        if padded:
            by_cik[padded] = {
                "cik": padded,
                "name": entry.get("name"),
                "category": entry.get("category"),
            }
    for entry in OPERATOR_SEED:
        padded = _zero_pad_cik(entry["cik"])
        if padded:
            by_cik[padded] = {
                "cik": padded,
                "name": entry["name"],
                "category": entry["category"],
            }

    logger.info(
        "validating %d unique CIKs (lookback=%dy, cutoff=%s)",
        len(by_cik), args.lookback_years, cutoff.date().isoformat(),
    )

    results: List[ValidationResult] = []
    for i, entry in enumerate(by_cik.values()):
        res = validate_one(
            entry["cik"], entry["name"], entry.get("category"),
            cutoff=cutoff,
        )
        results.append(res)
        if res.valid:
            logger.info(
                "[%3d/%3d] OK  %s %s (latest=%s, count=%d)",
                i + 1, len(by_cik), res.cik, res.name,
                res.latest_13f_filing, res.filing_count_in_window,
            )
        else:
            logger.info(
                "[%3d/%3d] BAD %s %s (%s)",
                i + 1, len(by_cik), res.cik, res.name, res.reason,
            )
        time.sleep(args.rate_sleep)

    valid_funds = [r for r in results if r.valid]
    invalid = [r for r in results if not r.valid]
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    # Safety net: if ALL submissions came back unavailable, the SEC is
    # almost certainly rate-limiting / blocking this IP. Refuse to
    # overwrite a populated roster with an empty one — re-run later
    # when SEC isn't grumpy.
    unavailable = sum(
        1 for r in invalid if r.reason == "submissions_unavailable"
    )
    if (valid_funds == [] and unavailable >= max(10, int(0.8 * len(results)))):
        logger.error(
            "%d of %d CIKs returned submissions_unavailable — SEC "
            "appears to be blocking this IP. REFUSING to overwrite "
            "the existing roster. Re-run with --rate-sleep 2.0 in a "
            "quiet window.",
            unavailable, len(results),
        )
        return 3

    new_roster = {
        "version": f"2026-06-09-validated-{len(valid_funds)}",
        "description": (
            "MITS Phase 11.2 — validated 13F-fund roster. Every CIK "
            "below was checked against SEC's submissions endpoint and "
            "confirmed to have at least one 13F-HR filing in the "
            f"trailing {args.lookback_years}y window. Re-generate with "
            "`python bin/validate_13f_ciks.py`."
        ),
        "validated_at": now_iso,
        "lookback_years": args.lookback_years,
        "stats": {
            "candidates": len(results),
            "valid": len(valid_funds),
            "invalid": len(invalid),
        },
        "invalid_examples": [
            {"cik": r.cik, "name": r.name, "reason": r.reason}
            for r in invalid[:25]
        ],
        "funds": [
            {
                "cik": r.cik,
                "name": r.name,
                "category": r.category or "uncategorized",
                "validated_at": now_iso,
                "latest_13f_filing": r.latest_13f_filing,
                "filings_in_window": r.filing_count_in_window,
            }
            for r in valid_funds
        ],
    }

    output_path = pathlib.Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=output_path.parent, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(new_roster, tmp, indent=2)
        tmp_name = tmp.name
    os.replace(tmp_name, output_path)
    logger.info(
        "wrote %s: %d valid, %d invalid", output_path,
        len(valid_funds), len(invalid),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
