from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sitemap_retrieval import (  # noqa: E402
    BOOKLAND_SITEMAP_INDEX_URL,
    refresh_sitemap_cache,
)

OUTPUT_DIR = ROOT / "data" / "bookland_sitemap_snapshot"
PART_SIZE = int(os.environ.get("BOOKLAND_SNAPSHOT_PART_SIZE_MB", "20")) * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def split_file(path: Path, output_dir: Path) -> list[str]:
    names: list[str] = []
    with path.open("rb") as src:
        part_no = 0
        while True:
            chunk = src.read(PART_SIZE)
            if not chunk:
                break
            name = f"bookland_sitemap.sqlite3.gz.part-{part_no:03d}"
            (output_dir / name).write_bytes(chunk)
            names.append(name)
            part_no += 1
    return names


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bookland-sitemap-") as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "bookland_sitemap.sqlite3"
        gz_path = tmp_dir / "bookland_sitemap.sqlite3.gz"

        stats = refresh_sitemap_cache(
            index_url=BOOKLAND_SITEMAP_INDEX_URL,
            db_path=db_path,
        )

        sqlite_sha256 = sha256_file(db_path)
        with db_path.open("rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

        gzip_sha256 = sha256_file(gz_path)

        for old in OUTPUT_DIR.glob("bookland_sitemap.sqlite3.gz.part-*"):
            old.unlink()
        parts = split_file(gz_path, OUTPUT_DIR)

        manifest = {
            "schema_version": 1,
            "source": BOOKLAND_SITEMAP_INDEX_URL,
            "refreshed_at": stats["refreshed_at"],
            "url_count": int(stats["url_count"]),
            "sitemap_count": int(stats["sitemap_count"]),
            "failed_sitemaps": int(stats["failed_sitemaps"]),
            "sqlite_bytes": db_path.stat().st_size,
            "gzip_bytes": gz_path.stat().st_size,
            "sqlite_sha256": sqlite_sha256,
            "gzip_sha256": gzip_sha256,
            "parts": parts,
        }
        (OUTPUT_DIR / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        f"Snapshot gotowy: {manifest['url_count']} URL-i, "
        f"{manifest['sitemap_count']} sitemap, {len(parts)} części, "
        f"{manifest['gzip_bytes'] / 1024 / 1024:.1f} MiB po gzip."
    )


if __name__ == "__main__":
    main()
