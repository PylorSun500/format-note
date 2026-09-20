#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
renumber.py — 笔记 Q&A 序号统一与结构规范化（format-note 规范的脚本化）

面向「已经初步排列、缩进关系正确，但序号顺序混乱」的 md 笔记，一次性整理：

  ① 补 `Q: ` 前缀   —— 0 缩进的有序项（`1. xxx` → `1. Q: xxx`），已有则跳过
  ② 序号连续化      —— 全文件 1..N，不按标题重置
  ③ 空行规则        —— 标题 / Q&A 对 / 表格 / 代码块 / 图片 / 块引用
  ④ 深层转无序      —— 2 个及以上 Tab 的 `1. ` 一律转 `- `（1 Tab 保持有序）
  ⑤ 块引用标签加粗  —— `> 辨析：…` → `> **辨析**：…`（口诀 / 辨析 / 参见）

明确不做：链接 🔗 别名替换、图片标题 `<br>*…*` 的生成、参见互引
（后者要写块标签并回链，属语义操作，留给人工）。

默认 dry-run，只打印统计；加 --write 才落盘。落盘为原子写，内容无变化则不碰文件。

用法：
    ./renumber.py "/绝对/路径/笔记.md"
    ./renumber.py --write "/绝对/路径/笔记.md"
    ./renumber.py --write "/绝对/路径/目录"        # 递归处理目录下所有 .md
    ./renumber.py --check "/绝对/路径"             # 有需改动则退出码 1
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import tempfile
from dataclasses import dataclass

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 块引用标签白名单。新增标签类型时在此追加即可。
LABELS = ("口诀", "辨析", "参见")

TOP_RE = re.compile(r"^\d+\.\s")                    # 0 缩进的有序项
TOP_ITEM = re.compile(r"^(\d+)(\.\s+)(.*)$")        # 拆出 序号 / 间隔 / 正文
NESTED_NUM = re.compile(r"^(\t{2,})\d+\.\s")        # 2 个及以上 Tab 的有序编号
HEADING_RE = re.compile(r"^#{1,6}\s")
FENCE_RE = re.compile(r"^\t*(?:```|~~~)")
IS_IMAGE = re.compile(r"^!\[\[|^!\[[^\]]*\]\(")     # ![[x.png]] 或 ![alt](x.png)
BOLD_LABEL = re.compile(r"^>\s*([^\s：:*]{1,6})：")
#: 标记型块引用（口诀 / 辨析 / 参见），无论是否已加粗都能识别
CALLOUT = re.compile(r"^>\s*(?:\*\*)?(?:%s)(?:\*\*)?：" % "|".join(LABELS))


# --------------------------------------------------------------------------
# 行工具
# --------------------------------------------------------------------------

def split_lines(text: str):
    """把文本拆成 [(正文, 行尾符)]，保留原行尾风格（LF / CRLF），不触碰任何字符。"""
    parts = text.split("\n")
    out = []
    for i, p in enumerate(parts):
        if i == len(parts) - 1:
            if p == "":          # 文件以换行结尾时 split 出的空尾元素
                break
            out.append((p, ""))  # 末行无换行符
        elif p.endswith("\r"):
            out.append((p[:-1], "\r\n"))
        else:
            out.append((p, "\n"))
    return out


def join_lines(lines) -> str:
    return "".join(body + eol for body, eol in lines)


def classify(body: str, masked: bool) -> str:
    """给一行归类，用于空行规则的分块。"""
    if masked:
        return "raw"                                   # 代码块内部 / frontmatter
    if HEADING_RE.match(body):
        return "heading"
    if body.startswith(">"):
        return "quote"
    if FENCE_RE.match(body):
        return "fence"
    if body.lstrip("\t").startswith("|"):
        return "table"
    if TOP_RE.match(body):
        return "toplevel"
    stripped = body.lstrip("\t ")
    if IS_IMAGE.match(stripped):
        return "image"                                 # 独占一行的图片
    return "text"


def compute_kinds(bodies, mask):
    """逐行归类。

    围栏代码块的「开/闭」需要维持状态才能分辨，而 mask 只标了「内容不可动」，
    所以这里单独走一趟：定界行归类为 fence_open / fence_close（空行上是边界），
    块内其它行归类为 raw（内容与空行都原样保留）。
    """
    kinds = []
    in_fence = False
    for body, masked in zip(bodies, mask):
        if FENCE_RE.match(body):
            kinds.append("fence_open" if not in_fence else "fence_close")
            in_fence = not in_fence
            continue
        kinds.append("raw" if in_fence else classify(body, masked))
    return kinds


def is_callout(unit) -> bool:
    """整块是否为标记型块引用（口诀 / 辨析 / 参见）——只有这类块之间才不留空行。"""
    return bool(CALLOUT.match(unit[0][0]))


