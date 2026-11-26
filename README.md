# Academic Paper Retriever

一个可独立安装的学术论文检索/下载技能。统一 CLI 覆盖 arXiv、PubMed、Semantic Scholar、Google Scholar；先直接下载 PDF，单篇失败后自动尝试 Sci-Hub 镜像。

## 特性

- 四个检索源 + 一个 DOI 回退入口，共用输出、过滤、引用和下载逻辑。
- 支持 `text`、`json`、`bibtex`、`ris`、`markdown` 五种输出。
- 直接下载优先：arXiv PDF、Semantic Scholar `openAccessPdf`、PubMed Central/Europe PMC、Crossref 链接。
- 直接链接失败时，按固定顺序尝试 8 个 Sci-Hub 镜像，并从页面 iframe/embed/meta/JS 中解析真实 PDF 地址。
- 遇到 CAPTCHA/Cloudflare 时，可选打开**可见 Chrome 窗口**由用户完成验证，脚本再自动检测 PDF 并复用浏览器会话下载；不自动破解验证码。
- 校验 `%PDF-` 文件头与大小，原子写入，保留 `download_manifest.json`（状态、URL、镜像、SHA-256）。
- 健壮解析 PubMed XML：只读取本文自己的 DOI/PMCID，不会被参考文献里的 ID 污染。
- 429/5xx 有界重试；不依赖 `arxiv`、`semanticscholar` 等额外 SDK。

## 安装

```bash
cd academic-paper-retriever
python -m pip install -r requirements.txt
# 可选：Google Scholar
python -m pip install -r requirements-optional.txt
# 可选：CAPTCHA/Cloudflare 可见浏览器人工验证
python -m pip install -r requirements-browser.txt
# 如果没有 Google Chrome，再安装 Playwright 的 Chromium
# python -m playwright install chromium
```

要求 Python 3.10+。核心依赖只有 `requests` 和 `beautifulsoup4`；不装 BeautifulSoup 时 Sci-Hub 页面仍可用正则解析。浏览器交接还需要 Playwright；本机有 Chrome 时不需要额外下载 Chromium。

## 快速开始

```bash
# 检索
python scripts/research.py arxiv "deep learning" --max-results 5

# 下载（直接链接优先，失败自动回落 Sci-Hub）
python scripts/research.py arxiv "deep learning" --download --output-dir papers/

# 带机器可读状态
python scripts/research.py arxiv "deep learning" --download \
  --output-dir papers/ --format json --output papers/results.json

# PubMed 全文链接
python scripts/research.py pubmed "cancer immunotherapy" --full-text

# Semantic Scholar 引用信息（无 key 时可能 429）
SEMANTIC_SCHOLAR_API_KEY="..." \
  python scripts/research.py semantic "graph neural networks" --min-citations 100

# DOI 回退
python scripts/research.py scihub "10.1038/s41586-020-2649-2" --download --output-dir papers/

# CAPTCHA/Cloudflare：打开可见浏览器，由用户完成验证
python scripts/scihub_browser.py "10.1038/s41586-020-2649-2" --output-dir papers/

# 引用导出
python scripts/research.py arxiv "transformers" --format bibtex --output refs.bib
```

## 在 Claude Code 中使用

本技能使用标准的 `SKILL.md` 结构，可直接作为 Claude Code Skill 安装：

```bash
# 个人级
mkdir -p ~/.claude/skills
cp -R academic-paper-retriever ~/.claude/skills/

# 或项目级
mkdir -p .claude/skills
cp -R academic-paper-retriever .claude/skills/
```

重新打开会话后使用 `/academic-paper-retriever`，或直接描述“帮我下载这篇论文 / 检索这个主题”让 Claude 自动触发。Claude Code 会替换技能正文中的 `${CLAUDE_SKILL_DIR}`，因此脚本和 `references/` 可以从技能目录稳定定位。需要允许 Bash，并在 Claude Code 的运行环境中安装 Python 依赖、放行相关学术站点与镜像域名。

## 目录

```text
academic-paper-retriever/
├── SKILL.md                     # 技能工作流与硬性规则
├── README.md
├── requirements.txt
├── requirements-optional.txt
├── requirements-browser.txt     # Playwright（可选）
├── references/
│   └── command-reference.md     # 完整参数、JSON 结构、退出码
├── scripts/
│   ├── research.py              # 检索/下载主 CLI
│   └── scihub_browser.py        # CAPTCHA 人工验证浏览器交接
└── tests/
    └── test_research.py         # 离线回归测试（不访问真实 API）
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用本地 HTTP 服务和假响应，覆盖：Sci-Hub 页面解析与回退、PDF 校验/清单、PubMed 参考文献 ID 污染、Semantic Scholar 解析、引用键、BibTeX/RIS/JSON，以及浏览器交接的 URL/验证页检测。当前离线测试共 13 项。

## 设计取舍

- **不静默伪造**：源未提供 DOI/年份/摘要时留空，不用猜测值填充。
- **逐篇回退**：一篇失败不会中断整批；`download_manifest.json` 给出每篇的失败原因。
- **有界重试**：网络/429 最多 4 次；镜像遍历一次后停止，避免对反爬页面硬刷。
- **人机交接**：CAPTCHA 只能由用户完成；浏览器脚本负责等待、检测和下载，不绕过验证。
- **PDF 真实性**：只接受 `%PDF-` 开头且大于 1 KB 的响应，HTML 登录页/Cloudflare 页面会被拒绝。
- **文件名可移植**：`第一作者_年份_标题_ID.pdf`，ASCII 化，限制长度。
- **不自动分发**：下载文件留在用户指定的 `--output-dir`，脚本不上传任何内容。

## 合规提示

Sci-Hub 回退默认开启以满足“直接失败后自动抓取”的工作流，可用 `--no-scihub` 关闭。仅在你有权访问、且当地法律和机构政策允许时使用；优先使用 arXiv、PubMed Central、机构订阅或开放获取版本。不要将下载内容再分发。
