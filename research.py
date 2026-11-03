"""Deep research: ask a big question, Kakarot searches many sources, reads them, cross-checks what they say
and writes a report with numbered sources - in the background, as a mission.

Steps (each one is logged in the Missions tab):
1. Plan    - split the question into sub-questions and search queries (English, and Bangla when it helps).
2. Search  - run every query, merge and rank the results (pages found by several queries rank higher).
3. Read    - download each source and keep its readable text (falls back to a real browser for JS pages).
4. Notes   - pull out the facts that answer the sub-questions, with a short quote and the page's date.
5. Gaps    - in deep mode, search again for sub-questions that only one source covers.
6. Check   - per sub-question: what several sources agree on, where they disagree, what is uncertain.
7. Report  - summary first, then the details, every claim with [n] sources; saved as Markdown + Word.
"""
import datetime
import json
import logging
import os
import re
from urllib.parse import urlparse

import requests

import db
from config import CFG
from tools import S, UA, ROOTS, tool

log = logging.getLogger("kakarot.research")

SKIP_HOSTS = re.compile(r"(^|\.)(youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com|pinterest\.|"
                        r"x\.com|twitter\.com|linkedin\.com|quora\.com|reddit\.com/user)", re.I)

PLAN_PROMPT = """You are a careful research planner. The user's research question:
\"\"\"{question}\"\"\"
Today is {today}.

Reply ONLY with JSON:
{{"title": "short report title",
  "language": "the language the user wrote in (e.g. English, Bangla)",
  "sub_questions": ["{n_sub} specific sub-questions that together answer the question"],
  "queries": [{{"q": "web search words", "for": index of the sub-question (0-based)}}]}}
Rules: {n_q} queries in total, short and specific (like a person types into Google). Use different angles (facts,
numbers, recent news {year}, official sources, criticism/problems). If the topic is about Bangladesh or the
question is in Bangla, also add some queries written in Bangla. Never answer the question here."""

NOTES_PROMPT = """Research question: {question}
Sub-questions:
{subs}

SOURCE [{sid}] {title} ({host})
\"\"\"
{text}
\"\"\"

Extract only what THIS source actually says that helps answer the sub-questions. Do not add your own knowledge.
Reply ONLY with JSON:
{{"relevant": true or false,
  "date": "publication date if the text shows one, else empty",
  "kind": "official / news / research / company / blog / forum / other",
  "facts": [{{"sub": sub-question index, "claim": "one fact in your own words, with numbers and dates",
             "quote": "the exact short phrase from the source that supports it (max 25 words)"}}]}}
At most 8 facts. If the source is off-topic, return relevant false and no facts."""

CHECK_PROMPT = """Sub-question: {sub}

Facts found, each with its source number:
{facts}

Cross-check them. Reply ONLY with JSON:
{{"answer": "2-5 sentence answer to the sub-question using ONLY these facts, citing sources like [1][3]",
  "agree": ["facts that 2 or more sources support, with their [n]"],
  "disagree": ["where sources contradict each other: who says what, with [n]"],
  "uncertain": ["what only one weak source says, or what is still unknown"]}}"""

REPORT_PROMPT = """Write a research report in {language} for this question:
\"\"\"{question}\"\"\"

Checked findings per sub-question (sources cited as [n]):
{findings}

Sources: {source_list}

Write simple markdown:
# {title}
## Summary
- 4 to 8 bullets with the most important answers, each with its [n] sources
## then one section per sub-question (## heading) with the details and [n] citations
## Where sources disagree   (only if they do)
## What is still uncertain
Rules: use ONLY the findings above, never invent facts, numbers or sources. Keep the [n] numbers exactly as given.
Do not write the source list yourself (it is added after your text)."""


# ---------------------------------------------------------------- small helpers
def _json(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("no JSON in answer")
    return json.loads(m.group(0))


def _ask(prompt, smart=False, fmt="json"):
    import agent
    return agent.think_once(prompt, fmt=fmt, smart=smart)


def _smart_ok():
    import agent
    if not CFG.get("research_smart", True):
        return False
    try:
        # the big brain only when it fits the graphics card - otherwise swapping brains makes research much slower
        return agent._has(CFG.get("smart_model", "qwen3:14b"), agent.installed_models()) and not agent.smart_slow()
    except Exception:
        return False


def is_bangla(text):
    return len(re.findall(r"[ঀ-৿]", text or "")) > 3


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:50]
    return s or "research"


