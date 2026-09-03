"""Report generators (Markdown, CSV and per-category fault dumps)."""

from __future__ import annotations

from .markdown_report import render_markdown, write_markdown
from .csv_report import render_rows, write_csv
from .category_dump import (
    CategoryDump,
    dump_dir_for,
    faults_for_category,
    write_category_dumps,
)

__all__ = [
    "render_markdown",
    "write_markdown",
    "render_rows",
    "write_csv",
    "CategoryDump",
    "dump_dir_for",
    "faults_for_category",
    "write_category_dumps",
]
