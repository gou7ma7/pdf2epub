# pdf2epub

PDF -> EPUB (中文书/手册友好) 的一个可控脚本：
- PyMuPDF 抽取文本与图片（不 OCR）
- 规则清洗：页眉/页脚强重复、广告段落（关键词/regex + 可选重复统计）
- **交互确认**：默认会列出候选删除项，你确认后才会真的从输出中移除
- EbookLib 生成 EPUB，多章节（保守分章：优先 `第X章/节` 模式，字号仅辅助）

## Install

建议使用 **uv** 管理虚拟环境（Windows）：

```powershell
uv venv
.\\.venv\\Scripts\\Activate.ps1
uv pip install -e .
```

## Usage

1) 复制规则文件：

```powershell
copy rules.example.yaml rules.yaml
```

2) 运行：

```powershell
python pdf2epub.py --pdf input.pdf --out output.epub --rules rules.yaml --workdir work
```

输出：
- `output.epub`
- `work/images/`（抽取的图片）
- `work/removal_report.json`（候选删除项 + 你的批准结果）

## Notes

- 默认只在页面 top/bottom 25% 区域做清洗；中间区域仅对“高置信规则”（如 URL/公众号）生效。
- 广告段落常见是短段（<80字），可在 `rules.yaml` 调整 `ad_detection.max_len_chars`。