def search(query, n=8):
    """Web search -> [{'title','url','snippet'}]. DuckDuckGo, like web_search."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    region = "bd-en" if is_bangla(query) else CFG.get("research_region", "wt-wt")
    try:
        res = list(DDGS().text(query, region=region, max_results=n))
    except TypeError:
        res = list(DDGS().text(query, max_results=n))
    out = []
    for r in res:
        url = r.get("href") or r.get("url") or ""
        if url.startswith("http"):
            out.append({"title": (r.get("title") or "").strip(), "url": url, "snippet": (r.get("body") or "").strip()})
    return out


def _main_text(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for key in ("article:published_time", "og:updated_time", "datePublished", "date", "pubdate"):
        t = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key}) or \
            soup.find("meta", attrs={"itemprop": key})
        if t and t.get("content"):
            meta["date"] = t["content"][:10]
            break
    if not meta.get("date"):
        tm = soup.find("time")
        if tm and (tm.get("datetime") or "").strip():
            meta["date"] = tm["datetime"][:10]
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "td", "blockquote"]):
        t = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        if len(t) > 30 or (el.name.startswith("h") and len(t) > 3):
            blocks.append(t)
    text = "\n".join(dict.fromkeys(blocks))
    if len(text) < 400:
        text = re.sub(r"\n\s*\n+", "\n", root.get_text("\n")).strip()
    return title, text, meta


def read(url, limit=14000):
    """Download a page and return (title, text, meta). Uses a real browser when the page needs JavaScript."""
    try:
        r = requests.get(url, headers=UA, timeout=25)
        ctype = r.headers.get("content-type", "")
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            import io
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(r.content))
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:20])
            return url.rsplit("/", 1)[-1], text[:limit], {}
        r.raise_for_status()
        title, text, meta = _main_text(r.text)
    except Exception as e:
        title, text, meta = "", "", {"error": str(e)[:120]}
    if len(text) < 500:
        try:
            import tools
            t2, body, _ = tools._browse(url, wait_seconds=3)
            if len(body) > len(text):
                title, text = t2 or title, body
        except Exception as e:
            meta.setdefault("error", str(e)[:120])
    return title, text[:limit], meta


def rank(results_by_query):
    """Merge search results: pages found by several queries and near the top rank first; one per URL,
    max 2 per website so one site can't dominate."""
    score, info = {}, {}
    for qi, (sub, results) in enumerate(results_by_query):
        for pos, r in enumerate(results):
            u = r["url"].split("#")[0]
            host = urlparse(u).hostname or ""
            if SKIP_HOSTS.search(host):
                continue
            score[u] = score.get(u, 0) + 1.0 / (1 + pos * 0.35)
            it = info.setdefault(u, {**r, "url": u, "host": host.replace("www.", ""), "subs": set()})
            it["subs"].add(sub)
    order = sorted(score, key=lambda u: -score[u])
    out, per_host = [], {}
    for u in order:
        h = info[u]["host"]
        if per_host.get(h, 0) >= 2:
            continue
        per_host[h] = per_host.get(h, 0) + 1
        out.append(info[u])
    return out


def _chunks(text, size=5500):
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) > size and cur:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def fix_citations(text, valid):
    """Remove [n] citations that point to sources that don't exist."""
    def repl(m):
        nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
        keep = [n for n in nums if n in valid]
        return "".join(f"[{n}]" for n in keep)
    return re.sub(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]", repl, text)


# ---------------------------------------------------------------- the mission
class _Job:
    def __init__(self, mid):
        import missions
        self.m, self.mid = missions, mid
        row = missions._get(mid)
        self.goal, self.plan, self.log = row["goal"], row["plan"], row["log"]
        self.data = json.loads(db.q("SELECT data FROM missions WHERE id=?", (mid,))[0]["data"] or "{}")

    def done(self, step, result, ok=True):
        self.log.append({"step": step, "ok": ok, "result": result, "actions": []})
        self.m._save(self.mid, log=self.log)

    def has(self, step):
        return any(e["step"] == step for e in self.log)

    def save_data(self):
        db.x("UPDATE missions SET data=? WHERE id=?", (json.dumps(self.data, ensure_ascii=False), self.mid))

    def cancelled(self):
        return (self.m._get(self.mid) or {}).get("status") == "cancelled"


