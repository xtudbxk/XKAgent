---
name: pdfreader
version: 1.0.0
description: 提取PDF文字表格与图片内容，生成带行页锚点的知识档案
category: workflow
triggers:
  - pdf
  - 读pdf
  - 提取pdf
compatible_modes:
  - build-unsafe
  - build
  - plan
requires.pip: []
requires.skills: []
---

# PDF Reader Skill

把指定 PDF 解析为结构化知识档案，输出到 `pdfreader_{pdfname}/` 目录（下列规则决定位置）。
**核心价值：所有内容按"行锚点"一一对应原 PDF 的页/行**，图内容由 agent（多模态）补充解读。

## 🧭 Mode Pre-Check（最先执行）

从系统消息前缀「模式: xxx」检测当前运行模式：

| 模式 | 脚本位置 | 产物输出位置 | 第三方库（pypdf/pdfminer.six） |
|------|---------|-------------|------------------------------|
| 🔥 build-unsafe | `skills/pdfreader/*.py` | `docs/pdfreader_{pdfname}/`（项目根；可用 `PDFREADER_OUTPUT_ROOT` 重定向） | ✅ 可用（缺失时脚本自动从 PyPI 下载注入 /tmp/pdfreader_libs） |
| 🟢 build | `skills/pdfreader/*.py` | 同上（写失败→ /tmp + 提醒） | ⚠️ 沙箱拒第三方模块 → **提示切 build-unsafe** |
| 🔵 plan | `/tmp/pdfreader_skill/`（importlib 动态加载） | **`/tmp/pdfreader_{pdfname}/`**（临时，不持久化） | ⚠️ 沙箱拒第三方模块 → **提示切 build-unsafe 或宿主 !xxx** |

**规则（用户约定）**：任何模式下调用本技能，若产物写入被权限限制，一律降级到 `PDFREADER_TMP_ROOT`（默认 `/tmp`）并**在最终报告首行醒目提示实际输出路径**。

**检测第三方库**：`try: from pdfminer.high_level import extract_pages`；抛 `ModuleNotFoundError` 且含 "sandbox" 字样 → 判定受限沙箱，走降级路径：
1. 提示用户切 build-unsafe（推荐），或
2. 提示用户在宿主侧执行：`!pip install pypdf pdfminer.six` 后 `!python3 skills/pdfreader/extract_light.py <pdf> --out <dir>`

## 产物结构（pdfreader_{pdfname}/）

```
__info__.json      类属性：源文件/sha256/页数/总行数/表数/图数/模式/版本/时间
__overview__.md    入口：摘要+统计+每页速览+图表清单(含⚠️未解读提醒)+导航说明
content.md         全文：每行带锚点 [p001-L0002]，页间以 ==== Page N ==== 分隔
pages/p001.md      逐页：行表(锚点+文本)+图/表引用
tables/t001.md     表格：数据 + [p001-L0008..L0012] 行区间
images/i001.png    原图(降采样省略，保持原样) + i001.md 解读(agent结果/占位)
images_meta.json   图元数据：页/bbox/尺寸/上下文行区间/状态(decorative等)
__index__.md       主题索引：关键词→锚点
__protocol__.md    方法学+校验记录+图片识别状态统计
```

## 锚点规范（硬性）

- **行定义**：视觉行（PDF 布局行），页内按 top 升序编号（同 top 按 x0），编号 1..N
- **行锚点**：`[p{P:03d}-L{N:04d}]` = 第 P 页第 N 行，如 `[p003-L0012]`
- **图锚点**：`[p{P:03d}-I{NN}]`（NN=该图全档序号 iNN 的后两位），图内文字不占行号，但图与其所在行区间关联
- **校验**：所有行必有锚点；未对齐图像块显式标记 `unreadable`，不静默忽略

## 执行流程（S1~S8）

### S1 参数解析与校验
输入：PDF 路径（必填）、可选 `--output` 覆盖输出目录、`--no-images` 跳过图片。校验：文件存在/是 PDF/可读；用 `extract_light.py meta` 获取页数（>500 页提示分批）。

### S2 文字+表格提取（脚本）
```python
from skills.pdfreader.extract_light import run
report = run(pdf_path, out_dir)   # 产出 meta.json/content.json/tables.json，返回 JSON 报告
```
流程：pdfminer.six 经典管线（PDFResourceManager + PDFPageAggregator + LAParams），
逐页收集 `LTTextLine`（bbox/top/x0）→ 行号；`LTRect/LTCurve` 线段聚类成网格 → 单元格字符归属 → 表格。

