import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path


class QuizError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, order=True)
class Cell:
    row: int
    col: int

    def label(self):
        column = self.col + 1
        letters = ""
        while column:
            column, char = divmod(column - 1, 26)
            letters = chr(65 + char) + letters
        return f"{letters}{self.row + 1}"


@dataclass
class Sheet:
    rows: int
    cols: int
    values: dict = field(default_factory=dict)
    blocked: list = field(default_factory=list)

    def writable(self, cell):
        return cell.col < self.cols and cell.row < self.rows and not any(
            limits.get("startRowIndex", 0) <= cell.row < limits.get("endRowIndex", self.rows)
            and limits.get("startColumnIndex", 0) <= cell.col < limits.get("endColumnIndex", self.cols)
            for limits in self.blocked
        )


MARKER = re.compile(r"\{\{([QA]):([^{}\s]+)\}\}")


def _locate(sheet, known_answers=None):
    known_answers = known_answers or {}
    questions, blanks, issues = {}, {}, []
    question_rows = set()
    markers = {}
    for cell, value in sorted(sheet.values.items()):
        if not isinstance(value, str):
            continue
        if not value.startswith(("{{Q:", "{{A:")):
            continue
        left = sheet.values.get(Cell(cell.row, cell.col - 1))
        left_marker = MARKER.fullmatch(left) if isinstance(left, str) else None
        if left_marker and left_marker[1] == "A" and known_answers.get(left_marker[2]) == value:
            continue
        marker = MARKER.fullmatch(value)
        if not marker:
            issues.append(f"{cell.label()} 的佔位符格式無效")
            continue
        kind, key = marker.groups()
        markers[cell] = key
        index = questions if kind == "Q" else blanks
        if key in index:
            issues.append(f"題目 {key} 的 {kind} 標記重複")
        else:
            index[key] = cell
        if kind == "Q":
            if cell.row in question_rows:
                issues.append(f"第 {cell.row + 1} 列包含多個題目標記")
            question_rows.add(cell.row)
    if not questions:
        issues.append("找不到題目標記")
    issues.extend(f"題目 {key} 缺少填空佔位符" for key in questions.keys() - blanks.keys())
    issues.extend(f"填空佔位符 {key} 沒有對應題目" for key in blanks.keys() - questions.keys())
    targets = {}
    for key, marker in blanks.items():
        cell = Cell(marker.row, marker.col + 1)
        if cell in markers or cell in targets.values() or not sheet.writable(cell):
            issues.append(f"題目 {key} 的填空格 {cell.label()} 無效、重疊或不可寫")
        targets[key] = cell
    if issues:
        raise QuizError("；".join(issues), 409)
    return questions, {key: targets[key] for key in questions}


def locate(sheet, known_answers=None):
    return _locate(sheet, known_answers)[1]


def read_questions(sheet):
    questions, targets = _locate(sheet)
    answer_cells = set(targets.values())
    prompts = {}
    for key, marker in questions.items():
        cell = Cell(marker.row, marker.col + 1)
        value = sheet.values.get(cell)
        if cell not in answer_cells and isinstance(value, str) and not MARKER.fullmatch(value):
            prompts[key] = value.strip()
    return targets, prompts


def write_answers(store, source, preview, winners):
    sheet = store.read(source)
    targets = locate(sheet, winners)
    if targets.keys() != preview.keys():
        changed = "、".join(sorted(targets.keys() ^ preview.keys()))
        raise QuizError(f"來源題目已變更（{changed}），整批未寫回。", 409)
    pending, conflicts = {}, []
    for key, answer in winners.items():
        cell = targets[key]
        previous = sheet.values.get(cell)
        if previous == answer:
            continue
        if previous is not None and previous != "":
            conflicts.append(f"{key}（{cell.label()}）")
        else:
            pending[cell] = answer
    if conflicts:
        raise QuizError(f"以下題目的填空格已有其他內容：{'、'.join(conflicts)}。整批未寫回。", 409)
    if not pending:
        return "所有勝出答案已存在，無須重複寫回。"
    try:
        store.write(source, pending)
    except QuizError as error:
        if error.status in (403, 404):
            raise
        raise QuizError("寫回結果待確認。請恢復連線後重試，系統會先讀回檢查。", 503) from None
    try:
        verified_sheet = store.read(source)
        verified_targets = locate(verified_sheet, winners)
        if verified_targets.keys() != preview.keys() or any(
            verified_sheet.values.get(verified_targets[key]) != answer for key, answer in winners.items()
        ):
            raise QuizError("寫回核對有異常。", 503)
    except QuizError:
        raise QuizError("寫回結果待確認。請恢復連線後重試，系統會先讀回檢查。", 503) from None
    return f"已確認 {len(winners)} 題，寫入 {len(pending)} 格。"


