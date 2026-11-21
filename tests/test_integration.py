import json
import unittest
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

from app import Server
from sheets import Cell, MemorySheets, QuizError, locate


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sheets = MemorySheets.from_fixtures()
        self.server = Server(0, self.sheets)
        self.worker = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.teacher = self.join("teacher", self.server.teacher_entry)
        self.a = self.join("A")
        self.b = self.join("B")

    def stop_server(self):
        self.server.shutdown()
        self.worker.join()
        self.server.server_close()

    def connect(self):
        return HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def post(self, action, data, token=None, headers=None):
        connection = self.connect()
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        try:
            connection.request("POST", "/api/" + action, json.dumps(data), request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def join(self, role, entry=None):
        status, result = self.post("session", {"role": role, "entry": entry})
        self.assertEqual(status, 200)
        return result["session"]

    def event_stream(self, token):
        connection = self.connect()
        connection.request("GET", "/events?" + urlencode({"session": token}))
        stream = connection.getresponse()
        self.assertEqual(stream.status, 200)
        self.addCleanup(connection.close)
        self.addCleanup(stream.close)
        return stream

    def state_event(self, stream):
        line = stream.readline().decode()
        self.assertTrue(line.startswith("data: "), line)
        self.assertEqual(stream.readline(), b"\n")
        return json.loads(line.removeprefix("data: "))

    def control(self, action, round_id):
        status, result = self.post(action, {"round": round_id}, self.teacher)
        self.assertEqual(status, 200, result)
        return result

    def test_two_templates_full_flow_with_three_live_clients(self):
        streams = [self.event_stream(token) for token in (self.teacher, self.a, self.b)]
        for stream in streams:
            self.assertIsNone(self.state_event(stream)["question"])
        for book, keys in (("starter", ["1", "2"]), ("alternate", ["甲", "乙", "丙"])):
            with self.subTest(book=book):
                source = self.sheets.resolve(book, "課堂筆記")
                original = dict(self.sheets.books[source].values)
                cells = locate(self.sheets.read(source))
                status, result = self.post("import", {"book": book, "sheet": "課堂筆記"}, self.teacher)
                self.assertEqual((status, result["count"]), (200, len(keys)))
                waiting = [self.state_event(stream) for stream in streams]
                self.assertEqual([row["key"] for row in waiting[0]["questions"]], keys)
                prompt = "電腦中負責執行程式指令的核心元件是什麼？" if book == "starter" else ""
                self.assertTrue(all(state["prompt"] == prompt for state in waiting))
                self.assertEqual(waiting[0]["questions"][0]["prompt"], prompt)
                round_id = waiting[0]["round"]
                self.assertEqual(self.post("start", {"round": round_id}, self.a)[0], 403)
                submission = {"round": round_id, "submission": "one", "answer": "=示範"}
                self.assertEqual(self.post("submit", submission, self.a)[0], 409)
                self.control("start", round_id)
                active = [self.state_event(stream) for stream in streams]
                self.assertTrue(all(s["phase"] == "搶答中" for s in active))
                reconnect = self.event_stream(self.b)
                self.assertEqual(self.state_event(reconnect), active[2])
                self.assertEqual(self.post("submit", submission, self.a)[0], 200)
                self.assertEqual(self.state_event(streams[1])["receipt"], "=示範")
                self.assertEqual(self.post("state", {}, self.b)[1], active[2])
                self.assertEqual(self.post("submit", {**submission, "answer": "第二組"}, self.b)[0], 200)
                b_receipt = self.state_event(streams[2])
                self.assertEqual(b_receipt, {**active[2], "receipt": "第二組"})
                self.control("end", round_id)
                ended = [self.state_event(stream) for stream in streams]
                for state in ended:
                    self.assertEqual(state["result"], {"group": "A", "answer": "=示範"})
                self.assertEqual(len(ended[0]["answers"]), 2)
                self.assertNotIn("answers", ended[2])
                self.control("next", round_id)
                waiting = [self.state_event(stream) for stream in streams]
                self.assertEqual(waiting[2]["question"], keys[1])
                self.assertEqual(waiting[2]["prompt"], "")
                self.assertEqual(self.post("submit", submission, self.a)[0], 409)
                self.control("start", waiting[0]["round"])
                for stream in streams:
                    self.state_event(stream)
                self.control("end", waiting[0]["round"])
                for stream in streams:
                    self.assertIsNone(self.state_event(stream)["result"])
                self.control("write", waiting[0]["round"])
                self.assertIn("寫入 1 格", self.state_event(streams[0])["write_status"])
                self.assertEqual(self.sheets.books[source].values, {**original, cells[keys[0]]: "=示範"})

    def test_write_failure_preserves_winner_and_retries(self):
        self.post("import", {"book": "starter", "sheet": "課堂筆記"}, self.teacher)
        round_id = self.post("state", {}, self.teacher)[1]["round"]
        self.control("start", round_id)
        self.post("submit", {"round": round_id, "submission": "one", "answer": "答案"}, self.b)
        self.control("end", round_id)
        with patch.object(self.sheets, "write", side_effect=QuizError("模擬斷線", 503)):
            status, result = self.post("write", {"round": round_id}, self.teacher)
        self.assertEqual(status, 503)
        self.assertIn("待確認", result["error"])
        self.assertEqual(self.post("state", {}, self.b)[1]["result"]["answer"], "答案")
        self.assertIn("寫入 1 格", self.control("write", round_id)["message"])

    def test_pages_use_traditional_chinese_and_no_session_material(self):
        self.assertEqual(self.post("session", {"role": "teacher"})[0], 403)
        self.assertEqual(self.post("state", {}, self.a, {"Origin": "http://example.invalid"})[0], 403)
        for path in ("/", "/app.js", "/style.css"):
            with self.subTest(path=path):
                connection = self.connect()
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    content = response.read().decode()
                    self.assertNotIn(self.server.teacher_entry, content)
                    self.assertNotIn(self.teacher, content)
                    if path == "/":
                        self.assertIn('lang="zh-Hant"', content)
                        self.assertIn("加入 A 組", content)
                finally:
                    connection.close()
