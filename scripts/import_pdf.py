"""
开发书籍 PDF 导入：PDF -> 文本提取 -> 清洗 -> 结构感知切块 -> bge-m3 向量化 -> Milvus

用法（在 recipe-back-end 目录下）：
    .venv/bin/python scripts/import_pdf.py --make-demo-pdf                          # 生成演示 PDF
    .venv/bin/python scripts/import_pdf.py --pdf data/demo_dev_book.pdf --dry-run   # 只跑清洗切块看效果
    .venv/bin/python scripts/import_pdf.py --pdf data/js_red_book.pdf --title "JavaScript高级程序设计"
    .venv/bin/python scripts/import_pdf.py --pdf xxx.pdf --fresh                    # 清集合重导

PDF 清洗三板斧（企业落地常做的）：
1. 页眉页脚剔除：跨页统计每页首行/尾行，出现频率 >=50% 的判定为页眉页脚（书名、页码）
2. 断行修复：连字符断词（asyn-\\nchronous）接回；中文断行直接拼、英文断行补空格
3. 目录页过滤：点线（……）占比高的页判定为目录页跳过——目录切出来的块是无意义碎片

切块策略：结构感知 > 固定长度硬切
- 先按标题正则（第X章 / 1.1 / 1.1.1）归组段落，再按段落贪心装箱
- 块目标 500 字、相邻块重叠 80 字：块太小语义不完整，太大稀释相关性且逼近 embedding 窗口；
  重叠是为了防止关键句正好被切在边界上
- 每块拼"书名 + 章节"作上下文前缀——块脱离原文位置后仍自包含，检索命中率明显提升
  （企业里更进一步的做法是 HyDE / 上下文检索：让 LLM 给每块生成一句背景说明再向量化）

扫描件说明：本脚本走文本层提取（PyMuPDF），扫描件需要先 OCR（PaddleOCR / 云厂商文档解析），
企业里更重的版式还原（表格、双栏、公式）用 MinerU / unstructured，口径见面试手册 §17。
"""
import argparse
import os
import re
import statistics
import sys

import pymupdf as fitz  # PyMuPDF

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402
from scripts.ingest_common import (EMBED_WORKERS, Embedder, RateLimiter,  # noqa: E402
                                   ensure_collection, milvus_client,
                                   stable_id, upsert_batches)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEMO_PDF_PATH = os.path.join(DATA_DIR, "demo_dev_book.pdf")
EMBED_CACHE_PATH = os.path.join(DATA_DIR, "embeddings_cache_pdf.pkl")

CHUNK_MAX_CHARS = 500    # 块目标长度：兼顾语义完整性和相关性密度
CHUNK_OVERLAP = 80       # 相邻块重叠字符数：防边界句被截断
RUNNING_LINE_RATIO = 0.5 # 首/尾行在多少比例的页面上重复出现，判定为页眉页脚

CHAPTER_RE = re.compile(r"^第[0-9一二三四五六七八九十百零]+[章节篇部]\s*\S*")
SECTION_RE = re.compile(r"^\d+(\.\d+){1,3}\s*\S+")
HEADING_RE = re.compile(f"({CHAPTER_RE.pattern}|{SECTION_RE.pattern})")
CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")
PAGE_NUM_RE = re.compile(r"^[-—–\s]*\d{1,4}[-—–\s]*$")   # 纯页码行（"- 12 -"），页码每页不同，靠模式而非频率识别


def _is_heading(line: str) -> bool:
    return len(line) <= 50 and bool(HEADING_RE.match(line))


def _is_cjk(ch: str) -> bool:
    return bool(CJK_RE.match(ch))


# ---------- 1. 文本提取 ----------

def extract_pages(pdf_path: str) -> list[str]:
    """按页提取文本层内容"""
    doc = fitz.open(pdf_path)
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return pages


