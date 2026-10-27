#!/usr/bin/env python3
"""每日新闻：从各大新闻网站的 RSS 抓取文章，按来源和分类生成静态网页。

用法:
  python newsdigest.py update [--open]    抓取新文章、翻译英文新闻并生成网页
  python newsdigest.py translate          只翻译还没翻译的英文新闻，然后重新生成网页
  python newsdigest.py build [--open]     只用已有数据重新生成网页
  python newsdigest.py serve [--port N]   在本地开一个网页服务器(手机同一局域网也可访问时用 --lan)

翻译使用 Claude API，需要在 .env 里设置 ANTHROPIC_API_KEY(见 .env.example)。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import functools
import hashlib
import html
import http.server
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "sources.yaml"
DB_PATH = ROOT / "data" / "news.db"
STATUS_PATH = ROOT / "data" / "last_run.json"
TEMPLATE = ROOT / "template.html"
SITE = ROOT / "site"
ENV_PATH = ROOT / ".env"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
WEEKDAYS = "一二三四五六日"
SUMMARY_MAX = 280
# 每百万 token 的美元价格(输入, 输出)，只用来估算本次翻译花费
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

DEFAULT_SECTION = "news"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)", re.I)
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
BBC_IMG_RE = re.compile(r"/ace/(standard|ws)/\d+/")
DAY_PAGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.html$")


# ---------------------------------------------------------------- 配置

def load_config(path: Path = CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    s = cfg.setdefault("settings", {})
    s.setdefault("max_age_hours", 48)
    s.setdefault("keep_days", 30)
    s.setdefault("timezone", "local")
    s.setdefault("timeout", 20)
    s.setdefault("workers", 12)
    tr = cfg.setdefault("translate", {}) or {}
    cfg["translate"] = tr
    tr.setdefault("enabled", True)
    tr.setdefault("model", "claude-haiku-5-5")
    tr.setdefault("batch_size", 30)
    tr.setdefault("workers", 4)
    tr.setdefault("max_per_run", 2000)
    cfg["sections"] = {str(k): str(v) for k, v in (cfg.get("sections") or {}).items()}
    cfg["regions"] = {str(k): v for k, v in (cfg.get("regions") or {}).items()}
    if DEFAULT_SECTION not in cfg["sections"]:
        raise SystemExit(f"sources.yaml 的 sections 里必须有 {DEFAULT_SECTION}")
    for src in cfg.get("sources", []):
        for feed in src.get("feeds", []):
            if feed.get("section") and feed["section"] not in cfg["sections"]:
                warn(f"{src['id']}: 未定义的栏目 {feed['section']!r}")
            if feed.get("default") and feed["default"] not in cfg["sections"]:
                warn(f"{src['id']}: 未定义的栏目 {feed['default']!r}")
            if feed.get("region") and feed["region"] not in cfg["regions"]:
                warn(f"{src['id']}: 未定义的地区 {feed['region']!r}")
    return cfg


def resolve_tz(name: str):
    """返回 (tzinfo, IANA 名称或 None)。"""
    if name and name != "local":
        return ZoneInfo(name), name
    key = os.environ.get("TZ", "").lstrip(":")
    if not key:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            key = link.split("zoneinfo/", 1)[1]
    if key:
        try:
            return ZoneInfo(key), key
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo, None


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 分类

WORD_RE = r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])"


class Matcher:
    """按关键词给文章打分：标题命中算 2 分，摘要命中算 1 分。
    中文按包含匹配；英文按整词匹配，含大写字母的词区分大小写(China ≠ china、US ≠ us)。"""

    def __init__(self, groups: dict):
        self.groups = []
        for key, words in groups.items():
            words = sorted({str(w) for w in words or []}, key=len, reverse=True)
            patterns = []
            exact = [w for w in words if w.isascii() and w != w.lower()]
            loose = [w for w in words if w.isascii() and w == w.lower()]
            if exact:
                patterns.append(re.compile(WORD_RE % "|".join(map(re.escape, exact))))
            if loose:
                patterns.append(re.compile(WORD_RE % "|".join(map(re.escape, loose)), re.I))
            self.groups.append((key, patterns, [w for w in words if not w.isascii()]))

    def scores(self, title: str, summary: str) -> dict:
        out = {}
        for order, (key, patterns, cjk) in enumerate(self.groups):
            score = 0
            for text, weight in ((title, 2), (summary, 1)):
                if text:
                    score += weight * (sum(len(p.findall(text)) for p in patterns) + sum(text.count(w) for w in cjk))
            out[key] = (score, -order)
        return out

    def top(self, title: str, summary: str, limit: int, minimum: int) -> list:
        scores = {k: v for k, v in self.scores(title, summary).items() if v[0] >= minimum}
        return sorted(scores, key=scores.get, reverse=True)[:limit]


# ---------------------------------------------------------------- 抓取

def feed_jobs(cfg: dict) -> list:
    return [{"source": src, "url": feed["url"], "section": feed.get("section"), "default": feed.get("default"),
             "region": feed.get("region"),
             "opinion": bool(feed.get("opinion")), "rank": bool(feed.get("rank")),
             "lang": feed.get("lang", src.get("lang", "en"))}
            for src in cfg.get("sources", []) for feed in src.get("feeds", [])]


def fetch(url: str, timeout: int) -> list:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    })
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "")
            parsed = feedparser.parse(body, response_headers={"content-type": ctype})
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"无法解析: {parsed.bozo_exception}")
            return parsed.entries
        except urllib.error.HTTPError as e:
            if attempt or e.code in (401, 403, 404, 410):
                raise
        except Exception:
            if attempt:
                raise
        time.sleep(2)
    return []


def short_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"连接失败: {e.reason}"
    return f"{type(e).__name__}: {e}"[:120]


def clean_text(s) -> str:
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", s or ""))).strip()


def canonical(link: str, keep_query: bool) -> str:
    parts = urllib.parse.urlsplit(link.strip())
    parts = parts._replace(fragment="") if keep_query else parts._replace(query="", fragment="")
    return urllib.parse.urlunsplit(parts)


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


@functools.lru_cache(maxsize=1)
def _opencc():
    from opencc import OpenCC
    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    return _opencc().convert(text) if text else text


def entry_image(e):
    """取 feed 里最大的配图；BBC 默认只给 240px 缩略图，换成 800px。"""
    best, width = None, -1
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            url = m.get("url")
            if not url or (m.get("medium") not in (None, "image") and not IMG_EXT_RE.search(url)):
                continue
            w = int(m["width"]) if str(m.get("width", "")).isdigit() else 0
            if w > width:
                best, width = url, w
    if not best:
        best = next((enc["href"] for enc in e.get("enclosures") or []
                     if (enc.get("type") or "").startswith("image/") and enc.get("href")), None)
    if not best:
        for text in [e.get("summary")] + [c.get("value") for c in e.get("content") or []]:
            m = IMG_RE.search(text or "")
            if m:
                best = html.unescape(m.group(1))
                break
    if not best:
        return None
    if best.startswith("//"):
        best = "https:" + best
    if not best.startswith(("https://", "http://")):
        return None
    return BBC_IMG_RE.sub(r"/ace/\1/800/", best)


def normalize(e, job: dict, position: int, now: datetime, cutoff: datetime, matchers: dict):
    src = job["source"]
    title = clean_text(e.get("title"))
    link = (e.get("link") or "").strip()
    if src.get("t2s"):
        title = to_simplified(title)
    if not title or not link:
        return None
    if any(re.search(p, title, re.I) for p in src.get("exclude") or []):
        return None

    published = entry_time(e)
    if published and published > now + timedelta(hours=1):
        published = now
    if published and published < cutoff:
        return None

    summary = clean_text(e.get("summary"))
    if src.get("t2s"):
        summary = to_simplified(summary)
    if summary == title:
        summary = ""
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX].rstrip() + "…"

    return {
        "uid": hashlib.sha1(f"{src['id']}|{canonical(link, False)}".encode()).hexdigest()[:16],
        "source": src["id"], "title": title, "link": canonical(link, src.get("keep_query", False)),
        "summary": summary, "published": iso(published) if published else None,
        "sections": [job["section"]] if job["section"] else [],
        "defaults": [job["default"]] if job["default"] else [],
        "scores": matchers["section"].scores(title, summary),
        # 地区以标题为准；标题里没有再看摘要(避免 "乌克兰停火" 因摘要提到白宫被归入美洲)
        "regions": matchers["region"].top(title, "", 2, 1) or matchers["region"].top("", summary, 2, 1),
        "hints": [job["region"]] if job["region"] else [],
        "image": entry_image(e), "opinion": job["opinion"],
        "rank": position if job["rank"] else None, "lang": job["lang"],
    }


def merge(prev: dict, a: dict) -> None:
    """同一篇文章出现在同一媒体的多个 feed 里：合并各 feed 给出的栏目、地区、配图等信息。"""
    for key in ("sections", "defaults"):
        prev[key] += [x for x in a[key] if x not in prev[key]]
    prev["hints"] += [h for h in a["hints"] if h not in prev["hints"]]
    prev["image"] = prev["image"] or a["image"]
    prev["opinion"] = prev["opinion"] or a["opinion"]
    if a["rank"] is not None and (prev["rank"] is None or a["rank"] < prev["rank"]):
        prev["rank"] = a["rank"]


def choose_section(a: dict) -> tuple:
    """返回 (栏目, 是否按关键词判断)。
    feed 写了 section 的以它为准；出现在多个栏目 feed 里时，看标题和摘要更像哪个；
    都没写的按关键词判断(至少 2 分)，判断不出用 feed 的 default，再不行归入时政。"""
    scores = a["scores"]
    if a["sections"]:
        return max(a["sections"], key=lambda s: scores.get(s, (0, 0))), False
    best = max(scores, key=scores.get, default=None) if scores else None
    if best and scores[best][0] >= 2:
        return best, True
    return (a["defaults"] or [DEFAULT_SECTION])[0], True


def collect(cfg: dict, now: datetime):
    s = cfg["settings"]
    cutoff = now - timedelta(hours=s["max_age_hours"])
    matchers = {
        "section": Matcher({k: v for k, v in (cfg.get("classify") or {}).items() if k in cfg["sections"]}),
        "region": Matcher({k: (v or {}).get("keywords") for k, v in cfg["regions"].items()}),
    }
    jobs = feed_jobs(cfg)
    articles, status = {}, []
    with cf.ThreadPoolExecutor(s["workers"]) as ex:
        futures = {ex.submit(fetch, j["url"], s["timeout"]): j for j in jobs}
        for fut in cf.as_completed(futures):
            job = futures[fut]
            sid = job["source"]["id"]
            try:
                entries = fut.result()
            except Exception as e:
                status.append({"s": sid, "url": job["url"], "ok": False, "err": short_error(e)})
                continue
            kept = 0
            for position, entry in enumerate(entries, 1):
                a = normalize(entry, job, position, now, cutoff, matchers)
                if not a:
                    continue
                kept += 1
                if a["uid"] in articles:
                    merge(articles[a["uid"]], a)
                else:
                    articles[a["uid"]] = a
            status.append({"s": sid, "url": job["url"], "ok": True, "entries": len(entries), "kept": kept})
    for a in articles.values():
        a["section"], a["section_auto"] = choose_section(a)
    return articles, status


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    uid        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    title      TEXT NOT NULL,
    link       TEXT NOT NULL,
    summary    TEXT,
    published  TEXT NOT NULL,   -- UTC ISO；没有发布时间的文章用首次抓到的时间
    first_seen TEXT NOT NULL,
    section    TEXT NOT NULL,   -- 栏目
    section_auto INTEGER,       -- 1 = 栏目是按关键词判断的
    regions    TEXT,            -- 地区，逗号分隔
    image      TEXT,
    opinion    INTEGER,
    rank       INTEGER,         -- 在媒体首页头条 feed 里的位置(1 = 头条)
    ranked_at  TEXT,            -- 上次出现在头条 feed 里的抓取时间
    lang       TEXT,
    title_zh   TEXT,            -- 英文文章的中文译文；NULL = 还没翻译
    summary_zh TEXT
);
CREATE INDEX IF NOT EXISTS idx_published ON articles(published);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    cols = {row[1] for row in db.execute("PRAGMA table_info(articles)")}
    if cols and "section" not in cols:   # 旧版数据库结构不同，另存后重建
        db.close()
        old = DB_PATH.with_suffix(".v1.db")
        os.replace(DB_PATH, old)
        print(f"数据库结构已更新，旧数据库另存为 {old.relative_to(ROOT)}")
        db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    return db


def store(db: sqlite3.Connection, articles: dict, now: datetime, keep_days: int) -> int:
    seen = iso(now)
    new = 0
    for a in articles.values():
        regions = ",".join(a["regions"] or a["hints"][:2])
        row = db.execute("SELECT section, section_auto, title, summary, image FROM articles WHERE uid = ?",
                         (a["uid"],)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO articles (uid, source, title, link, summary, published, first_seen, section, section_auto, "
                "regions, image, opinion, rank, ranked_at, lang) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a["uid"], a["source"], a["title"], a["link"], a["summary"], a["published"] or seen, seen,
                 a["section"], int(a["section_auto"]), regions, a["image"], int(a["opinion"]), a["rank"],
                 seen if a["rank"] is not None else None, a["lang"]))
            new += 1
        else:
            section, section_auto = a["section"], a["section_auto"]
            if section_auto and not row[1]:   # 以前明确归过栏目的，不被关键词判断覆盖
                section, section_auto = row[0], False
            summary = a["summary"] or row[3]
            changed = a["title"] != row[2] or summary != row[3]   # 原文改了，译文要重做
            db.execute(
                "UPDATE articles SET title = ?, link = ?, summary = ?, section = ?, section_auto = ?, regions = ?, "
                "image = ?, opinion = ?, "
                "rank = CASE WHEN ? IS NULL THEN rank ELSE ? END, ranked_at = CASE WHEN ? IS NULL THEN ranked_at ELSE ? END, "
                "title_zh = CASE WHEN ? THEN NULL ELSE title_zh END, "
                "summary_zh = CASE WHEN ? THEN NULL ELSE summary_zh END WHERE uid = ?",
                (a["title"], a["link"], summary, section, int(section_auto), regions, a["image"] or row[4],
                 int(a["opinion"]), a["rank"], a["rank"], a["rank"], seen, changed, changed, a["uid"]))
    db.execute("DELETE FROM articles WHERE published < ?", (iso(now - timedelta(days=keep_days)),))
    db.commit()
    return new


# ---------------------------------------------------------------- 翻译

TRANSLATE_SYSTEM = """你是资深的中文新闻编译，负责把英文新闻的标题和摘要译成简体中文，供中文读者快速浏览当天新闻。

