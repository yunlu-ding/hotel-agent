"""把 Markdown 转成 Word 文档。

> 说明：本脚本**来自 vibe-rag 项目**（`vibe-rag/工具/md转docx.py`），
> 原样复用、只改了用法示例。理由见下——它解决的正是"重复劳动"这件事。

为什么要这么个脚本，而不是手工复制：
面试文档会反复改，改完还要重新导出 Word。手工复制一次两次还行，
改到第五遍就会开始出错（表格错位、层级丢失）。
**凡是需要重复做的事，就该有个脚本。**

支持：# 标题、表格、列表、引用块、代码块、**加粗**、分隔线。
刻意不支持更复杂的 Markdown——够用就行，多写的每一行都是维护成本。

用法：
    cd hotel-agent
    python 工具/md转docx.py 08-面试文档.md
    python 工具/md转docx.py 08-面试文档.md -o 自定义名字.docx

依赖：python-docx（工作区 .venv 里已安装）
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

# 正文与代码用的字体。中文用等线，英文用 Calibri，
# 这样中英混排不会出现某一种字体缺失导致的方框。
BODY_FONT = 'Calibri'
CJK_FONT = '等线'
MONO_FONT = 'Consolas'

TABLE_RE = re.compile(r'^\s*\|(.+)\|\s*$')
SEPARATOR_RE = re.compile(r'^\s*\|[\s\-:|]+\|\s*$')
BOLD_RE = re.compile(r'\*\*(.+?)\*\*')


def set_run_font(run, *, name: str = BODY_FONT, size: int | None = None, bold: bool | None = None):
    """设置字体。中文必须单独设 eastasia，否则 Word 会回退到默认字体。"""

    run.font.name = name
    run._element.rPr.rFonts.set(qn('w:eastAsia'), CJK_FONT if name != MONO_FONT else MONO_FONT)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold


def add_rich_text(paragraph, text: str, *, size: int | None = None, italic: bool = False):
    """写入一段可能带 **加粗** 的文本。"""

    position = 0
    for match in BOLD_RE.finditer(text):
        if match.start() > position:
            run = paragraph.add_run(text[position:match.start()])
            set_run_font(run, size=size)
            run.italic = italic
        run = paragraph.add_run(match.group(1))
        set_run_font(run, size=size, bold=True)
        run.italic = italic
        position = match.end()
    if position < len(text):
        run = paragraph.add_run(text[position:])
        set_run_font(run, size=size)
        run.italic = italic


def split_row(line: str) -> list[str]:
    inner = line.strip().strip('|')
    return [cell.strip() for cell in inner.split('|')]


def convert(md_path: Path, out_path: Path) -> None:
    lines = md_path.read_text(encoding='utf-8').split('\n')
    doc = Document()

    # 全局默认字号，避免 Word 默认的 11pt 在中文下显得松散
    style = doc.styles['Normal']
    style.font.name = BODY_FONT
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), CJK_FONT)

    index = 0
    in_code = False
    code_buffer: list[str] = []

    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip()

        # ---------- 代码块 ----------
        if line.strip().startswith('```'):
            if in_code:
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.left_indent = Pt(18)
                run = paragraph.add_run('\n'.join(code_buffer))
                set_run_font(run, name=MONO_FONT, size=9)
                code_buffer = []
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_buffer.append(raw)
            index += 1
            continue

        # ---------- 表格 ----------
        if TABLE_RE.match(line):
            rows: list[list[str]] = []
            while index < len(lines) and TABLE_RE.match(lines[index]):
                if not SEPARATOR_RE.match(lines[index]):
                    rows.append(split_row(lines[index]))
                index += 1
            if rows:
                width = max(len(row) for row in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = 'Table Grid'
                for r, row in enumerate(rows):
                    for c in range(width):
                        cell = table.cell(r, c)
                        text = row[c] if c < len(row) else ''
                        paragraph = cell.paragraphs[0]
                        add_rich_text(paragraph, text, size=9.5, italic=False)
                        if r == 0:
                            for run in paragraph.runs:
                                run.bold = True
                doc.add_paragraph()
            continue

        # ---------- 分隔线 ----------
        if line.strip() in ('---', '***', '___'):
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run('· · ·')
            run.font.color.rgb = RGBColor(0xBB, 0xBB, 0xBB)
            index += 1
            continue

        # ---------- 标题 ----------
        heading = re.match(r'^(#{1,4})\s+(.*)$', line)
        if heading:
            level = len(heading.group(1))
            doc.add_heading(heading.group(2).strip(), level=min(level, 4))
            index += 1
            continue

        # ---------- 引用块 ----------
        if line.strip().startswith('>'):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Pt(22)
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.space_after = Pt(4)
            add_rich_text(paragraph, line.strip().lstrip('>').strip(), size=10)
            for run in paragraph.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            index += 1
            continue

        # ---------- 列表 ----------
        bullet = re.match(r'^(\s*)[-*]\s+(.*)$', line)
        if bullet:
            # 用缩进层级决定列表级别，最多两级
            level = 1 if len(bullet.group(1)) >= 2 else 0
            style_name = 'List Bullet' if level == 0 else 'List Bullet 2'
            try:
                paragraph = doc.add_paragraph(style=style_name)
            except KeyError:
                paragraph = doc.add_paragraph(style='List Bullet')
            add_rich_text(paragraph, bullet.group(2).strip())
            index += 1
            continue

        numbered = re.match(r'^\s*\d+\.\s+(.*)$', line)
        if numbered:
            paragraph = doc.add_paragraph(style='List Number')
            add_rich_text(paragraph, numbered.group(1).strip())
            index += 1
            continue

        # ---------- 空行 ----------
        if not line.strip():
            index += 1
            continue

        # ---------- 普通段落 ----------
        paragraph = doc.add_paragraph()
        add_rich_text(paragraph, line.strip())
        index += 1

    doc.save(str(out_path))


def main() -> int:
    parser = argparse.ArgumentParser(description='Markdown 转 Word')
    parser.add_argument('source', help='Markdown 文件路径')
    parser.add_argument('-o', '--output', help='输出文件路径（默认与源文件同名）')
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        print(f'找不到文件：{source}')
        return 1

    out = Path(args.output).resolve() if args.output else source.with_suffix('.docx')
    convert(source, out)
    print(f'已生成：{out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
