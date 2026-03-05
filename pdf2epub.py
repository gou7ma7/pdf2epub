#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pdf2epub.py

PDF -> EPUB (中文书/手册友好)，可控的去噪（页眉/页脚/广告段落）+ 保留图片（不 OCR）。

特点：
- 抽取：PyMuPDF get_text("dict")（不追求完美排版，优先不丢字）
- 去噪：
  - 页眉/页脚：强重复 + 区域(top/bottom) 规则
  - 广告段落：短段落 + 关键词/regex +（可选）重复统计
- 交互确认：默认先展示候选删除项，你确认后才会从输出中移除
- 输出：EbookLib 生成 EPUB（保守分章：优先“第X章/节”模式，字号仅辅助）

依赖：见 requirements.txt

用法（示例）：
  python pdf2epub.py --pdf input.pdf --out out.epub --rules rules.yaml

第一次跑：
  1) 复制 rules.example.yaml -> rules.yaml
  2) 跑脚本，按提示确认删除项

注意：
- 本脚本不会修改原 PDF。
- 若不想交互，设置 rules.yaml: approval.interactive=false
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import yaml
from ebooklib import epub


# -------------------------
# Models
# -------------------------

@dataclass
class Span:
    text: str
    size: float
    font: str
    flags: int
    color: int


@dataclass
class Para:
    page_index: int
    bbox: Tuple[float, float, float, float]  # x0,y0,x1,y1
    text: str
    spans: List[Span]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def y1(self) -> float:
        return self.bbox[3]


@dataclass
class RemovalCandidate:
    kind: str  # header_footer_repeat | ad_pattern | ad_repeat
    page_index: int
    bbox: Tuple[float, float, float, float]
    text: str
    norm: str
    score: float
    reasons: List[str]
    approved: bool = False


# -------------------------
# Config
# -------------------------

DEFAULT_RULES = {
    "regions": {
        "top_ratio": 0.25,
        "bottom_ratio": 0.25,
        "allow_midpage_high_confidence": True,
    },
    "repeating_text": {
        "header_footer_min_page_ratio": 0.70,
        "header_footer_min_pages": 5,
        "ad_repeat_min_page_ratio": 0.03,
        "ad_repeat_min_pages": 5,
    },
    "ad_detection": {
        "max_len_chars": 120,
        "high_confidence_patterns": [r"https?://", r"www\\.", r"公众号", r"扫码", r"微信"],
        "patterns": [r"关注", r"加群", r"QQ群", r"vx", r"V信", r"订阅", r"推广", r"广告", r"下载"],
    },
    "chapter_detection": {
        "chinese_chapter_regex": [r"^第[一二三四五六七八九十百千0-9]+章", r"^第[一二三四五六七八九十百千0-9]+节"],
        "size_multiplier": 1.6,
    },
    "approval": {
        "interactive": True,
        "auto_approve_header_footer": False,
    },
}


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_rules(path: Optional[str]) -> dict:
    rules = DEFAULT_RULES
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        rules = deep_merge(rules, user)
    return rules


# -------------------------
# Normalization & heuristics
# -------------------------

_CJK_SPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")


def normalize_text(s: str, drop_digits: bool = False) -> str:
    s = s.strip()
    s = s.replace("\u3000", " ")
    s = _CJK_SPACE_RE.sub(" ", s)
    # unify punctuation variants lightly
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("【", "[").replace("】", "]")
    s = s.replace("—", "-").replace("–", "-")
    if drop_digits:
        s = _DIGITS_RE.sub("", s)
    return s.strip()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def in_top_bottom_region(y0: float, y1: float, page_h: float, top_ratio: float, bottom_ratio: float) -> bool:
    top_h = page_h * float(top_ratio)
    bot_y = page_h * (1.0 - float(bottom_ratio))
    return (y1 <= top_h) or (y0 >= bot_y)


def in_mid_region(y0: float, y1: float, page_h: float, top_ratio: float, bottom_ratio: float) -> bool:
    top_h = page_h * float(top_ratio)
    bot_y = page_h * (1.0 - float(bottom_ratio))
    return (y0 >= top_h) and (y1 <= bot_y)


def compile_patterns(pats: List[str]) -> List[re.Pattern]:
    out = []
    for p in pats:
        out.append(re.compile(p, re.IGNORECASE))
    return out


# -------------------------
# PDF extraction
# -------------------------


