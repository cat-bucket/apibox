#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import posixpath
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
GENERATED_DIR = DATA_DIR / "generated"
CONVERSATIONS_PATH = DATA_DIR / "conversations.json"
MAX_BODY_SIZE = 16 * 1024 * 1024
GENERATED_IMAGE_MAX_BYTES = 25 * 1024 * 1024
CONVERSATIONS_LOCK = threading.RLock()
CONVERSATIONS_CACHE = {"mtime": None, "data": None}

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------

def load_env_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

load_env_file()

def read_json(path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return fallback

def write_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    tmp_path.replace(path)

def clean_text(value, max_length):
    return str(value or "").strip()[:max_length]

def strip_trailing_slash(value):
    return str(value or "").strip().rstrip("/")

def infer_api_path(api_host):
    parsed = urllib.parse.urlparse(strip_trailing_slash(api_host))
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return ""
    if path.endswith("/v1"):
        return "/chat/completions"
    if path in {"", "/"}:
        return "/v1/chat/completions"
    return "/chat/completions"

def chat_completions_url(base_url):
    host = strip_trailing_slash(base_url)
    if host.endswith("/chat/completions"):
        return host
    return f"{host}{infer_api_path(host)}"

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def looks_like_base64(value):
    if len(value) < 80 or len(value) % 4 not in (0, 2, 3):
        return False
    sample = value[:200].replace("\n", "").replace("\r", "")
    return all(char.isalnum() or char in "+/=_-" for char in sample)

def detect_image_extension(raw, content_type="", source_url=""):
    mime_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if mime_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[mime_type]
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return ".webp"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    path = urllib.parse.urlparse(source_url).path
    extension = Path(path).suffix.lower()
    if extension in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return ".jpg" if extension == ".jpeg" else extension
    return ""

def save_generated_image(raw, extension):
    if not raw:
        return ""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time())}-{secrets.token_hex(8)}{extension or '.png'}"
    (GENERATED_DIR / filename).write_bytes(raw)
    return f"/generated/{filename}"

def store_data_image(image):
    header, encoded = image.split(",", 1)
    mime_type = header.split(";", 1)[0].removeprefix("data:")
    extension = IMAGE_EXTENSIONS.get(mime_type, ".png")
    raw = base64.b64decode(encoded, validate=False)
    if len(raw) > GENERATED_IMAGE_MAX_BYTES:
        raise ValueError("uploaded image is too large")
    return save_generated_image(raw, extension)

