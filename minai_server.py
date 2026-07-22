
backend_code = r'''#!/usr/bin/env python3
import base64
import codecs
import json
import mimetypes
import os
import posixpath
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = ROOT / "data"
GENERATED_DIR = DATA_DIR / "generated"
UPLOAD_DIR = DATA_DIR / "uploads"
CONVERSATIONS_PATH = DATA_DIR / "conversations.json"
PROVIDERS_PATH = DATA_DIR / "providers.json"
MAX_BODY_SIZE = 16 * 1024 * 1024
GENERATED_IMAGE_MAX_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# 文件级锁
CONVERSATIONS_LOCK = threading.RLock()
PROVIDERS_LOCK = threading.RLock()

# 内存缓存
CONVERSATIONS_CACHE = {"mtime": None, "data": None}
PROVIDERS_CACHE = {"mtime": None, "data": None}

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# -------------------- 统一请求头（防 403 核心优化） --------------------
DEFAULT_API_HEADERS = {
    "User-Agent": "MinAI/2.1 (Compatible; OpenAI-Client/1.0.0; Python-urllib)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# -------------------- 静态文件缓存 --------------------
@lru_cache(maxsize=32)
def cached_static_file(filepath):
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


def safe_read_json(path, fallback):
    """安全读取 JSON，损坏时返回 fallback 并尝试恢复"""
    if not path.exists():
        return fallback
    try:
        text = path.read_text("utf-8")
        if not text.strip():
            backup = path.with_suffix(path.suffix + ".bak")
            if backup.exists():
                try:
                    data = json.loads(backup.read_text("utf-8"))
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
                    return data
                except:
                    pass
            return fallback
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[WARN] {path} 损坏: {e}")
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            try:
                data = json.loads(backup.read_text("utf-8"))
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
                print(f"[INFO] 已从备份恢复 {path}")
                return data
            except Exception as e2:
                print(f"[WARN] 备份也损坏: {e2}")
        corrupted = path.with_suffix(path.suffix + ".corrupted." + str(int(time.time())))
        try:
            path.rename(corrupted)
            print(f"[INFO] 已重命名损坏文件为 {corrupted}")
        except:
            pass
        return fallback
    except Exception as e:
        print(f"[WARN] 读取 {path} 失败: {e}")
        return fallback


def atomic_write_json(path, data):
    """原子写入 JSON，带备份"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(text, "utf-8")
        if path.exists():
            try:
                bak_path = path.with_suffix(path.suffix + ".bak")
                bak_path.write_text(path.read_text("utf-8"), "utf-8")
            except:
                pass
        tmp_path.replace(path)
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except:
            pass
        raise e


def clean_text(value, max_length):
    return str(value or "").strip()[:max_length]


def strip_trailing_slash(value):
    return str(value or "").strip().rstrip("/")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -------------------- 图片处理 --------------------
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
            **DEFAULT_API_HEADERS,
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


# -------------------- 文件上传处理 --------------------
def parse_multipart_form_data(handler):
    """简易 multipart/form-data 解析器"""
    content_type = handler.headers.get('Content-Type', '')
    if not content_type.startswith('multipart/form-data'):
        return []
    boundary = ''
    for part in content_type.split(';'):
        part = part.strip()
        if part.startswith('boundary='):
            boundary = part[9:].strip('"').strip("'")
            break
    if not boundary:
        return []
    try:
        length = int(handler.headers.get('Content-Length', '0'))
    except ValueError:
        length = 0
    if length > MAX_UPLOAD_SIZE:
        raise ValueError("上传文件过大")
    raw = handler.rfile.read(length) if length else b''
    delimiter = ('--' + boundary).encode()
    parts = raw.split(delimiter)
    files = []
    for part in parts[1:-1]:
        if b'\r\n\r\n' not in part:
            continue
        headers_raw, body = part.split(b'\r\n\r\n', 1)
        headers = headers_raw.decode('utf-8', 'replace').split('\r\n')
        filename = None
        name = None
        content_type_file = 'application/octet-stream'
        for h in headers:
            if h.lower().startswith('content-disposition'):
                for attr in h.split(';'):
                    attr = attr.strip()
                    if attr.startswith('name='):
                        name = attr[5:].strip('"').strip("'")
                    elif attr.startswith('filename='):
                        filename = attr[9:].strip('"').strip("'")
            elif h.lower().startswith('content-type'):
                content_type_file = h.split(':', 1)[1].strip()
        if filename and body:
            if body.endswith(b'\r\n'):
                body = body[:-2]
            files.append({
                'field': name,
                'filename': filename,
                'type': content_type_file,
                'data': body,
            })
    return files


def save_uploaded_file(file_info):
    """保存上传的文件并返回可访问路径"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file_info['filename']).suffix
    safe_name = f"{int(time.time())}-{secrets.token_hex(6)}{ext}"
    file_path = UPLOAD_DIR / safe_name
    file_path.write_bytes(file_info['data'])
    return {
        "url": f"/uploads/{safe_name}",
        "name": file_info['filename'],
        "type": file_info['type'],
        "size": len(file_info['data']),
    }


# -------------------- 联网搜索（免费 DuckDuckGo 方案） --------------------
class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._current = {}
        self._link_href = None
        self._tag_stack = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get('class', '')
        self._tag_stack.append((tag, attrs_dict))
        if tag == 'a':
            if 'result__a' in cls:
                self._in_title = True
                self._link_href = attrs_dict.get('href', '')
                self._current = {"title": "", "url": self._link_href, "snippet": ""}
            elif 'result__snippet' in cls:
                self._in_snippet = True
                self._current = self._current or {"title": "", "url": "", "snippet": ""}

    def handle_endtag(self, tag):
        if self._tag_stack:
            self._tag_stack.pop()
        if tag == 'a' and self._in_title:
            self._in_title = False
        if tag == 'a' and self._in_snippet:
            self._in_snippet = False
            if self._current and (self._current.get('title') or self._current.get('snippet')):
                self.results.append(self._current)
                self._current = None

    def handle_data(self, data):
        if self._in_title and self._current is not None:
            self._current['title'] += data
        elif self._in_snippet and self._current is not None:
            self._current['snippet'] += data


def duckduckgo_search(query, max_results=5):
    """使用 DuckDuckGo Lite 进行免费搜索"""
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = urllib.parse.urlencode({"q": query, "kl": "zh-cn"}).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            **BROWSER_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://lite.duckduckgo.com/",
            "Origin": "https://lite.duckduckgo.com",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', 'replace')

        results = []
        # 尝试用正则提取结果
        rows = re.findall(
            r'<tr[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>.*?<td[^>]*class="result-snippet"[^>]*>(.*?)</td>.*?</tr>',
            html, re.S | re.I
        )
        for href, title, snippet in rows[:max_results]:
            title = re.sub(r'<[^>]+>', '', title)
            snippet = re.sub(r'<[^>]+>', '', snippet)
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = 'https://duckduckgo.com' + href
            results.append({"title": title.strip(), "snippet": snippet.strip(), "url": href})

        if not results:
            # 备用解析：更宽松的正则
            links = re.findall(
                r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.S | re.I
            )
            snippets = re.findall(
                r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>',
                html, re.S | re.I
            )
            for i, (href, title) in enumerate(links[:max_results]):
                title = re.sub(r'<[^>]+>', '', title)
                snippet = re.sub(r'<[^>]+>', '', snippets[i]) if i < len(snippets) else ''
                if href.startswith('//'):
                    href = 'https:' + href
                elif href.startswith('/'):
                    href = 'https://duckduckgo.com' + href
                results.append({"title": title.strip(), "snippet": snippet.strip(), "url": href})

        if not results:
            # 再尝试通用解析
            parser = DDGParser()
            parser.feed(html)
            for r in parser.results[:max_results]:
                if r.get('title') or r.get('snippet'):
                    results.append(r)

        return results
    except Exception as e:
        print(f"[WARN] search error: {e}")
        return []


def build_search_context(query, results):
    """将搜索结果构建为模型可用的上下文"""
    if not results:
        return ""
    lines = ["以下是基于用户问题的网络搜索结果，请在回答时参考（如果与问题无关请忽略）："]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}")
        lines.append(f"来源: {r.get('url', '')}")
        lines.append(f"摘要: {r.get('snippet', '')}")
        lines.append("")
    return "\n".join(lines)


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
            file_url = str(file.get("url") or "")
            if file_type.startswith("image/") and (data_url.startswith("data:image/") or file_url.startswith("/uploads/")):
                img_url = data_url if data_url.startswith("data:image/") else file_url
                image_parts.append({"type": "image_url", "image_url": {"url": img_url}})
                if name:
                    file_blocks.append(f"\n\n[图片附件: {name}]")
            elif file_url and not file_url.startswith("data:"):
                file_blocks.append(f"\n\n[附件: {name or '未命名文件'} | {file_type or 'application/octet-stream'}]\n下载地址: {file_url}")
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
        data = safe_read_json(CONVERSATIONS_PATH, {})
        if not isinstance(data, dict):
            data = {}
        CONVERSATIONS_CACHE["mtime"] = mtime
        CONVERSATIONS_CACHE["data"] = data
        return data


def write_conversations(data):
    with CONVERSATIONS_LOCK:
        payload = data if isinstance(data, dict) else {}
        atomic_write_json(CONVERSATIONS_PATH, payload)
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
def read_providers():
    """读取 providers，带缓存"""
    with PROVIDERS_LOCK:
        try:
            mtime = PROVIDERS_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            PROVIDERS_CACHE["mtime"] = None
            PROVIDERS_CACHE["data"] = []
            return []
        if PROVIDERS_CACHE.get("mtime") == mtime and isinstance(PROVIDERS_CACHE.get("data"), list):
            return PROVIDERS_CACHE["data"]
        data = safe_read_json(PROVIDERS_PATH, [])
        if not isinstance(data, list):
            data = []
        for p in data:
            if "apiHost" not in p and "apiBaseUrl" in p:
                p["apiHost"] = p.pop("apiBaseUrl")
            if "apiHost" not in p:
                p["apiHost"] = "https://api.openai.com"
        PROVIDERS_CACHE["mtime"] = mtime
        PROVIDERS_CACHE["data"] = data
        return data


def write_providers(data):
    with PROVIDERS_LOCK:
        payload = data if isinstance(data, list) else []
        atomic_write_json(PROVIDERS_PATH, payload)
        PROVIDERS_CACHE["data"] = payload
        try:
            PROVIDERS_CACHE["mtime"] = PROVIDERS_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            PROVIDERS_CACHE["mtime"] = None


def list_providers():
    return read_providers()


def save_providers(providers):
    write_providers(providers)


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


def validate_api_host(api_host):
    """验证 apiHost 格式"""
    if not api_host:
        raise ValueError("接口地址不能为空")
    api_host = api_host.strip()
    if not (api_host.startswith("http://") or api_host.startswith("https://")):
        raise ValueError("接口地址必须以 http:// 或 https:// 开头")
    return api_host


def create_provider(data):
    providers = list_providers()
    now = now_iso()
    api_host = validate_api_host(data.get("apiHost", ""))
    api_key = data.get("apiKey", "")
    if not api_key:
        raise ValueError("API Key 不能为空")
    provider = {
        "id": secrets.token_hex(12),
        "name": clean_text(data.get("name", "新通道"), 60),
        "apiHost": strip_trailing_slash(api_host),
        "apiPath": clean_text(data.get("apiPath", ""), 120),
        "apiKey": api_key,
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
            if data.get("apiHost"):
                p["apiHost"] = strip_trailing_slash(validate_api_host(data["apiHost"]))
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
        **DEFAULT_API_HEADERS,
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
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[WARN] fetch_provider_models HTTP {e.code}: {body[:500]}")
        raise ValueError(f"获取模型失败: HTTP {e.code}")
    except Exception as e:
        print(f"[WARN] fetch_provider_models error: {e}")
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
    protocol_version = "HTTP/1.0"
    server_version = "MinAI/2.1"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {self.log_date_time_string()} {fmt % args}")

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.route_api("GET", parsed.path)
                return
            if parsed.path.startswith("/generated/"):
                self.serve_generated(parsed.path)
                return
            if parsed.path.startswith("/uploads/"):
                self.serve_uploads(parsed.path)
                return
            self.serve_static(parsed.path)
        except Exception as e:
            print(f"[ERROR] GET {self.path}: {e}")
            try:
                self.send_json(500, {"error": "服务器内部错误"})
            except:
                pass

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            self.route_api("POST", parsed.path)
        except Exception as e:
            print(f"[ERROR] POST {self.path}: {e}")
            try:
                self.send_json(500, {"error": "服务器内部错误"})
            except:
                pass

    def do_PUT(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            self.route_api("PUT", parsed.path)
        except Exception as e:
            print(f"[ERROR] PUT {self.path}: {e}")
            try:
                self.send_json(500, {"error": "服务器内部错误"})
            except:
                pass

    def do_DELETE(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            self.route_api("DELETE", parsed.path)
        except Exception as e:
            print(f"[ERROR] DELETE {self.path}: {e}")
            try:
                self.send_json(500, {"error": "服务器内部错误"})
            except:
                pass

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

        # 文件上传
        if method == "POST" and path == "/api/upload":
            self.handle_upload()
            return

        # 搜索
        if method == "POST" and path == "/api/search":
            self.handle_search()
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
            try:
                providers = list_providers()
                self.send_json(200, {"providers": [public_provider(p) for p in providers]})
            except Exception as e:
                print(f"[ERROR] list_providers: {e}")
                self.send_json(500, {"error": "读取通道列表失败，数据可能损坏"})
            return
        if method == "POST" and path == "/api/providers":
            body = self.read_json_body()
            try:
                provider = create_provider(body)
                self.send_json(201, {"provider": public_provider(provider)})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                print(f"[ERROR] create_provider: {e}")
                self.send_json(500, {"error": str(e)})
            return

        if method == "POST" and path == "/api/providers/test-connection":
            self.handle_test_provider()
            return

        if path.startswith("/api/providers/"):
            parts = path.split("/")
            if len(parts) == 4:
                provider_id = parts[3]
                if method == "PUT":
                    body = self.read_json_body()
                    try:
                        updated = update_provider(provider_id, body)
                        if updated:
                            self.send_json(200, {"provider": public_provider(updated)})
                        else:
                            self.send_json(404, {"error": "通道不存在"})
                    except ValueError as e:
                        self.send_json(400, {"error": str(e)})
                    except Exception as e:
                        print(f"[ERROR] update_provider: {e}")
                        self.send_json(500, {"error": str(e)})
                elif method == "DELETE":
                    try:
                        if delete_provider(provider_id):
                            self.send_json(200, {"ok": True})
                        else:
                            self.send_json(404, {"error": "通道不存在或无法删除"})
                    except Exception as e:
                        print(f"[ERROR] delete_provider: {e}")
                        self.send_json(500, {"error": str(e)})
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

        self.send_json(404, {"error": "Not found"})

    def handle_upload(self):
        """处理文件上传"""
        try:
            files = parse_multipart_form_data(self)
            if not files:
                self.send_json(400, {"error": "未检测到上传文件"})
                return
            results = []
            for f in files:
                info = save_uploaded_file(f)
                results.append(info)
            self.send_json(200, {"files": results})
        except ValueError as e:
            self.send_json(413, {"error": str(e)})
        except Exception as e:
            print(f"[ERROR] upload: {e}")
            self.send_json(500, {"error": "上传失败"})

    def handle_search(self):
        """处理联网搜索请求"""
        body = self.read_json_body()
        query = clean_text(body.get("query"), 500)
        if not query:
            self.send_json(400, {"error": "搜索内容不能为空"})
            return
        try:
            results = duckduckgo_search(query, max_results=body.get("maxResults", 5))
            self.send_json(200, {"query": query, "results": results})
        except Exception as e:
            print(f"[ERROR] search: {e}")
            self.send_json(500, {"error": f"搜索失败: {str(e)}"})

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
            models = fetch_provider_models(provider)
            self.send_json(200, {"success": True, "models": models[:5]})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_chat(self):
        """支持流式与非流式两种模式，集成联网搜索"""
        body = self.read_json_body()
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

        # 联网搜索集成
        enable_search = body.get("search", False)
        system_prompt = os.getenv("SYSTEM_PROMPT", "你是一个专业、简洁、友好的 AI 助手。请优先用中文回答。")
        if enable_search and messages:
            last_user_msg = None
            for m in reversed(messages):
                if m.get("role") == "user":
                    last_user_msg = m
                    break
            if last_user_msg:
                search_query = ""
                if isinstance(last_user_msg.get("content"), list):
                    for part in last_user_msg["content"]:
                        if part.get("type") == "text":
                            search_query = part.get("text", "")
                            break
                else:
                    search_query = last_user_msg.get("content", "")
                if search_query and len(search_query) < 500:
                    try:
                        search_results = duckduckgo_search(search_query, max_results=5)
                        if search_results:
                            search_context = build_search_context(search_query, search_results)
                            system_prompt = system_prompt + "\n\n" + search_context
                    except Exception as e:
                        print(f"[WARN] search integration failed: {e}")

        model = body.get("model") or provider["aiModel"]
        stream = body.get("stream", True)
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
                **DEFAULT_API_HEADERS,
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
                    self.end_headers()

                    decoder = codecs.getincrementaldecoder('utf-8')()
                    full_reply = ""

                    while True:
                        chunk = response.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()

                        try:
                            decoded = decoder.decode(chunk)
                            for line in decoded.split("\n"):
                                line = line.strip()
                                if line.startswith("data: "):
                                    data_str = line[6:].strip()
                                    if data_str == "[DONE]":
                                        continue
                                    try:
                                        data = json.loads(data_str)
                                        content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                        if content:
                                            full_reply += content
                                    except:
                                        pass
                        except:
                            pass

                    try:
                        remaining = decoder.decode(b'', final=True)
                        for line in remaining.split("\n"):
                            line = line.strip()
                            if line.startswith("data: "):
                                data_str = line[6:].strip()
                                if data_str == "[DONE]":
                                    continue
                                try:
                                    data = json.loads(data_str)
                                    content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if content:
                                        full_reply += content
                                except:
                                    pass
                    except:
                        pass

                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

                    if full_reply:
                        assistant_msg = {"role": "assistant", "content": full_reply}
                        conversation_id = body.get("conversationId") or ""
                        result = upsert_conversation(conversation_id, stored_messages + [assistant_msg])
                        meta = json.dumps({"conversationId": result["conversationId"], "conversations": result["conversations"]})
                        self.wfile.write(f"data: {meta}\n\n".encode())
                        self.wfile.flush()

                else:
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
            print(f"[WARN] handle_chat HTTP {error.code}: {error_text[:500]}")
            self.send_json(error.code, {"error": msg})
        except Exception as e:
            print(f"[WARN] handle_chat error: {e}")
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

    def serve_uploads(self, path, head_only=False):
        safe_name = posixpath.basename(urllib.parse.unquote(path))
        file_path = (UPLOAD_DIR / safe_name).resolve()
        try:
            file_path.relative_to(UPLOAD_DIR.resolve())
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
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(safe_name)}")
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
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化对话文件
    if not CONVERSATIONS_PATH.exists():
        atomic_write_json(CONVERSATIONS_PATH, {"activeId": "", "items": []})
    else:
        try:
            data = safe_read_json(CONVERSATIONS_PATH, None)
            if data is None:
                print("[WARN] conversations.json 损坏，已重置")
                atomic_write_json(CONVERSATIONS_PATH, {"activeId": "", "items": []})
        except Exception as e:
            print(f"[WARN] 验证 conversations.json 失败: {e}")
            atomic_write_json(CONVERSATIONS_PATH, {"activeId": "", "items": []})

    # 初始化通道文件
    if not PROVIDERS_PATH.exists():
        env_host = os.getenv("API_BASE_URL", "")
        env_key = os.getenv("API_KEY", "")
        env_model = os.getenv("AI_MODEL", "gpt-4o-mini")
        if env_host and env_key:
            atomic_write_json(PROVIDERS_PATH, [{
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
            atomic_write_json(PROVIDERS_PATH, [])
    else:
        try:
            data = safe_read_json(PROVIDERS_PATH, None)
            if data is None:
                print("[WARN] providers.json 损坏，已重置为空列表")
                atomic_write_json(PROVIDERS_PATH, [])
        except Exception as e:
            print(f"[WARN] 验证 providers.json 失败: {e}")
            atomic_write_json(PROVIDERS_PATH, [])

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"MinAI running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
'''

with open('/mnt/agents/output/minai_server.py', 'w', encoding='utf-8') as f:
    f.write(backend_code)

print("后端代码已保存，长度:", len(backend_code))
