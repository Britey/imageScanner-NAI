from __future__ import annotations

import sqlite3


def iter_failures(conn: sqlite3.Connection, *, limit: int = 100):
    return conn.execute(
        """
        SELECT i.path,
               CASE
                   WHEN i.decode_error IS NOT NULL AND ip.prompt_error IS NOT NULL
                       THEN i.decode_error || '; prompt: ' || ip.prompt_error
                   WHEN i.decode_error IS NOT NULL THEN i.decode_error
                   ELSE 'prompt: ' || ip.prompt_error
               END AS decode_error
        FROM images i
        LEFT JOIN image_prompts ip ON ip.image_id = i.id
        WHERE i.decode_error IS NOT NULL
           OR (ip.prompt_error IS NOT NULL AND ip.prompt_error NOT IN ('no_metadata', 'no_prompt'))
        ORDER BY i.indexed_at DESC, i.path
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