def store_remote_image(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8",
            "User-Agent": "MinAI/1.0 image fetcher",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read(GENERATED_IMAGE_MAX_BYTES + 1)
        if len(raw) > GENERATED_IMAGE_MAX_BYTES:
            raise ValueError("remote image is too large")
        extension = detect_image_extension(raw, response.headers.get("Content-Type", ""), url)
        if not extension:
            raise ValueError("remote result is not an image")
        return save_generated_image(raw, extension)

def persist_image_url(url):
    """将 data: 或远程图片持久化到 /generated/ 目录，返回存储后的相对路径"""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("/generated/"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        try:
            return store_remote_image(url)
        except Exception:
            return url  # 保留原始链接
    if url.startswith("data:image/"):
        try:
            return store_data_image(url)
        except Exception:
            return ""
    return ""

# ----------------------------------------------------------------------
# 消息标准化与对话存储
# ----------------------------------------------------------------------

def normalize_messages(messages):
    """处理前端发来的消息，标准化 role 和 content，并处理文件附件"""
    if not isinstance(messages, list):
        return []
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = clean_text(message.get("content"), 12000)
        files = message.get("files") if isinstance(message.get("files"), list) else []
        file_blocks = []
        image_parts = []
        for file in files[:6]:
            if not isinstance(file, dict):
                continue
            name = clean_text(file.get("name"), 120)
            file_type = clean_text(file.get("type"), 80)
            text = clean_text(file.get("text"), 60000)
            data_url = str(file.get("dataUrl") or "")
            if file_type.startswith("image/") and data_url.startswith("data:image/"):
                image_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                if name:
                    file_blocks.append(f"\n\n[图片附件: {name}]")
            elif text:
                file_blocks.append(f"\n\n[附件: {name or '未命名文件'} | {file_type or 'text/plain'}]\n{text}")
            elif name:
                file_blocks.append(f"\n\n[附件: {name}]\n该附件不是可直接读取的文本文件。")
        if file_blocks:
            content = f"{content}{''.join(file_blocks)}"
        if not content and not image_parts:
            continue
        role = "assistant" if message.get("role") == "assistant" else "user"
        if image_parts and role == "user":
            parts = []
            if content:
                parts.append({"type": "text", "text": content})
            parts.extend(image_parts[:4])
            normalized.append({"role": role, "content": parts})
        else:
            normalized.append({"role": role, "content": content})
    return normalized[-16:]

def conversation_messages(messages):
    """存储对话时标准化消息，将图片 URL 持久化"""
    if not isinstance(messages, list):
        return []
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = "assistant" if message.get("role") == "assistant" else "user"
        content = clean_text(message.get("content"), 12000)
        images = message.get("images") if isinstance(message.get("images"), list) else []
        files = message.get("files") if isinstance(message.get("files"), list) else []
        safe_images = []
        for url in images:
            safe_url = persist_image_url(url)
            if safe_url:
                safe_images.append(safe_url)
            if len(safe_images) >= 4:
                break
        safe_files = []
        for file in files[:6]:
            if not isinstance(file, dict):
                continue
            data_url = str(file.get("dataUrl") or "")
            file_url = persist_image_url(file.get("url"))
            if str(file.get("type") or "").startswith("image/") and data_url.startswith("data:image/"):
                file_url = persist_image_url(data_url) or file_url
            safe_files.append({
                "name": clean_text(file.get("name"), 120),
                "type": clean_text(file.get("type"), 80),
                "text": clean_text(file.get("text"), 60000),
                "url": file_url,
                "dataUrl": "",  # 不保存 base64 到文件，避免文件过大
            })
        if content or safe_images or safe_files:
            normalized.append({
                "role": role,
                "content": content,
                "images": safe_images,
                "files": safe_files,
            })
    return normalized[-40:]

def read_conversations():
    with CONVERSATIONS_LOCK:
        try:
            mtime = CONVERSATIONS_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            CONVERSATIONS_CACHE["mtime"] = None
            CONVERSATIONS_CACHE["data"] = {}
            return {}
        if CONVERSATIONS_CACHE.get("mtime") == mtime and isinstance(CONVERSATIONS_CACHE.get("data"), dict):
            return CONVERSATIONS_CACHE["data"]
        data = read_json(CONVERSATIONS_PATH, {})
        if not isinstance(data, dict):
            data = {}
        CONVERSATIONS_CACHE["mtime"] = mtime
        CONVERSATIONS_CACHE["data"] = data
        return data

def write_conversations(data):
    with CONVERSATIONS_LOCK:
        payload = data if isinstance(data, dict) else {}
        write_json(CONVERSATIONS_PATH, payload)
        CONVERSATIONS_CACHE["data"] = payload
        try:
            CONVERSATIONS_CACHE["mtime"] = CONVERSATIONS_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            CONVERSATIONS_CACHE["mtime"] = None

def normalize_conversation_store(data):
    """将原始存储数据规范化为统一的对话 store 结构"""
    if not isinstance(data, dict):
        data = {}
    items = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        messages = conversation_messages(item.get("messages"))
        if not messages:
            continue
        conversation_id = clean_text(item.get("id"), 64) or secrets.token_hex(8)
        updated_at = clean_text(item.get("updatedAt"), 40) or now_iso()
        created_at = clean_text(item.get("createdAt"), 40) or updated_at
        items.append({
            "id": conversation_id,
            "title": clean_text(item.get("title"), 60) or conversation_title(messages),
            "messages": messages,
            "pinned": bool(item.get("pinned")),
            "createdAt": created_at,
            "updatedAt": updated_at,
        })
    items.sort(key=lambda it: it.get("updatedAt") or "", reverse=True)
    items.sort(key=lambda it: not it.get("pinned"))
    active_id = clean_text(data.get("activeId"), 64)
    if active_id and not any(it["id"] == active_id for it in items):
        active_id = ""
    if not active_id and items:
        active_id = items[0]["id"]
    return {"activeId": active_id, "items": items[:50]}

def conversation_title(messages):
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            text = msg.get("content")
            if isinstance(text, list):  # multimodal 消息
                for part in text:
                    if part.get("type") == "text":
                        return clean_text(part.get("text", ""), 28) or "新对话"
            else:
                return clean_text(text, 28) or "新对话"
    return "新对话"

def get_conversation_store():
    return normalize_conversation_store(read_conversations())

def save_conversation_store(store):
    write_conversations(store)

def conversation_summary(item):
    return {
        "id": item["id"],
        "title": item["title"],
        "pinned": bool(item.get("pinned")),
        "createdAt": item["createdAt"],
        "updatedAt": item["updatedAt"],
    }

def conversation_payload(store):
    active = next((it for it in store["items"] if it["id"] == store["activeId"]), None)
    return {
        "activeId": store["activeId"],
        "conversationId": store["activeId"],
        "messages": active["messages"] if active else [],
        "conversations": [conversation_summary(it) for it in store["items"]],
    }

# 对话操作函数（无用户）
def upsert_conversation(conversation_id, messages):
    store = get_conversation_store()
    now = now_iso()
    conversation_id = clean_text(conversation_id, 64) or store.get("activeId") or secrets.token_hex(8)
    item = next((it for it in store["items"] if it["id"] == conversation_id), None)
    if not item:
        item = {"id": conversation_id, "title": "新对话", "messages": [], "pinned": False, "createdAt": now, "updatedAt": now}
        store["items"].insert(0, item)
    item["messages"] = conversation_messages(messages)
    item["title"] = conversation_title(item["messages"])
    item["updatedAt"] = now
    store["activeId"] = item["id"]
    save_conversation_store(store)
    return conversation_payload(store)

def create_conversation():
    store = get_conversation_store()
    now = now_iso()
    item = {"id": secrets.token_hex(8), "title": "新对话", "messages": [], "pinned": False, "createdAt": now, "updatedAt": now}
    store["items"].insert(0, item)
    store["activeId"] = item["id"]
    save_conversation_store(store)
    return conversation_payload(store)

def select_conversation(conversation_id):
    store = get_conversation_store()
    conversation_id = clean_text(conversation_id, 64)
    if not any(it["id"] == conversation_id for it in store["items"]):
        return None
    store["activeId"] = conversation_id
    save_conversation_store(store)
    return conversation_payload(store)

def pin_conversation(conversation_id, pinned):
    store = get_conversation_store()
    conversation_id = clean_text(conversation_id, 64)
    item = next((it for it in store["items"] if it["id"] == conversation_id), None)
    if not item:
        return None
    item["pinned"] = bool(pinned)
    save_conversation_store(store)
    return conversation_payload(store)

def delete_conversation(conversation_id):
    store = get_conversation_store()
    conversation_id = clean_text(conversation_id, 64)
    before = len(store["items"])
    store["items"] = [it for it in store["items"] if it["id"] != conversation_id]
    if len(store["items"]) == before:
        return None
    if store["activeId"] == conversation_id:
        store["activeId"] = store["items"][0]["id"] if store["items"] else ""
    save_conversation_store(store)
    return conversation_payload(store)

# ----------------------------------------------------------------------
# API 配置
# ----------------------------------------------------------------------

def get_api_config():
    return {
        "apiBaseUrl": os.getenv("API_BASE_URL", "https://api.openai.com/v1"),
        "apiKey": os.getenv("API_KEY", ""),
        "model": os.getenv("AI_MODEL", "gpt-4o-mini"),
        "systemPrompt": os.getenv("SYSTEM_PROMPT", "你是一个专业、简洁、友好的 AI 助手。请优先用中文回答。"),
    }

# ----------------------------------------------------------------------
# HTTP 请求处理器
# ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MinAI/1.0"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.route_api("GET", parsed.path)
            return
        if parsed.path.startswith("/generated/"):
            self.serve_generated(parsed.path, head_only=False)
            return
        self.serve_static(parsed.path)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/generated/"):
            self.serve_generated(parsed.path, head_only=True)
            return
        self.serve_static(parsed.path, head_only=True)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        self.route_api("POST", parsed.path)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        self.route_api("PUT", parsed.path)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        self.route_api("DELETE", parsed.path)

    def route_api(self, method, path):
        if method == "GET" and path == "/api/public-config":
            config = get_api_config()
            self.send_json(200, {
                "siteTitle": "MinAI Lite",
                "requireLogin": False,
                "user": None,
            })
            return

        if method == "POST" and path == "/api/chat":
            self.handle_chat()
            return

        if method == "GET" and path == "/api/models":
            self.handle_models()
            return

        # 对话接口
        if path == "/api/conversations/current":
            if method == "GET":
                self.handle_get_conversation()
                return
            if method == "POST":
                self.handle_save_conversation()
                return

        if method == "GET" and path == "/api/conversations":
            self.handle_get_conversations()
            return

        if method == "POST" and path == "/api/conversations/new":
            self.handle_new_conversation()
            return

        if method == "POST" and path == "/api/conversations/select":
            self.handle_select_conversation()
            return

        if method == "POST" and path == "/api/conversations/pin":
            self.handle_pin_conversation()
            return

        if method == "DELETE" and path == "/api/conversations":
            self.handle_delete_conversation()
            return

        self.send_json(404, {"error": "Not found"})

    # ------------------------------------------------------------------
    # API 处理方法
    # ------------------------------------------------------------------

    def handle_chat(self):
        config = get_api_config()
        if not config["apiKey"]:
            self.send_json(500, {"error": "请在 .env 中设置 API_KEY。"})
            return

        body = self.read_json_body()
        raw_messages = body.get("messages")
        messages = normalize_messages(raw_messages)
        stored_messages = conversation_messages(raw_messages)
        if not messages:
            self.send_json(400, {"error": "请输入消息内容。"})
            return

        model = body.get("model") or config["model"]

        payload = {
            "model": model,
            "temperature": body.get("temperature") if isinstance(body.get("temperature"), (int, float)) else 0.7,
            "messages": [{"role": "system", "content": config["systemPrompt"]}] + messages,
        }

        request = urllib.request.Request(
            chat_completions_url(config["apiBaseUrl"]),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f'Bearer {config["apiKey"]}',
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                upstream = json.loads(response.read().decode("utf-8"))
                choice = (upstream.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                reply = message.get("content")
                if not reply:
                    reply = "没有收到有效回复。"

                # 保存对话
                assistant_msg = {"role": "assistant", "content": reply}
                conversation_id = body.get("conversationId") or ""
                result = upsert_conversation(conversation_id, stored_messages + [assistant_msg])

                self.send_json(200, {
                    "reply": reply,
                    "model": upstream.get("model") or model,
                    "usage": upstream.get("usage"),
                    "conversationId": result["conversationId"],
                    "conversations": result["conversations"],
                })
        except urllib.error.HTTPError as error:
            error_text = error.read().decode("utf-8", "replace")
            try:
                error_json = json.loads(error_text)
            except Exception:
                error_json = {}
            message = (
                ((error_json.get("error") or {}).get("message") if isinstance(error_json.get("error"), dict) else None)
                or error_json.get("message")
                or f"上游接口请求失败，状态码 {error.code}"
            )
            self.send_json(error.code, {"error": message})
        except Exception as e:
            self.send_json(502, {"error": f"无法连接上游 AI 接口: {str(e)}"})

    def handle_models(self):
        config = get_api_config()
        model = config["model"]
        self.send_json(200, {
            "defaultModel": model,
            "models": [{"id": model, "name": model}]
        })

    def handle_get_conversation(self):
        store = get_conversation_store()
        self.send_json(200, conversation_payload(store))

    def handle_save_conversation(self):
        body = self.read_json_body()
        messages = conversation_messages(body.get("messages"))
        conversation_id = body.get("conversationId") or ""
        result = upsert_conversation(conversation_id, messages)
        self.send_json(200, {"ok": True, **result})

    def handle_get_conversations(self):
        self.send_json(200, conversation_payload(get_conversation_store()))

    def handle_new_conversation(self):
        result = create_conversation()
        self.send_json(200, result)

    def handle_select_conversation(self):
        body = self.read_json_body()
        conversation_id = body.get("conversationId")
        if not conversation_id:
            self.send_json(400, {"error": "缺少 conversationId"})
            return
        result = select_conversation(conversation_id)
        if result is None:
            self.send_json(404, {"error": "对话不存在。"})
            return
        self.send_json(200, result)

    def handle_pin_conversation(self):
        body = self.read_json_body()
        conversation_id = body.get("conversationId")
        pinned = body.get("pinned", False)
        result = pin_conversation(conversation_id, pinned)
        if result is None:
            self.send_json(404, {"error": "对话不存在。"})
            return
        self.send_json(200, result)

    def handle_delete_conversation(self):
        body = self.read_json_body()
        conversation_id = body.get("conversationId")
        result = delete_conversation(conversation_id)
        if result is None:
            self.send_json(404, {"error": "对话不存在。"})
            return
        self.send_json(200, result)

    # ------------------------------------------------------------------
    # 静态文件服务
    # ------------------------------------------------------------------

    def serve_static(self, path, head_only=False):
        if path == "/":
            path = "/index.html"
        safe_path = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if safe_path.startswith(".") or "/." in f"/{safe_path}":
            self.send_json(404, {"error": "Not found"})
            return
        file_path = (PUBLIC_DIR / safe_path).resolve()
        try:
            file_path.relative_to(PUBLIC_DIR.resolve())
        except ValueError:
            self.send_json(403, {"error": "Forbidden"})
            return
        if not file_path.exists() or not file_path.is_file():
            requested_suffix = Path(safe_path).suffix
            if requested_suffix or safe_path.startswith("assets/"):
                self.send_json(404, {"error": "Not found"})
                return
            file_path = PUBLIC_DIR / "index.html"
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript"
        if file_path.suffix in {".html", ".css", ".js", ".json"}:
            content_type += "; charset=utf-8"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        cache_control = "no-cache" if file_path.suffix in {".html", ".css", ".js"} else "public, max-age=86400"
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_generated(self, path, head_only=False):
        safe_name = posixpath.basename(urllib.parse.unquote(path))
        file_path = (GENERATED_DIR / safe_name).resolve()
        try:
            file_path.relative_to(GENERATED_DIR.resolve())
        except ValueError:
            self.send_json(403, {"error": "Forbidden"})
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_json(404, {"error": "Not found"})
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_SIZE:
            self.send_json(413, {"error": "请求内容过大。"})
            return {}
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    if not CONVERSATIONS_PATH.exists():
        write_json(CONVERSATIONS_PATH, {"activeId": "", "items": []})
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"MinAI Lite running at http://{host}:{port}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