def extract_paragraphs(doc: fitz.Document) -> Tuple[List[Para], List[Tuple[float, float]]]:
    """Extract paragraphs using get_text('dict').

    We are conservative: keep text, don't over-normalize line breaks.
    We form paragraphs by joining lines inside each block with a newline.
    This is not perfect but robust.

    Returns:
      paras: list of Para
      page_sizes: [(w,h), ...]
    """

    paras: List[Para] = []
    page_sizes: List[Tuple[float, float]] = []

    for pi in range(len(doc)):
        page = doc.load_page(pi)
        rect = page.rect
        page_sizes.append((rect.width, rect.height))

        d = page.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                # type 1 is image block; we keep images separately
                continue
            bbox = tuple(b.get("bbox", (0, 0, 0, 0)))
            lines = b.get("lines", [])
            if not lines:
                continue

            texts: List[str] = []
            spans_out: List[Span] = []

            for ln in lines:
                line_text_parts: List[str] = []
                for sp in ln.get("spans", []):
                    t = sp.get("text", "")
                    if not t:
                        continue
                    line_text_parts.append(t)
                    spans_out.append(
                        Span(
                            text=t,
                            size=float(sp.get("size", 0.0) or 0.0),
                            font=str(sp.get("font", "")),
                            flags=int(sp.get("flags", 0) or 0),
                            color=int(sp.get("color", 0) or 0),
                        )
                    )
                line_text = "".join(line_text_parts).strip()
                if line_text:
                    texts.append(line_text)

            if not texts:
                continue

            # Keep as paragraph-ish unit
            text = "\n".join(texts)
            paras.append(Para(page_index=pi, bbox=bbox, text=text, spans=spans_out))

    return paras, page_sizes


def extract_images(doc: fitz.Document, out_img_dir: str) -> Dict[Tuple[int, int], str]:
    """Extract raster images.

    Returns mapping: (page_index, image_number) -> relative image path.

    Note: For manuals, inline placement is hard. We'll insert images at the end of each page
    by default (simple and robust). Later can improve with bbox-based placement.
    """
    os.makedirs(out_img_dir, exist_ok=True)
    mapping: Dict[Tuple[int, int], str] = {}

    for pi in range(len(doc)):
        page = doc.load_page(pi)
        img_list = page.get_images(full=True)
        for idx, img in enumerate(img_list):
            xref = img[0]
            base = doc.extract_image(xref)
            ext = base.get("ext", "png")
            img_bytes = base.get("image")
            if not img_bytes:
                continue

            fname = f"p{pi+1:04d}_{idx+1:03d}.{ext}"
            fpath = os.path.join(out_img_dir, fname)
            with open(fpath, "wb") as f:
                f.write(img_bytes)

            mapping[(pi, idx)] = fname

    return mapping


# -------------------------
# Detection (repeat + ad)
# -------------------------


def build_repeat_stats(paras: List[Para], page_sizes: List[Tuple[float, float]], rules: dict) -> Dict[str, Any]:
    top_ratio = rules["regions"]["top_ratio"]
    bottom_ratio = rules["regions"]["bottom_ratio"]

    # map norm -> set(pages)
    norm_pages_dropdigits: Dict[str, set] = defaultdict(set)
    norm_pages_keepdigits: Dict[str, set] = defaultdict(set)

    for p in paras:
        _, h = page_sizes[p.page_index]
        if not in_top_bottom_region(p.y0, p.y1, h, top_ratio, bottom_ratio):
            continue

        n1 = normalize_text(p.text, drop_digits=True)
        n2 = normalize_text(p.text, drop_digits=False)
        if n1:
            norm_pages_dropdigits[n1].add(p.page_index)
        if n2:
            norm_pages_keepdigits[n2].add(p.page_index)

    return {
        "dropdigits": {k: sorted(list(v)) for k, v in norm_pages_dropdigits.items()},
        "keepdigits": {k: sorted(list(v)) for k, v in norm_pages_keepdigits.items()},
    }


