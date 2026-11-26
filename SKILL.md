---
name: academic-paper-retriever
description: 'Search, download, and cite academic papers from arXiv, PubMed, Semantic Scholar, and Google Scholar from one CLI. Use when the user asks to find papers, run a literature search, download a paper/PDF or DOI, build a bibliography, or export BibTeX/RIS/JSON/Markdown. Direct PDF download is attempted first; Sci-Hub mirrors are an automatic per-paper fallback. 触发词：找论文、检索文献、下载论文、抓取论文、文献综述检索、生成参考文献、导出 BibTeX。'
---

# Academic Paper Retriever

## 目标

用一个脚本完成：多源论文检索 → 结果格式化 → 引用导出 → PDF 下载。下载时先尝试直接/开放获取地址；单篇失败后自动按 `--doi` 尝试 Sci-Hub 镜像。所有状态必须如实报告，不得伪造元数据或“下载成功”。

## 路径与安装

`SKILL_DIR` 是本 `SKILL.md` 所在目录。安装依赖时可进入该目录：

```bash
# Claude Code 会替换 ${CLAUDE_SKILL_DIR}；其他环境改用 SKILL.md 的绝对路径
cd "${CLAUDE_SKILL_DIR}"
python -m pip install -r requirements.txt
# 仅在需要 Google Scholar 时安装（可选、可能被反爬）
python -m pip install -r requirements-optional.txt
```

非 Claude Code 环境（例如 DSH）用：

```bash
cd "<SKILL_DIR>"
```

如果希望下载的 `papers/` 落在当前项目而不是技能目录，不要 `cd` 进技能目录，直接从项目目录调用绝对路径：

```bash
# Claude Code
python "${CLAUDE_SKILL_DIR}/scripts/research.py" arxiv "deep learning" --download --output-dir papers/

# 其他环境
python "<SKILL_DIR>/scripts/research.py" arxiv "deep learning" --download --output-dir papers/
```

要求 Python 3.10+。不需要安装 `arxiv`、`semanticscholar` 或 `pubmed-parser` 包。若系统没有 `python`，把命令中的 `python` 换成 `python3`。

### Claude Code 安装

本技能遵循 Agent Skills 的 `SKILL.md` 结构，可直接放到 Claude Code 的技能目录：

```bash
# 个人级：所有项目可用
mkdir -p ~/.claude/skills
cp -R academic-paper-retriever ~/.claude/skills/

# 或项目级：只对当前仓库生效
mkdir -p .claude/skills
cp -R academic-paper-retriever .claude/skills/
```

重启/重新打开 Claude Code 会话后，可以用 `/academic-paper-retriever` 直接调用，也可以让 Claude 根据 description 自动触发。Claude Code 的 Bash 工具需要被允许执行，且其运行环境必须安装 `requests`、能访问 arXiv/PubMed/Semantic Scholar/Crossref；若启用了网络白名单或沙箱，需要放行这些域名。

本技能没有声明 `allowed-tools`，因此 Claude Code 不会静默预授权任意 Bash 命令，下载/联网操作仍按你的权限设置确认。

## 硬性规则

1. 先确认用户要的是“检索/引用”还是“下载 PDF”。用户只给主题时不要默认下载 10 篇；用户明确要下载时才用 `--download`。
2. 用户给出 DOI 但没给来源时，用 `scihub` 子命令按 DOI 取元数据并下载；不要凭空猜标题。
3. 下载后必须验证文件真实存在、以 `%PDF-` 开头、不是 HTML 登录页。以 `papers/download_manifest.json` 或 JSON 输出里的 `download.status` 为准，不凭命令退出码臆测。
4. 元数据缺失就保持为空并在结果中说明；不要补造 DOI、年份、作者或摘要。
5. Sci-Hub 只作为直接获取失败后的逐篇回退。若用户要求仅开放获取，加 `--no-scihub`。只在用户有权访问、且当地法律与机构政策允许的场景使用；不要把下载的 PDF 再分发。
6. 遇到 429、Cloudflare、CAPTCHA 不要反复硬刷。完成一轮镜像尝试后停止并报告，让用户改用机构订阅、开放获取版本或稍后重试。

## 标准工作流

### 1. 选择来源

| 需求 | 来源 | 说明 |
|---|---|---|
| CS / ML / 物理 / 数学预印本 | `arxiv` | 支持分类、年份、日期、作者、直接 PDF |
| 生物医学 / 临床 / 基因 | `pubmed` | 支持 MeSH 式查询、发表日期、文献类型、PMC 全文链接 |
| 引用数 / 引用网络 / 跨学科 | `semantic` | 引用数、影响力引用、参考列表；无 API key 时易 429 |
| 广谱但无 API | `scholar` | 可选依赖 `scholarly`，经常被 Google 限流 |
| 已知 DOI / 直接回退 | `scihub` | 先用 Crossref 取元数据，再尝试直接链接和镜像 |

主题跨领域时，分别跑 `arxiv` 和 `semantic` 做交叉验证。没有 `all` 聚合命令，不要编造。

### 2. 下载：第一步永远先试直接抓取

用户明确要下载时，执行（把查询替换为用户主题；默认 10 篇）：