要求：
- 忠实原意：不增删信息，不加评论或解释；数字、日期、金额、百分比、引语的含义必须准确。
- 读起来像中文媒体自己写的：标题简洁有力，符合中文新闻标题的习惯；摘要用通顺自然的书面语，避免翻译腔、生硬的长定语和多余的"的"。
- 人名、地名、机构名用中文媒体(如新华社)的通用译名；没有通用中文名的公司、产品、品牌保留英文原名。
- 常见缩写按中文习惯处理，例如 Fed→美联储、ECB→欧洲央行、BoE→英国央行、GDP→GDP。
- 标题里的栏目前缀照译，例如 "Opinion |"→"观点｜"、"Exclusive |"→"独家｜"、"Live:"→"直播："。
- 原文摘要为空时，译文摘要也返回空字符串。
- 输入里的每一条都要翻译，id 原样返回。"""

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "title", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def load_env(path: Path = ENV_PATH) -> None:
    """读取 .env 里的 KEY=VALUE(已存在的环境变量优先)。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def translate_batch(client, model: str, batch: list) -> tuple:
    """翻译一批文章，返回 ({uid: (标题, 摘要)}, usage)。"""
    payload = [{"id": uid, "title": title, "summary": summary or ""} for uid, title, summary in batch]
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=TRANSLATE_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": TRANSLATE_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"模型没有正常结束(stop_reason={response.stop_reason})")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("返回里没有文本")
    wanted = {uid for uid, _, _ in batch}
    result = {}
    for item in json.loads(text)["items"]:
        title = item["title"].strip()
        if item["id"] in wanted and title:
            result[item["id"]] = (title, item["summary"].strip())
    return result, response.usage


