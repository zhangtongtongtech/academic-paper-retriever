# Command Reference

`scripts/research.py` 是唯一入口。所有示例默认当前目录为本技能根目录；如从别处执行，用绝对路径替换 `scripts/research.py`。

```bash
python scripts/research.py <SOURCE> "<QUERY>" [OPTIONS]
```

`SOURCE` 可用值：

| 值 | 别名 | 说明 |
|---|---|---|
| `arxiv` | — | arXiv Atom API；预印本、分类、日期、作者 |
| `pubmed` | — | NCBI E-utilities；生物医学文献 |
| `semantic` | `semanticscholar`, `semantic-scholar` | Semantic Scholar Graph API |
| `scholar` | `google-scholar`, `google_scholar` | Google Scholar，需可选包 `scholarly` |
| `scihub` | `sci-hub` | 把 query 当作 DOI，用 Crossref 取元数据并下载 |

## 全局参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-n`, `--max-results N` | 10 | 1–100；Semantic Scholar 单次上限 100 |
| `-f`, `--format FMT` | `text` | `text`、`json`、`bibtex`、`ris`、`markdown` |
| `-o`, `--output PATH` | 无 | 写入文件；父目录自动创建；不写则输出到 stdout |
| `--sort-by` | `relevance` | `relevance`、`date`、`citations` |
| `--year YYYY` | 无 | 单年过滤；与显式 `--start-date`/`--end-date` 同时用时，显式日期优先未指定的那一端 |
| `--start-date YYYY-MM-DD` | 无 | 起始日期 |
| `--end-date YYYY-MM-DD` | 无 | 结束日期 |
| `--author "Last, First"` | 无 | 作者过滤；arXiv/PubMed 服务端过滤，Semantic/Scholar 客户端 best-effort |
| `--min-citations N` | 无 | 最小引用数；Semantic 服务端、Scholar 客户端；arXiv/PubMed 忽略并警告 |
| `--download` | 关 | 下载 PDF；直接链接优先，失败自动尝试 Sci-Hub |
| `--output-dir DIR` | `downloads` | PDF 与 `download_manifest.json` 的目录 |
| `--citations` | 关 | 不按 `--format` 输出检索结果，而是导出结果论文的引用记录 |
| `--citation-format` | `bibtex` | `bibtex`、`ris`、`json`；仅与 `--citations` 配合 |
| `--no-scihub` | 关 | 禁用 Sci-Hub 回退，只走直接/开放获取链接 |
| `--scihub-only` | 关 | 跳过直接链接直接走镜像；必须同时有 `--download` |
| `--timeout SEC` | 30 | HTTP 超时；网络/429/5xx 最多重试 4 次 |
| `--delay SEC` | 1.0 | 下载之间、镜像之间的礼貌等待；0 表示不等待 |
| `--verbose` | 关 | 把调试信息写到 stderr |
| `--version` | — | 版本号 |
| `-h`, `--help` | — | 帮助 |

`--no-scihub` 与 `--scihub-only` 互斥。`scihub` 子命令强制 `--download`，且不能与 `--no-scihub` 同用。

## 来源特定参数

### arXiv

| 参数 | 说明 |
|---|---|
| `--category CAT` | 可重复；多个分类按 OR 组合，例如 `--category cs.LG --category cs.CL` |

- 普通主题会自动包成 `all:"..."`；查询里已有字段前缀或 `AND`/`OR`/`ANDNOT` 时按原样传入。
- 日期映射到 `submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]`。
- `--sort-by citations` 无原生支持，回退 relevance 并警告。
- `--min-citations` 不支持；结果里没有引用数字段。
- 每条结果的 DOI 优先取 arXiv 元数据里的期刊 DOI，否则写 DataCite DOI `10.48550/arXiv.<id>`。

示例：

```bash
python scripts/research.py arxiv "quantum computing"
python scripts/research.py arxiv "neural networks" --category cs.LG --year 2023
python scripts/research.py arxiv "attention mechanism" --download --max-results 5 --output-dir papers/
python scripts/research.py arxiv "deep learning" --author "LeCun, Yann" --sort-by date
```

### PubMed