def bare_labels(body: str) -> str:
    """`> 辨析：x` → `> **辨析**：x`；已在白名单外或已加粗则不匹配。"""
    m = BOLD_LABEL.match(body)
    if m and m.group(1) in LABELS:
        return "> **%s**：%s" % (m.group(1), body[m.end():])
    return body


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

@dataclass
class Report:
    path: str
    total: int = 0          # 顶层 Q&A 项数
    prefix: int = 0         # 补 Q: 前缀处数
    renumber: int = 0       # 序号被改动处数
    resets: int = 0         # 检测到的编号重置/断档处数
    flatten: int = 0        # 深层有序 → 无序处数
    bold: int = 0           # 块引用标签加粗处数
    blank_add: int = 0      # 新增空行数
    blank_del: int = 0      # 删除空行数
    quote_merge: int = 0    # 相邻块引用合并处数
    no_q_warning: bool = False
    diff: str = ""

    @property
    def changed(self) -> int:
        return (self.prefix + self.renumber + self.flatten + self.bold
                + self.blank_add + self.blank_del + self.quote_merge)


# --------------------------------------------------------------------------
# 各处理步骤
# --------------------------------------------------------------------------

def mask_regions(bodies):
    """标出不应改动的行：YAML frontmatter 与围栏代码块。"""
    mask = [False] * len(bodies)
    start = 0
    if bodies and bodies[0].strip() == "---":
        for j in range(1, len(bodies)):
            if bodies[j].strip() in ("---", "..."):
                for k in range(0, j + 1):
                    mask[k] = True
                start = j + 1
                break
    in_fence = False
    for i in range(start, len(bodies)):
        if FENCE_RE.match(bodies[i]):
            in_fence = not in_fence
            mask[i] = True
            continue
        if in_fence:
            mask[i] = True
    return mask


def content_pass(lines, mask, opts, rep: Report):
    """①②④⑤：内容级改写，不动空行。"""
    had_q_before = False
    for body, _ in lines:
        m = TOP_ITEM.match(body)
        if m and re.match(r"^Q\s*[:：]", m.group(3)):
            had_q_before = True
            break
    out = []
    old_numbers = []
    n = 0
    for (body, eol), masked in zip(lines, mask):
        if masked:
            out.append((body, eol))
            continue

        b = body

        # ④ 2 个及以上 Tab 的有序编号 → 无序
        if opts.flatten:
            m = NESTED_NUM.match(b)
            if m:
                b = m.group(1) + "- " + b[m.end():]
                rep.flatten += 1

        m = TOP_ITEM.match(b)
        if m:
            # ① 补 Q: 前缀
            if opts.prefix and not re.match(r"^Q\s*[:：]", m.group(3)):
                b = m.group(1) + m.group(2) + "Q: " + m.group(3)
                rep.prefix += 1
                m = TOP_ITEM.match(b)
            # ② 序号连续化
            old_numbers.append(int(m.group(1)))
            n += 1
            if m.group(1) != str(n):
                rep.renumber += 1
            b = "%d%s%s" % (n, m.group(2), m.group(3))

        # ⑤ 块引用标签加粗
        if opts.bold and b.startswith(">"):
            nb = bare_labels(b)
            if nb != b:
                rep.bold += 1
                b = nb

        out.append((b, eol))

    # 统计编号重置（后一个不大于前一个即视为重置/断档）
    prev = 0
    for num in old_numbers:
        if num <= prev:
            rep.resets += 1
        prev = num
    rep.total = n
    if opts.prefix and n and not had_q_before:
        rep.no_q_warning = True
    return out


