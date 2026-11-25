"""Serialize subscription data for download."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, tzinfo
from io import BytesIO
from typing import Literal
from unicodedata import normalize
from zipfile import ZIP_DEFLATED, ZipFile

from xlsxwriter import Workbook

from .models import Subscription
from .nga.bbcode import extract_quotes, to_display_text
from .nga.client import post_url

MEDIA_TYPES = {
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

SUBSCRIPTION_FIELDS = (
    "id", "tid", "author_uid", "title", "author_name", "alias", "color", "enabled",
    "created_at", "last_checked_at",
)

EXPORT_NAME = "NGA提醒助手"
REPLY_HEADERS = ("订阅名称", "回复时间", "回复内容")
XLSX_ROW_LIMIT = 1_048_576


def build_export(
    selection: list[tuple[Subscription, list[dict]]],
    file_format: Literal["json", "xlsx"],
    exported_at: datetime,
) -> tuple[str, str, bytes]:
    """Return a single file or a ZIP with one file per selected subscription."""
    stamp = exported_at.strftime("%Y%m%d%H%M%S")
    zone = exported_at.tzinfo or UTC
    if len(selection) == 1:
        subscription, replies = selection[0]
        name = _filename_label(subscription)
        return (
            f"{EXPORT_NAME}_{name}_{stamp}.{file_format}",
            MEDIA_TYPES[file_format],
            _serialize(subscription, replies, file_format, zone),
        )

    output = BytesIO()
    used_names: set[str] = set()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for subscription, replies in selection:
            label = _filename_label(subscription)
            stem = f"{EXPORT_NAME}_{label}"
            filename = f"{stem}_{stamp}.{file_format}"
            suffix = 2
            while filename.casefold() in used_names:
                filename = f"{stem}（{suffix}）_{stamp}.{file_format}"
                suffix += 1
            used_names.add(filename.casefold())
            archive.writestr(filename, _serialize(subscription, replies, file_format, zone))
    return f"{EXPORT_NAME}_{stamp}.zip", "application/zip", output.getvalue()


def _filename_label(subscription: Subscription) -> str:
    name = normalize("NFC", subscription.display_name)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:50]
    return safe or f"UID {subscription.author_uid}"


def _serialize(
    subscription: Subscription, records: list[dict], file_format: str, zone: tzinfo
) -> bytes:
    if file_format == "xlsx":
        return _xlsx(subscription.display_name, records, zone)
    sub = subscription.to_dict()
    metadata = {field: sub[field] for field in SUBSCRIPTION_FIELDS}
    replies = [_display_reply(subscription.tid, record) for record in records]
    return json.dumps(
        {"subscription": metadata, "replies": replies}, ensure_ascii=False, indent=2
    ).encode("utf-8")


def _display_reply(tid: int, record: dict) -> dict:
    timestamp = record["ts"]
    content = record["content"]
    quote_text = record["quote_text"]
    if not quote_text:
        quote_text = "\n".join(to_display_text(quote.text) for quote in extract_quotes(content))
    return {
        "pid": record["pid"],
        "posted_at": datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp > 0 else None,
        "floor_no": max(record["floor_no"], 0) or None,
        "page": record["floor_page"] or None,
        "content": to_display_text(content),
        "quote_text": quote_text,
        "url": post_url(tid, record["pid"], record["floor_page"]),
    }


def _xlsx(name: str, replies: list[dict], zone: tzinfo) -> bytes:
    output = BytesIO()
    with Workbook(output, {"constant_memory": True}) as workbook:
        header = workbook.add_format({"bold": True, "bg_color": "#313857", "font_color": "#FFFFFF"})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
        reply_time = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        sheet_no = 1
        sheet = _reply_sheet(workbook, "回复记录", header, wrap)
        row = 1
        name = _excel_text(name)
        for reply in replies:
            if row == XLSX_ROW_LIMIT:
                sheet.autofilter(0, 0, row - 1, 2)
                sheet_no += 1
                sheet = _reply_sheet(workbook, f"回复记录 {sheet_no}", header, wrap)
                row = 1
            sheet.write_string(row, 0, name)
            if reply["ts"] > 0:
                posted_at = datetime.fromtimestamp(reply["ts"], zone).replace(tzinfo=None)
                sheet.write_datetime(row, 1, posted_at, reply_time)
            sheet.write_string(row, 2, _excel_text(to_display_text(reply["content"])))
            row += 1
        if row > 1:
            sheet.autofilter(0, 0, row - 1, 2)
    return output.getvalue()


def _reply_sheet(workbook, name, header, wrap):
    sheet = workbook.add_worksheet(name)
    sheet.set_column(0, 1, 24)
    sheet.set_column(2, 2, 76, wrap)
    sheet.freeze_panes(1, 0)
    sheet.write_row(0, 0, REPLY_HEADERS, header)
    return sheet


def _excel_text(text: str) -> str:
    encoded = text.encode("utf-16-le")
    if len(encoded) <= 2 * 32_767:
        return text
    return encoded[: 2 * 32_766].decode("utf-16-le", errors="ignore") + "…"
