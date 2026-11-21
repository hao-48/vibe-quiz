import argparse
import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from quiz import Quiz
from sheets import GoogleSheets, MemorySheets, QuizError

STATIC = Path(__file__).with_name("static")


class Server(ThreadingHTTPServer):
    def __init__(self, port, sheets):
        super().__init__(("127.0.0.1", port), Handler)
        self.quiz = Quiz(sheets)
        self.teacher_entry = secrets.token_urlsafe(32)
        self.sessions = {}
        self.student_numbers = {"A": 0, "B": 0}
        self.running = True

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    def server_close(self):
        with self.quiz.changed:
            self.running = False
            self.quiz.changed.notify_all()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, body, status=200, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def verify_host(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            raise QuizError("請使用本機服務提供的網址。", 403)

    def do_GET(self):
        try:
            self.verify_host()
            route = urlsplit(self.path)
            if route.path == "/events":
                token = parse_qs(route.query).get("session", [""])[0]
                self.events(self.role(token))
                return
            files = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
                     "/app.js": ("app.js", "text/javascript")}
            if route.path not in files:
                raise QuizError("找不到此頁面。", 404)
            name, kind = files[route.path]
            self.reply((STATIC / name).read_bytes(), content_type=kind + "; charset=utf-8")
        except QuizError as error:
            self.reply({"error": str(error)}, error.status)
        except (ConnectionError, TimeoutError):
            pass

    def role(self, token):
        role = self.server.sessions.get(token)
        if role is None:
            raise QuizError("工作階段已失效，請重新進入。", 401)
        return role

    def do_POST(self):
        try:
            self.verify_host()
            if self.headers.get("Origin") not in (None, self.server.origin):
                raise QuizError("請從本機活動頁面進行操作。", 403)
            if self.headers.get_content_type() != "application/json":
                raise QuizError("請送出 JSON 資料。", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise QuizError("請求大小無效。", 413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise QuizError("請求格式無效。")
            result = self.dispatch(urlsplit(self.path).path, body)
            self.reply(result)
        except QuizError as error:
            self.reply({"error": str(error)}, error.status)
        except ValueError:
            self.reply({"error": "請求格式無效。"}, 400)
        except (ConnectionError, TimeoutError):
            pass

    def dispatch(self, path, body):
        quiz = self.server.quiz
        with quiz.changed:
            if path == "/api/session":
                role = body.get("role")
                if role == "teacher":
                    entry = body.get("entry")
                    if (not isinstance(entry, str) or not entry.isascii()
                            or not secrets.compare_digest(entry, self.server.teacher_entry)):
                        raise QuizError("請使用啟動時開啟的老師頁面。", 403)
                elif role not in ("A", "B"):
                    raise QuizError("請選擇 A 組或 B 組。")
                token = secrets.token_urlsafe(32)
                self.server.sessions[token] = role
                result = {"session": token, "role": role}
                if role == "teacher":
                    result["mode"] = quiz.sheets.mode
                else:
                    self.server.student_numbers[role] += 1
                    result["number"] = self.server.student_numbers[role]
                return result
            role = self.role(self.headers.get("Authorization", "").removeprefix("Bearer "))
            if path == "/api/state":
                return quiz.snapshot(role)
            try:
                if path == "/api/submit":
                    if role == "teacher":
                        raise QuizError("請使用學生頁提交答案。", 403)
                    return quiz.submit(role, body)
                if path in {"/api/" + action for action in ("import", "start", "end", "next", "write")}:
                    if role != "teacher":
                        raise QuizError("這項操作僅供老師使用。", 403)
                    return quiz.control(path.removeprefix("/api/"), body)
                raise QuizError("找不到此操作。", 404)
            finally:
                quiz.changed.notify_all()

    def events(self, role):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        quiz = self.server.quiz
        previous = None
        while self.server.running:
            with quiz.changed:
                quiz.changed.wait_for(
                    lambda: not self.server.running or quiz.snapshot(role) != previous, timeout=10
                )
                if not self.server.running:
                    return
                current = quiz.snapshot(role)
            if current != previous:
                payload = "data: " + json.dumps(current, ensure_ascii=False) + "\n\n"
                previous = current
            else:
                payload = ": 保持連線\n\n"
            self.wfile.write(payload.encode())
            self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description="課堂搶答系統")
    parser.add_argument("--mode", choices=("offline", "google"), default="offline", help="選擇離線或 Google Sheets 模式")
    parser.add_argument("--port", type=int, default=8000, help="本機連接埠")
    options = parser.parse_args()
    try:
        sheets = MemorySheets.from_fixtures() if options.mode == "offline" else GoogleSheets()
        with Server(options.port, sheets) as server:
            teacher_url = f"{server.origin}/#teacher={server.teacher_entry}"
            if not webbrowser.open(teacher_url):
                print("無法開啟老師頁面。請從有預設瀏覽器的桌面環境啟動。")
                return 1
            print(f"課堂搶答服務已啟動，學生入口為 {server.origin}/", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except QuizError as error:
        print(error)
        return 1
    except OSError:
        print("無法啟動本機服務，請確認連接埠可用。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
