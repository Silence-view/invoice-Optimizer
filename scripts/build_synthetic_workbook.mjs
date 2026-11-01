// Author the bounded synthetic XLSX companion with the bundled Artifact Tool.
// Run the spreadsheet operation marker before invoking this builder.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const inputDir = path.resolve(process.argv[2] || path.join(project, 'var/synthetic/2026-09-42-2000'));
const outputDir = path.join(project, 'output/spreadsheets');
const verificationDir = path.join(project, 'output/verification');
const previewDir = path.join(verificationDir, 'workbook-previews');
const dependencies = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');
const moduleDirectory = process.env.ARTIFACT_NODE_MODULES || path.join(dependencies, 'node/node_modules');
const python = process.env.ARTIFACT_PYTHON || path.join(dependencies, 'python/bin/python3');
const runtime = await fs.mkdtemp(path.join(os.tmpdir(), 'invoice-workbook-deps-'));
await fs.symlink(moduleDirectory, path.join(runtime, 'node_modules'), 'dir');
const resolver = createRequire(path.join(runtime, 'package.json'));
const {Workbook, SpreadsheetFile} = await import(pathToFileURL(resolver.resolve('@oai/artifact-tool')).href);
const numericFields = new Set(['revision', 'amount', 'expected_amount', 'customer_age_days', 'supplier_age_days', 'hour_utc', 'cross_border']);
const financialFields = new Set(['amount', 'expected_amount']);
const outputPath = path.join(outputDir, 'synthetic-source-workbook.xlsx');
const sourceMetadata = [];

function csvWithLiteralTimestamps(filename) {
  // The current Artifact Tool coerces ISO-looking strings to Date even with @
  // formatting. A zero-width presentation sentinel prevents this during render;
  // the narrow OpenXML repair below removes it from final event_at cells.
  return execFileSync(python, ['-c',
    "import csv,io,sys; rows=list(csv.reader(open(sys.argv[1],newline=''))); ix=rows[0].index('event_at'); [(row.__setitem__(ix,'\\u200b'+row[ix])) for row in rows[1:]]; out=io.StringIO(); writer=csv.writer(out,lineterminator='\\n');writer.writerows(rows);sys.stdout.write(out.getvalue())",
    path.join(inputDir, filename)], {encoding: 'utf8', maxBuffer: 16 * 1024 * 1024});
}

function columnName(index) {
  let output = '';
  for (let value = index + 1; value; value = Math.floor((value - 1) / 26)) output = String.fromCharCode(65 + (value - 1) % 26) + output;
  return output;
}

function convertValues(matrix) {
  const headers = matrix[0];
  return [headers, ...matrix.slice(1).map(row => row.map((value, index) => {
    if (!numericFields.has(headers[index])) return value;
    if (value === '' || value === null) return null;
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`Unexpected nonnumeric source value in ${headers[index]}`);
    if (financialFields.has(headers[index])) {
      const sourceCents = BigInt(String(value).replace('.', ''));
      const exportedCents = BigInt(number.toFixed(2).replace('.', ''));
      if (sourceCents !== exportedCents) throw new Error(`Monetary conversion changed ${value}`);
    }
    return number;
  }))];
}

