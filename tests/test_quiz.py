import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from quiz import Quiz
from sheets import MemorySheets, QuizError


class QuizTests(unittest.TestCase):
    def setUp(self):
        self.quiz = Quiz(MemorySheets.from_fixtures())
        self.quiz.control("import", {"book": "starter", "sheet": "課堂筆記"})
        self.first = self.quiz.round_id

    def control(self, action):
        with self.quiz.changed:
            return self.quiz.control(action, {"round": self.quiz.round_id})

    def answer(self, group, text, submission="first", round_id=None):
        with self.quiz.changed:
            return self.quiz.submit(group, {"round": round_id or self.quiz.round_id,
                                            "submission": submission, "answer": text})

    def test_phase_and_retries(self):
        with self.assertRaisesRegex(QuizError, "未開放"):
            self.answer("A", "答案")
        self.control("start")
        with self.assertRaisesRegex(QuizError, "有效答案"):
            self.answer("A", "  ")
        first = self.answer("A", " 答案 ")
        self.assertEqual(self.answer("A", "答案"), first)
        with self.assertRaisesRegex(QuizError, "已完成"):
            self.answer("A", "替換")
        self.control("end")
        self.assertEqual(self.answer("A", "答案"), first)
        with self.assertRaisesRegex(QuizError, "未開放"):
            self.answer("B", "太晚")
        self.control("next")
        with self.assertRaisesRegex(QuizError, "題目已更新"):
            self.answer("A", "舊答案", round_id=self.first)
        self.assertEqual(self.quiz.snapshot("B")["phase"], "等待中")

    def test_concurrent_receipts_select_first_committed(self):
        self.control("start")
        ready = Barrier(2)

        def arrive(group):
            ready.wait(timeout=3)
            return self.answer(group, group)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(arrive, ["A", "B"])), [{"receipt": "A"}, {"receipt": "B"}])
        self.control("end")
        teacher = self.quiz.snapshot("teacher")
        self.assertEqual(teacher["result"], teacher["answers"][0])
        self.assertEqual(len(teacher["answers"]), 2)

    def test_invalid_import_cannot_be_started(self):
        with self.assertRaises(QuizError):
            self.quiz.control("import", {"book": "unknown", "sheet": "課堂筆記"})
        self.assertIsNone(self.quiz.snapshot("teacher")["question"])
        with self.assertRaises(QuizError):
            self.control("start")