STEPS = ["Plan the research", "Search the web", "Read the sources", "Take notes", "Fill the gaps",
         "Cross-check the findings", "Write the report"]


def run(mid):
    import missions
    job = _Job(mid)
    missions._save(mid, status="running")
    if not job.plan:
        job.plan = STEPS
        missions._save(mid, plan=STEPS)
    d = job.data
    q = d["question"]
    deep = d.get("depth") == "deep"
    smart = _smart_ok()
    today = datetime.date.today()
    # 1. plan
    if not job.has(STEPS[0]):
        n_sub, n_q = ("5-7", "12-16") if deep else ("3-5", "8-10")
        try:
            plan = _json(_ask(PLAN_PROMPT.format(question=q, today=today.isoformat(), year=today.year,
                                                 n_sub=n_sub, n_q=n_q), smart=smart))
        except Exception:
            plan = {}
        subs = [str(s) for s in plan.get("sub_questions") or [] if str(s).strip()][:8] or [q]
        queries = []
        for x in plan.get("queries") or []:
            if isinstance(x, dict) and str(x.get("q", "")).strip():
                try:
                    sub = max(0, min(int(x.get("for", 0)), len(subs) - 1))
                except (TypeError, ValueError):
                    sub = 0
                queries.append({"q": str(x["q"]).strip(), "for": sub})
            elif isinstance(x, str) and x.strip():
                queries.append({"q": x.strip(), "for": 0})
        if not queries:
            queries = [{"q": s, "for": i} for i, s in enumerate(subs)]
        d.update(title=plan.get("title") or q[:80], language=plan.get("language") or ("Bangla" if is_bangla(q)
                                                                                     else "English"),
                 subs=subs, queries=queries[:18])
        job.save_data()
        job.done(STEPS[0], "Sub-questions:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(subs)) +
                 f"\n\n{len(d['queries'])} searches planned.")
    if job.cancelled():
        return
    # 2. search
    if not job.has(STEPS[1]):
        per = []
        for x in d["queries"]:
            try:
                per.append((x["for"], search(x["q"], 8)))
            except Exception as e:
                log.warning("search failed %s: %s", x["q"], e)
        ranked = rank(per)
        limit = int(CFG.get("research_sources_deep" if deep else "research_sources", 16 if deep else 9))
        d["candidates"] = [{**r, "subs": sorted(r["subs"])} for r in ranked[:limit * 2]]
        job.save_data()
        job.done(STEPS[1], f"{sum(len(r) for _, r in per)} results from {len(per)} searches → "
                           f"{len(ranked)} different pages. Best: " +
                 ", ".join(r["host"] for r in ranked[:8]), ok=bool(ranked))
        if not ranked:
            return _finish(job, "I couldn't find anything on the web for this question (is the internet working?).")
    # 3. read
    if not job.has(STEPS[2]):
        limit = int(CFG.get("research_sources_deep" if deep else "research_sources", 16 if deep else 9))
        sources = []
        for c in d["candidates"]:
            if len(sources) >= limit or job.cancelled():
                break
            title, text, meta = read(c["url"])
            if len(text) < 300:
                continue
            sources.append({"id": len(sources) + 1, "url": c["url"], "host": c["host"], "title": title or c["title"],
                            "date": meta.get("date", ""), "subs": c["subs"], "text": text})
        d["sources"] = sources
        job.save_data()
        job.done(STEPS[2], f"Read {len(sources)} sources: " + ", ".join(f"[{s['id']}] {s['host']}" for s in sources),
                 ok=bool(sources))
        if not sources:
            return _finish(job, "The pages I found couldn't be opened, so there is nothing reliable to report.")
    # 4. notes
    subs_txt = "\n".join(f"{i}. {s}" for i, s in enumerate(d["subs"]))
    if not job.has(STEPS[3]):
        notes = []
        for s in d["sources"]:
            if job.cancelled():
                return
            for part in _chunks(s["text"])[:2]:
                try:
                    r = _json(_ask(NOTES_PROMPT.format(question=q, subs=subs_txt, sid=s["id"], title=s["title"][:120],
                                                       host=s["host"], text=part)))
                except Exception:
                    continue
                if r.get("date") and not s.get("date"):
                    s["date"] = str(r["date"])[:20]
                s["kind"] = r.get("kind") or s.get("kind", "")
                if r.get("relevant") is False:
                    continue
                for f in (r.get("facts") or [])[:8]:
                    if isinstance(f, dict) and str(f.get("claim", "")).strip():
                        try:
                            sub = max(0, min(int(f.get("sub", 0)), len(d["subs"]) - 1))
                        except (TypeError, ValueError):
                            sub = 0
                        notes.append({"src": s["id"], "sub": sub, "claim": str(f["claim"])[:400],
                                      "quote": str(f.get("quote", ""))[:220]})
        d["notes"] = notes
        for s in d["sources"]:
            s["text"] = s["text"][:3000]  # keep the mission data small after reading
        job.save_data()
        used = sorted({n["src"] for n in notes})
        job.done(STEPS[3], f"{len(notes)} facts from {len(used)} sources.", ok=bool(notes))
    # 5. gaps (deep mode only)
    if not job.has(STEPS[4]):
        thin = [i for i in range(len(d["subs"])) if len({n["src"] for n in d["notes"] if n["sub"] == i}) < 2]
        added = 0
        if deep and thin:
            have = {s["url"] for s in d["sources"]}
            for i in thin[:3]:
                for r in search(d["subs"][i], 6)[:4]:
                    u = r["url"].split("#")[0]
                    host = (urlparse(u).hostname or "").replace("www.", "")
                    if u in have or SKIP_HOSTS.search(host):
                        continue
                    title, text, meta = read(u)
                    if len(text) < 300:
                        continue
                    sid = len(d["sources"]) + 1
                    d["sources"].append({"id": sid, "url": u, "host": host, "title": title or r["title"],
                                         "date": meta.get("date", ""), "subs": [i], "text": text[:3000]})
                    have.add(u)
                    try:
                        rr = _json(_ask(NOTES_PROMPT.format(question=q, subs=subs_txt, sid=sid, title=title[:120],
                                                            host=host, text=text[:5500])))
                        for f in (rr.get("facts") or [])[:8]:
                            if isinstance(f, dict) and str(f.get("claim", "")).strip():
                                d["notes"].append({"src": sid, "sub": i, "claim": str(f["claim"])[:400],
                                                   "quote": str(f.get("quote", ""))[:220]})
                    except Exception:
                        pass
                    added += 1
                    break
            job.save_data()
        job.done(STEPS[4], (f"Searched again for {len(thin)} thin part(s); added {added} source(s)." if deep else
                            "Skipped (normal depth)." if thin else "Every part has 2+ sources."))
    # 6. cross-check
    if not job.has(STEPS[5]):
        findings = []
        for i, sub in enumerate(d["subs"]):
            facts = [n for n in d["notes"] if n["sub"] == i]
            if not facts:
                findings.append({"sub": sub, "answer": "", "agree": [], "disagree": [],
                                 "uncertain": ["No source answered this part."]})
                continue
            ftxt = "\n".join(f"[{n['src']}] {n['claim']}" + (f" (\"{n['quote']}\")" if n["quote"] else "")
                             for n in facts[:30])
            try:
                c = _json(_ask(CHECK_PROMPT.format(sub=sub, facts=ftxt), smart=smart))
            except Exception:
                c = {"answer": " ".join(f"{n['claim']} [{n['src']}]" for n in facts[:6]), "agree": [],
                     "disagree": [], "uncertain": []}
            c["sub"] = sub
            findings.append(c)
        d["findings"] = findings
        job.save_data()
        conflicts = sum(len(f.get("disagree") or []) for f in findings)
        job.done(STEPS[5], f"Checked {len(findings)} parts; {conflicts} disagreement(s) between sources.")
    # 7. report
    _report(job, smart)


