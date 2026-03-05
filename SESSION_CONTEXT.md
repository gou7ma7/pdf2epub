# Session context (精炼版)：pdf2epub 的目标、约束与方案

## 目标
把 **PDF（中文书/手册、无双栏）→ EPUB**，并尽量：
- 保留文本（允许换行不完美，但不要丢字）
- 保留图片（**不做 OCR**）
- 自动识别并移除：页眉/页脚、以及“与正文无关的广告段落”（通常短段落、1–3 句）
- **删除前必须提示并由用户确认**（默认交互模式）

## 关键约束/策略
- 抽取：PyMuPDF `get_text("dict")`（保信息优先）
- 去噪范围：
  - 默认只在页面 **top/bottom 25%** 做候选识别（避免误删正文）
  - 页面中间区域仅对 **高置信规则** 生效（如 URL/公众号/扫码/微信）
- 广告可能：
  - 不一定重复，但也需要“频次统计”删（若确实重复）
  - 可能断行：脚本按 block/lines 合成“段落-ish”，不追求完美复原
- 分章：保守策略
  - 优先匹配中文标题模式：`第X章/第X节`
  - 字号仅作非常保守的辅助（因为 PDF 标题字号不一定可靠）

## 实现落点（repo 内）
目录：`convert_to_ebook/pdf2epub`

- `pdf2epub.py`：主脚本
  - 提取段落（文本 block）
  - 提取图片（不 OCR；当前策略：插在每页内容末尾，保证不丢图）
  - 检测候选删除项：
    - `header_footer_repeat`：页眉/页脚强重复（归一化时 drop digits）
    - `ad_repeat`：广告段落重复（阈值更宽松）
    - `ad_pattern`：关键词/regex（中间区域仅高置信）
  - **交互确认**：y/n/a/s/q
  - EbookLib 打包 EPUB
- `rules.example.yaml`：规则模板（复制为 `rules.yaml` 调参）
- `requirements.txt`：依赖
- `work/removal_report.json`：运行后报告（候选删除项+你的批准结果，便于复盘调参）

## LLM 的位置（可选、默认不启用）
原则：**不要全程用 LLM**。
- 只对“灰区候选段落”送 LLM（每 100 页几十段量级）
- 结果 `REMOVE/KEEP` 可写入本地记忆（hash/归一化文本），后续同类不再问
- 规则沉淀建议：记录到报告中，由人工确认后再写入 `rules.yaml`（避免自动改规则误伤）

## 运行方式（使用 uv 管理环境）
在本目录：

```powershell
uv venv
.\\.venv\\Scripts\\Activate.ps1
uv pip install -e .
copy rules.example.yaml rules.yaml
python pdf2epub.py --pdf input.pdf --out output.epub --rules rules.yaml --workdir work
```

产物：
- `output.epub`
- `work/images/`（抽取图片）
- `work/removal_report.json`（可审计/可调参）