```bash
python scripts/research.py arxiv "deep learning" --download --output-dir papers/
```

这是规定的第一步。脚本内部顺序是：

1. 查询该来源并拿到 PDF 直链/开放获取链接；
2. 逐篇尝试直接下载并校验 PDF；
3. 单篇直接失败且该文有 DOI 时，自动按顺序尝试 Sci-Hub 镜像；
4. 全部失败才标记 `failed`。

建议同时保留机器可读结果，便于后续判断回退是否发生：

```bash
python scripts/research.py arxiv "<topic>" --download --max-results 10 \
  --output-dir papers/ --format json --output papers/results.json
```

退出码 `4` 表示所有请求的 PDF 都失败；`0` 表示至少一篇成功或已存在。部分失败仍可能是 `0`，必须检查 `results.json` / `download_manifest.json`。

### 3. 手动补抓（直接下载已失败时）

如果第一步没有拿到 PDF，且结果中有 DOI，逐篇补抓：

```bash
python scripts/research.py scihub "10.1038/s41586-020-2649-2" \
  --download --output-dir papers/
```

只想跳过直接链接、直接走镜像时加 `--scihub-only`。镜像顺序固定为：

```text
sci-hub.se → sci-hub.st → sci-hub.ru → sci-net.xyz → sci-hub.box → sci-hub.red → sci-hub.ren → sci-hub.ee
```

页面会跳转到当前可用镜像（例如 `sci-hub.jp`）；脚本以最终页面解析 PDF，不把跳转当作失败。`PAPERHUB_SCIHUB_MIRRORS` 可用逗号分隔覆盖顺序，便于测试或使用用户指定的镜像。

### 4. 检索、引用、导出

```bash
# 文本结果
python scripts/research.py arxiv "quantum computing"

# 指定分类 + 年份
python scripts/research.py arxiv "neural networks" --category cs.LG --year 2023

# PubMed 日期区间 + 文献类型 + PMC 全文链接
python scripts/research.py pubmed "cancer immunotherapy" \
  --start-date 2023-01-01 --end-date 2023-12-31 \
  --publication-type "Clinical Trial" --full-text

# Semantic Scholar 高引用论文，包含参考列表
python scripts/research.py semantic "BERT" --min-citations 100 --include-references

# 生成参考文献
python scripts/research.py arxiv "transformers" --format bibtex --output refs.bib
python scripts/research.py pubmed "gene editing" --format ris --output genes.ris

# --citations 导出“检索到的论文”的引用记录，而不是解析每篇论文的参考文献
python scripts/research.py arxiv "attention mechanism" \
  --citations --citation-format bibtex --output citations.bib
```

`--include-references` 和 `--include-citations` 仅 Semantic Scholar 支持；后者会把施引论文列表写入 JSON。`--citations` 是导出结果论文的引用记录，两者不要混淆。

## 输出契约

- 默认 `text`；支持 `json`、`bibtex`、`ris`、`markdown`。
- 用 `--output <file>` 落盘；用 `--format json` 获得稳定结构。
- JSON 每篇论文包含 `title`、`authors`、`source`、`published`、`abstract`，以及可用的 `doi`、`arxiv_id`、`pmid`、`pmcid`、`paper_id`、`venue`、`categories`、`pdf_url`、`full_text_url`、引用计数等；下载后附加 `download`。
- 每个下载目录写 `download_manifest.json`，记录状态、文件、来源 URL、镜像、字节数、SHA-256 和尝试列表。
- 同名 PDF 已存在且有效时标记 `existing`，不重复下载；损坏的同名文件会改名为 `-2.pdf` 再写。

向用户汇报时至少包含：来源与查询、命中数、成功下载数、失败条目及原因、PDF 路径、链接/引用文件路径。不得把 `failed` 说成 `downloaded`。

## 失败处理速查

| 现象 | 处理 |
|---|---|
| Semantic Scholar `429` | 等待后重试；设置 `SEMANTIC_SCHOLAR_API_KEY` 或 `S2_API_KEY`；仍失败则改用 arXiv/PubMed |
| `scholarly` 未安装 | `python -m pip install -r requirements-optional.txt`；装后仍被 Google 拦截就停用该源 |
| Sci-Hub DNS/403/CAPTCHA | 已自动尝试剩余镜像；仍失败就报告 DOI，建议机构访问/开放版本，不要循环重跑 |
| PubMed PMC 直链 403 | 属常见反爬；脚本会继续 Sci-Hub 回退；也可让用户从 PMC 页面手动获取 |
| 0 条结果 | 放宽查询、去掉 `--author`/`--category`/日期限制，或换来源 |
| 全部下载失败，退出 4 | 读取 JSON 中每篇的 `download.error`，逐篇处理；不要只重跑整批 |
| 缺少 `requests` | `python -m pip install -r requirements.txt` |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 检索成功；若请求下载，则至少一篇下载成功或已存在 |
| 1 | 远程 API 错误、网络错误、429 等 |
| 2 | 参数/本地文件错误 |
| 3 | 缺少依赖（如 `scholarly`） |
| 4 | 检索成功但所有 PDF 下载失败 |
| 130 | 用户中断 |

完整参数、JSON 字段和更多示例见 `references/command-reference.md`。
