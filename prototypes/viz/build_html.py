"""Inline web/common.js and out/match.json into each web/*.html page, writing self-contained files to out/."""
import sys
from pathlib import Path

here = Path(__file__).parent
data = Path(sys.argv[1] if len(sys.argv) > 1 else here / "out/match.json").read_text().replace("</", "<\\/")
common = (here / "web/common.js").read_text()
pages = {"replay.html": "b-replay.html", "report.html": "c-report.html", "grid.html": "d-seat-grid.html"}
for src, dst in pages.items():
    if not (here / "web" / src).exists():
        continue
    page = (here / "web" / src).read_text()
    page = page.replace("/*__COMMON__*/", common).replace("/*__DATA__*/", data)
    (here / "out" / dst).write_text(page)
    print(here / "out" / dst, f"{len(page):,} bytes")
