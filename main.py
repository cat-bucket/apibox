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
from functools import lru_cache

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
GENERATED_DIR = DATA_DIR / "generated"
CONVERSATIONS_PATH = DATA_DIR / "conversations.json"
PROVIDERS_PATH = DATA_DIR / "providers.json"
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

# -------------------- 静态文件缓存 --------------------
@lru_cache(maxsize=32)
def cached_static_file(filepath):
    """缓存静态文件内容，减少磁盘读取"""
    try:
        return filepath.read_bytes()
    except:
        return None

# -------------------- 工具函数 --------------------
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

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# -------------------- 图片处理（保留） --------------------
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
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if url.startswith("/generated/"):
        return url
    if url.startswith("http://") or url.startswith("https://"):
        try:
            return store_remote_image(url)
        except Exception:
            return url
    if url.startswith("data:image/"):
        try:
            return store_data_image(url)
        except Exception:
            return ""
    return ""

# -------------------- 消息标准化 --------------------
def normalize_messages(messages):
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

# -------------------- 对话存储 --------------------
def conversation_messages(messages):
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
                "dataUrl": "",
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
            if isinstance(text, list):
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

# -------------------- API 通道管理 --------------------
def list_providers():
    providers = read_json(PROVIDERS_PATH, [])
    if not isinstance(providers, list):
        providers = []
    for p in providers:
        if "apiHost" not in p and "apiBaseUrl" in p:
            p["apiHost"] = p.pop("apiBaseUrl")
        if "apiHost" not in p:
            p["apiHost"] = "https://api.openai.com"
    return providers

def save_providers(providers):
    write_json(PROVIDERS_PATH, providers)

def public_provider(p):
    return {
        "id": p.get("id"),
        "name": p.get("name", "未命名"),
        "apiHost": p.get("apiHost"),
        "apiPath": p.get("apiPath", ""),
        "aiModel": p.get("aiModel", "gpt-4o-mini"),
        "enabled": bool(p.get("enabled", True)),
        "isDefault": bool(p.get("isDefault", False)),
        "apiKeySet": bool(p.get("apiKey")),
    }

def get_active_provider():
    providers = list_providers()
    enabled = [p for p in providers if p.get("enabled") and p.get("apiKey")]
    if not enabled:
        return None
    default = next((p for p in enabled if p.get("isDefault")), None)
    return default or enabled[0]

def get_provider_by_id(provider_id):
    for p in list_providers():
        if p["id"] == provider_id:
            return p
    return None

def create_provider(data):
    providers = list_providers()
    now = now_iso()
    provider = {
        "id": secrets.token_hex(12),
        "name": clean_text(data.get("name", "新通道"), 60),
        "apiHost": strip_trailing_slash(data.get("apiHost", "")),
        "apiPath": clean_text(data.get("apiPath", ""), 120),
        "apiKey": data.get("apiKey", ""),
        "aiModel": clean_text(data.get("aiModel", "gpt-4o-mini"), 80),
        "enabled": data.get("enabled", True),
        "isDefault": data.get("isDefault", False),
        "createdAt": now,
        "updatedAt": now,
    }
    if not providers:
        provider["isDefault"] = True
    if provider["isDefault"]:
        for p in providers:
            p["isDefault"] = False
    providers.append(provider)
    save_providers(providers)
    return provider

def update_provider(provider_id, data):
    providers = list_providers()
    for p in providers:
        if p.get("id") == provider_id:
            p["name"] = clean_text(data.get("name", p["name"]), 60)
            p["apiHost"] = strip_trailing_slash(data.get("apiHost", p["apiHost"]))
            p["apiPath"] = clean_text(data.get("apiPath", p.get("apiPath", "")), 120)
            if data.get("apiKey") is not None and data["apiKey"] != "":
                p["apiKey"] = data["apiKey"]
            p["aiModel"] = clean_text(data.get("aiModel", p["aiModel"]), 80)
            p["enabled"] = data.get("enabled", p["enabled"])
            is_default = data.get("isDefault", p.get("isDefault"))
            if is_default and not p.get("isDefault"):
                for op in providers:
                    op["isDefault"] = False
            p["isDefault"] = is_default
            p["updatedAt"] = now_iso()
            save_providers(providers)
            return p
    return None

def delete_provider(provider_id):
    providers = list_providers()
    new_list = [p for p in providers if p.get("id") != provider_id]
    if len(new_list) == len(providers):
        return False
    if any(p.get("id") == provider_id and p.get("isDefault") for p in providers):
        if new_list:
            new_list[0]["isDefault"] = True
    save_providers(new_list)
    return True

def fetch_provider_models(provider):
    api_host = provider.get("apiHost")
    api_key = provider.get("apiKey")
    if not api_host or not api_key:
        raise ValueError("请先填写 API Host 和 Key")
    url = models_url(api_host)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = data.get("data", [])
        if isinstance(models, list):
            return [{"id": m.get("id", ""), "name": m.get("id", "")} for m in models if m.get("id")]
        return []
    except Exception as e:
        raise ValueError(f"获取模型失败: {str(e)}")