def translate_pending(cfg: dict, db: sqlite3.Connection, now: datetime) -> int:
    """把最近还没翻译的英文文章译成中文，结果存进数据库。返回翻译成功的篇数。"""
    tr = cfg["translate"]
    if not tr["enabled"]:
        return 0
    load_env()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("翻译：未设置 ANTHROPIC_API_KEY，跳过(把 key 写进 .env，见 .env.example)")
        return 0
    try:
        import anthropic
    except ImportError:
        print("翻译：没有安装 anthropic，跳过(运行 .venv/bin/pip install -r requirements.txt)")
        return 0

    cutoff = iso(now - timedelta(hours=cfg["settings"]["max_age_hours"]))
    rows = db.execute(
        "SELECT uid, title, summary FROM articles WHERE lang = 'en' AND title_zh IS NULL AND published >= ? "
        "ORDER BY published DESC LIMIT ?", (cutoff, tr["max_per_run"])).fetchall()
    if not rows:
        print("翻译：没有需要翻译的新文章")
        return 0

    model, size = tr["model"], max(1, tr["batch_size"])
    batches = [rows[i:i + size] for i in range(0, len(rows), size)]
    print(f"翻译 {len(rows)} 篇英文新闻({len(batches)} 批，模型 {model}) …")
    client = anthropic.Anthropic(max_retries=4)
    done, tokens_in, tokens_out, failed = 0, 0, 0, 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(tr["workers"]) as ex:
        futures = [ex.submit(translate_batch, client, model, b) for b in batches]
        for fut in cf.as_completed(futures):
            try:
                result, usage = fut.result()
            except anthropic.AuthenticationError:
                print("  ✗ API key 无效，请检查 .env 里的 ANTHROPIC_API_KEY")
                for f in futures:
                    f.cancel()
                break
            except anthropic.PermissionDeniedError as e:
                print(f"  ✗ 没有权限使用 {model}：{e.message}")
                for f in futures:
                    f.cancel()
                break
            except anthropic.NotFoundError:
                print(f"  ✗ 找不到模型 {model}，请检查 sources.yaml 里的 translate.model")
                for f in futures:
                    f.cancel()
                break
            except anthropic.RateLimitError:
                failed += 1
                print("  ✗ 一批因限流失败，下次运行会重试")
                continue
            except anthropic.APIStatusError as e:
                failed += 1
                print(f"  ✗ 一批失败(HTTP {e.status_code})：{e.message}")
                continue
            except anthropic.APIConnectionError:
                failed += 1
                print("  ✗ 一批因网络问题失败，下次运行会重试")
                continue
            except (RuntimeError, ValueError, KeyError) as e:
                failed += 1
                print(f"  ✗ 一批结果无法使用：{e}")
                continue
            tokens_in += usage.input_tokens
            tokens_out += usage.output_tokens
            db.executemany("UPDATE articles SET title_zh = ?, summary_zh = ? WHERE uid = ?",
                           [(t, s, uid) for uid, (t, s) in result.items()])
            db.commit()
            done += len(result)
    cost = ""
    if model in PRICES:
        p_in, p_out = PRICES[model]
        cost = f"，约 ${(tokens_in * p_in + tokens_out * p_out) / 1e6:.3f}"
    print(f"翻译完成，用时 {time.time() - t0:.0f} 秒：成功 {done} 篇"
          + (f"，{failed} 批失败" if failed else "")
          + f"；用量 {tokens_in} 输入 / {tokens_out} 输出 token{cost}")
    return done


