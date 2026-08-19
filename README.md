# 申论帮 Essay-Grading

> AI 驱动的公务员申论备考平台 — 智能批改 · 批改问答 · 卷库训练 · 能力诊断

**当前版本：v1.0.0**

---

## 核心功能

| 模块 | 说明 |
|------|------|
| **AI 智能批改** | 支持五种题型（归纳概括、综合分析、提出对策、贯彻执行、大作文），多模型 Ensemble 评分 |
| **批改问答** | 针对批改结果与 AI 对话，追问扣分原因、改进方法，对话记录服务端存储 |
| **真题卷库** | 1000+ 套真题，覆盖国考 + 省考 + 联考，支持自定义批改 |
| **题型训练** | 归纳概括、综合分析、提出对策、贯彻执行、大作文，分类专项练习 |
| **能力诊断** | 雷达图多维度分析，自动识别薄弱点，推荐针对性练习 |
| **素材学习** | 好词好句、时政热点，支持收藏分类 |
| **备考计划** | 个性化学习计划，进度追踪 |
| **OCR 识别** | 拍照识别手写答案，支持百度 OCR / 腾讯云 OCR / Tesseract |
| **管理后台** | 用户管理、试卷管理、模型管理、批改复核、Token 消耗统计、数据统计 |

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Flask 3.0 + Python 3.12 |
| 数据库 | SQLite（零配置，单文件部署） |
| AI 引擎 | OpenAI 兼容 API，支持多模型 Ensemble / Fallback |
| 前端 | HTML5 + Vanilla JS + Chart.js + GSAP 动画 |
| 部署 | Docker + Gunicorn + Caddy / Nginx |
| OCR | 百度 OCR（手写模型）/ 腾讯云 OCR / Tesseract |

## 支持模型

| 模型 | 协议 | 说明 |
|------|------|------|
| DeepSeek V4 Pro | OpenAI | 主力批改模型 |
| DeepSeek V4 Flash | OpenAI | 快速批改 |
| LongCat-2.0 | OpenAI | 推理模型 |
| gpt-5.6-sol | OpenAI | 辅助批改 |
| claude-opus-4-8 | OpenAI | 辅助批改 |
| doubao-seed-2-1-pro | OpenAI | 火山引擎豆包 |

## 快速部署

### 1. 克隆项目

```bash
git clone https://github.com/1523826455647/Essay-Grading.git
cd Essay-Grading
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填写 LLM_API_KEY、ADMIN_PASSWORD 等
```

### 3. Docker 部署

```bash
docker-compose up -d
```

访问 `http://localhost:18004`

### 4. 本地开发

```bash
pip install -r requirements.txt
python -m src.app
```

## 项目结构

```
├── src/
│   ├── api/              # API 路由（auth, papers, submissions, grading_chat, ocr, admin）
│   ├── services/         # 业务逻辑
│   │   ├── grader/       # 批改引擎（prompts, scorer, ensemble, provider_adapters）
│   │   └── ocr_service.py
│   ├── config.py         # 配置管理
│   └── app.py            # Flask 应用入口
├── templates/            # Jinja2 模板（前台 + 管理后台）
├── static/               # CSS/JS 静态资源
├── data/
│   └── schema.sql        # 数据库表结构
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

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



## 许可证

MIT License

---

**v1.0.0** (2026-08-19)
- 初始正式版发布
- 五种题型 AI 批改 + 多模型 Ensemble
- 批改问答（服务端对话存储）
- 1000+ 套真题卷库
- OCR 手写识别（百度/腾讯/Tesseract）
- 管理后台（用户/试卷/模型/复核/Token 统计）
- Docker 一键部署