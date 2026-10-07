#!/usr/bin/env python3
"""轻量级 AI 资讯采集与中文展示服务；仅依赖 Python 标准库。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DATABASE = ROOT / "data" / "ainews.sqlite3"


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", str(DEFAULT_DATABASE))).expanduser()
COLLECTION_INTERVAL_MINUTES = int(os.environ.get("COLLECTION_INTERVAL_MINUTES", "30"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
SUMMARY_API_URL = os.environ.get("SUMMARY_API_URL", "").strip()
SUMMARY_API_KEY = os.environ.get("SUMMARY_API_KEY", "").strip()
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "").strip()
USER_AGENT = "AI观察室/1.0 (公开资讯采集; contact: site-operator)"
MAX_ARTICLE_AGE_DAYS = 45
MAX_FEATURED = 8
MIN_FEATURED = 5
COLLECTION_LOCK = threading.Lock()
ROBOTS_CACHE: dict[str, tuple[float, object]] = {}
ROBOTS_LOCK = threading.Lock()

if COLLECTION_INTERVAL_MINUTES <= 0 or REQUEST_TIMEOUT_SECONDS <= 0:
    raise SystemExit("采集周期和网络超时必须为正整数。")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("来源链接必须是公开 HTTP 或 HTTPS 地址。")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "fbclid", "gclid"}]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query, doseq=True), ""))


def fingerprint(title: str, publisher: str) -> str:
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    value = f"{publisher.casefold()}|{normalized}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_matches(url: str, allowed_domain: str) -> bool:
    host = safe_host(url)
    domain = allowed_domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def load_json(filename: str):
    with (ROOT / filename).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def connect_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel TEXT NOT NULL,
                kind TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                trust_level TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                last_success_at TEXT,
                last_error TEXT,
                last_discovered_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id),
                canonical_url TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                publisher TEXT NOT NULL,
                channel TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '综合',
                background TEXT,
                summary TEXT,
                excerpt TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'unknown',
                published_at TEXT,
                collected_at TEXT NOT NULL,
                verified_at TEXT,
                verified_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                evidence_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(id),
                evidence_url TEXT NOT NULL,
                claims_json TEXT NOT NULL DEFAULT '[]',
                result TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collection_runs (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result TEXT NOT NULL,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                error_category TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_articles_status_date ON articles(status, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_evidence_article ON evidence(article_id);
            """
        )
        source_columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
        if "language" not in source_columns:
            db.execute("ALTER TABLE sources ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
        for source in load_json("sources.json"):
            db.execute(
                """INSERT OR IGNORE INTO sources
                   (id,name,channel,kind,url,domain,enabled,trust_level,language,status,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (source["id"], source["name"], source["channel"], source["kind"], source["url"],
                 source["domain"], int(source["enabled"]), source["trust_level"], source.get("language", "en"),
                 source["status"], source["note"]),
            )
            db.execute("UPDATE sources SET language=? WHERE id=?", (source.get("language", "en"), source["id"]))
        for article in load_json("seed_articles.json"):
            db.execute(
                """INSERT OR IGNORE INTO articles
                   (id,source_id,canonical_url,fingerprint,publisher,channel,title,category,background,summary,
                    excerpt,language,published_at,collected_at,verified_at,verified_by,status,evidence_note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article["id"], article["source_id"], normalize_url(article["canonical_url"]),
                 fingerprint(article["title"], article["publisher"]), article["publisher"], article["channel"],
                 article["title"], article["category"], article["background"], article["summary"],
                 article["summary"], "zh-CN", article["published_at"], article["verified_at"],
                 article["verified_at"], article["verified_by"], "verified_primary", article["evidence_note"]),
            )
            db.execute(
                """INSERT OR IGNORE INTO evidence
                   (id,article_id,source_id,evidence_url,claims_json,result,verified_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (str(uuid.uuid5(uuid.NAMESPACE_URL, article["canonical_url"])), article["id"],
                 article["source_id"], article["canonical_url"],
                 json.dumps([article["evidence_note"]], ensure_ascii=False), "supported", article["verified_at"]),
            )


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.in_title = False
        self.title_parts: list[str] = []
        self.in_body = False
        self.body_parts: list[str] = []
        self.skip_depth = 0
        self.json_ld: list[str] = []
        self.in_json_ld = False
        self.json_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.lower(): value for key, value in attrs if key}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content:
                self.metadata[key] = content
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
            if tag == "script" and "ld+json" in values.get("type", "").lower():
                self.in_json_ld = True
                self.json_parts = []
        if tag == "body":
            self.in_body = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "body":
            self.in_body = False
        if tag == "script" and self.in_json_ld:
            payload = "".join(self.json_parts).strip()
            if payload:
                self.json_ld.append(payload)
            self.in_json_ld = False
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        clean = re.sub(r"\s+", " ", data).strip()
        if not clean:
            return
        if self.in_title:
            self.title_parts.append(clean)
        if self.in_body and not self.skip_depth:
            self.body_parts.append(clean)
        if self.in_json_ld:
            self.json_parts.append(data)

    def result(self) -> dict[str, str]:
        values = dict(self.metadata)
        values["title"] = " ".join(self.title_parts).strip()
        values["body_text"] = " ".join(self.body_parts)[:16000]
        for payload in self.json_ld:
            try:
                decoded = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            objects = decoded if isinstance(decoded, list) else [decoded]
            for item in objects:
                if not isinstance(item, dict):
                    continue
                if "@graph" in item and isinstance(item["@graph"], list):
                    objects.extend(entry for entry in item["@graph"] if isinstance(entry, dict))
                if str(item.get("@type", "")).lower() in {"article", "newsarticle", "blogposting"}:
                    values.setdefault("jsonld_title", str(item.get("headline", "")))
                    values.setdefault("datepublished", str(item.get("datePublished", "")))
                    values.setdefault("description", str(item.get("description", "")))
                    values.setdefault("articlebody", str(item.get("articleBody", ""))[:16000])
        return values


class ListingParser(HTMLParser):
    def __init__(self, base_url: str, domain: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.domain = domain
        self.links: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.skip_depth = 0
        self.current_time = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_map = {key.lower(): value for key, value in attrs if key}
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "time":
            self.current_time = attrs_map.get("datetime", "")
        if tag == "a" and not self.skip_depth:
            href = attrs_map.get("href", "")
            if href:
                absolute = urljoin(self.base_url, href)
                if host_matches(absolute, self.domain):
                    self.current = {"url": absolute, "title": "", "published_at": self.current_time}

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current:
            self.current["title"] = html.unescape(self.current["title"]).strip()
            if len(self.current["title"]) > 12:
                self.links.append(self.current)
            self.current = None
        if tag == "time":
            self.current_time = ""
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current and not self.skip_depth:
            self.current["title"] += re.sub(r"\s+", " ", data)


def robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    cached = ROBOTS_CACHE.get(origin)
    if cached and cached[0] > time.time():
        return cached[1].can_fetch(USER_AGENT, url)
    robots_url = origin + "/robots.txt"
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = Request(robots_url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                raise OSError(f"robots.txt 返回 HTTP {response.status}")
            text = response.read(256_000).decode("utf-8", errors="replace")
        parser.parse(text.splitlines())
    except HTTPError as exc:
        if exc.code in {404, 410}:
            parser.parse([])
        else:
            return False
    except (OSError, URLError, TimeoutError):
        return False
    ROBOTS_CACHE[origin] = (time.time() + 6 * 3600, parser)
    return parser.can_fetch(USER_AGENT, url)


def fetch_bytes(url: str) -> tuple[bytes, str]:
    if not robots_allowed(url):
        raise PermissionError("robots.txt 不允许采集该页面，或暂时无法确认访问规则。")
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5",
        "Accept-Encoding": "identity",
    })
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        if not host_matches(final_url, safe_host(url)):
            raise ValueError("来源发生跨域跳转，已停止采集。")
        return response.read(2_000_000), response.headers.get_content_charset() or "utf-8"


def parse_feed(payload: bytes, source: dict) -> list[dict[str, str]]:
    root = ET.fromstring(payload)
    items = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        for child in element:
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "link":
                fields["link"] = child.attrib.get("href", "") or (child.text or "").strip()
            elif name in {"title", "description", "summary", "content", "pubdate", "published", "updated", "date"}:
                fields[name] = " ".join("".join(child.itertext()).split())
        title = fields.get("title", "").strip()
        link = urljoin(source["url"], fields.get("link", "").strip())
        try:
            canonical = normalize_url(link)
        except ValueError:
            continue
        if not host_matches(canonical, source["domain"]) or not title:
            continue
        published = next((fields[key] for key in ("pubdate", "published", "updated", "date") if fields.get(key)), "")
        items.append({"url": canonical, "title": title, "published_at": published,
                      "excerpt": fields.get("description", fields.get("summary", fields.get("content", "")))[:4000]})
    return items[:60]


def parse_listing(payload: bytes, url: str, source: dict) -> list[dict[str, str]]:
    parser = ListingParser(url, source["domain"])
    parser.feed(payload.decode("utf-8", errors="replace"))
    seen: set[str] = set()
    output = []
    for item in parser.links:
        try:
            item_url = normalize_url(item["url"])
        except ValueError:
            continue
        if item_url in seen or item_url.rstrip("/") == url.rstrip("/"):
            continue
        seen.add(item_url)
        output.append({**item, "url": item_url, "excerpt": ""})
    return output[:30]


AI_TERMS = re.compile(r"\b(ai|artificial intelligence|model|llm|agent|copilot|gemini|claude|gpt|machine learning|neural|foundation model|hunyuan|hunyuan|大模型|人工智能|智能体|生成式|机器学习|文心|混元|AI)\b", re.I)


def is_ai_story(title: str, excerpt: str) -> bool:
    return bool(AI_TERMS.search(f"{title} {excerpt[:600]}"))


def chinese_content(title: str, excerpt: str, canonical_url: str, source: dict) -> tuple[str, str, str] | None:
    if SUMMARY_API_URL and SUMMARY_MODEL:
        payload = {
            "model": SUMMARY_MODEL,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是中文科技编辑。只使用给定的一手来源文字，生成简体中文。不得补充来源没有的数字、引语、能力、日期或因果推断。返回 JSON：title、background、summary、claims。claims 是最多 4 条短事实。原文不足以支持摘要时返回空 claims。"},
                {"role": "user", "content": f"来源：{canonical_url}\n标题：{title}\n原文摘录：{excerpt[:8000]}"},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if SUMMARY_API_KEY:
            headers["Authorization"] = f"Bearer {SUMMARY_API_KEY}"
        request = Request(SUMMARY_API_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read(512_000).decode("utf-8"))
        text = result["choices"][0]["message"]["content"]
        decoded = json.loads(text)
        translated_title = str(decoded.get("title", "")).strip()
        background = str(decoded.get("background", "")).strip()
        summary = str(decoded.get("summary", "")).strip()
        claims = decoded.get("claims", [])
        if not translated_title or not background or not summary or not isinstance(claims, list) or not claims:
            return None
        cjk = sum("\u4e00" <= char <= "\u9fff" for char in translated_title + background + summary)
        if cjk < 12:
            return None
        return translated_title, background, summary
    if source["language"] == "zh-CN":
        text = re.sub(r"\s+", " ", excerpt).strip()
        if not text:
            return None
        summary = text[:420]
        return title, "该动态来自官方中文公开来源。", summary
    return None


def page_metadata(url: str) -> dict[str, str]:
    data, charset = fetch_bytes(url)
    parser = MetadataParser()
    parser.feed(data.decode(charset, errors="replace"))
    return parser.result()


def published_time_from(meta: dict[str, str], candidate: dict[str, str]) -> datetime | None:
    return (parse_datetime(candidate.get("published_at"))
            or parse_datetime(meta.get("article:published_time"))
            or parse_datetime(meta.get("datepublished"))
            or parse_datetime(meta.get("date")))


def make_source_index() -> dict[str, dict]:
    with connect_db() as db:
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM sources")}


def insert_candidate(db: sqlite3.Connection, source: dict, candidate: dict) -> bool:
    url = normalize_url(candidate["url"])
    title = re.sub(r"\s+", " ", candidate.get("title", "")).strip()
    if not title or not host_matches(url, source["domain"]):
        return False
    published = parse_datetime(candidate.get("published_at"))
    if not is_ai_story(title, candidate.get("excerpt", "")):
        return False
    article_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    with connect_db() as db:
        exists = db.execute("SELECT 1 FROM articles WHERE canonical_url=? OR fingerprint=?", (url, fingerprint(title, source["name"]))).fetchone()
        if exists:
            return False
        with db:
            db.execute(
                """INSERT INTO articles
                   (id,source_id,canonical_url,fingerprint,publisher,channel,title,category,excerpt,language,
                    published_at,collected_at,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article_id, source["id"], url, fingerprint(title, source["name"]), source["name"], source["channel"],
                 title, "综合", candidate.get("excerpt", "")[:4000], source.get("language", "en"),
                 published.isoformat() if published else None, iso_now(), "discovered"),
            )
    return True


def verify_and_summarize(article_id: str, source: dict) -> str:
    with connect_db() as db:
        article_row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    if not article_row:
        return "rejected"
    article = dict(article_row)
    url = article["canonical_url"]
    if not host_matches(url, source["domain"]):
        return "rejected"
    try:
        metadata = page_metadata(url)
    except Exception as exc:
        return "pending_verification" if isinstance(exc, PermissionError) else "pending_verification"
    page_title = metadata.get("og:title") or metadata.get("jsonld_title") or metadata.get("title") or article["title"]
    excerpt = (metadata.get("og:description") or metadata.get("description") or metadata.get("articlebody")
               or metadata.get("body_text") or article["excerpt"])
    published = parse_datetime(article.get("published_at")) or published_time_from(metadata, {})
    if not published:
        return "pending_verification"
    claims = ["原文页面位于已配置的官方域名。", "发布时间可从官方来源确认。"]
    translated = chinese_content(page_title, excerpt, url, source)
    if not translated:
        with connect_db() as db:
            db.execute("UPDATE articles SET published_at=?,excerpt=?,status='pending_verification' WHERE id=?",
                       (published.isoformat(), excerpt[:4000], article_id))
        return "pending_verification"
    title, background, summary = translated
    verified_at = iso_now()
    with connect_db() as db:
        with db:
            db.execute(
                """UPDATE articles SET title=?,background=?,summary=?,excerpt=?,published_at=?,language='zh-CN',
                   verified_at=?,verified_by='自动核对官方来源',status='verified_primary' WHERE id=?""",
                (title, background, summary, excerpt[:4000], published.isoformat(), verified_at, article_id),
            )
            db.execute(
                "INSERT INTO evidence(id,article_id,source_id,evidence_url,claims_json,result,verified_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), article_id, source["id"], url, json.dumps(claims, ensure_ascii=False), "supported", verified_at),
            )
    return "verified_primary"


def collect_source(source: dict) -> tuple[int, str | None]:
    run_id = str(uuid.uuid4())
    started = iso_now()
    with connect_db() as db:
        db.execute("INSERT INTO collection_runs(id,source_id,started_at,result) VALUES(?,?,?,'running')", (run_id, source["id"], started))
    discovered = 0
    try:
        payload, charset = fetch_bytes(source["url"])
        if source["kind"] in {"rss", "atom"}:
            candidates = parse_feed(payload, source)
        else:
            candidates = parse_listing(payload, source["url"], source)
        for candidate in candidates:
            try:
                created = insert_candidate(connect_db(), source, candidate)
            except (ValueError, sqlite3.Error):
                created = False
            if created:
                discovered += 1
                article_id = str(uuid.uuid5(uuid.NAMESPACE_URL, normalize_url(candidate["url"])))
                verify_and_summarize(article_id, source)
        with connect_db() as db:
            db.execute("UPDATE sources SET status='采集正常',last_success_at=?,last_error=NULL,last_discovered_count=? WHERE id=?",
                       (iso_now(), discovered, source["id"]))
            db.execute("UPDATE collection_runs SET finished_at=?,result='success',discovered_count=? WHERE id=?",
                       (iso_now(), discovered, run_id))
        return discovered, None
    except Exception as exc:
        reason = str(exc)[:180]
        if isinstance(exc, PermissionError):
            status = "遵循访问规则暂停"
        elif isinstance(exc, (TimeoutError, URLError)):
            status = "暂时无法访问"
        else:
            status = "采集失败"
        with connect_db() as db:
            db.execute("UPDATE sources SET status=?,last_error=? WHERE id=?", (status, reason, source["id"]))
            db.execute("UPDATE collection_runs SET finished_at=?,result='failed',error_category=? WHERE id=?",
                       (iso_now(), status, run_id))
        return discovered, reason


def run_collection() -> dict:
    if not COLLECTION_LOCK.acquire(blocking=False):
        return {"status": "running", "message": "采集任务正在运行"}
    results = []
    started = iso_now()
    try:
        sources = [source for source in make_source_index().values() if source["enabled"]]
        for source in sources:
            count, error = collect_source(source)
            results.append({"source": source["name"], "count": count, "error": error})
        return {"status": "complete", "started_at": started, "finished_at": iso_now(), "sources": results}
    finally:
        COLLECTION_LOCK.release()


def scheduler_loop() -> None:
    while True:
        time.sleep(COLLECTION_INTERVAL_MINUTES * 60)
        try:
            run_collection()
        except Exception:
            pass


def article_view(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["source_url"] = item["canonical_url"]
    return item


def api_news() -> dict:
    cutoff = (now_utc() - timedelta(days=MAX_ARTICLE_AGE_DAYS)).isoformat()
    with connect_db() as db:
        rows = db.execute(
            """SELECT a.*, s.name AS source_name, s.status AS source_status FROM articles a
               JOIN sources s ON a.source_id=s.id
               WHERE a.status='verified_primary' AND a.published_at IS NOT NULL AND a.published_at>=?
               ORDER BY a.published_at DESC, a.collected_at DESC LIMIT ?""",
            (cutoff, MAX_FEATURED),
        ).fetchall()
        newest = db.execute("SELECT MAX(finished_at) AS updated FROM collection_runs").fetchone()["updated"]
    return {
        "items": [article_view(row) for row in rows],
        "count": len(rows),
        "minimum": MIN_FEATURED,
        "maximum": MAX_FEATURED,
        "featured_state": "精选已就绪" if len(rows) >= MIN_FEATURED else "精选不足",
        "last_collection_at": newest,
        "collection_interval_minutes": COLLECTION_INTERVAL_MINUTES,
        "translation_configured": bool(SUMMARY_API_URL and SUMMARY_MODEL),
    }


def api_status() -> dict:
    with connect_db() as db:
        sources = [dict(row) for row in db.execute(
            "SELECT id,name,channel,kind,enabled,status,note,last_success_at,last_error,last_discovered_count FROM sources ORDER BY name"
        )]
        last_run = db.execute("SELECT MAX(finished_at) AS finished FROM collection_runs").fetchone()["finished"]
        pending = db.execute("SELECT COUNT(*) AS count FROM articles WHERE status IN ('discovered','pending_verification')").fetchone()["count"]
    return {
        "sources": sources,
        "last_collection_at": last_run,
        "pending_count": pending,
        "summary_service_configured": bool(SUMMARY_API_URL and SUMMARY_MODEL),
        "collection_interval_minutes": COLLECTION_INTERVAL_MINUTES,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AIObserver/1.0"

    def log_message(self, format_string: str, *args) -> None:
        message = format_string % args
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {message}")

    def send_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "正常", "time": iso_now()})
        elif path == "/api/news":
            self.send_json(200, api_news())
        elif path == "/api/status":
            self.send_json(200, api_status())
        elif path == "/" or path == "/index.html":
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            candidate = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in candidate.parents:
                self.send_error(404)
                return
            content_type = "text/css; charset=utf-8" if candidate.suffix == ".css" else "text/javascript; charset=utf-8"
            self.serve_file(candidate, content_type)
        else:
            self.send_error(404, "未找到该页面")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/collect":
            self.send_error(404, "未找到该接口")
            return
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(403, "仅允许本机触发采集")
            return
        if not COLLECTION_LOCK.acquire(blocking=False):
            self.send_json(409, {"status": "running", "message": "采集任务正在运行"})
            return
        COLLECTION_LOCK.release()
        worker = threading.Thread(target=run_collection, name="manual-collection", daemon=True)
        worker.start()
        self.send_json(202, {"status": "已开始", "message": "正在检查已启用的公开来源"})

    def serve_file(self, path: Path, content_type: str) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(404, "页面文件不存在")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=scheduler_loop, name="collection-scheduler", daemon=True).start()
    if os.environ.get("RUN_COLLECTION_ON_STARTUP", "true").lower() in {"1", "true", "yes"}:
        threading.Thread(target=run_collection, name="initial-collection", daemon=True).start()
    print(f"人工智能观察室已启动：http://{HOST}:{PORT}")
    print(f"定时采集间隔：{COLLECTION_INTERVAL_MINUTES} 分钟；已配置中文摘要服务：{'是' if SUMMARY_API_URL and SUMMARY_MODEL else '否'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