# ---------------------------------------------------------------- 同题报道

STOPWORDS = set("""
a about after again against all also amid an and any are as at be been before being between both but by can could
did do does down during each few for from had has have he her here him his how if in into is it its just more most
new no nor not now of off on once only or other our out over own said says same she should so some still such than
that the their them then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your year years week day days live latest watch video opinion analysis
review letter letters exclusive update updates first last next one two three four five six seven eight nine ten
monday tuesday wednesday thursday friday saturday sunday january february march april june july august september
october november december people man woman men women told report reports news time make makes made get gets take
takes call calls back like set sets way ways part plan plans move moves amid shows show warns warn faces face
urges urge hit hits big top key high low near early late long open former help need needs want wants expected say
billion million trillion percent sales share shares profit profits loss losses deal deals invest investment firm
firms company companies stock stocks market markets price prices rate rates vote votes voters poll polls election
elections fear fears grow grows growing
""".split())
STOP_STEMS = {w[:6] for w in STOPWORDS}
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")
MAX_STORY = 20   # 一组最多这么多篇，防止把不相干的报道连成一大串


def story_tokens(title: str) -> set:
    """标题里的实词，截成前 6 个字母当作简易词根(Israeli / Israelis → israel)。"""
    words = set()
    for w in TOKEN_RE.findall(title):
        w = w.lower().strip("'-")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) >= 3 and w not in STOPWORDS and w[:6] not in STOP_STEMS:
            words.add(w[:6])
    return words