class MemorySheets:
    mode = "offline"

    def __init__(self, books):
        self.books = books

    @classmethod
    def from_fixtures(cls):
        books = {}
        for path in Path(__file__).with_name("fixtures").glob("*.json"):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            cells = {Cell(row, col): value for row, line in enumerate(fixture["rows"])
                     for col, value in enumerate(line) if value is not None}
            books[(path.stem, fixture["sheet"])] = Sheet(len(fixture["rows"]), fixture["columns"], cells)
        return cls(books)

    def resolve(self, book, sheet):
        if not isinstance(book, str) or not isinstance(sheet, str) or (book, sheet) not in self.books:
            raise QuizError("找不到合成範本或工作表。", 404)
        return book, sheet

    def read(self, source):
        return deepcopy(self.books[source])

    def write(self, source, values):
        self.books[source].values.update(values)


class GoogleSheets:
    mode = "google"

    def __init__(self, service=None):
        if service is None:
            try:
                import google.auth
                import httplib2
                from google_auth_httplib2 import AuthorizedHttp
                from googleapiclient.discovery import build

                credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
                transport = AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
                service = build("sheets", "v4", http=transport, cache_discovery=False)
            except ImportError:
                raise QuizError("請先安裝 Google Sheets 模式所需套件。") from None
            except Exception:
                raise QuizError("無法取得 Google Sheets 授權，請檢查本機應用程式預設憑證。", 403) from None
        self.api = service.spreadsheets()

    @staticmethod
    def execute(request):
        try:
            return request.execute(num_retries=0)
        except Exception as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status in (401, 403):
                raise QuizError("Google Sheets 授權不足或已失效，請確認工作表的存取與編輯權限。", 403) from None
            if status == 404:
                raise QuizError("找不到來源 Google 試算表。", 404) from None
            raise QuizError("目前無法存取 Google Sheets，請確認連線及來源設定。", 503) from None

    def resolve(self, book, sheet):
        if isinstance(book, str):
            book = book.strip()
            url = re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)(?:[/?#].*)?", book)
            if url:
                book = url[1]
        if not isinstance(book, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", book) or not isinstance(sheet, str):
            raise QuizError("請輸入有效的 Google 試算表 ID 或網址及工作表名稱。")
        result = self.execute(self.api.get(spreadsheetId=book, fields="sheets(properties(sheetId,title))"))
        for item in result.get("sheets", []):
            if item["properties"]["title"] == sheet:
                return book, item["properties"]["sheetId"]
        raise QuizError("找不到指定的工作表。", 404)

    def read(self, source):
        book, sheet_id = source
        meta = self.execute(self.api.get(
            spreadsheetId=book,
            fields="sheets(properties,merges,protectedRanges),namedRanges(namedRangeId,range)",
        ))
        selected = next((item for item in meta.get("sheets", [])
                         if item["properties"]["sheetId"] == sheet_id), None)
        if selected is None:
            raise QuizError("來源工作表已不存在。", 404)
        props = selected["properties"]
        size = props.get("gridProperties")
        if size is None:
            raise QuizError("來源須為一般工作表。")
        title = "'" + props["title"].replace("'", "''") + "'"
        data = self.execute(self.api.get(
            spreadsheetId=book, ranges=[title],
            fields="sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue))))",
        ))
        grid = next((item for item in data.get("sheets", [])
                     if item["properties"]["sheetId"] == sheet_id), None)
        if grid is None:
            raise QuizError("來源工作表已變更，請重試。", 409)
        sheet = Sheet(size["rowCount"], size["columnCount"], blocked=list(selected.get("merges", [])))
        for block in grid.get("data", []):
            for row, cells in enumerate(block.get("rowData", []), block.get("startRow", 0)):
                for col, value in enumerate(cells.get("values", []), block.get("startColumn", 0)):
                    entered = value.get("userEnteredValue")
                    if entered:
                        sheet.values[Cell(row, col)] = entered.get("stringValue", entered)
                    elif value.get("effectiveValue"):
                        sheet.values[Cell(row, col)] = {"effectiveValue": value["effectiveValue"]}
        named = {item["namedRangeId"]: item["range"] for item in meta.get("namedRanges", [])}
        for protection in selected.get("protectedRanges", []):
            if not protection.get("warningOnly"):
                limits = protection.get("range", named.get(protection.get("namedRangeId"), {}))
                sheet.blocked.append(limits)
        return sheet

    def write(self, source, values):
        book, sheet_id = source
        requests = [{"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": cell.row, "columnIndex": cell.col},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
            "fields": "userEnteredValue",
        }} for cell, value in values.items()]
        self.execute(self.api.batchUpdate(spreadsheetId=book, body={"requests": requests}))