| 参数 | 说明 |
|---|---|
| `--publication-type TYPE` | 可重复；例如 `Clinical Trial`、`Review`、`Meta-Analysis` |
| `--full-text` | 有 PMCID 时输出 `full_text_url`（PMC 页面）；无论是否加此参数，PMC 可用时都会尝试 PDF |

- 使用 E-utilities `esearch` + `efetch`，不需要 NCBI key；`PAPERHUB_EMAIL` 或 `NCBI_EMAIL` 可加入礼貌池。
- 日期映射到 `[Date - Publication]` 区间。
- `--sort-by citations` 不支持，回退。
- DOI/PMCID 只从本文自己的 `PubmedData/ArticleIdList` 读取；参考文献里的 ID 不会污染结果。

示例：

```bash
python scripts/research.py pubmed "cancer immunotherapy"
python scripts/research.py pubmed "CRISPR" --start-date 2023-01-01 --end-date 2023-12-31
python scripts/research.py pubmed "covid vaccine" --publication-type "Clinical Trial" --full-text
python scripts/research.py pubmed "gene therapy" --download --max-results 3 --output-dir papers/
```

### Semantic Scholar

| 参数 | 说明 |
|---|---|
| `--include-references` | 请求每篇论文的参考列表，写入 `references` |
| `--include-citations` | 请求施引论文列表，写入 `citing_papers` |
| `--min-citations N` | 服务端 `minCitationCount` |
| `--sort-by citations` | 拉取最多 3× 候选后按引用数排序 |

- 无 key 时公共 API 经常返回 429。设置 `SEMANTIC_SCHOLAR_API_KEY` 或 `S2_API_KEY`。
- `--author` 没有服务端参数：脚本拉取更多候选后按姓名 token 客户端过滤，可能少于 `--max-results`。
- 返回 `citation_count`、`influential_citation_count`、`reference_count`、`fields_of_study`、`publication_types`。
- 无 DOI 时 arXiv ID 可作为直接下载线索。

示例：

```bash
python scripts/research.py semantic "reinforcement learning"
python scripts/research.py semantic "transformers" --min-citations 100 --sort-by citations
python scripts/research.py semantic "BERT" --include-references --include-citations
python scripts/research.py semantic "graph neural networks" --year 2022
```

### Google Scholar（可选）

```bash
python -m pip install -r requirements-optional.txt
python scripts/research.py scholar "graph neural networks" --max-results 10
```

- 依赖 `scholarly`，无官方 API；经常被 Google 限流或要求验证码。
- 元数据通常没有 DOI，`--download` 大多数情况下无法工作。
- `--author`、`--min-citations` 为客户端过滤。
- 若只需要可靠的引用/PDF，优先用 `semantic` 或 `arxiv`。

### DOI / Sci-Hub 回退

```bash
python scripts/research.py scihub "10.1038/s41586-020-2649-2" --download --output-dir papers/
python scripts/research.py scihub "https://doi.org/10.1038/s41586-020-2649-2" --scihub-only --download
```

流程：

1. 规范化 DOI（接受 `doi:`、`https://doi.org/...` 前缀）。
2. Crossref `works/{doi}` 取标题、作者、期刊、日期、摘要、链接；Crossref 失败不阻断下载，但标题会退化为 `DOI <doi>`。
3. 若无 `--scihub-only`，先尝试 Crossref `link` 中的 PDF/直接链接。
4. 失败后按镜像顺序请求：`sci-hub.se`、`.st`、`.ru`、`sci-net.xyz`、`.box`、`.red`、`.ren`、`.ee`。
5. 页面可能跳转到其他镜像；解析 iframe、embed、object、`citation_pdf_url`、`.pdf` 链接、`location.href`。
6. 校验响应以 `%PDF-` 开头且 ≥1 KB，再原子写入。

`PAPERHUB_SCIHUB_MIRRORS` 可覆盖顺序：

```bash
PAPERHUB_SCIHUB_MIRRORS="https://sci-hub.se,https://sci-hub.st" \
  python scripts/research.py scihub "10.1000/example" --download
```

arXiv DataCite DOI（`10.48550/arXiv.*`）不会走 Sci-Hub，因为镜像不收录这类 DOI；此时直接 PDF 失败会明确报错。

## 输出格式

### text

