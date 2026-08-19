# 申论帮 Essay-Grading

> AI 驱动的公务员申论备考平台 —— 智能批改 · 批改问答 · 卷库训练 · 能力诊断

**当前版本：v1.0.1**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.0-green.svg)](https://flask.palletsprojects.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

---

## 目录

- [项目简介](#项目简介)
- [核心功能](#核心功能)
- [系统架构](#系统架构)
- [技术栈](#技术栈)
- [快速部署](#快速部署)
- [项目结构](#项目结构)
- [更新日志](#更新日志)
- [许可证](#许可证)

---

## 项目简介

申论帮是一个面向公务员考试申论科目的 AI 智能备考平台。平台集成了多种大语言模型，通过多模型 Ensemble 评分机制，为考生提供客观、精准的申论批改服务。同时提供卷库训练、能力诊断、好词好句积累、备考计划等功能，覆盖申论学习的全流程。

### 为什么选择申论帮？

| 特点 | 说明 |
|------|------|
| **多模型评分** | 支持 6+ 种大模型同时批改，Ensemble 综合评分，有效消除单一模型的偏差 |
| **分题型评分** | 针对归纳概括、综合分析、提出对策、贯彻执行、大作文五种题型，使用不同的评分维度和提示词 |
| **采分点覆盖法** | 逐条对照参考答案的采分点，判断 full/partial/none，而不是笼统打分 |
| **手写识别** | 支持拍照上传手写答案，OCR 自动识别（百度/腾讯/Tesseract） |
| **批改问答** | 针对批改结果与 AI 对话，追问扣分原因和改进方法 |
| **零配置部署** | Docker 一键部署，SQLite 数据库无需额外配置 |

---

## 核心功能

### 1. AI 智能批改

平台的核 心功能，支持五种申论题型的自动评分：

| 题型 | 评分维度 | 评分方式 |
|------|----------|----------|
| 归纳概括 | 踩点命中(70%)、语言简洁(15%)、归纳准确(10%)、条理清晰(5%) | 采分点逐条覆盖判定 |
| 综合分析 | 逻辑链(30%)、覆盖(30%)、深度(20%)、语言(10%)、格式(10%) | 逻辑链完整性 + 要点覆盖 |
| 提出对策 | 问题定位(20%)、针对性(25%)、可行性(25%)、具体性(20%)、格式(10%) | 万能对策检测 + 可行性评估 |
| 贯彻执行 | 格式(20%)、目的(25%)、内容(30%)、语言(15%)、字数(10%) | 9 种文种格式检查 |
| 大作文 | 立意(25%)、论证(25%)、结构(20%)、语言(20%)、创新(10%) | 分档赋分制（四档） |

**多模型协作模式：**

- **Ensemble 模式**：多个模型同时批改，综合评分，消除单一模型偏差
- **Fallback 模式**：按优先级依次尝试，任一模型成功即返回结果
- **单模型模式**：指定单个模型快速批改

**支持的 AI 模型：**

| 模型 | 提供商 | 特点 |
|------|--------|------|
| DeepSeek V4 Pro | DeepSeek | 主力批改模型，综合能力强 |
| DeepSeek V4 Flash | DeepSeek | 快速批改，成本低 |
| LongCat-2.0 | LongCat | 推理模型，深度分析 |
| GPT-5.6-Sol | OpenAI 兼容 | 高精度批改 |
| Claude Opus 4.8 | Anthropic 兼容 | 语言表达评估 |
| 豆包 Seed 2.1 Pro | 火山引擎 | 国产模型，中文优化 |

### 2. 批改问答

批改完成后，用户可以针对批改结果与 AI 进行多轮对话：

- 追问扣分原因："我为什么这道题扣了这么多分？"
- 请求改进指导："如何改进我的答案？请给我一个修改示例"
- 学习答题思路："参考答案为什么要这样写？答题思路是什么？"
- 掌握题型框架："这类题型的答题框架和步骤是什么？"

对话记录存储在服务器端，按用户和批改记录隔离，换设备登录也能看到历史对话。

### 3. 真题卷库

平台内置 1000+ 套申论真题，覆盖：

- **国考**：2011-2026 年中央机关及其直属机构考试
- **省考**：31 个省/自治区/直辖市历年真题
- **联考**：多省联考真题
- **事业单位**：综合应用能力测试

每套试卷包含完整的题目、给定材料、参考答案和采分点。支持按年份、省份、考试类型筛选。

### 4. 题型训练

针对五种申论题型的专项练习：

- 按题型分类刷题，强化薄弱环节
- 记录每次练习的得分和维度表现
- 追踪各题型的进步趋势

### 5. 能力诊断

基于批改数据的多维度能力分析：

- **雷达图**：五维度能力可视化（踩点命中、逻辑分析、对策针对性、文书写作、大作文立意）
- **薄弱点识别**：自动识别低于平均水平的维度
- **针对性推荐**：根据薄弱点推荐对应的题型练习

### 6. 素材学习

- **好词好句**：人民日报、求是网、新华网精选素材，按主题分类
- **时政热点**：最新时政热点整理，关联申论考点
- **AI 造段**：根据主题自动生成申论规范表达

### 7. OCR 手写识别

支持拍照上传手写答案，自动识别为文字：

- **百度 OCR**：手写模型，识别率最高（免费 500次/天）
- **腾讯云 OCR**：通用高精度（免费 1000次/月）
- **Tesseract**：本地离线识别，无需联网

图像预处理流水线：灰度化 → 对比度增强 → 自适应二值化 → 去噪 → 锐化

### 8. 管理后台

完整的管理后台，支持以下功能：

| 模块 | 功能 |
|------|------|
| 工作台 | 总览统计（用户数、批改数、VIP 转化） |
| 用户管理 | 用户列表、VIP 管理、积分管理 |
| 试卷管理 | 试卷编辑、题目管理、采分点配置 |
| 模型管理 | 模型增删改、测试连接、权重配置、价格配置 |
| 功能模型配置 | 为不同功能绑定不同模型（批改/问答/素材生成等） |
| 批改复核 | 查看批改详情、修改评分、变更复核状态 |
| 批改消耗 | 每次批改的模型、Token、费用统计（支持搜索筛选） |
| 数据统计 | 用户增长、分数分布、Token 消耗图表 |
| 兑换码管理 | 生成/管理兑换码 |
| 系统设置 | 全局参数配置 |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                      用户端                              │
│  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────────────┐  │
│  │ 智能批改 │ │ 批改问答  │ │ 卷库   │ │ 题型训练/诊断 │  │
│  └────┬────┘ └────┬─────┘ └───┬────┘ └──────┬───────┘  │
│       │           │           │              │           │
│  ┌────┴───────────┴───────────┴──────────────┴───────┐  │
│  │                  Flask API 层                      │  │
│  │  auth / papers / submissions / chat / ocr / admin  │  │
│  └──────────────────────┬────────────────────────────┘  │
│                         │                               │
│  ┌──────────────────────┴────────────────────────────┐  │
│  │                   服务层                           │  │
│  │  Grader Engine (prompts / scorer / ensemble)      │  │
│  │  OCR Service (baidu / tencent / tesseract)        │  │
│  │  Token Usage Service                              │  │
│  │  Model Registry                                   │  │
│  └──────────────────────┬────────────────────────────┘  │
│                         │                               │
│  ┌──────────────────────┴────────────────────────────┐  │
│  │                   数据层                           │  │
│  │  SQLite (submissions / papers / users / chat_msgs)│  │
│  │  Token Usage Logs                                 │  │
│  └───────────────────────────────────────────────────┘  │
│                         │                               │
│  ┌──────────────────────┴────────────────────────────┐  │
│  │              外部 AI 模型（OpenAI 兼容 API）        │  │
│  │  DeepSeek / LongCat / GPT / Claude / Doubao       │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 批改流程

```
用户提交答案
    │
    ▼
创建 Submission 记录
    │
    ├── 多模型模式 ──→ 选择模型（Ensemble/Fallback）
    │       │
    │       ├── 模型 A ──→ 构建 Prompt ──→ 调用 LLM ──→ 解析 JSON ──→ JudgeResult
    │       ├── 模型 B ──→ 构建 Prompt ──→ 调用 LLM ──→ 解析 JSON ──→ JudgeResult
    │       └── 模型 C ──→ 构建 Prompt ──→ 调用 LLM ──→ 解析 JSON ──→ JudgeResult
    │       │
    │       └── 聚合评分 ──→ 加权综合分 ──→ 一致性检验 ──→ 最终结果
    │
    └── 单模型模式 ──→ 构建 Prompt ──→ 调用 LLM ──→ 本地规则校验 ──→ 最终结果
    │
    ▼
记录 Token 消耗 + 费用
    │
    ▼
返回批改结果（分数 + 维度 + 采分点 + 反馈 + 建议）
```

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端框架 | Flask 3.0 | 轻量级 Python Web 框架 |
| 数据库 | SQLite | 零配置，单文件部署，适合中小规模应用 |
| 服务容器 | Gunicorn | 生产级 WSGI 服务器，2 workers |
| AI 引擎 | OpenAI 兼容 API | 支持所有兼容 OpenAI Chat Completions 协议的模型 |
| 前端 | HTML5 + Vanilla JS | 无框架依赖，轻量快速 |
| 图表 | Chart.js | 管理后台数据可视化 |
| 动画 | GSAP + ScrollTrigger | 页面滚动动画 |
| 平滑滚动 | Lenis | 非固定布局页面的平滑滚动 |
| 图标 | Lucide | 开源 SVG 图标库 |
| 部署 | Docker + Docker Compose | 一键部署，环境隔离 |
| OCR | 百度 OCR / 腾讯云 OCR / Tesseract | 手写识别 + 图像预处理 |
| Python 版本 | 3.12 | 使用最新语言特性 |

---

## 快速部署

### 前置要求

- Docker 20.10+
- Docker Compose 2.0+

### 1. 克隆项目

```bash
git clone https://github.com/1523826455647/Essay-Grading.git
cd Essay-Grading
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填写以下必填项：

```env
# 管理员账号（首次启动自动创建）
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你的强密码（至少16位）

# LLM API 配置（至少配置一个）
LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 安全密钥（部署脚本自动生成，勿使用默认值）
SECRET_KEY=随机字符串
JWT_SECRET=随机字符串
LLM_CREDENTIALS_KEY=加密密钥
```

### 3. 启动服务

```bash
docker-compose up -d
```

访问 `http://localhost:18004` 即可使用。

### 4. 可选：配置 OCR

```env
# 百度 OCR（推荐，手写识别准确率高）
BAIDU_OCR_API_KEY=你的API_KEY
BAIDU_OCR_SECRET_KEY=你的SECRET_KEY

# 腾讯云 OCR
TENCENT_OCR_SECRET_ID=你的SECRET_ID
TENCENT_OCR_SECRET_KEY=你的SECRET_KEY
```

### 5. 本地开发

```bash
pip install -r requirements.txt
python -m src.app
```

---

## 项目结构

```
Essay-Grading/
├── src/
│   ├── api/                    # API 路由层
│   │   ├── admin.py            # 管理后台接口
│   │   ├── auth.py             # 认证接口
│   │   ├── custom_grade.py     # 自定义批改
│   │   ├── grading_chat.py     # 批改问答（含对话存取）
│   │   ├── ocr.py              # OCR 识别接口
│   │   ├── papers.py           # 试卷接口
│   │   ├── submissions.py      # 批改提交接口
│   │   └── utils.py            # 工具函数（DB、JWT、迁移）
│   ├── services/               # 业务逻辑层
│   │   ├── grader/             # 批改引擎
│   │   │   ├── prompts.py      # 五种题型 System Prompt
│   │   │   ├── scorer.py       # 评分主流程
│   │   │   ├── ensemble.py     # 多模型 Ensemble
│   │   │   ├── provider_adapters.py  # 模型适配器
│   │   │   ├── llm_client.py   # LLM 调用客户端
│   │   │   ├── dimensions.py   # 维度评分定义
│   │   │   ├── backends.py     # 批改后端抽象
│   │   │   └── summarize.py    # 多模型结果汇总
│   │   ├── ocr_service.py      # OCR 服务（百度/腾讯/Tesseract）
│   │   ├── token_usage_service.py  # Token 消耗记录
│   │   ├── model_registry.py   # 模型注册管理
│   │   ├── feature_model_service.py  # 功能模型绑定
│   │   ├── submission_service.py  # 批改记录服务
│   │   └── ...                 # 其他业务服务
│   ├── config.py               # 全局配置
│   └── app.py                  # Flask 应用入口
├── templates/                  # Jinja2 模板
│   ├── admin/                  # 管理后台页面
│   │   ├── dashboard.html      # 工作台
│   │   ├── users.html          # 用户管理
│   │   ├── papers.html         # 试卷管理
│   │   ├── models.html         # 模型管理
│   │   ├── reviews.html        # 批改复核
│   │   ├── stats.html          # 数据统计
│   │   ├── usage.html          # 批改消耗
│   │   └── ...
│   ├── chat.html               # 批改问答页
│   ├── exam.html               # 答题页
│   ├── result.html             # 批改结果页
│   ├── custom_grade.html       # 自定义批改
│   └── ...                     # 其他前端页面
├── static/                     # 静态资源
│   ├── css/                    # 样式表
│   │   ├── admin.css           # 管理后台样式
│   │   ├── main.css            # 主样式
│   │   └── ...
│   └── js/                     # JavaScript
│       ├── components.js       # 通用组件
│       └── ...
├── data/
│   └── schema.sql              # 数据库表结构定义
├── gen_training.py             # 大作文微调训练数据生成脚本
├── Dockerfile                  # Docker 镜像构建文件
├── docker-compose.yml          # Docker Compose 配置
├── requirements.txt            # Python 依赖
├── .env.example                # 环境变量模板
└── README.md                   # 本文件
```

---

## 更新日志

### v1.0.1 (2026-08-19)

**新功能：**
- 管理后台新增「批改消耗」页面：按用户/试卷/日期筛选，查看每次批改的模型、Token、费用
- 批改复核功能完善：支持查看完整题目、参考答案、模型判断详情、Token 消耗，可修改评分和状态
- 豆包（Doubao）模型支持：修复火山引擎 `/v3` 路径拼接问题
- 大作文微调训练数据生成脚本（787 条 Qwen Chat Format）

**Bug 修复：**
- 修复批改复核详情页加载失败（404 → 补充 GET 路由 + 修复 import）
- 修复 Token 记录线程不安全（Flask `g` 对象依赖 → 独立 `sqlite3.connect()`）
- 修复 LongCat-2.0 推理模型仅返回 `reasoning_content` 导致批改失败
- 修复编辑/新增模型弹窗 CSS 对齐问题
- 修复 `max_tokens` 全局限制（0=不限制，不再强制 8000）
- 修复批改问答 Markdown 未渲染（列表样式、表格边框、占位符泄漏）
- 修复批改问答页面鼠标滚轮无法滚动（Lenis 拦截）

### v1.0.0 (2026-08-19)

- 初始正式版发布
- 五种题型 AI 批改 + 多模型 Ensemble/Fallback
- 批改问答（服务端对话存储，按用户+批改记录隔离）
- OCR 手写识别（百度/腾讯/Tesseract，图像预处理）
- 1000+ 套真题卷库
- 管理后台（用户/试卷/模型/复核/统计/日志）
- Docker 一键部署

---

## 许可证

MIT License

Copyright (c) 2026 申论帮