def cluster_stories(items: list) -> None:
    """把不同媒体报道的同一件事归成一组：两篇标题共有至少 2 个少见的词，且发布时间相差不超过 36 小时。
    给报道媒体数 ≥ 2 的文章加上 k(组号)和 n(报道这件事的媒体数)。目前只比较英文文章。"""
    en = [it for it in items if it["lg"] == "en"]
    when = [datetime.fromisoformat(it["p"]).timestamp() for it in en]
    tokens = [story_tokens(it["t"]) for it in en]
    df = Counter(t for ts in tokens for t in ts)
    limit = max(4, len(en) // 40)   # 太常见的词(如 Trump)不算
    index = defaultdict(list)
    for i, ts in enumerate(tokens):
        tokens[i] = {t for t in ts if df[t] <= limit}
        for t in tokens[i]:
            index[t].append(i)

    parent, size = list(range(len(en))), [1] * len(en)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, ts in enumerate(tokens):
        shared = Counter(j for t in ts for j in index[t] if j > i)
        for j, c in shared.items():
            if c < 2 or en[j]["s"] == en[i]["s"] or abs(when[i] - when[j]) > 36 * 3600:
                continue
            a, b = find(i), find(j)
            if a != b and size[a] + size[b] <= MAX_STORY:
                parent[b], size[a] = a, size[a] + size[b]

    groups = defaultdict(list)
    for i in range(len(en)):
        groups[find(i)].append(i)
    for k, members in enumerate(groups.values(), 1):
        n = len({en[i]["s"] for i in members})
        if n >= 2:
            for i in members:
                en[i]["k"], en[i]["n"] = k, n


# ---------------------------------------------------------------- 生成网页

def day_label(d) -> str:
    return f"{d.month}月{d.day}日 周{WEEKDAYS[d.weekday()]}"


def full_date(d) -> str:
    return f"{d.month}月{d.day}日 星期{WEEKDAYS[d.weekday()]}"


def build(cfg: dict, db: sqlite3.Connection, status: list) -> int:
    s = cfg["settings"]
    tz, tzname = resolve_tz(s["timezone"])
    now = datetime.now(timezone.utc)
    sources = {src["id"]: src for src in cfg.get("sources", [])}
    sections, regions = cfg["sections"], cfg["regions"]

    rows = db.execute(
        "SELECT uid, source, title, link, summary, published, section, regions, image, opinion, rank, ranked_at, "
        "lang, title_zh, summary_zh FROM articles WHERE published >= ? ORDER BY published DESC",
        (iso(now - timedelta(days=s["keep_days"])),)).fetchall()
    # 首页"要闻"只参考最近一次抓取时还在各媒体头条 feed 里的文章
    last_ranked = db.execute("SELECT MAX(ranked_at) FROM articles").fetchone()[0]

    by_day, latest = {}, []
    latest_cutoff = iso(now - timedelta(hours=24))
    translated = 0
    for (uid, sid, title, link, summary, published, section, regs, image, opinion, rank, ranked_at,
         lang, title_zh, summary_zh) in rows:
        if sid not in sources:
            continue
        item = {"i": uid, "s": sid, "t": title, "l": link, "d": summary or "", "p": published,
                "c": section if section in sections else DEFAULT_SECTION,
                "g": [r for r in (regs or "").split(",") if r in regions], "lg": lang}
        if image:
            item["m"] = image
        if opinion:
            item["o"] = 1
        if title_zh:
            item["tz"], item["dz"] = title_zh, summary_zh or ""
            translated += 1
        day = datetime.fromisoformat(published).astimezone(tz).date()
        by_day.setdefault(day, []).append(dict(item, r=rank) if rank else dict(item))
        if published >= latest_cutoff:
            latest.append(dict(item, r=rank) if rank and ranked_at == last_ranked else dict(item))

    days = sorted(by_day, reverse=True)
    for page_items in [latest] + list(by_day.values()):
        cluster_stories(page_items)
    base = {
        "generated": iso(now),
        "tz": tzname,
        "days": [{"d": d.isoformat(), "n": len(by_day[d]), "label": day_label(d)} for d in days],
        "sources": [{"id": src["id"], "name": src.get("name", src["id"]), "en": src.get("en", ""),
                     "lang": src.get("lang", "en")}
                    for src in sources.values()],
        "sections": [{"id": k, "name": v} for k, v in sections.items()],
        "regions": [{"id": k, "name": (v or {}).get("name", k)} for k, v in regions.items()],
        "translator": cfg["translate"]["model"] if translated else None,
        "feeds": {"total": len(status), "ok": sum(1 for x in status if x["ok"])},
        "failed": [{"s": x["s"], "url": x["url"], "err": x["err"]} for x in status if not x["ok"]],
    }
    template = TEMPLATE.read_text(encoding="utf-8")
    SITE.mkdir(parents=True, exist_ok=True)

    today = now.astimezone(tz).date()
    write_page(template, SITE / "index.html", dict(
        base, mode="latest", page="index", date=today.isoformat(), heading=full_date(today), items=latest),
        "每日新闻")
    wanted = {"index.html"}
    for d in days:
        name = f"{d.isoformat()}.html"
        wanted.add(name)
        write_page(template, SITE / name, dict(
            base, mode="day", page=d.isoformat(), date=d.isoformat(), heading=full_date(d), items=by_day[d]),
            f"每日新闻 · {d.month}月{d.day}日")
    for f in SITE.iterdir():
        if DAY_PAGE_RE.match(f.name) and f.name not in wanted:
            f.unlink()
    return len(days)


def write_page(template: str, path: Path, data: dict, title: str) -> None:
    payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":"))
               .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    page = template.replace("__PAGE_TITLE__", html.escape(title)).replace("/*__DATA__*/null", payload)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 命令

