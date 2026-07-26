# SPDX-License-Identifier: Apache-2.0
"""License compatibility check for installed dependencies.

Runs ``pip-licenses`` and flags any installed package whose declared license
is not compatible with this project's own license (Apache-2.0).

Compatibility follows the Apache Software Foundation's third-party license
policy: https://www.apache.org/legal/resolved.html
  - Category A (permissive, allowed)      → COMPATIBLE
  - Category B (weak copyleft, allowed
    with binary-only redistribution caveat) → COMPATIBLE (caveat is on you to honor)
  - Category X (strong copyleft, network
    copyleft, source-available, proprietary) → INCOMPATIBLE

Notable consequences for an Apache-2.0 project (different from a GPL one):
  - GPL-2.0, GPL-3.0, AGPL-3.0, LGPL (all variants) → INCOMPATIBLE
  - CDDL, EPL, MPL → COMPATIBLE (Category B; review the caveat)
  - BSD-4-Clause, Apache-1.x → COMPATIBLE (just permissive with attribution)

Usage:
    python scripts/license_check.py                 # human report; exit 1 on INCOMPATIBLE
    python scripts/license_check.py --strict        # also exit 2 on UNKNOWN
    python scripts/license_check.py --json          # emit log payload to stdout
    python scripts/license_check.py --log PATH      # write JSON log to PATH (default: debug/license-compliance.json)
    python scripts/license_check.py --md  PATH      # write markdown summary to PATH (default: debug/license-compliance.md)
    python scripts/license_check.py --no-log        # skip writing the JSON log file
    python scripts/license_check.py --no-md         # skip writing the markdown summary

The JSON log is a single document with `metadata` and `findings` keys, iterable via
``jq '.findings[]' debug/license-compliance.json``. The markdown summary lists only
the non-compliant findings (INCOMPATIBLE + UNKNOWN) for quick human review.

Requires:
    pip install pip-licenses
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


# Keys are upper-cased and whitespace-collapsed for lookup. Covers SPDX
# identifiers, PyPI Trove classifier strings, and common free-form spellings.
# Classification follows ASF Cat A/B/X (see module docstring).
_LICENSE_TABLE: dict[str, Status] = {
    # --- Category A: permissive (allowed) ---
    "MIT": Status.COMPATIBLE,
    "MIT LICENSE": Status.COMPATIBLE,
    "MIT-CMU": Status.COMPATIBLE,
    "X11": Status.COMPATIBLE,
    "EXPAT": Status.COMPATIBLE,
    "BSD": Status.COMPATIBLE,
    "BSD LICENSE": Status.COMPATIBLE,
    "BSD-2-CLAUSE": Status.COMPATIBLE,
    "BSD-3-CLAUSE": Status.COMPATIBLE,
    "BSD 2-CLAUSE": Status.COMPATIBLE,
    "BSD 3-CLAUSE": Status.COMPATIBLE,
    "2-CLAUSE BSD LICENSE": Status.COMPATIBLE,
    "3-CLAUSE BSD LICENSE": Status.COMPATIBLE,
    "BSD-4-CLAUSE": Status.COMPATIBLE,  # advertising clause; permissive
    "ORIGINAL BSD LICENSE": Status.COMPATIBLE,
    "ISC": Status.COMPATIBLE,
    "ISC LICENSE (ISCL)": Status.COMPATIBLE,
    "APACHE-2.0": Status.COMPATIBLE,
    "APACHE 2.0": Status.COMPATIBLE,
    "APACHE 2.0 LICENSE": Status.COMPATIBLE,
    "APACHE LICENSE 2.0": Status.COMPATIBLE,
    "APACHE LICENSE, VERSION 2.0": Status.COMPATIBLE,
    "APACHE SOFTWARE LICENSE": Status.COMPATIBLE,
    "APACHE SOFTWARE LICENSE 2.0": Status.COMPATIBLE,
    "APACHE-1.0": Status.COMPATIBLE,  # permissive, just attribution differences
    "APACHE-1.1": Status.COMPATIBLE,
    "PYTHON-2.0": Status.COMPATIBLE,
    "PYTHON SOFTWARE FOUNDATION LICENSE": Status.COMPATIBLE,
    "PSF": Status.COMPATIBLE,
    "PSF-2.0": Status.COMPATIBLE,
    "ZLIB": Status.COMPATIBLE,
    "ZLIB/LIBPNG": Status.COMPATIBLE,
    "BSL-1.0": Status.COMPATIBLE,
    "BOOST SOFTWARE LICENSE 1.0 (BSL-1.0)": Status.COMPATIBLE,
    "UNLICENSE": Status.COMPATIBLE,
    "THE UNLICENSE (UNLICENSE)": Status.COMPATIBLE,
    "CC0-1.0": Status.COMPATIBLE,
    "CC0 1.0 UNIVERSAL (CC0 1.0) PUBLIC DOMAIN DEDICATION": Status.COMPATIBLE,
    "PUBLIC DOMAIN": Status.COMPATIBLE,
    "WTFPL": Status.COMPATIBLE,
    "BLUEOAK-1.0.0": Status.COMPATIBLE,
    "MS-PL": Status.COMPATIBLE,
    "MICROSOFT PUBLIC LICENSE (MS-PL)": Status.COMPATIBLE,
    "OPENSSL": Status.COMPATIBLE,  # advertising clause; permissive

    # --- Category B: weak copyleft (allowed, binary-only redistribution caveat) ---
    "MPL-2.0": Status.COMPATIBLE,
    "MOZILLA PUBLIC LICENSE 2.0 (MPL 2.0)": Status.COMPATIBLE,
    "MPL-1.0": Status.COMPATIBLE,
    "MPL-1.1": Status.COMPATIBLE,
    "MOZILLA PUBLIC LICENSE 1.1 (MPL 1.1)": Status.COMPATIBLE,
    "CDDL-1.0": Status.COMPATIBLE,
    "CDDL-1.1": Status.COMPATIBLE,
    "COMMON DEVELOPMENT AND DISTRIBUTION LICENSE 1.0 (CDDL-1.0)": Status.COMPATIBLE,
    "EPL-1.0": Status.COMPATIBLE,
    "EPL-2.0": Status.COMPATIBLE,
    "ECLIPSE PUBLIC LICENSE 1.0 (EPL-1.0)": Status.COMPATIBLE,
    "ECLIPSE PUBLIC LICENSE 2.0 (EPL-2.0)": Status.COMPATIBLE,

    # --- Category X: strong copyleft, network copyleft, source-available, proprietary ---
    "GPL-2.0-ONLY": Status.INCOMPATIBLE,
    "GPL-2.0-OR-LATER": Status.INCOMPATIBLE,
    "GPL-3.0-ONLY": Status.INCOMPATIBLE,
    "GPL-3.0-OR-LATER": Status.INCOMPATIBLE,
    "GPLV2": Status.INCOMPATIBLE,
    "GPLV2+": Status.INCOMPATIBLE,
    "GPLV3": Status.INCOMPATIBLE,
    "GPLV3+": Status.INCOMPATIBLE,
    "GNU GENERAL PUBLIC LICENSE V2 (GPLV2)": Status.INCOMPATIBLE,
    "GNU GENERAL PUBLIC LICENSE V2 OR LATER (GPLV2+)": Status.INCOMPATIBLE,
    "GNU GENERAL PUBLIC LICENSE V3 (GPLV3)": Status.INCOMPATIBLE,
    "GNU GENERAL PUBLIC LICENSE V3 OR LATER (GPLV3+)": Status.INCOMPATIBLE,
    "LGPL-2.0-ONLY": Status.INCOMPATIBLE,
    "LGPL-2.0-OR-LATER": Status.INCOMPATIBLE,
    "LGPL-2.1-ONLY": Status.INCOMPATIBLE,
    "LGPL-2.1-OR-LATER": Status.INCOMPATIBLE,
    "LGPL-3.0-ONLY": Status.INCOMPATIBLE,
    "LGPL-3.0-OR-LATER": Status.INCOMPATIBLE,
    "LGPLV3": Status.INCOMPATIBLE,
    "LGPLV3+": Status.INCOMPATIBLE,
    "GNU LESSER GENERAL PUBLIC LICENSE V3 (LGPLV3)": Status.INCOMPATIBLE,
    "GNU LESSER GENERAL PUBLIC LICENSE V3 OR LATER (LGPLV3+)": Status.INCOMPATIBLE,
    "GNU LIBRARY OR LESSER GENERAL PUBLIC LICENSE (LGPL)": Status.INCOMPATIBLE,
    "AGPL-3.0-ONLY": Status.INCOMPATIBLE,
    "AGPL-3.0-OR-LATER": Status.INCOMPATIBLE,
    "AGPLV3": Status.INCOMPATIBLE,
    "AGPLV3+": Status.INCOMPATIBLE,
    "GNU AFFERO GENERAL PUBLIC LICENSE V3 (AGPLV3)": Status.INCOMPATIBLE,
    "GNU AFFERO GENERAL PUBLIC LICENSE V3 OR LATER (AGPLV3+)": Status.INCOMPATIBLE,
    "SSPL-1.0": Status.INCOMPATIBLE,
    "SERVER SIDE PUBLIC LICENSE": Status.INCOMPATIBLE,
    "BUSL-1.1": Status.INCOMPATIBLE,
    "BUSINESS SOURCE LICENSE 1.1": Status.INCOMPATIBLE,
    "COMMONS CLAUSE": Status.INCOMPATIBLE,
    "ELASTIC-2.0": Status.INCOMPATIBLE,
    "ELASTIC LICENSE 2.0 (ELASTIC-2.0)": Status.INCOMPATIBLE,
    "JSON": Status.INCOMPATIBLE,  # ASF Cat X: "shall be used for Good, not Evil"
    "PROPRIETARY": Status.INCOMPATIBLE,
    "OTHER/PROPRIETARY LICENSE": Status.INCOMPATIBLE,
    "NVIDIA PROPRIETARY SOFTWARE": Status.INCOMPATIBLE,
    "LICENSEREF-NVIDIA-PROPRIETARY": Status.INCOMPATIBLE,
    "LICENSEREF-NVIDIA-SOFTWARE-LICENSE": Status.INCOMPATIBLE,
}

_UNKNOWN_TOKENS = {"UNKNOWN", "", "NONE", "NOASSERTION", "OTHER"}


@dataclass
class PkgResult:
    name: str
    version: str
    raw_license: str
    tokens: list[str]
    status: Status
    matched: str


def _normalize(license_str: str) -> list[str]:
    if not license_str:
        return [""]
    # Intentionally NOT splitting on "/": Trove names like "Other/Proprietary
    # License" are a single license, and "MIT/X11" is both compatible anyway.
    parts = re.split(r"\s*(?:;|,| OR | AND )\s*", license_str)
    return [re.sub(r"\s+", " ", p.strip()).upper() for p in parts if p.strip()]


def _classify_token(tok: str) -> Status:
    if tok in _UNKNOWN_TOKENS:
        return Status.UNKNOWN
    if tok in _LICENSE_TABLE:
        return _LICENSE_TABLE[tok]
    # Fallback heuristic: anything self-describing as proprietary fails.
    if "PROPRIETARY" in tok:
        return Status.INCOMPATIBLE
    return Status.UNKNOWN


def _classify_package(name: str, version: str, raw: str) -> PkgResult:
    tokens = _normalize(raw)
    decisions = [(t, _classify_token(t)) for t in tokens]
    # Dual-licensed packages: any compatible token lets us choose that license.
    for tok, status in decisions:
        if status is Status.COMPATIBLE:
            return PkgResult(name, version, raw, tokens, Status.COMPATIBLE, tok)
    for tok, status in decisions:
        if status is Status.INCOMPATIBLE:
            return PkgResult(name, version, raw, tokens, Status.INCOMPATIBLE, tok)
    return PkgResult(name, version, raw, tokens, Status.UNKNOWN, decisions[0][0] if decisions else "")


def _run_pip_licenses() -> list[dict]:
    if shutil.which("pip-licenses") is None:
        print(
            "error: 'pip-licenses' is not installed.\n"
            "  install it with:  pip install pip-licenses",
            file=sys.stderr,
        )
        sys.exit(127)
    out = subprocess.check_output(
        ["pip-licenses", "--format=json", "--with-urls"],
        text=True,
    )
    return json.loads(out)


def _report(results: list[PkgResult]) -> None:
    buckets: dict[Status, list[PkgResult]] = {s: [] for s in Status}
    for r in results:
        buckets[r.status].append(r)

    width = max((len(r.name) for r in results), default=20)

    def fmt(r: PkgResult) -> str:
        return f"  {r.name:<{width}}  {r.version:<12}  {r.raw_license}"

    if buckets[Status.INCOMPATIBLE]:
        print(f"INCOMPATIBLE with {PROJECT_LICENSE} (must be removed or replaced):")
        for r in buckets[Status.INCOMPATIBLE]:
            print(fmt(r) + f"   [matched: {r.matched}]")
        print()
    if buckets[Status.UNKNOWN]:
        print("UNKNOWN (needs manual review):")
        for r in buckets[Status.UNKNOWN]:
            print(fmt(r))
        print()
    print(
        f"Summary: {len(buckets[Status.COMPATIBLE])} compatible, "
        f"{len(buckets[Status.INCOMPATIBLE])} incompatible, "
        f"{len(buckets[Status.UNKNOWN])} unknown "
        f"({len(results)} total)."
    )


DEFAULT_LOG_PATH = Path("debug/license-compliance.json")
DEFAULT_MD_PATH = Path("debug/license-compliance.md")
PROJECT_LICENSE = "Apache-2.0"


def _pip_licenses_version() -> str:
    try:
        out = subprocess.check_output(["pip-licenses", "--version"], text=True).strip()
        return out.split()[-1] if out else ""
    except (subprocess.CalledProcessError, OSError):
        return ""


def _build_payload(results: list[PkgResult], exit_code: int) -> dict:
    counts = {s.value: 0 for s in Status}
    for r in results:
        counts[r.status.value] += 1
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_license": PROJECT_LICENSE,
            "python_version": platform.python_version(),
            "pip_licenses_version": _pip_licenses_version(),
            "total": len(results),
            "counts": counts,
            "exit_code": exit_code,
        },
        "findings": [
            {
                "name": r.name,
                "version": r.version,
                "license": r.raw_license,
                "tokens": r.tokens,
                "status": r.status.value,
                "matched_token": r.matched,
            }
            for r in results
        ],
    }


def _write_log(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _render_markdown(payload: dict) -> str:
    md = payload["metadata"]
    findings = payload["findings"]
    incompatible = [f for f in findings if f["status"] == Status.INCOMPATIBLE.value]
    unknown = [f for f in findings if f["status"] == Status.UNKNOWN.value]

    lines: list[str] = []
    lines.append("# License Compliance Report")
    lines.append("")
    lines.append(f"- **Generated:** {md['generated_at']}")
    lines.append(f"- **Project license:** `{md['project_license']}`")
    lines.append(
        f"- **Counts:** {md['counts']['compatible']} compatible · "
        f"{md['counts']['incompatible']} incompatible · "
        f"{md['counts']['unknown']} unknown · "
        f"{md['total']} total"
    )
    lines.append(f"- **Exit code:** `{md['exit_code']}`")
    lines.append("")

    if not incompatible and not unknown:
        lines.append("All dependencies are compatible with the project license. No action required.")
        lines.append("")
        return "\n".join(lines)

    if incompatible:
        lines.append(f"## Incompatible ({len(incompatible)})")
        lines.append("")
        lines.append(
            f"These dependencies declare a license that is **not compatible** with "
            f"`{md['project_license']}`. Each must be removed, replaced, or relied on "
            f"under a documented exception (e.g. proprietary binary runtimes that ship "
            f"separately and are not redistributed with this project's source)."
        )
        lines.append("")
        lines.append("| Package | Version | Declared License | Matched Token |")
        lines.append("|---|---|---|---|")
        for f in incompatible:
            url = f"https://pypi.org/project/{f['name']}/"
            lines.append(
                f"| [`{_md_escape(f['name'])}`]({url}) "
                f"| {_md_escape(f['version'])} "
                f"| {_md_escape(f['license']) or '_(none)_'} "
                f"| `{_md_escape(f['matched_token'])}` |"
            )
        lines.append("")

    if unknown:
        lines.append(f"## Unknown ({len(unknown)})")
        lines.append("")
        lines.append(
            "These dependencies have no recognized license token. Verify the actual "
            "license (check the project's PyPI page, repository, or `LICENSE` file) "
            "and either add the spelling to `_LICENSE_TABLE` in `scripts/license_check.py` "
            "or treat as non-compliant."
        )
        lines.append("")
        lines.append("| Package | Version | Declared License |")
        lines.append("|---|---|---|")
        for f in unknown:
            url = f"https://pypi.org/project/{f['name']}/"
            lines.append(
                f"| [`{_md_escape(f['name'])}`]({url}) "
                f"| {_md_escape(f['version'])} "
                f"| {_md_escape(f['license']) or '_(none)_'} |"
            )
        lines.append("")

    return "\n".join(lines)


def _write_md(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on UNKNOWN as well as INCOMPATIBLE.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the log payload to stdout instead of the human-readable summary.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH, metavar="PATH",
                        help=f"Write JSON log to PATH (default: {DEFAULT_LOG_PATH}).")
    parser.add_argument("--no-log", action="store_true",
                        help="Skip writing the JSON log file.")
    parser.add_argument("--md", type=Path, default=DEFAULT_MD_PATH, metavar="PATH",
                        help=f"Write markdown summary to PATH (default: {DEFAULT_MD_PATH}).")
    parser.add_argument("--no-md", action="store_true",
                        help="Skip writing the markdown summary.")
    args = parser.parse_args()

    raw = _run_pip_licenses()
    results = [
        _classify_package(pkg["Name"], pkg.get("Version", ""), pkg.get("License", ""))
        for pkg in raw
    ]

    incompatible = sum(1 for r in results if r.status is Status.INCOMPATIBLE)
    unknown = sum(1 for r in results if r.status is Status.UNKNOWN)
    exit_code = 1 if incompatible else (2 if args.strict and unknown else 0)

    payload = _build_payload(results, exit_code)

    if not args.no_log:
        try:
            _write_log(payload, args.log)
            if not args.json:
                print(f"wrote license log: {args.log}")
        except OSError as exc:
            print(f"warning: failed to write log to {args.log}: {exc}", file=sys.stderr)

    if not args.no_md:
        try:
            _write_md(_render_markdown(payload), args.md)
            if not args.json:
                print(f"wrote markdown summary: {args.md}")
        except OSError as exc:
            print(f"warning: failed to write markdown to {args.md}: {exc}", file=sys.stderr)

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _report(results)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