```text
Search Results: 1 papers found
Source: arxiv
Query: deep learning

1. The Modern Mathematics of Deep Learning
   Authors: Julius Berner; Philipp Grohs; Gitta Kutyniok; Philipp Petersen
   Published: 2021-05-09
   arXiv ID: 2105.04026
   DOI: 10.1017/9781009025096.002
   Categories: cs.LG, stat.ML
   Abstract: We describe the new field...
   PDF: https://arxiv.org/pdf/2105.04026v2
   File: papers/Berner_2021_..._2105.04026.pdf via direct
```

### json

`--format json` 输出数组，不夹带日志（日志走 stderr）。每个对象按存在情况包含：

```json
{
  "title": "...",
  "authors": ["..."],
  "source": "arXiv",
  "published": "2024-01-02",
  "abstract": "...",
  "doi": "10...",
  "arxiv_id": "2401.00001",
  "pmid": "123",
  "pmcid": "PMC123",
  "paper_id": "Semantic Scholar paperId",
  "venue": "Journal / conference",
  "categories": ["cs.LG"],
  "pdf_url": "https://...",
  "full_text_url": "https://...",
  "url": "https://...",
  "citation_count": 42,
  "influential_citation_count": 4,
  "reference_count": 10,
  "fields_of_study": ["Computer Science"],
  "publication_types": ["JournalArticle"],
  "references": [],
  "citing_papers": [],
  "download": {
    "status": "downloaded",
    "file": "papers/....pdf",
    "via": "direct",
    "url": "https://...",
    "bytes": 123456,
    "sha256": "...",
    "attempts": [{"url": "https://..."}]
  }
}
```

`download.status`：

- `downloaded`：本次下载并验证成功；
- `existing`：同名有效 PDF 已存在，未重复下载；
- `failed`：失败，`error` 给出原因。

`download.via`：`direct` 或 `scihub`；`mirror` 仅 Sci-Hub 成功时出现。`attempts` 只给失败项附 `error`。

### bibtex

```bibtex
@article{berner2021modern,
  title = {The Modern Mathematics of Deep Learning},
  author = {Berner, Julius and Grohs, Philipp},
  journal = {arXiv preprint arXiv:2105.04026},
  year = {2021},
  doi = {10.1017/9781009025096.002},
  url = {https://arxiv.org/abs/2105.04026v2},
  eprint = {2105.04026},
  archivePrefix = {arXiv},
  primaryClass = {cs.LG}
}
```

- 引用键：第一作者姓 + 年份 + 标题首个实词；重名自动加 `a`/`b` 后缀。
- 特殊字符按 LaTeX 转义；UTF-8 作者名保留。
- arXiv 条目统一用 `@article` + `journal = arXiv preprint`，与需求示例一致；需要严格 `@misc` 时可在下游改写。

### ris

```text
TY  - JOUR
TI  - The Modern Mathematics of Deep Learning
AU  - Berner, Julius
PY  - 2021
DA  - 2021/05/09
JO  - arXiv preprint
VL  - arXiv:2105.04026
DO  - 10.1017/9781009025096.002
UR  - https://arxiv.org/abs/2105.04026v2
ER  - 
```

### markdown

标题、作者、日期、ID、DOI、摘要、PDF/全文链接、下载状态；适合直接粘进笔记或报告。

### 引用记录（`--citations`）

```bash
python scripts/research.py arxiv "attention mechanism" \
  --citations --citation-format bibtex --output citations.bib
```

- `--citations` 优先于 `--format`。
- `--citation-format json` 会在每篇 JSON 里加 `citation_key`。
- 语义是“为本次检索到的论文生成引用”，不是“解析这些论文的参考文献”。要后者用 Semantic Scholar 的 `--include-references`。

## 下载行为

1. 生成文件名：`第一作者_年份_标题_ID.pdf`；ASCII 化、限长。
2. 目标文件已存在且通过 PDF 校验：`existing`，不重复下载。
3. 目标存在但损坏：改用 `-2.pdf`，不覆盖原文件。
4. 直接 URL 顺序：来源 PDF 直链 → arXiv 双域名 → PMCID 的 Europe PMC/NCBI → Crossref links → DOI 解析（仅特定来源）。
5. 每个响应检查 HTTP 200、`%PDF-` 开头、大于 1 KB、小于 200 MB。
6. 直接失败且 DOI 存在：逐镜像 Sci-Hub。
7. 写 `download_manifest.json`；合并历史记录，按 DOI/arXiv ID/标题去重，最多保留 500 条。