def detect_candidates(paras: List[Para], page_sizes: List[Tuple[float, float]], rules: dict) -> List[RemovalCandidate]:
    top_ratio = rules["regions"]["top_ratio"]
    bottom_ratio = rules["regions"]["bottom_ratio"]
    allow_mid_high = bool(rules["regions"].get("allow_midpage_high_confidence", True))

    rep = build_repeat_stats(paras, page_sizes, rules)
    dd = rep["dropdigits"]
    kd = rep["keepdigits"]

    total_pages = len(page_sizes)

    hf_min_ratio = float(rules["repeating_text"]["header_footer_min_page_ratio"])
    hf_min_pages = int(rules["repeating_text"]["header_footer_min_pages"])

    adrep_min_ratio = float(rules["repeating_text"]["ad_repeat_min_page_ratio"])
    adrep_min_pages = int(rules["repeating_text"]["ad_repeat_min_pages"])

    max_len = int(rules["ad_detection"]["max_len_chars"])
    high_pats = compile_patterns(list(rules["ad_detection"].get("high_confidence_patterns", [])))
    pats = compile_patterns(list(rules["ad_detection"].get("patterns", [])))

    # Precompute repeated norms
    header_footer_norms = set()
    ad_repeat_norms = set()

    for norm, pages in dd.items():
        c = len(pages)
        if c >= hf_min_pages and (c / max(1, total_pages)) >= hf_min_ratio:
            header_footer_norms.add(norm)
        # For ad repeats, allow lower ratio, but avoid accidentally deleting short common words
        if c >= adrep_min_pages and (c / max(1, total_pages)) >= adrep_min_ratio:
            if len(norm) >= 10:  # heuristic: ignore very short norms
                ad_repeat_norms.add(norm)

    cands: List[RemovalCandidate] = []

    for p in paras:
        _, h = page_sizes[p.page_index]
        in_tb = in_top_bottom_region(p.y0, p.y1, h, top_ratio, bottom_ratio)
        in_mid = in_mid_region(p.y0, p.y1, h, top_ratio, bottom_ratio)

        text = p.text.strip()
        if not text:
            continue

        norm_dd = normalize_text(text, drop_digits=True)
        norm_kd = normalize_text(text, drop_digits=False)

        # 1) header/footer strong repeat (drop digits)
        if in_tb and norm_dd in header_footer_norms:
            cands.append(
                RemovalCandidate(
                    kind="header_footer_repeat",
                    page_index=p.page_index,
                    bbox=p.bbox,
                    text=text,
                    norm=norm_dd,
                    score=1.0,
                    reasons=["repeat:header_footer"],
                    approved=False,
                )
            )
            continue

        # 2) ad repeat (still only in top/bottom)
        if in_tb and norm_dd in ad_repeat_norms and len(text) <= max_len:
            cands.append(
                RemovalCandidate(
                    kind="ad_repeat",
                    page_index=p.page_index,
                    bbox=p.bbox,
                    text=text,
                    norm=norm_dd,
                    score=0.85,
                    reasons=["repeat:ad"],
                    approved=False,
                )
            )
            continue

        # 3) ad pattern
        # - High confidence: can apply in mid region if enabled
        # - Normal confidence: only in top/bottom
        if len(text) <= max_len:
            hit_high = [pat.pattern for pat in high_pats if pat.search(text)]
            if hit_high and (in_tb or (allow_mid_high and in_mid)):
                cands.append(
                    RemovalCandidate(
                        kind="ad_pattern",
                        page_index=p.page_index,
                        bbox=p.bbox,
                        text=text,
                        norm=norm_kd,
                        score=0.95,
                        reasons=[f"pat:high:{'|'.join(hit_high)}"],
                        approved=False,
                    )
                )
                continue

            hit = [pat.pattern for pat in pats if pat.search(text)]
            if hit and in_tb:
                cands.append(
                    RemovalCandidate(
                        kind="ad_pattern",
                        page_index=p.page_index,
                        bbox=p.bbox,
                        text=text,
                        norm=norm_kd,
                        score=0.70,
                        reasons=[f"pat:{'|'.join(hit)}"],
                        approved=False,
                    )
                )

    return cands


# -------------------------
# Approval (interactive)
# -------------------------


