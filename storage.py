"""SQLite 元数据存储层（文件助手专用）。

设计要点：
- 只保存文件的 *元数据*（file_id / file_unique_id / 文件名 / 分类 / 标签等），
  文件本体始终留在 Telegram 服务器上，不占用本机磁盘。
- file_unique_id 是全局唯一键：同一份文件重复发送时自动去重（先查后写）。
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_unique_id TEXT NOT NULL UNIQUE,
    file_id TEXT NOT NULL,
    file_type TEXT NOT NULL,          -- photo / document / video / audio / voice / sticker / animation / video_note / text
    file_name TEXT,
    mime_type TEXT,
    file_size INTEGER,
    caption TEXT,
    text_content TEXT,                -- 文本记录存正文，媒体记录为 NULL
    tags TEXT NOT NULL DEFAULT '',    -- 空格分隔的标签，如 "work 2024 合同"
    category TEXT NOT NULL,           -- 图片 / 文档 / 文本 / 视频 / 音频 / 语音 / 贴纸 / 动画 / 视频笔记
    chat_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL          -- UTC ISO8601
);

CREATE INDEX IF NOT EXISTS idx_files_created ON files (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_category ON files (category);
"""


class FileStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """旧版本数据库（无 text_content 列）自动升级。"""
        cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(files)")]
        if "text_content" not in cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN text_content TEXT")
            print("Migrated DB: added column text_content")

    # ---------- 增 ----------
    def add(self, **fields) -> tuple[Optional[sqlite3.Row], bool]:
        """插入一条记录；若 file_unique_id 已存在则更新并返回 (record, False)。

        用「先查后写」而非 UPSERT，避免 SQLite 在冲突更新时也消耗自增 ID
        导致记录编号出现空洞。
        """
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = self.get_by_unique_id(fields["file_unique_id"])
        if row:
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE files SET
                        file_id = ?, file_name = ?, mime_type = ?, file_size = ?,
                        caption = ?, text_content = ?, tags = ?, category = ?,
                        chat_id = ?, message_id = ?, created_at = ?
                    WHERE file_unique_id = ?
                    """,
                    (
                        fields["file_id"],
                        fields.get("file_name"),
                        fields.get("mime_type"),
                        fields.get("file_size"),
                        fields.get("caption"),
                        fields.get("text_content"),
                        fields.get("tags", ""),
                        fields["category"],
                        fields.get("chat_id"),
                        fields.get("message_id"),
                        created_at,
                        fields["file_unique_id"],
                    ),
                )
            return self.get(row["id"]), False

        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO files
                    (file_unique_id, file_id, file_type, file_name, mime_type,
                     file_size, caption, text_content, tags, category, chat_id,
                     message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["file_unique_id"],
                    fields["file_id"],
                    fields["file_type"],
                    fields.get("file_name"),
                    fields.get("mime_type"),
                    fields.get("file_size"),
                    fields.get("caption"),
                    fields.get("text_content"),
                    fields.get("tags", ""),
                    fields["category"],
                    fields.get("chat_id"),
                    fields.get("message_id"),
                    created_at,
                ),
            )
        return self.get(cur.lastrowid), True

    # ---------- 查 ----------
    def get(self, record_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM files WHERE id = ?", (record_id,)
        ).fetchone()

    def get_by_unique_id(self, file_unique_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM files WHERE file_unique_id = ?", (file_unique_id,)
        ).fetchone()

    def list_files(self, category: Optional[str] = None, limit: int = 10, offset: int = 0):
        if category and category != "all":
            return self._conn.execute(
                """SELECT * FROM files WHERE category = ?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (category, limit, offset),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM files ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def search(self, keyword: str, limit: int = 10, offset: int = 0):
        like = f"%{keyword}%"
        return self._conn.execute(
            """SELECT * FROM files
               WHERE file_name LIKE ? OR caption LIKE ? OR tags LIKE ?
                  OR text_content LIKE ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (like, like, like, like, limit, offset),
        ).fetchall()

    def count_search(self, keyword: str) -> int:
        like = f"%{keyword}%"
        row = self._conn.execute(
            """SELECT COUNT(*) AS n FROM files
               WHERE file_name LIKE ? OR caption LIKE ? OR tags LIKE ?
                  OR text_content LIKE ?""",
            (like, like, like, like),
        ).fetchone()
        return int(row["n"])

    def count(self, category: Optional[str] = None) -> int:
        if category and category != "all":
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM files WHERE category = ?", (category,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        return int(row["n"])

    def counts_by_category(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT category, COUNT(*) AS n FROM files GROUP BY category"
        ).fetchall()
        return {r["category"]: int(r["n"]) for r in rows}

    # ---------- 改 ----------
    def update_tags(self, record_id: int, tags: str) -> bool:
        cur = self._conn.execute(
            "UPDATE files SET tags = ? WHERE id = ?", (tags, record_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def rename(self, record_id: int, file_name: str) -> bool:
        cur = self._conn.execute(
            "UPDATE files SET file_name = ? WHERE id = ?", (file_name, record_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---------- 删 ----------
    def delete(self, record_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM files WHERE id = ?", (record_id,))
        self._conn.commit()
        return cur.rowcount > 0
