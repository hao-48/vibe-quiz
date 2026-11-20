import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sheets import Cell, GoogleSheets, MemorySheets, QuizError, Sheet, locate, write_answers


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySheets.from_fixtures()
        self.source = self.store.resolve("starter", "課堂筆記")
        self.sheet = self.store.books[self.source]
        self.preview = locate(self.sheet)

    def test_fixtures(self):
        second = self.store.resolve("alternate", "課堂筆記")
        alternate = locate(self.store.read(second))
        self.assertEqual(list(self.preview), ["1", "2"])
        self.assertEqual(list(alternate), ["甲", "乙", "丙"])
        self.assertNotEqual(set(self.preview.values()), set(alternate.values()))

    def test_readme_template_recipe_uses_question_row_order(self):
        sheet = Sheet(10, 12, {
            Cell(2, 4): "{{Q:單元二}}",
            Cell(3, 7): "{{Q:單元一}}",
            Cell(7, 2): "{{A:單元一}}",
            Cell(8, 9): "{{A:單元二}}",
        })
        self.assertEqual(list(locate(sheet).items()), [
            ("單元二", Cell(8, 10)),
            ("單元一", Cell(7, 3)),
        ])

    def test_conflicting_markers(self):
        cases = [
            (Cell(1, 3), None, "缺少"),
            (Cell(0, 4), "{{A:1}}", "重複"),
            (Cell(0, 4), "{{A:未知}}", "沒有對應"),
            (Cell(0, 4), "{{Q:1}}", "重複"),
            (Cell(1, 4), "{{Q:3}}", "重疊"),
            (Cell(0, 7), "{{A:3}}", "不可寫"),
            (Cell(0, 4), "{{A: }}", "格式"),
        ]
        for cell, value, message in cases:
            with self.subTest(cell=cell, value=value):
                copy = self.store.read(self.source)
                copy.values[cell] = value
                with self.assertRaisesRegex(QuizError, message):
                    locate(copy)

    def test_move_marker_and_write_only_new_target_as_text(self):
        self.sheet.values[Cell(0, 0)] = self.sheet.values.pop(Cell(1, 3))
        old = dict(self.sheet.values)
        write_answers(self.store, self.source, self.preview, {"1": "=1+1"})
        self.assertEqual(self.sheet.values, {**old, Cell(0, 1): "=1+1"})

    def test_all_conflicts_prevent_any_writes(self):
        for previous in ("其他答案", {"formulaValue": "=1+1"}):
            with self.subTest(previous=previous):
                self.sheet.values[self.preview["2"]] = previous
                with self.assertRaisesRegex(QuizError, "整批未寫回"):
                    write_answers(self.store, self.source, self.preview, {"1": "甲", "2": "乙"})
                self.assertNotIn(self.preview["1"], self.sheet.values)
        self.sheet.blocked = [{"startRowIndex": 2}]
        with self.assertRaisesRegex(QuizError, "不可寫"):
            write_answers(self.store, self.source, self.preview, {"1": "甲", "2": "乙"})
        self.assertNotIn(self.preview["1"], self.sheet.values)

    def test_changed_question_inventory(self):
        self.sheet.values[Cell(2, 0)] = "{{Q:新題}}"
        self.sheet.values[Cell(2, 3)] = "{{A:新題}}"
        with self.assertRaisesRegex(QuizError, "來源題目已變更"):
            write_answers(self.store, self.source, self.preview, {"1": "甲"})

    def test_uncertain_write_and_read_back_retry(self):
        actual_write = self.store.write

        def lost_reply(source, cells):
            first = next(iter(cells))
            actual_write(source, {first: cells[first]})
            raise QuizError("模擬斷線", 503)

        winners = {"1": "甲", "2": "乙"}
        self.store.write = Mock(side_effect=lost_reply)
        with self.assertRaisesRegex(QuizError, "待確認"):
            write_answers(self.store, self.source, self.preview, winners)
        self.store.write = Mock(wraps=actual_write)
        self.sheet.values[self.preview["2"]] = "新衝突"
        with self.assertRaisesRegex(QuizError, "已有其他內容"):
            write_answers(self.store, self.source, self.preview, winners)
        self.store.write.assert_not_called()
        self.sheet.values.pop(self.preview["2"])
        write_answers(self.store, self.source, self.preview, winners)
        self.store.write.assert_called_once_with(self.source, {self.preview["2"]: "乙"})
        self.store.write.reset_mock()
        write_answers(self.store, self.source, self.preview, winners)
        self.store.write.assert_not_called()

    def test_marker_like_answer_can_be_verified_and_retried(self):
        winners = {"1": "{{A:示例}}"}
        write_answers(self.store, self.source, self.preview, winners)
        self.assertIn("無須重複", write_answers(self.store, self.source, self.preview, winners))
        self.assertEqual(self.sheet.values[self.preview["1"]], winners["1"])


class GoogleTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.store = GoogleSheets(self.service)
        self.api = self.service.spreadsheets.return_value

    def test_permission_error_is_localized_and_sanitized(self):
        error = RuntimeError("模擬私密回應")
        error.resp = SimpleNamespace(status=403)
        self.api.get.return_value.execute.side_effect = error
        with self.assertRaisesRegex(QuizError, "授權不足") as safe:
            self.store.resolve("synthetic-id", "筆記")
        self.assertNotIn(str(error), str(safe.exception))

    def test_google_batch_explicitly_writes_strings(self):
        self.store.write(("synthetic-id", 37), {Cell(6, 4): "=1+1"})
        self.api.batchUpdate.assert_called_once_with(
            spreadsheetId="synthetic-id",
            body={"requests": [{"updateCells": {
                "start": {"sheetId": 37, "rowIndex": 6, "columnIndex": 4},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": "=1+1"}}]}],
                "fields": "userEnteredValue",
            }}]},
        )

    def test_google_reads_renamed_tab_by_id_and_preserves_formula(self):
        meta = {"sheets": [{
            "properties": {"sheetId": 37, "title": "老師的'筆記", "gridProperties": {"rowCount": 20, "columnCount": 8}},
            "protectedRanges": [{"range": {"startRowIndex": 8, "endRowIndex": 9}}],
        }]}
        grid = {"sheets": [{"properties": {"sheetId": 37}, "data": [{
            "startRow": 2,
            "startColumn": 1,
            "rowData": [{"values": [{"userEnteredValue": {"formulaValue": "=1+1"}}]}],
        }]}]}
        self.api.get.return_value.execute.side_effect = [meta, grid]
        sheet = self.store.read(("synthetic-id", 37))
        self.assertEqual(sheet.values[Cell(2, 1)], {"formulaValue": "=1+1"})
        self.assertFalse(sheet.writable(Cell(8, 0)))
        self.assertEqual(self.api.get.call_args.kwargs["ranges"], ["'老師的''筆記'"])