def chat_completions_url(base_url):
    host = strip_trailing_slash(base_url)
    if host.endswith("/chat/completions"):
        return host
    return f"{host}/v1/chat/completions"

def models_url(base_url):
    endpoint = chat_completions_url(base_url)
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")] + "/models"
    else:
        path = path + "/models"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

# -------------------- HTTP Handler --------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MinAI/2.0"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {self.log_date_time_string()} {fmt % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.route_api("GET", parsed.path)
            return
        if parsed.path.startswith("/generated/"):
            self.serve_generated(parsed.path)
            return
        self.serve_static(parsed.path)

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
        # 聊天核心
        if method == "POST" and path == "/api/chat":
            self.handle_chat()
            return
        if method == "GET" and path == "/api/public-config":
            self.send_json(200, {"siteTitle": "MinAI", "requireLogin": False})
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

        # 通道管理
        if method == "GET" and path == "/api/providers":
            self.send_json(200, {"providers": [public_provider(p) for p in list_providers()]})
            return
        if method == "POST" and path == "/api/providers":
            body = self.read_json_body()
            try:
                provider = create_provider(body)
                self.send_json(201, {"provider": public_provider(provider)})
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return
        if path.startswith("/api/providers/"):
            parts = path.split("/")
            if len(parts) == 4:  # /api/providers/{id}
                provider_id = parts[3]
                if method == "PUT":
                    body = self.read_json_body()
                    updated = update_provider(provider_id, body)
                    if updated:
                        self.send_json(200, {"provider": public_provider(updated)})
                    else:
                        self.send_json(404, {"error": "通道不存在"})
                elif method == "DELETE":
                    if delete_provider(provider_id):
                        self.send_json(200, {"ok": True})
                    else:
                        self.send_json(404, {"error": "通道不存在或无法删除"})
                else:
                    self.send_json(405, {"error": "方法不允许"})
                return
            if len(parts) == 5 and parts[4] == "models" and method == "GET":
                provider_id = parts[3]
                provider = get_provider_by_id(provider_id)
                if not provider:
                    self.send_json(404, {"error": "通道不存在"})
                    return
                try:
                    models = fetch_provider_models(provider)
                    self.send_json(200, {"models": models})
                except Exception as e:
                    self.send_json(500, {"error": str(e)})
                return
            if len(parts) == 5 and parts[4] == "test" and method == "POST":
                self.handle_test_provider()
                return
        self.send_json(404, {"error": "Not found"})

    def handle_test_provider(self):
        """测试指定通道的连接"""
        body = self.read_json_body()
        provider_id = body.get("providerId")
        provider = None
        if provider_id:
            provider = get_provider_by_id(provider_id)
        if not provider:
            api_host = strip_trailing_slash(body.get("apiHost", ""))
            api_key = body.get("apiKey")
            if not api_host or not api_key:
                self.send_json(400, {"error": "缺少必要参数"})
                return
            provider = {"apiHost": api_host, "apiKey": api_key}
        try:
            # 简单测试：请求 models 列表
            models = fetch_provider_models(provider)
            self.send_json(200, {"success": True, "models": models[:5]})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_chat(self):
        """支持流式与非流式两种模式"""
        body = self.read_json_body()
        # 确定使用的 provider
        provider_id = body.get("providerId")
        provider = None
        if provider_id:
            provider = get_provider_by_id(provider_id)
        if not provider:
            provider = get_active_provider()
        if not provider:
            self.send_json(500, {"error": "没有可用的 API 通道，请先在设置中添加。"})
            return

        messages = normalize_messages(body.get("messages"))
        stored_messages = conversation_messages(body.get("messages"))
        if not messages:
            self.send_json(400, {"error": "请输入消息内容。"})
            return

        model = body.get("model") or provider["aiModel"]
        stream = body.get("stream", True)  # 默认开启流式
        system_prompt = os.getenv("SYSTEM_PROMPT", "你是一个专业、简洁、友好的 AI 助手。请优先用中文回答。")
        payload = {
            "model": model,
            "temperature": body.get("temperature", 0.7),
            "stream": stream,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
        }
        url = chat_completions_url(provider["apiHost"])
        if provider.get("apiPath"):
            url = strip_trailing_slash(provider["apiHost"]) + "/" + provider["apiPath"].lstrip("/")

        req = urllib.request.Request(url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "text/event-stream" if stream else "application/json",
                "Content-Type": "application/json",
                "Authorization": f'Bearer {provider["apiKey"]}',
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    # 读取流式数据并直接转发
                    full_reply = ""
                    while True:
                        chunk = response.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        # 解析出文本内容用于持久化
                        try:
                            decoded = chunk.decode("utf-8")
                            for line in decoded.split("\n"):
                                if line.startswith("data: "):
                                    data_str = line[6:].strip()
                                    if data_str == "[DONE]":
                                        continue
                                    data = json.loads(data_str)
                                    content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    full_reply += content
                        except:
                            pass
                    # 流结束后更新对话存储
                    if full_reply:
                        assistant_msg = {"role": "assistant", "content": full_reply}
                        conversation_id = body.get("conversationId") or ""
                        result = upsert_conversation(conversation_id, stored_messages + [assistant_msg])
                        # 发送一个额外的 SSE 事件携带 conversationId (可选)
                        meta = json.dumps({"conversationId": result["conversationId"], "conversations": result["conversations"]})
                        self.wfile.write(f"data: {meta}\n\n".encode())
                        self.wfile.flush()
                else:
                    # 非流式模式
                    upstream = json.loads(response.read().decode("utf-8"))
                    choice = (upstream.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    reply = message.get("content") or "没有收到有效回复。"
                    assistant_msg = {"role": "assistant", "content": reply}
                    conversation_id = body.get("conversationId") or ""
                    result = upsert_conversation(conversation_id, stored_messages + [assistant_msg])
                    self.send_json(200, {
                        "reply": reply,
                        "model": upstream.get("model") or model,
                        "conversationId": result["conversationId"],
                        "conversations": result["conversations"],
                    })
        except urllib.error.HTTPError as error:
            error_text = error.read().decode("utf-8", "replace")
            try:
                error_json = json.loads(error_text)
            except:
                error_json = {}
            msg = (error_json.get("error", {}).get("message") or error_json.get("message") or f"上游接口请求失败，状态码 {error.code}")
            self.send_json(error.code, {"error": msg})
        except Exception as e:
            self.send_json(502, {"error": f"无法连接上游 AI 接口: {str(e)}"})

    def handle_models(self):
        provider = get_active_provider()
        if not provider:
            self.send_json(200, {"defaultModel": "请先配置 API", "models": []})
            return
        self.send_json(200, {
            "defaultModel": provider["aiModel"],
            "models": [{"id": provider["aiModel"], "name": provider["aiModel"]}]
        })

    # 对话方法（保持原有逻辑）
    def handle_get_conversation(self):
        self.send_json(200, conversation_payload(get_conversation_store()))
    def handle_save_conversation(self):
        body = self.read_json_body()
        messages = conversation_messages(body.get("messages"))
        conversation_id = body.get("conversationId") or ""
        result = upsert_conversation(conversation_id, messages)
        self.send_json(200, {"ok": True, **result})
    def handle_get_conversations(self):
        self.send_json(200, conversation_payload(get_conversation_store()))
    def handle_new_conversation(self):
        self.send_json(200, create_conversation())
    def handle_select_conversation(self):
        body = self.read_json_body()
        cid = body.get("conversationId")
        if not cid:
            self.send_json(400, {"error": "缺少 conversationId"})
            return
        result = select_conversation(cid)
        if result is None:
            self.send_json(404, {"error": "对话不存在"})
            return
        self.send_json(200, result)
    def handle_pin_conversation(self):
        body = self.read_json_body()
        result = pin_conversation(body.get("conversationId"), body.get("pinned", False))
        if result is None:
            self.send_json(404, {"error": "对话不存在"})
            return
        self.send_json(200, result)
    def handle_delete_conversation(self):
        body = self.read_json_body()
        result = delete_conversation(body.get("conversationId"))
        if result is None:
            self.send_json(404, {"error": "对话不存在"})
            return
        self.send_json(200, result)

    # 静态文件服务（带内存缓存）
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
            if Path(safe_path).suffix or safe_path.startswith("assets/"):
                self.send_json(404, {"error": "Not found"})
                return
            file_path = PUBLIC_DIR / "index.html"

        # 缓存文件内容
        body = cached_static_file(file_path)
        if body is None:
            body = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript"
        if file_path.suffix in {".html", ".css", ".js", ".json"}:
            content_type += "; charset=utf-8"

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

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    if not CONVERSATIONS_PATH.exists():
        write_json(CONVERSATIONS_PATH, {"activeId": "", "items": []})
    if not PROVIDERS_PATH.exists():
        env_host = os.getenv("API_BASE_URL", "")
        env_key = os.getenv("API_KEY", "")
        env_model = os.getenv("AI_MODEL", "gpt-4o-mini")
        if env_host and env_key:
            write_json(PROVIDERS_PATH, [{
                "id": "default",
                "name": "默认通道",
                "apiHost": env_host,
                "apiPath": "",
                "apiKey": env_key,
                "aiModel": env_model,
                "enabled": True,
                "isDefault": True,
                "createdAt": now_iso(),
                "updatedAt": now_iso(),
            }])
        else:
            write_json(PROVIDERS_PATH, [])

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"MinAI running at http://{host}:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