function styleSource(sheet, values, tableName) {
  const lastRow = values.length;
  const lastColumn = columnName(values[0].length - 1);
  const range = sheet.getRange(`A1:${lastColumn}${lastRow}`);
  range.format.font = {name: 'Arial', size: 10, color: '#172536'};
  range.format.rowHeight = 21;
  range.format.verticalAlignment = 'center';
  range.format.horizontalAlignment = 'left';
  sheet.showGridLines = false;
  const table = sheet.tables.add(`A1:${lastColumn}${lastRow}`, true, tableName);
  table.style = 'TableStyleMedium2';
  table.showFilterButton = true;
  const widths = [29, 10, 27, 16, 33, 13, 11, 17, 56, 16, 20, 25, 25, 13, 17];
  widths.forEach((width, index) => {
    const letter = columnName(index);
    sheet.getRange(`${letter}1:${letter}${lastRow}`).format.columnWidth = width;
  });
  for (const [index, name] of values[0].entries()) {
    const letter = columnName(index);
    const column = sheet.getRange(`${letter}2:${letter}${lastRow}`);
    if (numericFields.has(name)) {
      column.format.horizontalAlignment = 'right';
      column.setNumberFormat(financialFields.has(name) ? '#,##0.00;[Red](#,##0.00)' : '0');
    } else column.setNumberFormat('@');
  }
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: '#223A55', font: {name: 'Arial', size: 10, bold: true, color: '#FFFFFF'},
    horizontalAlignment: 'center', verticalAlignment: 'center',
    borders: {insideVertical: {style: 'thin', color: '#FFFFFF'}},
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 29;
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(1);
  sheet.getRange(`H2:H${lastRow}`).conditionalFormats.add('cellIs', {
    operator: 'lessThan', formula: 0,
    format: {fill: '#FCE8E6', font: {color: '#9B1C1C', bold: true}},
  });
}