`download_manifest.json`：

```json
{
  "updated_at": "2026-01-01T00:00:00+00:00",
  "source": "arxiv",
  "query": "deep learning",
  "downloads": [
    {
      "title": "...",
      "authors": ["..."],
      "published": "2024-01-02",
      "source": "arXiv",
      "doi": "10...",
      "arxiv_id": "2401.00001",
      "download": {"status": "downloaded", "file": "papers/....pdf", "via": "direct"},
      "recorded_at": "2026-01-01T00:00:00+00:00",
      "file": "papers/....pdf"
    }
  ]
}
```

## 常见工作流

### 文献综述

```bash
# 1. 多源检索，保存 JSON
python scripts/research.py arxiv "graph neural networks" --max-results 20 --format json --output arxiv_gnn.json
python scripts/research.py semantic "graph neural networks" --max-results 20 --format json --output semantic_gnn.json

# 2. 下载关键论文
python scripts/research.py arxiv "graph neural networks" --download --max-results 10 --output-dir papers/gnn/

# 3. 生成书目
python scripts/research.py arxiv "graph neural networks" --max-results 20 --format bibtex --output gnn_references.bib
```

### 近期论文

```bash
python scripts/research.py arxiv "large language models" --year 2023 --sort-by date --max-results 30
python scripts/research.py pubmed "gene therapy" \
  --start-date 2023-11-01 --end-date 2023-11-30 --format markdown --output recent_gene_therapy.md
```

### 高引用论文

```bash
python scripts/research.py semantic "reinforcement learning" \
  --min-citations 500 --sort-by citations --max-results 25
```

### 作者追踪

```bash
python scripts/research.py arxiv "deep learning" --author "LeCun, Yann" --sort-by date --max-results 50
python scripts/research.py pubmed "cancer" --author "Smith, John"
```

Semantic/Scholar 的 `--author` 是客户端过滤；若作者论文不在候选集中，可能返回 0 条。

### 已知 DOI 下载

```bash
python scripts/research.py scihub "10.1053/j.gastro.2018.03.029" --download --output-dir papers/
```

### 只要开放获取

```bash
python scripts/research.py arxiv "deep learning" --download --no-scihub --output-dir papers/
python scripts/research.py pubmed "cancer" --download --no-scihub --output-dir papers/
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar API key（也接受 `S2_API_KEY`） |
| `PAPERHUB_SCIHUB_MIRRORS` | 逗号分隔的镜像列表，覆盖默认顺序 |
| `PAPERHUB_EMAIL` / `NCBI_EMAIL` | NCBI/Crossref 礼貌池联系邮箱 |
| `PAPERHUB_USER_AGENT` | 自定义 HTTP User-Agent |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功；请求下载时至少一篇 `downloaded`/`existing` |
| 1 | API/网络错误（含 429、超时、不可解析响应） |
| 2 | 参数/本地文件错误 |
| 3 | 缺少可选依赖（如 `scholarly`） |
| 4 | 检索成功但所有 PDF 均失败 |
| 130 | 中断 |

部分下载失败仍返回 0；以 JSON/manifest 为准。

## 限制

- `--max-results` 上限 100；不提供分页参数。
- arXiv/PubMed 不提供引用数；`--sort-by citations`/`--min-citations` 对它们无效。
- PubMed 直链（Europe PMC/NCBI）可能被 Cloudflare 403，脚本会继续回退。
- Google Scholar 无官方 API，可能返回空、429 或验证码；不保证稳定。
- Semantic Scholar 公共额度紧张；大批量任务应使用 API key。
- `--full-text` 目前只输出 PMC 链接，不解析全文。
- 不解析 PDF 内文，不抽取文内参考文献；`--citations` 是结果论文的书目记录。
- Sci-Hub 域名、页面结构、反爬策略会变化；失败时报告具体 DOI 和错误，不承诺一定可下载。

## 合规

Sci-Hub 回退默认开启，以满足“直接失败后自动抓取”的工作流；可用 `--no-scihub` 关闭。仅在你有权访问、且当地法律和机构政策允许时使用。优先 arXiv、PubMed Central、机构订阅或开放获取版本；不要再分发下载内容。
