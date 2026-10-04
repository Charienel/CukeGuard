"""Export CukeGuard records to an Excel workbook with optional filters."""
import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import or_


ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from backend.database import (  # noqa: E402
    AlertLog,
    CucumberSample,
    ScanSession,
    SensorReading,
    SessionLocal,
    StorageBatch,
)
from backend.iot_controller import (  # noqa: E402
    TARGET_HUMIDITY_RANGE,
    TARGET_TEMP_RANGE,
)


HEADER_FILL = PatternFill("solid", fgColor="176B4D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(size=16, bold=True, color="176B4D")
ALERT_FILLS = {
    "Normal": PatternFill("solid", fgColor="DDF2E4"),
    "Warning": PatternFill("solid", fgColor="FFF0C2"),
    "Threshold Breach": PatternFill("solid", fgColor="F8D7DA"),
}
ALERT_STATUSES = tuple(ALERT_FILLS)


@dataclass(frozen=True)
class ExportFilters:
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    batch_ids: tuple[int, ...] = ()
    exclude_batch_ids: tuple[int, ...] = ()
    exclude_alert_statuses: tuple[str, ...] = ()
    active_only: bool = False


def parse_utc_datetime(value, end_of_day=False):
    value = value.strip()
    date_only = len(value) == 10
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "use an ISO date or timestamp, such as 2026-10-02 or 2026-10-02T14:30:00Z"
        ) from error

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if date_only and end_of_day:
        parsed += timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def parse_end_date(value):
    return parse_utc_datetime(value, end_of_day=True)


def build_argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=parse_utc_datetime, help="Inclusive UTC date or timestamp")
    parser.add_argument("--end-date", type=parse_end_date, help="Inclusive UTC date or timestamp")
    parser.add_argument("--batch-id", type=int, action="append", default=[], help="Include this batch ID; repeat to include several")
    parser.add_argument("--exclude-batch-id", type=int, action="append", default=[], help="Exclude this batch ID; repeat as needed")
    parser.add_argument("--active-only", action="store_true", help="Include records from currently active batches only")
    parser.add_argument(
        "--exclude-alert-status",
        choices=ALERT_STATUSES,
        action="append",
        default=[],
        help="Exclude sensor readings with this Alert Status; repeat as needed",
    )
    parser.add_argument("--output", type=Path, help="Workbook path (default: timestamped file in exports/)")
    return parser


def reading_alert_status(reading):
    breaches = []
    if reading.temperature_c is not None and not (
        TARGET_TEMP_RANGE[0] <= reading.temperature_c <= TARGET_TEMP_RANGE[1]
    ):
        breaches.append("temperature")
    if reading.humidity_pct is not None and not (
        TARGET_HUMIDITY_RANGE[0] <= reading.humidity_pct <= TARGET_HUMIDITY_RANGE[1]
    ):
        breaches.append("humidity")
    if breaches:
        return f"Threshold Breach: {', '.join(breaches)}"
    if reading.correction_note:
        return "Warning"
    return "Normal"


def _apply_record_filters(query, timestamp_column, batch_column, filters):
    if filters.start_date is not None:
        query = query.filter(timestamp_column >= filters.start_date)
    if filters.end_date is not None:
        query = query.filter(timestamp_column <= filters.end_date)
    if filters.batch_ids:
        query = query.filter(batch_column.in_(filters.batch_ids))
    if filters.exclude_batch_ids:
        query = query.filter(batch_column.notin_(filters.exclude_batch_ids))
    return query


def _date_groups(timestamp):
    if timestamp is None:
        return None, None
    month = datetime(timestamp.year, timestamp.month, 1)
    day = datetime(timestamp.year, timestamp.month, timestamp.day)
    return month, day


def _scan_id_for_reading(reading, scans_by_batch):
    previous_scans = [
        scan for scan in scans_by_batch.get(reading.batch_id, ())
        if scan.started_at <= reading.timestamp
    ]
    if not previous_scans:
        return None
    return max(previous_scans, key=lambda scan: (scan.started_at, scan.id)).id


def _filter_summary(filters):
    active = []
    if filters.start_date is not None or filters.end_date is not None:
        start = filters.start_date.isoformat(sep=" ") if filters.start_date else "beginning"
        end = filters.end_date.isoformat(sep=" ") if filters.end_date else "now"
        active.append(f"UTC range: {start} through {end}")
    if filters.batch_ids:
        active.append(f"Batch IDs: {', '.join(map(str, filters.batch_ids))}")
    if filters.exclude_batch_ids:
        active.append(f"Excluded batch IDs: {', '.join(map(str, filters.exclude_batch_ids))}")
    if filters.exclude_alert_statuses:
        active.append(f"Excluded alert statuses: {', '.join(filters.exclude_alert_statuses)}")
    if filters.active_only:
        active.append("Active batches only")
    return "; ".join(active) if active else "No filters; all records included"