try {
  await fs.mkdir(outputDir, {recursive: true});
  await fs.mkdir(previewDir, {recursive: true});
  const sourceA = csvWithLiteralTimestamps('source_a.csv');
  const workbook = await Workbook.fromCSV(sourceA, {sheetName: 'Source A'});
  const first = workbook.worksheets.getItem('Source A');
  const matrixA = convertValues(first.getUsedRange().values);
  first.getRange(`A1:O${matrixA.length}`).values = matrixA;
  styleSource(first, matrixA, 'SyntheticSourceA');

  const sourceB = csvWithLiteralTimestamps('source_b.csv');
  const importB = await Workbook.fromCSV(sourceB, {sheetName: 'Import'});
  const matrixB = convertValues(importB.worksheets.getItem('Import').getUsedRange().values);
  const second = workbook.worksheets.add('Source B');
  second.getRange(`A1:O${matrixB.length}`).values = matrixB;
  styleSource(second, matrixB, 'SyntheticSourceB');

  const customers = JSON.parse(await fs.readFile(path.join(inputDir, 'customers.json'), 'utf8'));
  const customerHeaders = ['id', 'legal_name', 'address', 'email', 'alert_email', 'vat_number'];
  const masterValues = [customerHeaders, ...customers.map(customer => customerHeaders.map(name => customer[name]))];
  const master = workbook.worksheets.add('Master Key');
  const masterRange = master.getRange(`A1:F${masterValues.length}`);
  masterRange.values = masterValues;
  masterRange.format.font = {name: 'Arial', size: 10, color: '#172536'};
  masterRange.format.rowHeight = 24;
  masterRange.format.verticalAlignment = 'center';
  masterRange.format.horizontalAlignment = 'left';
  masterRange.setNumberFormat('@');
  master.showGridLines = false;
  const customerTable = master.tables.add(`A1:F${masterValues.length}`, true, 'SyntheticMasterKey');
  customerTable.style = 'TableStyleMedium2';
  customerTable.showFilterButton = true;
  [13, 38, 67, 38, 37, 24].forEach((width, index) => master.getRange(`${columnName(index)}1:${columnName(index)}${masterValues.length}`).format.columnWidth = width);
  master.getRange('A1:F1').format = {
    fill: '#223A55', font: {name: 'Arial', size: 10, bold: true, color: '#FFFFFF'},
    horizontalAlignment: 'center', verticalAlignment: 'center',
    borders: {insideVertical: {style: 'thin', color: '#FFFFFF'}},
  };
  master.getRange('A1:F1').format.rowHeight = 29;
  master.freezePanes.freezeRows(1);

  for (const name of ['source_a.csv', 'source_b.csv', 'customers.json', 'manifest.json']) {
    const bytes = await fs.readFile(path.join(inputDir, name));
    sourceMetadata.push({name, bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex')});
  }
  workbook.recalculate();
  const inspections = [];
  for (const name of ['Source A', 'Source B', 'Master Key']) {
    inspections.push({sheet: name, result: (await workbook.inspect({kind: 'table', range: `'${name}'!A1:${name === 'Master Key' ? 'F9' : 'O6'}`, include: 'values,formulas', tableMaxRows: 6, tableMaxCols: 15, maxChars: 3000})).ndjson});
  }
  const errors = await workbook.inspect({kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options: {useRegex: true, maxResults: 30}, summary: 'formula error scan', maxChars: 1500});
  await fs.writeFile(path.join(verificationDir, 'workbook-artifact-inspection.json'), JSON.stringify({inspections, errorScan: errors.ndjson}, null, 2));
  const previews = [];
  for (const [sheetName, range, filename] of [
    ['Source A', 'A1:H12', 'source-a-identifiers.png'],
    ['Source A', 'I1:O12', 'source-a-features.png'],
    ['Source B', 'A1:H12', 'source-b-identifiers.png'],
    ['Source B', 'I1:O12', 'source-b-features.png'],
    ['Master Key', 'A1:F9', 'master-key.png'],
  ]) {
    const preview = await workbook.render({sheetName, range, scale: 1.4, format: 'png'});
    const destination = path.join(previewDir, filename);
    await fs.writeFile(destination, new Uint8Array(await preview.arrayBuffer()));
    previews.push({sheet: sheetName, range, path: destination});
  }
  const file = await SpreadsheetFile.exportXlsx(workbook);
  await file.save(outputPath);

  // Missing-capability fallback: repair only the two event_at columns using
  // Python's ZIP/XML standard library. All workbook authoring/layout is Artifact
  // Tool output; openpyxl remains read-only. Exact source timestamps are the
  // authority, and every saved value is verified immediately afterwards.
  const repairPython = `
import csv,json,sys,xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
source,target=Path(sys.argv[1]),Path(sys.argv[2])
ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('x',ns)
ET.register_namespace('r','http://schemas.openxmlformats.org/officeDocument/2006/relationships')
replacements={}
counts={}
with ZipFile(target) as original:
    for number,filename in [(1,'source_a.csv'),(2,'source_b.csv')]:
        part=f'xl/worksheets/sheet{number}.xml'
        tree=ET.fromstring(original.read(part))
        cells={cell.attrib['r']:cell for cell in tree.findall('.//{'+ns+'}c')}
        with (source/filename).open(newline='') as handle: rows=list(csv.DictReader(handle))
        for row_number,row in enumerate(rows,start=2):
            cell=cells[f'E{row_number}']
            cell.set('t','inlineStr')
            for child in list(cell):cell.remove(child)
            inline=ET.SubElement(cell,'{'+ns+'}is')
            ET.SubElement(inline,'{'+ns+'}t').text=row['event_at']
        replacements[part]=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
        counts[filename]=len(rows)
    temporary=target.with_suffix('.timestamp-repair.tmp')
    with ZipFile(temporary,'w',compression=ZIP_DEFLATED) as repaired:
        for item in original.infolist():repaired.writestr(item,replacements.get(item.filename,original.read(item.filename)))
temporary.replace(target)
print(json.dumps(counts))
`;
  const timestampRepair = JSON.parse(execFileSync(python, ['-c', repairPython, inputDir, outputPath], {encoding: 'utf8'}));

  // Read-only XLSX round trip. No openpyxl authoring is used.
  const validatePython = `
import csv, hashlib, json, sys
from decimal import Decimal
from pathlib import Path
from openpyxl import load_workbook
source, target = Path(sys.argv[1]), Path(sys.argv[2])
workbook = load_workbook(target, read_only=False, data_only=False)
assert workbook.sheetnames == ['Source A', 'Source B', 'Master Key']
numeric = {'revision','amount','expected_amount','customer_age_days','supplier_age_days','hour_utc','cross_border'}
money = {'amount','expected_amount'}
results=[]
for sheet_name, filename in [('Source A','source_a.csv'), ('Source B','source_b.csv')]:
    with (source/filename).open(newline='') as f:
        csv_rows=list(csv.reader(f))
    sheet=workbook[sheet_name]
    actual=list(sheet.iter_rows(values_only=True))
    assert list(actual[0]) == csv_rows[0], 'Header mismatch'
    assert len(actual) == len(csv_rows), 'Row count mismatch'
    assert sheet.freeze_panes and int(''.join(filter(str.isdigit,sheet.freeze_panes)))>1, 'Header not frozen'
    assert len(sheet.tables)==1 and sheet.sheet_view.showGridLines is not True
    assert not {'label','scenario','synthetic_risk_label','label_available_at'}.intersection(csv_rows[0])
    monetary_cells=0
    for row_number, (expected, observed) in enumerate(zip(csv_rows[1:],actual[1:]),start=2):
        for column, (name, original, value) in enumerate(zip(csv_rows[0],expected,observed), start=1):
            assert sheet.cell(row_number,column).data_type!='f', 'Unexpected formula'
            if name in numeric:
                if original=='': assert value is None, 'Blank numeric input replaced'
                else:
                    assert isinstance(value,(int,float)) and not isinstance(value,bool), 'Numeric source became text'
                    assert Decimal(str(value))==Decimal(original), f'Changed decimal: {sheet_name} row {row_number} {name}'
                    if name in money:
                        monetary_cells+=1
                        assert '0.00' in sheet.cell(row_number,column).number_format
            else:
                assert value==original, f'Changed text: {sheet_name} row {row_number} {name}'
    results.append({'sheet':sheet_name,'source':filename,'data_rows':len(actual)-1,'columns':len(actual[0]),'headers_exact':True,'all_values_preserved':True,'monetary_numeric_cells_verified':monetary_cells,'freeze_panes':sheet.freeze_panes})
customers=json.loads((source/'customers.json').read_text())
master=workbook['Master Key']
headers=['id','legal_name','address','email','alert_email','vat_number']
assert list(next(master.iter_rows(values_only=True)))==headers
assert list(master.iter_rows(min_row=2,values_only=True))==[tuple(c[k] for k in headers) for c in customers]
assert master.freeze_panes=='A2' and len(master.tables)==1
results.append({'sheet':'Master Key','data_rows':len(customers),'columns':6,'all_values_preserved':True,'freeze_panes':master.freeze_panes})
workbook.close()
print(json.dumps({'checks_passed':True,'sheets':results,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size}))
`;
  const roundtrip = JSON.parse(execFileSync(python, ['-c', validatePython, inputDir, outputPath], {encoding: 'utf8', maxBuffer: 1024 * 1024}));
  const receipt = {
    kind: 'synthetic_xlsx_input_companion', synthetic: true, output_path: outputPath,
    authored_with: '@oai/artifact-tool', input_directory: inputDir, sources: sourceMetadata,
    ...roundtrip, previews,
    serializer_workaround: {scope: 'event_at cells only', operation: 'Restore exact CSV ISO text through standard-library OpenXML patch after Artifact Tool export', rows: timestampRepair},
    visual_review: 'PENDING_AGENT_INSPECTION',
    limitations: ['Synthetic test fixture only. Deliberately invalid source values are preserved.', 'event_at remains ISO timezone text, including deliberate invalid fixtures.', 'Amounts use native Excel numeric cells at two decimal places; every saved monetary value was checked against source Decimal values.', 'No formulas, labels, model truth, invoice approvals or dispatch instructions are embedded.'],
  };
  await fs.writeFile(path.join(verificationDir, 'workbook-validation.json'), JSON.stringify(receipt, null, 2) + '\n');
  console.log(JSON.stringify({outputPath, checksPassed: roundtrip.checks_passed, sheets: roundtrip.sheets, previews: previews.length}, null, 2));
} finally {
  await fs.rm(runtime, {recursive: true, force: true});
}
