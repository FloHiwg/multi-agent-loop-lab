import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = new URL("../", import.meta.url).pathname;
const outputDir = `${root}vault/northstar`;
const previewDir = `${root}tmp/northstar-previews`;
await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const wb = Workbook.create();
const summary = wb.worksheets.add("Customer Summary");
const segments = wb.worksheets.add("Segment Detail");
for (const sheet of [summary, segments]) sheet.showGridLines = false;

const navy = "#17324D", teal = "#1E7A78", pale = "#EAF3F2", line = "#D7E0E5", ink = "#24313D";
summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [["Northstar Analytics | Q4 2025 Customer Appendix"]];
summary.getRange("A1:F1").format = { fill: navy, font: { bold: true, color: "#FFFFFF", size: 16 }, rowHeight: 32, verticalAlignment: "center" };
summary.getRange("A3:B7").values = [
  ["Document ID", "northstar-customer-appendix-q4-2025"], ["Reporting period", "Q4 2025"],
  ["As-of date", new Date("2025-12-31T00:00:00Z")], ["Issued", new Date("2026-01-16T00:00:00Z")],
  ["Status", "Final management reporting"],
];
summary.getRange("A3:A7").format = { fill: pale, font: { bold: true, color: teal } };
summary.getRange("B5:B6").format.numberFormat = "yyyy-mm-dd";
summary.getRange("A9:F9").values = [["Segment", "Customers", "Annual recurring revenue", "ACV", "Period", "Role"]];
summary.getRange("A10:F12").formulas = [
  ["='Segment Detail'!A5", "='Segment Detail'!B5", "='Segment Detail'!C5", "='Segment Detail'!D5", "='Segment Detail'!E5", "='Segment Detail'!F5"],
  ["='Segment Detail'!A6", "='Segment Detail'!B6", "='Segment Detail'!C6", "='Segment Detail'!D6", "='Segment Detail'!E6", "='Segment Detail'!F6"],
  ["='Segment Detail'!A7", "='Segment Detail'!B7", "='Segment Detail'!C7", "='Segment Detail'!D7", "='Segment Detail'!E7", "='Segment Detail'!F7"],
];
summary.getRange("A13:F13").values = [["Enterprise total", 126, 2318400, null, "2025-12-31", "actual"]];
summary.getRange("D13").formulas = [["=C13/B13"]];
summary.getRange("A9:F9").format = { fill: navy, font: { bold: true, color: "#FFFFFF" }, rowHeight: 26 };
summary.getRange("A10:F13").format.borders = { preset: "inside", style: "thin", color: line };
summary.getRange("A13:F13").format = { fill: pale, font: { bold: true, color: ink }, borders: { preset: "doubleBottom", style: "medium", color: teal } };
summary.getRange("B10:B13").format.numberFormat = "#,##0";
summary.getRange("C10:C13").format.numberFormat = '€#,##0';
summary.getRange("D10:D13").format.numberFormat = '€#,##0';
summary.getRange("A15:F16").merge(true);
summary.getRange("A15").values = [["Control note"]];
summary.getRange("A16").values = [["Enterprise ACV equals enterprise annual recurring revenue divided by enterprise customers: EUR 2,318,400 / 126 = EUR 18,400."]];
summary.getRange("A15:F15").format = { fill: teal, font: { bold: true, color: "#FFFFFF" } };
summary.getRange("A16:F16").format = { fill: "#F6F8FA", font: { color: ink }, wrapText: true, rowHeight: 34 };
summary.freezePanes.freezeRows(1);
summary.getRange("A1:F16").format.font.name = "Aptos";
summary.getRange("A1:A16").format.columnWidth = 25;
summary.getRange("B1:B16").format.columnWidth = 14;
summary.getRange("C1:C16").format.columnWidth = 24;
summary.getRange("D1:D16").format.columnWidth = 15;
summary.getRange("E1:E16").format.columnWidth = 16;
summary.getRange("F1:F16").format.columnWidth = 13;

segments.getRange("A1:F1").merge();
segments.getRange("A1").values = [["Enterprise Segment Detail"]];
segments.getRange("A1:F1").format = { fill: navy, font: { bold: true, color: "#FFFFFF", size: 15 }, rowHeight: 30 };
segments.getRange("A4:F4").values = [["Segment", "Customers", "ARR", "ACV", "As of", "Role"]];
segments.getRange("A5:C7").values = [["Strategic", 18, 648000], ["Mid-market", 52, 1040000], ["Growth", 56, 630400]];
segments.getRange("D5").formulas = [["=C5/B5"]];
segments.getRange("D5:D7").fillDown();
segments.getRange("E5:F7").values = [["2025-12-31", "actual"], ["2025-12-31", "actual"], ["2025-12-31", "actual"]];
segments.getRange("A4:F4").format = { fill: teal, font: { bold: true, color: "#FFFFFF" } };
segments.getRange("A5:F7").format.borders = { preset: "inside", style: "thin", color: line };
segments.getRange("B5:B7").format.numberFormat = "#,##0";
segments.getRange("C5:D7").format.numberFormat = '€#,##0';
segments.getRange("A1:F7").format.font.name = "Aptos";
segments.getRange("A1:A7").format.columnWidth = 22;
segments.getRange("B1:B7").format.columnWidth = 14;
segments.getRange("C1:C7").format.columnWidth = 18;
segments.getRange("D1:D7").format.columnWidth = 14;
segments.getRange("E1:E7").format.columnWidth = 16;
segments.getRange("F1:F7").format.columnWidth = 12;

const inspection = await wb.inspect({ kind: "table", range: "'Customer Summary'!A1:F16", include: "values,formulas", tableMaxRows: 20, tableMaxCols: 8 });
console.log(inspection.ndjson);
const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
console.log(errors.ndjson);
for (const sheetName of ["Customer Summary", "Segment Detail"]) {
  const preview = await wb.render({ sheetName, autoCrop: "all", scale: 1.5, format: "png" });
  await fs.writeFile(`${previewDir}/${sheetName.replaceAll(" ", "-").toLowerCase()}.png`, new Uint8Array(await preview.arrayBuffer()));
}
const out = await SpreadsheetFile.exportXlsx(wb);
await out.save(`${outputDir}/board-appendix-q4-2025.xlsx`);

