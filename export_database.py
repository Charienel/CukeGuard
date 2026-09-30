"""Export the complete CukeGuard SQLite history to a filterable Excel workbook."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


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


HEADER_FILL = PatternFill("solid", fgColor="176B4D")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TITLE_FONT = Font(size=16, bold=True, color="176B4D")


def load_rows():
    db = SessionLocal()
    try:
        with db.begin():
            batches = db.query(StorageBatch).order_by(
                StorageBatch.started_at, StorageBatch.id
            ).all()
            readings = db.query(SensorReading).order_by(
                SensorReading.timestamp, SensorReading.id
            ).all()
            scans = db.query(ScanSession).order_by(
                ScanSession.started_at, ScanSession.id
            ).all()
            samples = db.query(CucumberSample).order_by(
                CucumberSample.scan_id, CucumberSample.cucumber_number
            ).all()
            alerts = db.query(AlertLog).order_by(
                AlertLog.sent_at, AlertLog.id
            ).all()

            rows = {
                "batches": [[
                    row.id, row.batch_code, row.cucumber_variety,
                    row.initial_quantity, row.started_at, row.ended_at,
                    row.target_temp_c, row.target_humidity_pct,
                    row.owner_phone, row.notes,
                ] for row in batches],
                "readings": [[
                    row.id, row.batch_id, row.timestamp, row.temperature_c,
                    row.humidity_pct, row.peltier_pwm_pct, row.mister_active,
                    row.correction_note,
                ] for row in readings],
                "scans": [[
                    row.id, row.batch_id,
                    row.batch.batch_code if row.batch else None,
                    row.started_at, row.trigger_type,
                ] for row in scans],
                "samples": [[
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
                    row.id, row.batch_id,
                    row.batch.batch_code if row.batch else None,
                    row.scan_session_id, row.sent_at, row.phone_number,
                    row.bad_count, row.delivery_status, row.message,
                ] for row in alerts],
            }
            timestamps = (
                [row.started_at for row in batches]
                + [row.timestamp for row in readings]
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
            if header == "YOLO Confidence" and isinstance(cell.value, (float, int)):
                cell.number_format = "0.0%"
            if header in {"Message", "Notes", "Correction Note"}:
                cell.alignment = Alignment(vertical="top", wrap_text=True)


def main():
    rows, timestamps = load_rows()
    exported_at = datetime.now(timezone.utc)
    output_dir = ROOT / "exports"
    output_dir.mkdir(exist_ok=True)
    filename = f"CukeGuard_Database_Export_{exported_at:%Y%m%d_%H%M%S_UTC}.xlsx"
    output_path = output_dir / filename

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
    overview["A3"].font = Font(bold=True)
    overview["A4"].font = Font(bold=True)
    overview["A5"].font = Font(bold=True)
    overview["A6"].font = Font(bold=True)
    overview["B3"].number_format = "yyyy-mm-dd hh:mm:ss"
    overview["A8"] = "Sheet"
    overview["B8"] = "Records"
    for cell in overview[8]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    for row_number, (label, key) in enumerate([
        ("Batches", "batches"),
        ("Sensor Readings", "readings"),
        ("Scan Sessions", "scans"),
        ("Cucumber Samples", "samples"),
        ("Alerts", "alerts"),
    ], start=9):
        overview.cell(row_number, 1, label)
        overview.cell(row_number, 2, len(rows[key]))
    overview.column_dimensions["A"].width = 25
    overview.column_dimensions["B"].width = 80
    overview.freeze_panes = "A8"

    definitions = [
        ("Batches", ["Batch ID", "Batch Code", "Variety", "Initial Quantity", "Started At UTC", "Ended At UTC", "Target Temp C", "Target Humidity Pct", "Owner Phone", "Notes"], "batches"),
        ("Sensor Readings", ["Reading ID", "Batch ID", "Timestamp UTC", "Temperature C", "Humidity Pct", "Peltier PWM Pct", "Mister Active", "Correction Note"], "readings"),
        ("Scan Sessions", ["Scan ID", "Batch ID", "Batch Code", "Started At UTC", "Trigger Type"], "scans"),
        ("Cucumber Samples", ["Sample ID", "Batch ID", "Batch Code", "Scan ID", "Scan Started At UTC", "Cucumber Number", "Track Position MM", "BBox X", "BBox Y", "BBox Width", "BBox Height", "YOLO Confidence", "HSI Hue Mean", "LBP Texture Score", "Condition", "Est Shelf Life Days"], "samples"),
        ("Alerts", ["Alert ID", "Batch ID", "Batch Code", "Scan ID", "Sent At UTC", "Phone Number", "Bad Count", "Delivery Status", "Message"], "alerts"),
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