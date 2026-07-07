from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.supabase import get_supabase

JAPANESE_TEXT_RE = re.compile(r"[ぁ-んァ-ン一-龯]")
PAGE_SIZE = 1000


def main() -> None:
    db = get_supabase()
    scanned = 0
    updated = 0

    offset = 0
    while True:
        response = (
            db.table("meanings")
            .select("id, definition_en, definition_ja")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            break

        for row in rows:
            scanned += 1
            definition_en = row.get("definition_en") or ""
            definition_ja = (row.get("definition_ja") or "").strip()
            if not JAPANESE_TEXT_RE.search(definition_en):
                continue

            payload = {"definition_en": None}
            if not definition_ja:
                payload["definition_ja"] = definition_en
            db.table("meanings").update(payload).eq("id", row["id"]).execute()
            updated += 1

        offset += PAGE_SIZE

    print(f"scanned={scanned} updated={updated}")


if __name__ == "__main__":
    main()