def blank_pass(lines, mask, opts, rep: Report):
    """③ 空行规则：按块重组，块间恰好一个空行。"""
    bodies = [b for b, _ in lines]
    kinds = compute_kinds(bodies, mask)

    OPENS = {"heading", "toplevel", "image", "fence_open"}      # 这些行自成一个块的开头
    CLOSES = {"heading", "quote", "image", "fence_close"}       # 这些行之后必然换块
    PAIRED = ("quote", "table")                                 # 同类连续时不拆

    units = []          # list[list[(body, eol)]]
    cur = []
    prev_kind = None

    for (body, eol), masked, kind in zip(lines, mask, kinds):
        if body.strip() == "" and not masked:
            if cur:
                units.append(cur)
                cur = []
            prev_kind = "blank"
            continue

        split = False
        if cur:
            if kind in OPENS:
                split = True
            elif kind == prev_kind and kind in PAIRED:
                split = False       # 多行块引用 / 表格，整体成块
            elif kind in PAIRED or prev_kind in PAIRED:
                split = True
            elif prev_kind in CLOSES:
                split = True
        if split:
            units.append(cur)
            cur = []

        cur.append((body, eol))
        prev_kind = kind

    if cur:
        units.append(cur)

    # 重组：块间恰好一个空行。
    # 例外——同属标记组（口诀 / 辨析 / 参见）的相邻块引用之间不留空行。
    # 无标签的引用块不参与合并，原样保留。
    out = []
    for idx, unit in enumerate(units):
        if idx:
            if (opts.merge_quotes
                    and is_callout(units[idx - 1]) and is_callout(unit)):
                rep.quote_merge += 1
            else:
                out.append(("", "\n"))
        out.extend(unit)

    if out:
        out[-1] = (out[-1][0], "\n")    # 文件以恰好一个换行结尾

    # 空行增删统计（诚实计数：拿新旧行序列做比对）
    old_b = [b for b, _ in lines]
    new_b = [b for b, _ in out]
    sm = difflib.SequenceMatcher(None, old_b, new_b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            rep.blank_add += sum(1 for j in range(j1, j2) if new_b[j].strip() == "")
        if tag in ("delete", "replace"):
            rep.blank_del += sum(1 for i in range(i1, i2) if old_b[i].strip() == "")
    return out


def process(text: str, opts, rep: Report) -> str:
    lines = split_lines(text)
    if not lines:
        return text
    bodies = [b for b, _ in lines]
    mask = mask_regions(bodies)
    lines = content_pass(lines, mask, opts, rep)
    if opts.blank:
        lines = blank_pass(lines, mask, opts, rep)
    return join_lines(lines)


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".renumber-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def print_report(rep: Report, opts) -> None:
    name = os.path.relpath(rep.path, os.getcwd())
    if not rep.changed:
        print("  %s\n    无变化" % name)
        return
    print("  %s" % name)
    print("    Q 前缀 +%d    序号 %d 项（改 %d，重置 %d）    深层转无序 %d"
          % (rep.prefix, rep.total, rep.renumber, rep.resets, rep.flatten))
    print("    标签加粗 %d    块引用合并 %d    空行 新增 %d / 删除 %d"
          % (rep.bold, rep.quote_merge, rep.blank_add, rep.blank_del))
    if rep.no_q_warning:
        print("    ⚠ 该文件原本没有任何 `Q: ` 行，已为全部顶层项补前缀 —— 确认它确实是 Q&A 笔记")
    if rep.diff:
        print()
        for line in rep.diff.splitlines():
            print("    " + line)


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs
                                 if d not in (".git", ".obsidian", ".claude", "assets"))
                out.extend(os.path.join(root, f) for f in sorted(files)
                           if f.endswith(".md"))
        else:
            out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="笔记 Q&A 序号统一与结构规范化（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1].strip())
    ap.add_argument("paths", nargs="+", help="目标 md 文件或目录（绝对路径）")
    ap.add_argument("-w", "--write", action="store_true", help="真正写入文件（默认只预览）")
    ap.add_argument("--check", action="store_true", help="只检查；有需改动则退出码 1")
    ap.add_argument("--diff", action="store_true", help="预览时打印 unified diff")
    ap.add_argument("--no-prefix", action="store_true", help="不补 `Q: ` 前缀")
    ap.add_argument("--no-flatten", action="store_true", help="不把深层有序编号转无序")
    ap.add_argument("--no-bold", action="store_true", help="不给块引用标签加粗")
    ap.add_argument("--no-blank", action="store_true", help="不整理空行")
    ap.add_argument("--no-merge-quotes", action="store_true", help="相邻块引用之间保留空行")
    opts = ap.parse_args(argv)
    opts.prefix = not opts.no_prefix
    opts.flatten = not opts.no_flatten
    opts.bold = not opts.no_bold
    opts.blank = not opts.no_blank
    opts.merge_quotes = not opts.no_merge_quotes

    files = collect(opts.paths)
    if not files:
        print("没有找到任何 .md 文件", file=sys.stderr)
        return 2

    mode = "写入" if opts.write else ("检查" if opts.check else "预览（dry-run）")
    print("renumber.py — %s，共 %d 个文件\n" % (mode, len(files)))

    dirty = 0
    for path in files:
        rep = Report(path=path)
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            print("  %s\n    跳过：%s" % (path, e))
            continue

        new_text = process(text, opts, rep)
        if new_text != text:
            dirty += 1
        if opts.diff and new_text != text:
            rep.diff = "".join(difflib.unified_diff(
                text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile="原文件", tofile="整理后", n=1))

        if opts.write and new_text != text:
            atomic_write(path, new_text)
        print_report(rep, opts)

    print("\n%s：%d 个文件%s"
          % ("已写入" if opts.write else ("需整理" if opts.check else "待整理"),
             dirty, "" if opts.write else "（加 --write 落盘）"))
    if opts.check and dirty:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