def _report(job, smart):
    d = job.data
    valid = {s["id"] for s in d["sources"]}
    ftxt = ""
    for f in d["findings"]:
        ftxt += f"\n### {f['sub']}\nAnswer: {f.get('answer', '')}\n"
        for k, lab in (("agree", "Agreed"), ("disagree", "Disagree"), ("uncertain", "Uncertain")):
            for x in f.get(k) or []:
                ftxt += f"- {lab}: {x}\n"
    src_short = "; ".join(f"[{s['id']}] {s['host']}" for s in d["sources"])
    try:
        body = _ask(REPORT_PROMPT.format(language=d.get("language", "English"), question=d["question"],
                                         findings=ftxt[:14000], source_list=src_short, title=d["title"]),
                    smart=smart, fmt=None)
    except Exception as e:
        body = f"# {d['title']}\n\n(The writing step failed: {e})\n\n{ftxt}"
    body = fix_citations(body.strip(), valid)
    if not body.lstrip().startswith("#"):
        body = f"# {d['title']}\n\n" + body
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    lines = ["", "## Sources"]
    for s in d["sources"]:
        mark = "" if s["id"] in cited else " (read, not cited)"
        lines.append(f"{s['id']}. {s['title'][:110] or s['host']} - {s['host']}"
                     f"{', ' + s['date'] if s.get('date') else ''} - {s['url']}{mark}")
    lines.append(f"\n_Researched by Kakarot on {datetime.date.today().isoformat()} from {len(d['sources'])} sources. "
                 f"Check important facts in the sources before you rely on them._")
    report = body + "\n" + "\n".join(lines)
    folder = os.path.join(ROOTS[0], "reports")
    os.makedirs(folder, exist_ok=True)
    stem = f"research-{_slug(d['title'])}-{datetime.datetime.now():%Y%m%d-%H%M}"
    md = os.path.join(folder, stem + ".md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(report)
    files = [md]
    try:
        import office
        office.create_word(os.path.join(folder, stem + ".docx"), "", report)
        files.append(os.path.join(folder, stem + ".docx"))
    except Exception as e:
        log.warning("word report failed: %s", e)
    job.done(STEPS[6], "Saved: " + ", ".join(files))
    summary = re.search(r"##[^\n]*\n(.*?)(\n## |\Z)", body, re.S)
    short = (summary.group(1).strip() if summary else body[:1500])[:2500]
    _finish(job, f"🔎 {d['title']}\n\n{short}\n\nFull report with {len(d['sources'])} sources:\n" + "\n".join(files))


def _finish(job, text):
    import missions
    missions._save(job.mid, status="done", result=text)
    missions.deliver(job.mid, f"🔎 Research done: {job.data.get('title') or job.goal[:60]}", text)
    try:
        import scheduler
        scheduler.notify("Research done", (job.data.get("title") or job.goal)[:120])
    except Exception:
        pass


# ---------------------------------------------------------------- tool for the assistant
@tool("deep_research",
      "DEEP RESEARCH on any question (markets, prices, laws, products, competitors, news, history, how-to): plans "
      "sub-questions, searches many websites (English and Bangla), reads them, cross-checks what they say and writes a "
      "report with numbered sources (Word + Markdown) in the background. Use for questions that need more than one "
      "quick search. The report is posted back into this chat when it's done.",
      {"question": S("The full research question, in the user's words plus any details from the chat."),
       "depth": S("'normal' (about 9 sources, ~10-20 min) or 'deep' (about 16 sources + a second search round).")},
      ["question"])
def deep_research(question, depth="normal"):
    import missions
    depth = "deep" if str(depth).lower().startswith("deep") else "normal"
    mid = missions.start(f"Research: {question[:150]}", kind="research",
                         data={"question": question.strip(), "depth": depth})
    n = 16 if depth == "deep" else 9
    return (f"Started researching it (Mission #{mid}, {depth}): I'll search the web, read about {n} sources, cross-check "
            f"them and write a report with sources. The report will appear right here in this chat when it's done (progress is in the Missions tab).")