def interactive_approve(cands: List[RemovalCandidate], rules: dict) -> List[RemovalCandidate]:
    interactive = bool(rules["approval"].get("interactive", True))
    auto_hf = bool(rules["approval"].get("auto_approve_header_footer", False))

    # sort by kind, then page
    cands = sorted(cands, key=lambda c: (c.kind, c.page_index, -c.score))

    if not interactive:
        # no interaction: nothing approved by default, unless auto_hf is true
        for c in cands:
            if auto_hf and c.kind == "header_footer_repeat":
                c.approved = True
        return cands

    print("\n=== Removal candidates (preview) ===")
    print("输入 y 批准删除该项；输入 n 保留；输入 a 批准本组全部；输入 s 跳过本组剩余；输入 q 退出。\n")

    # group by kind
    groups: Dict[str, List[RemovalCandidate]] = defaultdict(list)
    for c in cands:
        groups[c.kind].append(c)

    for kind, items in groups.items():
        if auto_hf and kind == "header_footer_repeat":
            for c in items:
                c.approved = True
            print(f"[auto-approved] {kind}: {len(items)} items")
            continue

        print(f"\n--- Group: {kind} ({len(items)} items) ---")
        approve_all = False
        skip_rest = False

        for i, c in enumerate(items, start=1):
            if skip_rest:
                break
            if approve_all:
                c.approved = True
                continue

            snippet = c.text.replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:120] + "..."

            print(f"[{i}/{len(items)}] page={c.page_index+1} score={c.score:.2f} reasons={c.reasons}")
            print(f"  {snippet}")

            while True:
                ans = input("  delete? (y/n/a/s/q) > ").strip().lower()
                if ans in ("y", "n", "a", "s", "q"):
                    break

            if ans == "q":
                print("Quit approval.")
                return cands
            if ans == "s":
                skip_rest = True
                break
            if ans == "a":
                approve_all = True
                c.approved = True
                continue
            if ans == "y":
                c.approved = True
            else:
                c.approved = False

    return cands


# -------------------------
# Chapter detection
# -------------------------


def estimate_body_font_size(paras: List[Para]) -> float:
    sizes = []
    for p in paras:
        for sp in p.spans:
            if sp.size > 0:
                sizes.append(sp.size)
    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def is_chapter_title(text: str, spans: List[Span], body_median: float, rules: dict) -> bool:
    t = text.strip().replace("\n", " ")
    if not t:
        return False

    # Prefer regex for Chinese chapters (more reliable than font size for your PDFs)
    for pat in rules["chapter_detection"].get("chinese_chapter_regex", []):
        if re.search(pat, t):
            return True

    # Fallback: font size heuristic (very conservative)
    mult = float(rules["chapter_detection"].get("size_multiplier", 1.6))
    max_size = max([sp.size for sp in spans], default=0.0)
    if max_size >= body_median * mult and len(t) <= 40:
        return True

    return False


# -------------------------
# EPUB building
# -------------------------


