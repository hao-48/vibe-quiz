from dataclasses import dataclass, field
from threading import Condition

from sheets import QuizError, locate, write_answers


@dataclass
class Receipt:
    submission: str
    answer: str


@dataclass
class Round:
    key: str
    phase: str = "等待中"
    receipts: dict = field(default_factory=dict)

    def result(self):
        winner = next(iter(self.receipts), None)
        return {"group": winner, "answer": self.receipts[winner].answer} if winner else None


class Quiz:
    def __init__(self, sheets):
        self.sheets = sheets
        self.changed = Condition()
        self.source = None
        self.preview = {}
        self.rounds = []
        self.index = 0
        self.generation = 0
        self.write_status = ""

    @property
    def current(self):
        return self.rounds[self.index] if self.rounds else None

    @property
    def round_id(self):
        return f"{self.generation}:{self.index}" if self.current else None

    def snapshot(self, role):
        current = self.current
        state = {
            "question": current.key if current else None,
            "phase": current.phase if current else "等待中",
            "round": self.round_id,
        }
        if current and current.phase == "已結束":
            state["result"] = current.result()
        if role == "teacher":
            state.update(
                questions=[{"key": key, "cell": cell.label()} for key, cell in self.preview.items()],
                can_next=bool(current and current.phase == "已結束" and self.index + 1 < len(self.rounds)),
                can_write=any(r.phase == "已結束" and r.receipts for r in self.rounds),
                write_status=self.write_status,
            )
            if current and current.phase == "已結束":
                state["answers"] = [{"group": group, "answer": receipt.answer}
                                    for group, receipt in current.receipts.items()]
        elif current and role in current.receipts:
            state["receipt"] = current.receipts[role].answer
        return state

    def control(self, action, body):
        if action == "import":
            if self.current and self.current.phase == "搶答中":
                raise QuizError("請先結束目前的搶答。", 409)
            try:
                source = self.sheets.resolve(body.get("book"), body.get("sheet"))
                preview = locate(self.sheets.read(source))
            except QuizError:
                # An invalid import must never leave a startable preview for that source.
                self.source = None
                self.preview = {}
                self.generation += 1
                self.rounds = []
                self.index = 0
                self.write_status = ""
                raise
            self.source, self.preview = source, preview
            self.rounds = [Round(key) for key in preview]
            self.index = 0
            self.generation += 1
            self.write_status = ""
            return {"count": len(preview)}
        if not self.current or body.get("round") != self.round_id:
            raise QuizError("題目已更新，請確認目前題號。", 409)
        if action == "start" and self.current.phase == "等待中":
            self.current.phase = "搶答中"
        elif action == "end" and self.current.phase == "搶答中":
            self.current.phase = "已結束"
        elif action == "next" and self.current.phase == "已結束" and self.index + 1 < len(self.rounds):
            self.index += 1
        elif action == "write":
            winners = {r.key: r.result()["answer"] for r in self.rounds
                       if r.phase == "已結束" and r.receipts}
            if not winners:
                raise QuizError("目前沒有可寫回的勝出答案。", 409)
            try:
                self.write_status = write_answers(self.sheets, self.source, self.preview, winners)
            except QuizError as error:
                self.write_status = str(error)
                raise
            return {"message": self.write_status}
        else:
            raise QuizError("目前階段無法執行這項操作。", 409)
        return {}

    def submit(self, role, body):
        current = self.current
        if not current or body.get("round") != self.round_id:
            raise QuizError("題目已更新，請確認目前題號。", 409)
        answer, submission = body.get("answer"), body.get("submission")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 10000:
            raise QuizError("請輸入有效答案（最多 10,000 字）。")
        if not isinstance(submission, str) or not 1 <= len(submission) <= 100:
            raise QuizError("提交識別值無效。")
        answer = answer.strip()
        previous = current.receipts.get(role)
        if previous:
            if previous == Receipt(submission, answer):
                return {"receipt": previous.answer}
            raise QuizError("本組已完成提交。", 409)
        if current.phase != "搶答中":
            raise QuizError("目前未開放搶答。", 409)
        current.receipts[role] = Receipt(submission, answer)
        return {"receipt": answer}