### S3 图片提取（脚本）
```python
from skills.pdfreader.extract_images import run
report = run(pdf_path, out_dir)   # 产出 images/iNNN.{jpg,png} + images_meta.json
```
规则：`LTImage`（name/bbox/stream）→ 原字节（magic 判 ext；FlateDecode 灰度/RGB 组 PNG）；
去重（按 name）；`SMask` 过滤；宽或高 < 30px → `decorative`（不入 agent 清单）。

### S4 图片内容识别（agent，核心）
对 images_meta.json 中 `decorative=false` 的图，调用 agent 工具（**带 images=图片路径列表**）：

```
agent(
  model="xiaomi/mimo-v2.5",      # 任意支持图像输入的模型（provider/model 语法；effort 后缀会被剥除）
  images=[i001.png, i002.png],
  prompt="""你是 PDF 图像解析器。对每张图输出合法 JSON（严格数组）：
  [{"image_id":"i001","kind":"chart|photo|diagram|screenshot|formula|table_image|figure",
    "caption_title":"...","content_desc":"...","ocr_text":"图内逐字转录的全部文字",
    "values":{"关键数值/数据点":"..."},"confidence":"high|medium|low",
    "unreadable_reason":null}]
  规则：图内文字必须逐字转录；数值必须给出；无法识别写 unreadable_reason，禁止编造。""",
  max_steps=1, timeout=120)
```
- 批量：≤4 图/批；单图失败重试 1 次 → `unreadable` 单张降级
- **agent 不支持图片（返回错误/模型不支持）** → 终止 S4，全部图标记 `unreadable_agent`，
  在 `__overview__.md` 图表清单标注 ⚠️，`__protocol__.md` 记录原因，报告首行提醒
- 结果存 `vision.json`：`[{"image_id","page","kind","caption_title","content_desc","ocr_text","values","confidence","unreadable_reason"}]`

### S5 关联归档（脚本）
```python
from skills.pdfreader.merge_align import run
run(out_dir, vision_json_path)   # 图↔行区间关联 → images/iNNN.md + merged.json
```
行区间规则：同页行中，图 bbox 上方最近的 1 行（caption/上文）与下方最近的 1 行（图注/下文）组成区间 `[L_a..L_b]`。

### S6 汇总产物（脚本）
```python
from skills.pdfreader.build_overview import run
run(out_dir)   # → __info__.json / content.md(带锚点) / pages/ / tables/ / __overview__.md 骨架 / __index__.md
```
LLM 补充（可选优化）：基于每页预览与全文，充实 __overview__.md 摘要段与图表清单的一句话内容描述。

### S7 校验（脚本）
```python
from skills.pdfreader.verify import run
run(out_dir)   # → __protocol__.md + 校验报告 JSON
```
断言：行锚点唯一/页内连续、页号 1..P 连续、图-页匹配、表行区间合法、文件齐全、统计一致。

### S8 报告
输出：产物目录绝对路径 + 统计（页/行/表/图/已识别/未识别）+ ⚠️提醒段（agent 不可用或降级时置顶）。

## 边界与禁忌

- 扫描件 PDF（无文字层）：文字行提取结果为空 → 报告明确说明"扫描件需 OCR，本技能默认不做页级 OCR"
- 损坏/加密 PDF：异常抛给用户说明；不静默
- 不覆盖已存在输出目录（存在 → 追加 `-v2` 或询问；`--force` 显式重建）
- 图内文字不编造（低置信 → 标记 unreadable 或 confidence:low）


## 实测注意事项（2026-08-30 验证）

- **`.xkagent/docs/` 为系统 protected 目录（仅 summary 工具可写）** → 产物默认落 `{项目根}/docs/pdfreader_{pdfname}/` 或 `PDFREADER_OUTPUT_ROOT`
- **受限沙箱（plan/build）拒绝所有第三方模块** → 脚本仅 build-unsafe / 宿主 `!python3` 可运行；脚本内置 PyPI 纯 py wheel 自动下载注入（pdfminer.six，~6.4MB，首次约需 1-2 分钟）
- **视觉模型**：需选择支持图像输入的模型（provider/model 语法）；部分模型可能不兼容，建议先小样本实测确认
- **pdfminer 20260107 特性**：顶层布局元素为 `LTTextBoxHorizontal`（行在内部需递归），图像 XObject 呈现为 `LTFigure`（内含 `LTImage`），表格线是 `LTLine`；元素**无 obj_type/top/bottom 属性**，坐标一律 `x0/y0/x1/y1`（PDF y 向上，视觉行序 = y0 降序），用 `isinstance` 判断类型
- **锚点**：`[p{P:03d}-L{N:04d}]` 行号 = 页内视觉行序（y0 降序）；表格单元格 = 字符中心点归属（±0.5 容差）