def load_rows(filters=None, session_factory=SessionLocal):
    filters = filters or ExportFilters()
    db = session_factory()
    try:
        with db.begin():
            active_batch_ids = None
            if filters.active_only:
                active_batch_ids = tuple(
                    batch_id
                    for (batch_id,) in db.query(StorageBatch.id)
                    .filter(StorageBatch.ended_at.is_(None))
                    .all()
                )

            batch_query = db.query(StorageBatch)
            if filters.batch_ids:
                batch_query = batch_query.filter(StorageBatch.id.in_(filters.batch_ids))
            if filters.exclude_batch_ids:
                batch_query = batch_query.filter(StorageBatch.id.notin_(filters.exclude_batch_ids))
            if active_batch_ids is not None:
                batch_query = batch_query.filter(StorageBatch.id.in_(active_batch_ids))
            if filters.start_date is not None:
                batch_query = batch_query.filter(
                    or_(StorageBatch.ended_at.is_(None), StorageBatch.ended_at >= filters.start_date)
                )
            if filters.end_date is not None:
                batch_query = batch_query.filter(StorageBatch.started_at <= filters.end_date)
            batches = batch_query.order_by(StorageBatch.started_at, StorageBatch.id).all()

            readings = _apply_record_filters(
                db.query(SensorReading), SensorReading.timestamp,
                SensorReading.batch_id, filters,
            ).order_by(SensorReading.timestamp, SensorReading.id).all()
            if active_batch_ids is not None:
                readings = [row for row in readings if row.batch_id in active_batch_ids]
            scans_by_batch = {}
            association_scans = db.query(ScanSession)
            if filters.batch_ids:
                association_scans = association_scans.filter(ScanSession.batch_id.in_(filters.batch_ids))
            if filters.exclude_batch_ids:
                association_scans = association_scans.filter(ScanSession.batch_id.notin_(filters.exclude_batch_ids))
            if active_batch_ids is not None:
                association_scans = association_scans.filter(ScanSession.batch_id.in_(active_batch_ids))
            for scan in association_scans.order_by(ScanSession.started_at, ScanSession.id).all():
                scans_by_batch.setdefault(scan.batch_id, []).append(scan)

            scans = _apply_record_filters(
                db.query(ScanSession), ScanSession.started_at,
                ScanSession.batch_id, filters,
            ).order_by(ScanSession.started_at, ScanSession.id).all()
            if active_batch_ids is not None:
                scans = [row for row in scans if row.batch_id in active_batch_ids]
            samples = _apply_record_filters(
                db.query(CucumberSample).join(ScanSession), ScanSession.started_at,
                CucumberSample.batch_id, filters,
            ).order_by(ScanSession.started_at, CucumberSample.scan_id, CucumberSample.cucumber_number).all()
            if active_batch_ids is not None:
                samples = [row for row in samples if row.batch_id in active_batch_ids]
            alerts = _apply_record_filters(
                db.query(AlertLog), AlertLog.sent_at,
                AlertLog.batch_id, filters,
            ).order_by(AlertLog.sent_at, AlertLog.id).all()
            if active_batch_ids is not None:
                alerts = [row for row in alerts if row.batch_id in active_batch_ids]

            latest_scans = {}
            for scan in scans:
                latest_scans[scan.batch_id] = scan
            samples_by_scan = {}
            for sample in samples:
                samples_by_scan.setdefault(sample.scan_id, []).append(sample)

            latest_scan_summaries = {}
            for batch_id, scan in latest_scans.items():
                scan_samples = samples_by_scan.get(scan.id, [])

                def average(attribute):
                    values = [
                        getattr(sample, attribute)
                        for sample in scan_samples
                        if getattr(sample, attribute) is not None
                    ]
                    return round(sum(values) / len(values), 2) if values else None

                condition_counts = {
                    condition: sum(sample.condition == condition for sample in scan_samples)
                    for condition in ("good", "bad")
                }
                condition_summary = (
                    f"{condition_counts['good']} good, {condition_counts['bad']} bad"
                    if scan_samples else "No detections"
                )
                latest_scan_summaries[batch_id] = [
                    scan.id,
                    scan.started_at,
                    average("yolo_confidence"),
                    average("hsi_hue_mean"),
                    average("lbp_texture_score"),
                    condition_summary,
                    average("est_shelf_life_days"),
                ]

            readings = [
                (reading, reading_alert_status(reading), _scan_id_for_reading(reading, scans_by_batch))
                for reading in readings
            ]
            excluded_statuses = {status.casefold() for status in filters.exclude_alert_statuses}
            readings = [
                (reading, status, scan_id)
                for reading, status, scan_id in readings
                if status.split(":", 1)[0].casefold() not in excluded_statuses
            ]

            rows = {
                "batches": [[
                    *_date_groups(row.started_at),
                    row.id, row.batch_code,
                    row.initial_quantity, row.started_at, row.ended_at,
                    row.target_temp_c, row.target_humidity_pct,
                    row.owner_phone, row.notes,
                    *latest_scan_summaries.get(row.id, [None] * 7),
                ] for row in batches],
                "readings": [[
                    *_date_groups(reading.timestamp),
                    reading.id, reading.batch_id,
                    reading.batch.batch_code if reading.batch else None,
                    reading.timestamp, scan_id,
                    reading.temperature_c, reading.humidity_pct,
                    reading.peltier_pwm_pct, reading.mister_active,
                    status, reading.correction_note,
                ] for reading, status, scan_id in readings],
                "scans": [[
                    *_date_groups(row.started_at),
                    row.id, row.batch_id,
                    row.batch.batch_code if row.batch else None,
                    row.started_at, row.trigger_type,
                ] for row in scans],
                "samples": [[
                    *_date_groups(row.scan_session.started_at if row.scan_session else None),
                    row.id, row.batch_id,
                    row.batch.batch_code if row.batch else None,
                    row.scan_id, row.scan_session.started_at if row.scan_session else None,
                    row.cucumber_number, row.track_position_mm,
                    row.bbox_x, row.bbox_y, row.bbox_w, row.bbox_h,
                    row.yolo_confidence, row.hsi_hue_mean,
                    row.lbp_texture_score, row.condition,
                    row.est_shelf_life_days,
                ] for row in samples],
                "alerts": [[
                    *_date_groups(row.sent_at),
                    row.id, row.batch_id,
                    row.batch.batch_code if row.batch else None,
                    row.scan_session_id, row.sent_at, row.phone_number,
                    row.bad_count, row.delivery_status, row.message,
                ] for row in alerts],
            }
            timestamps = (
                [row.started_at for row in batches]
                + [reading.timestamp for reading, _, _ in readings]
                + [row.started_at for row in scans]
                + [row.sent_at for row in alerts]
            )
        return rows, timestamps
    finally:
        db.close()