def para_to_html(p: Para) -> str:
    # Simple: preserve line breaks as <br/> inside paragraph
    # (We don't attempt full reflow; focus on not losing text.)
    escaped = (
        p.text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    escaped = escaped.replace("\n", "<br/>")
    return f"<p>{escaped}</p>\n"


def build_epub(
    title: str,
    author: str,
    paras: List[Para],
    page_sizes: List[Tuple[float, float]],
    approved_removals: List[RemovalCandidate],
    img_dir: str,
    img_map: Dict[Tuple[int, int], str],
    out_path: str,
    rules: dict,
) -> None:

    # Index removals by page and by normalized text hash for quick filter
    to_remove: Dict[int, List[RemovalCandidate]] = defaultdict(list)
    for c in approved_removals:
        if c.approved:
            to_remove[c.page_index].append(c)

    # Determine body font size for title heuristic
    body_median = estimate_body_font_size(paras)

    book = epub.EpubBook()
    book.set_identifier("pdf2epub")
    book.set_title(title)
    book.add_author(author)
    book.set_language("zh")

    # Add CSS
    style = """
    body { font-family: serif; }
    pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
    img { max-width: 100%; height: auto; }
    p { line-height: 1.4; }
    """.strip()

    nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
    book.add_item(nav_css)

    # Add images into epub
    for (_, _), rel in img_map.items():
        fpath = os.path.join(img_dir, rel)
        if not os.path.exists(fpath):
            continue
        with open(fpath, "rb") as f:
            data = f.read()
        media = "image/png"
        ext = os.path.splitext(rel)[1].lower()
        if ext == ".jpg" or ext == ".jpeg":
            media = "image/jpeg"
        elif ext == ".webp":
            media = "image/webp"
        item = epub.EpubItem(uid=f"img_{rel}", file_name=f"images/{rel}", media_type=media, content=data)
        book.add_item(item)

    # Group paras by page for simple reading order
    paras_by_page: Dict[int, List[Para]] = defaultdict(list)
    for p in paras:
        paras_by_page[p.page_index].append(p)

    # Build chapters conservatively
    chapters: List[epub.EpubHtml] = []
    spine = ["nav"]
    toc = []

    current_html_parts: List[str] = []
    current_title = "开始"
    chap_index = 1

    def flush_chapter():
        nonlocal chap_index, current_html_parts, current_title
        if not current_html_parts:
            return
        chap = epub.EpubHtml(title=current_title, file_name=f"chap_{chap_index:03d}.xhtml", lang="zh")
        chap.content = ("<html><head><link rel='stylesheet' type='text/css' href='style/nav.css'/></head><body>" + "\n".join(current_html_parts) + "</body></html>")
        book.add_item(chap)
        chapters.append(chap)
        toc.append(chap)
        spine.append(chap)
        chap_index += 1
        current_html_parts = []

    for pi in range(len(page_sizes)):
        page_paras = paras_by_page.get(pi, [])
        # remove approved candidates by fuzzy matching on bbox overlap + normalized text
        removals = to_remove.get(pi, [])

        def should_remove(para: Para) -> Optional[RemovalCandidate]:
            if not removals:
                return None
            nt = normalize_text(para.text, drop_digits=True)
            for r in removals:
                # text match is primary; bbox only as a weak signal
                if nt and r.norm and nt == r.norm:
                    return r
            return None

        for para in page_paras:
            rem = should_remove(para)
            if rem is not None:
                continue

            # Chapter detection
            if is_chapter_title(para.text, para.spans, body_median, rules):
                flush_chapter()
                current_title = para.text.strip().replace("\n", " ")
                current_html_parts.append(f"<h1>{current_title}</h1>")
                continue

            current_html_parts.append(para_to_html(para))

        # Append images for the page at the end (robust)
        # (Placement improvement can be done later)
        imgs_for_page = [rel for (pidx, _), rel in img_map.items() if pidx == pi]
        for rel in imgs_for_page:
            current_html_parts.append(f"<div class='page-image'><img src='images/{rel}'/></div>")

    flush_chapter()

    book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(out_path, book, {})


# -------------------------
# Reporting
# -------------------------


def write_report(report_path: str, cands: List[RemovalCandidate]) -> None:
    data = []
    for c in cands:
        data.append(dataclasses.asdict(c))
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"candidates": data}, f, ensure_ascii=False, indent=2)


# -------------------------
# CLI
# -------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="input PDF path")
    ap.add_argument("--out", required=True, help="output EPUB path")
    ap.add_argument("--rules", default=None, help="rules yaml path")
    ap.add_argument("--title", default=None, help="book title (default: pdf filename)")
    ap.add_argument("--author", default="", help="author")
    ap.add_argument("--workdir", default="work", help="working dir (images, report)")
    args = ap.parse_args()

    rules = load_rules(args.rules)

    pdf_path = args.pdf
    out_path = args.out
    title = args.title or os.path.splitext(os.path.basename(pdf_path))[0]
    author = args.author

    workdir = args.workdir
    os.makedirs(workdir, exist_ok=True)
    img_dir = os.path.join(workdir, "images")
    report_path = os.path.join(workdir, "removal_report.json")

    doc = fitz.open(pdf_path)
    try:
        print(f"[1/5] Extracting paragraphs from: {pdf_path}")
        paras, page_sizes = extract_paragraphs(doc)
        print(f"  pages={len(page_sizes)} paragraphs={len(paras)}")

        print(f"[2/5] Extracting images (no OCR) -> {img_dir}")
        img_map = extract_images(doc, img_dir)
        print(f"  images={len(img_map)}")

        print("[3/5] Detecting removal candidates (header/footer + ads)")
        cands = detect_candidates(paras, page_sizes, rules)
        print(f"  candidates={len(cands)}")

        print("[4/5] Approval step")
        cands = interactive_approve(cands, rules)
        approved = [c for c in cands if c.approved]
        print(f"  approved_to_remove={len(approved)}")

        write_report(report_path, cands)
        print(f"  report_written={report_path}")

        print("[5/5] Building EPUB")
        build_epub(
            title=title,
            author=author,
            paras=paras,
            page_sizes=page_sizes,
            approved_removals=approved,
            img_dir=img_dir,
            img_map=img_map,
            out_path=out_path,
            rules=rules,
        )
        print(f"  epub_written={out_path}")

    finally:
        doc.close()


if __name__ == "__main__":
    main()