def cmd_update(args) -> None:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    print(f"抓取 {len(feed_jobs(cfg))} 个 feed …")
    t0 = time.time()
    articles, status = collect(cfg, now)
    names = {src["id"]: src.get("name", src["id"]) for src in cfg["sources"]}
    failed = [x for x in status if not x["ok"]]
    print(f"完成，用时 {time.time() - t0:.0f} 秒：成功 {len(status) - len(failed)}，失败 {len(failed)}")
    for x in failed:
        print(f"  ✗ {names.get(x['s'], x['s'])}  {x['err']}  {x['url']}")
    for x in status:
        if x["ok"] and x["entries"] == 0:
            print(f"  ? {names.get(x['s'], x['s'])}  feed 为空  {x['url']}")

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    db = open_db()
    new = store(db, articles, now, cfg["settings"]["keep_days"])
    total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"本次 {len(articles)} 篇，其中新增 {new} 篇；数据库共 {total} 篇")
    if not args.no_translate:
        translate_pending(cfg, db, now)
    finish_build(cfg, db, status, args)


def cmd_translate(args) -> None:
    cfg = load_config()
    db = open_db()
    translate_pending(cfg, db, datetime.now(timezone.utc))
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else []
    finish_build(cfg, db, status, args)


def cmd_build(args) -> None:
    cfg = load_config()
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else []
    finish_build(cfg, open_db(), status, args)