def is_toc_page(text: str) -> bool:
    """目录页特征：大量以点线结尾的行（'1.1 历史 …… 3'），切成块是无意义碎片"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    dotted = sum(1 for l in lines if re.search(r"[.…·]{4,}\s*\d+\s*$", l))
    return dotted / len(lines) > 0.5 or (lines and re.fullmatch(r"(目\s*录|contents)", lines[0], re.I))


# ---------- 2. 清洗 ----------

def detect_running_lines(pages: list[str], ratio: float = RUNNING_LINE_RATIO) -> set[str]:
    """统计每页首行/尾行，跨页高频重复的判定为页眉页脚（书名、页码、章节名）"""
    from collections import Counter
    counter: Counter = Counter()
    for text in pages:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for edge in (lines[:1] + lines[-1:]):   # 每页只取首行和尾行，set 语义防同页重复计数
            if edge and len(edge) <= 40:
                counter[edge] += 1
    threshold = max(2, int(len(pages) * ratio))
    return {line for line, cnt in counter.items() if cnt >= threshold}


def merge_lines(lines: list[str]) -> list[str]:
    """把页内物理行合并成逻辑段落：空行分段；连字符断词接回；中文断行直拼、英文补空格"""
    paras, buf = [], ""
    for line in lines:
        line = line.strip()
        if not line:
            if buf:
                paras.append(buf)
                buf = ""
            continue
        if _is_heading(line):
            if buf:                         # 标题必须独立成段，否则会被并进正文丢失结构信息
                paras.append(buf)
                buf = ""
            paras.append(line)
            continue
        if not buf:
            buf = line
        elif buf.endswith("-") and len(buf) >= 2 and buf[-2].isalpha() and line[0].islower():
            buf = buf[:-1] + line           # asyn- + chronous -> asynchronous
        elif _is_cjk(buf[-1]) or _is_cjk(line[0]):
            buf += line                     # 中文断行没有空格
        else:
            buf += " " + line
    if buf:
        paras.append(buf)
    return paras


def clean_pages(pages: list[str], skip_toc: bool = True) -> tuple[list[dict], dict]:
    """整本清洗：目录过滤 -> 页眉页脚剔除 -> 断行合并。返回 (段落列表[{page, text}], 统计)"""
    running = detect_running_lines(pages)
    stats = {"总页数": len(pages), "目录页跳过": 0, "页眉页脚剔除": len(running),
             "页眉页脚内容": sorted(running)}
    paras = []
    for page_no, text in enumerate(pages, start=1):
        if skip_toc and is_toc_page(text):
            stats["目录页跳过"] += 1
            continue
        lines = [l for l in text.splitlines()
                 if l.strip() and l.strip() not in running and not PAGE_NUM_RE.match(l.strip())]
        for p in merge_lines(lines):
            paras.append({"page": page_no, "text": p})
    stats["清洗后段落数"] = len(paras)
    return paras, stats


# ---------- 3. 结构感知切块 ----------

def _sentence_tail(text: str, limit: int) -> str:
    """取文本尾部 limit 字内的完整句子作为重叠内容——重叠块从半句话开始会污染语义"""
    tail = text[-limit:]
    m = re.search(r"[。！？!?；;]\s*", tail)
    return tail[m.end():] if m else tail


def pack_chunks(paras: list[dict], max_chars: int = CHUNK_MAX_CHARS,
                overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """按标题层级（章 > 节）归组段落，再贪心装箱：块不超过 max_chars，相邻块重叠 overlap 字"""
    chunks, chapter, section, buf, buf_pages = [], "", "", [], []

    def heading() -> str:
        return " > ".join(x for x in (chapter, section) if x)

    def seal():
        if buf:
            chunks.append({"heading": heading(), "text": "\n".join(buf),
                           "page_start": buf_pages[0], "page_end": buf_pages[-1]})

    for para in paras:
        text, page = para["text"], para["page"]
        if _is_heading(text):               # 新标题：封存当前块，切换章节上下文
            seal()
            buf, buf_pages = [], []
            if CHAPTER_RE.match(text):
                chapter, section = text, ""
            else:
                section = text
            continue
        candidate = "\n".join(buf + [text])
        if buf and len(candidate) > max_chars:
            seal()
            tail = _sentence_tail(buf[-1], overlap) if overlap else ""  # 带前一块的尾巴进下一块
            buf = [tail, text] if tail else [text]
            buf_pages = ([buf_pages[-1], page] if tail else [page])
        else:
            buf.append(text)
            buf_pages.append(page)
        # 单段超长的兜底硬切（代码块/长段落）
        while sum(len(t) for t in buf) > max_chars * 2:
            joined = "\n".join(buf)
            chunks.append({"heading": heading(), "text": joined[:max_chars],
                           "page_start": buf_pages[0], "page_end": buf_pages[-1]})
            tail = joined[max_chars - overlap:max_chars]
            buf, buf_pages = [tail + joined[max_chars:]], [buf_pages[-1]]
    seal()
    return chunks


def build_embed_text(chunk: dict, title: str) -> str:
    """书名 + 章节作上下文前缀，块脱离原文位置后仍自包含"""
    prefix = f"《{title}》{chunk['heading']}" if chunk["heading"] else f"《{title}》"
    return f"{prefix}\n{chunk['text']}"


# ---------- 4. 演示 PDF 生成 ----------

def make_demo_pdf(path: str):
    """生成一份带页眉页脚、目录页、连字符断词的演示 PDF，用于无版权风险地跑通全链路"""
    HEADER, FONT = "JavaScript 高级程序设计 · 演示版", "china-s"
    chapters = [
        ("第1章 事件循环", [
            ("1.1 宏任务与微任务",
             "JavaScript 是单线程语言，通过事件循环调度任务。每轮循环从宏任务队列取一个任务执行，"
             "执行完毕后清空当前所有微任务，再进行页面渲染。常见的宏任务有 setTimeout、setInterval 和 "
             "I/O 回调，微任务有 Promise.then 和 queueMicrotask。理解这个顺序是分析 asyn-\nchronous "
             "代码执行结果的基础。\n需要注意的是，await 后面的代码等价于放在微任务中执行。"),
            ("1.2 常见面试题",
             "setTimeout 和 Promise 混用时，先执行同步代码，再清空微任务队列，最后执行宏任务。"
             "例如 Promise.resolve().then 永远排在 setTimeout(fn, 0) 之前。"),
        ]),
        ("第2章 闭包与作用域", [
            ("2.1 什么是闭包",
             "闭包是指函数能够记住并访问其词法作用域，即使这个函数在其词法作用域之外执行。"
             "闭包的常见用途包括数据私有化、柯里化和回调状态保持。过度使用闭包会导致内存无法释放，"
             "因为被引用的外层变量一直存活。\n在循环中使用 var 声明计数器是闭包的经典陷阱，"
             "所有回调共享同一个变量，改用 let 可以为每次迭代创建独立绑定。"),
        ]),
        ("第3章 原型与继承", [
            ("3.1 原型链",
             "每个对象都有一个内部指针指向其原型对象，原型对象又有自己的原型，层层向上直到 null，"
             "这条链就是原型链。读取属性时沿链查找，找到即返回。hasOwnProperty 可以区分属性来自"
             "实例自身还是原型链。\nclass 语法本质上是原型继承的语法糖，extends 和 super 最终都"
             "编译为原型操作。"),
        ]),
    ]
    doc = fitz.open()

    def new_page():
        page = doc.new_page()
        page.insert_text((40, 30), HEADER, fontname=FONT, fontsize=9)  # 页眉
        return page

    toc = new_page()
    toc.insert_text((40, 80), "目  录", fontname=FONT, fontsize=18)
    y = 130
    for ch, secs in chapters:
        toc.insert_text((40, y), f"{ch} .............. 2", fontname=FONT, fontsize=11); y += 22
        for sec, _ in secs:
            toc.insert_text((60, y), f"{sec} .............. 3", fontname=FONT, fontsize=10); y += 20

    for ch, secs in chapters:
        for sec, body in secs:
            page = new_page()
            page.insert_text((40, 70), ch, fontname=FONT, fontsize=16)
            page.insert_text((40, 100), sec, fontname=FONT, fontsize=13)
            page.insert_textbox(fitz.Rect(40, 120, 555, 780), body,
                                fontname=FONT, fontsize=11, lineheight=1.6)
    for i, page in enumerate(doc, start=1):
        page.insert_text((280, 820), f"- {i} -", fontname=FONT, fontsize=9)  # 页脚页码
    doc.save(path)
    print(f"✅ 演示 PDF 已生成：{path}（{len(doc)} 页）")


# ---------- 主流程 ----------

def main():
    parser = argparse.ArgumentParser(description="开发书籍 PDF 导入 Milvus")
    parser.add_argument("--pdf", help="PDF 文件路径")
    parser.add_argument("--title", default="", help="书名（默认取文件名），用于块上下文前缀")
    parser.add_argument("--collection", default="dev_book_chunks", help="Milvus 集合名")
    parser.add_argument("--fresh", action="store_true", help="清空集合后重新导入（不影响 embedding 缓存）")
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 块（0 或负数表示全量）")
    parser.add_argument("--keep-toc", action="store_true", help="不过滤目录页")
    parser.add_argument("--dry-run", action="store_true", help="只跑清洗切块，打印统计和样本，不调向量/不写库")
    parser.add_argument("--tpm", type=int, default=2_000_000, help="初始 TPM 限流值")
    parser.add_argument("--make-demo-pdf", action="store_true", help=f"生成演示 PDF 到 {DEMO_PDF_PATH}")
    args = parser.parse_args()

    if args.make_demo_pdf:
        make_demo_pdf(DEMO_PDF_PATH)
        if not args.pdf:
            return
    pdf_path = args.pdf or DEMO_PDF_PATH
    title = args.title or os.path.splitext(os.path.basename(pdf_path))[0]
    doc_id = os.path.basename(pdf_path)

    print(f"📖 解析 {pdf_path} …")
    pages = extract_pages(pdf_path)
    paras, stats = clean_pages(pages, skip_toc=not args.keep_toc)
    for k, v in stats.items():
        print(f"   {k}: {v}")

    chunks = pack_chunks(paras)
    if args.limit > 0:
        chunks = chunks[:args.limit]
    lens = [len(c["text"]) for c in chunks]
    print(f"🧩 切块完成：{len(chunks)} 块，长度 min/avg/max = "
          f"{min(lens)}/{int(statistics.mean(lens))}/{max(lens)}")

    if args.dry_run:
        print("\n🔍 切块样本（前 2 块，含上下文前缀）：")
        for c in chunks[:2]:
            print("─" * 60)
            print(build_embed_text(c, title))
        print(f"\n✅ dry-run 完成（未向量化、未写库）")
        return

    rows = []
    for i, c in enumerate(chunks):
        rows.append({
            # 主键 = hash(文档 + 块序号)，重跑幂等；同书换版重新切块，旧块靠 --fresh 或按 doc_id 删除
            "id": stable_id(f"{doc_id}:{i}"),
            "doc_id": doc_id,
            "title": title,
            "heading": c["heading"],
            "chunk_index": i,
            "page_start": c["page_start"],
            "page_end": c["page_end"],
            "text": c["text"],
            "_embed": build_embed_text(c, title),
        })
    print(f"🔢 调用硅基流动 {settings.EMBEDDING_MODEL} 向量化（{EMBED_WORKERS} 线程并发）…")
    vectors = Embedder(RateLimiter(args.tpm), EMBED_CACHE_PATH).embed_all(
        [{"cid": r["id"], "text": r.pop("_embed")} for r in rows])
    for r in rows:
        r["vector"] = vectors[r["id"]]

    client = milvus_client()
    ensure_collection(client, args.collection, settings.MILVUS_EMBEDDING_DIM, args.fresh)
    upsert_batches(client, args.collection, rows)
    print(f"🎉 完成：{len(rows)} 块导入 {args.collection}")


if __name__ == "__main__":
    main()