def style_data_sheet(sheet, headers, rows):
    sheet.append(headers)
    for row in rows:
        sheet.append(row)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 30

    for column_index, header in enumerate(headers, start=1):
        values = [sheet.cell(row, column_index).value for row in range(2, min(sheet.max_row, 202) + 1)]
        width = max([len(str(header))] + [len(str(value)) for value in values if value is not None]) + 2
        sheet.column_dimensions[sheet.cell(1, column_index).column_letter].width = min(width, 64)
        for row_index in range(2, sheet.max_row + 1):
            cell = sheet.cell(row_index, column_index)
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            if header == "Month" and isinstance(cell.value, datetime):
                cell.number_format = "mmmm yyyy"
            if header == "Day" and isinstance(cell.value, datetime):
                cell.number_format = "mmmm d"
            if header.startswith("YOLO Confidence") and isinstance(cell.value, (float, int)):
                cell.number_format = "0.0%"
            if header == "Alert Status":
                status = str(cell.value).split(":", 1)[0]
                if status in ALERT_FILLS:
                    cell.fill = ALERT_FILLS[status]
                    cell.font = Font(bold=True)
            if header in {"Message", "Notes", "Correction Note"}:
                cell.alignment = Alignment(vertical="top", wrap_text=True)


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must be earlier than or equal to --end-date")

    filters = ExportFilters(
        start_date=args.start_date,
        end_date=args.end_date,
        batch_ids=tuple(dict.fromkeys(args.batch_id)),
        exclude_batch_ids=tuple(dict.fromkeys(args.exclude_batch_id)),
        exclude_alert_statuses=tuple(dict.fromkeys(args.exclude_alert_status)),
        active_only=args.active_only,
    )
    rows, timestamps = load_rows(filters)
    exported_at = datetime.now(timezone.utc)
    output_dir = ROOT / "exports"
    output_dir.mkdir(exist_ok=True)
    filename = f"CukeGuard_Database_Export_{exported_at:%Y%m%d_%H%M%S_UTC}.xlsx"
    output_path = args.output or output_dir / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"
    overview.sheet_view.showGridLines = False
    overview["A1"] = "CukeGuard Database Export"
    overview["A1"].font = TITLE_FONT
    overview.merge_cells("A1:D1")
    overview.append([])
    overview.append(["Exported at (UTC)", exported_at.replace(tzinfo=None)])
    overview.append(["Data range (UTC)", (
        f"{min(timestamps):%Y-%m-%d %H:%M:%S} to {max(timestamps):%Y-%m-%d %H:%M:%S}"
        if timestamps else "No dated records"
    )])
    overview.append(["Source database", str(ROOT / "cukeguard.db")])
    overview.append(["Refresh", "This workbook is a snapshot. Run python export_database.py to refresh it."])
    overview.append(["Applied filters", _filter_summary(filters)])
    overview.append(["Sensor Scan ID", "Most recent scan in the same batch at or before the reading; blank if none exists."])
    overview.append(["Date hierarchy", "Month > Day > exact record time. Month and Day are Excel dates; use the sheet filters to select periods."])
    overview.append(["Inactive records", "Included unless --active-only is selected."])
    overview.append(["Batch metrics", "Averages and condition counts come from each batch's latest scan within the selected export filters."])
    for row_number in range(3, 12):
        overview.cell(row_number, 1).font = Font(bold=True)
    overview["B3"].number_format = "yyyy-mm-dd hh:mm:ss"
    overview["A13"] = "Sheet"
    overview["B13"] = "Records"
    for cell in overview[13]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    for row_number, (label, key) in enumerate([
        ("Batches", "batches"),
        ("Sensor Readings", "readings"),
        ("Scan Sessions", "scans"),
        ("Cucumber Samples", "samples"),
        ("Alerts", "alerts"),
    ], start=14):
        overview.cell(row_number, 1, label)
        overview.cell(row_number, 2, len(rows[key]))
    overview.column_dimensions["A"].width = 25
    overview.column_dimensions["B"].width = 80
    overview.freeze_panes = "A13"

    definitions = [
        ("Batches", ["Month", "Day", "Batch ID", "Batch Code", "Initial Quantity", "Started At UTC", "Ended At UTC", "Target Temp C", "Target Humidity Pct", "Owner Phone", "Notes", "Latest Scan ID", "Latest Scan At UTC", "YOLO Confidence (Latest Scan Avg)", "HSI Hue Mean (Latest Scan Avg)", "LBP Texture Score (Latest Scan Avg)", "Condition (Latest Scan)", "Est Shelf Life Days (Latest Scan Avg)"], "batches"),
        ("Sensor Readings", ["Month", "Day", "Reading ID", "Batch ID", "Batch Code", "Timestamp UTC", "Scan ID (latest at/before reading)", "Temperature C", "Humidity Pct", "Peltier PWM Pct", "Mister Active", "Alert Status", "Correction Note"], "readings"),
        ("Scan Sessions", ["Month", "Day", "Scan ID", "Batch ID", "Batch Code", "Started At UTC", "Trigger Type"], "scans"),
        ("Cucumber Samples", ["Month", "Day", "Sample ID", "Batch ID", "Batch Code", "Scan ID", "Scan Started At UTC", "Cucumber Number", "Track Position MM", "BBox X", "BBox Y", "BBox Width", "BBox Height", "YOLO Confidence", "HSI Hue Mean", "LBP Texture Score", "Condition", "Est Shelf Life Days"], "samples"),
        ("Alerts", ["Month", "Day", "Alert ID", "Batch ID", "Batch Code", "Scan ID", "Sent At UTC", "Phone Number", "Bad Count", "Delivery Status", "Message"], "alerts"),
    ]
    for title, headers, key in definitions:
        sheet = workbook.create_sheet(title)
        style_data_sheet(sheet, headers, rows[key])

    workbook.properties.title = "CukeGuard Database Export"
    workbook.properties.subject = "Full historical snapshot of CukeGuard database records"
    workbook.save(output_path)
    print(f"Workbook: {output_path}")
    print(f"Records: { {key: len(value) for key, value in rows.items()} }")


if __name__ == "__main__":
    main()