def finish_build(cfg, db, status, args) -> None:
    n = build(cfg, db, status)
    index = SITE / "index.html"
    print(f"已生成 {index.relative_to(ROOT)} 和 {n} 个日期页面")
    if getattr(args, "open", False):
        webbrowser.open(index.as_uri())


def cmd_serve(args) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    with http.server.ThreadingHTTPServer((host, args.port), handler) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        print(f"网页已在 {url} 运行，按 Ctrl+C 停止")
        if not args.no_open:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="每日新闻：抓取各大新闻网站并生成分类网页")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update", help="抓取新文章、翻译并生成网页")
    u.add_argument("--open", action="store_true", help="完成后用浏览器打开")
    u.add_argument("--no-translate", action="store_true", help="这次不翻译")
    u.set_defaults(func=cmd_update)
    t = sub.add_parser("translate", help="只翻译还没翻译的英文新闻")
    t.add_argument("--open", action="store_true", help="完成后用浏览器打开")
    t.set_defaults(func=cmd_translate)
    b = sub.add_parser("build", help="只重新生成网页")
    b.add_argument("--open", action="store_true", help="完成后用浏览器打开")
    b.set_defaults(func=cmd_build)
    sv = sub.add_parser("serve", help="本地网页服务器")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--lan", action="store_true", help="允许同一局域网的设备访问")
    sv.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    sv.set_defaults(func=cmd_serve)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
