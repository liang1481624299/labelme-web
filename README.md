<h1 align="center">labelme-web · 在线图像标注平台</h1>

<p align="center">把桌面版 <strong>labelme 7.11</strong> 的标注工作台搬进浏览器：工程、标签集、标注 JSON 与桌面版同构，标注结果可以直接打包回桌面版继续用。</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square">
  <img alt="FastAPI 0.110+" src="https://img.shields.io/badge/FastAPI-0.110%2B-009688?style=flat-square">
  <img alt="Uvicorn 0.29+" src="https://img.shields.io/badge/Uvicorn-0.29%2B-499848?style=flat-square">
  <img alt="Vanilla JS" src="https://img.shields.io/badge/Vanilla_JS-F7DF1E?style=flat-square&logo=javascript&logoColor=black">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white">
  <img alt="NGINX" src="https://img.shields.io/badge/NGINX-009639?style=flat-square&logo=nginx&logoColor=white">
</p>

<p align="center">
  <img alt="labelme 7.11" src="https://img.shields.io/badge/labelme-7.11.0-blueviolet?style=flat-square">
  <img alt="No Docker" src="https://img.shields.io/badge/No_Docker-2496ED?style=flat-square&logo=docker&logoColor=white">
  <img alt="Smoke Test" src="https://img.shields.io/badge/Smoke_Test-brightgreen?style=flat-square">
  <img alt="Status Active" src="https://img.shields.io/badge/Status-Active-brightgreen?style=flat-square">
  <img alt="License Unspecified" src="https://img.shields.io/badge/License-Unspecified-lightgrey?style=flat-square">
</p>

<p align="center">
  <a href="#开发规范">开发规范</a> ·
  <a href="#本地体验">本地体验</a> ·
  <a href="#功能介绍">功能介绍</a> ·
  <a href="#技术架构">技术架构</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#配置说明">配置说明</a> ·
  <a href="#使用流程">使用流程</a> ·
  <a href="#文档导航">文档导航</a> ·
  <a href="#许可证">许可证</a>
</p>

## 简介

一句话定位：把真实图片变成可保存、可回溯、可带回桌面版的 labelme 标注工程，服务浏览器端和局域网内的图像标注场景。

平台由三部分组成：

- **标注工作台**：原生 Canvas 交互，支持多边形、矩形、有向矩形、圆、三点画圆、点、线、折线，以及 mask 栅格化、撤销/重做、多选合并。
- **工程与数据**：按“账号 → 工程”组织数据，标注 JSON 与桌面版 labelme 7.11.0 同构；自动保存、崩溃草稿、工程打包下载都在服务端完成。
- **接入与运维**：本地账号、LDAP/AD、OIDC SSO 三种登录方式；`/admin` 管理后台；NGINX + systemd 源码部署，不需要 Docker。

当前生产示例：<https://lab.gdueca.com/login>。仓库当前版本对应 labelme `7.11.0`。

## 开发规范

### 分层边界

| 目录 | 职责 | 约束 |
| --- | --- | --- |
| `frontend/` | 页面、样式、原生 JS | 纯静态，无构建步骤；新增功能不引入 npm/框架依赖 |
| `backend/` | FastAPI 应用、账号、工程、文件、配置 | 路由只做参数校验和响应；业务逻辑放在独立模块 |
| `data/` | 运行时的图片、JSON、标签集、草稿 | 不提交 Git；删掉可重建，备份时必须带走 |
| `var/` | SQLite 账号库、敏感配置密钥 | 不提交 Git；`.secret.key` 必须与数据库一起备份 |
| `deploy/` | NGINX、systemd、安装脚本 | 安装目录可配置，不写死 `/opt/labelme-web` |

### 必须保持不变

- **标注 JSON 契约**：`version`、`flags`、`shapes`、`imagePath`、`imageData`、`imageWidth`、`imageHeight` 的字段和语义保持与 labelme 7.11.0 一致；不要为了前端方便改字段名。
- **原图与 JSON 同目录、同名**：`data/<账号>/<工程>/<stem>.jpg` 对应 `data/<账号>/<工程>/<stem>.json`。
- **安全底线**：密码只存 bcrypt 哈希；会话 cookie 为 HttpOnly；写请求必须校验 CSRF；LDAP/SSO/AI 密钥落库前加密；日志不打印密码和密钥。
- **前端零构建**：`frontend/` 直接由 FastAPI 静态托管；改完刷新即可，不引入 bundler。
- **接口变更要成套**：后端路由、前端 `app.js`、README 和 `dev/` 自检脚本同步更新。

### 提交与验证

- Git 提交固定使用 `type(scope): 中文简述`，单行，不加句号，例如 `docs(readme): 重写项目说明与导航徽章`。
- 改动账号、LDAP、SSO 或管理后台时，至少跑对应自检：

```bash
python dev/ldap_mock_check.py
python dev/bind_mock_check.py
python dev/sso_mock_check.py
pwsh dev/admin_smoke.ps1
```

- 前端改动要在桌面浏览器和 390px/768px 窄屏各看一次；手机端只保留管理后台，不把标注页重新做成手机布局。

## 本地体验

前置条件：Python 3.10+。前端不需要 Node、npm 或构建工具；生产环境另需 NGINX。

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt

# 终端 1：后端 9631
.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 9631 --reload

# 终端 2：前端 9630（静态托管 + /api 反代）
.venv\Scripts\python dev\serve.py
```

Linux / macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 9631 --reload
# 另开终端
python dev/serve.py
```

打开 <http://127.0.0.1:9630>。第一次使用时点“注册”创建第一个账号，**第一个账号自动成为管理员**；之后注册的账号默认是标注员。

快速确认后端已起来：

```powershell
Invoke-RestMethod http://127.0.0.1:9631/api/health
```

```bash
curl -fsS http://127.0.0.1:9631/api/health
```

## 功能介绍

| 模块 | 已实现能力 |
| --- | --- |
| 标注形状 | `polygon`、`rectangle`、`oriented_rectangle`、`circle`、三点画圆、`point`、`line`、`linestrip`；支持 mask 渲染、形状转 mask、mask 转多边形/矩形 |
| 编辑 | 选中、整体拖动、拖顶点、复制/粘贴/副本、删除、改标签/Group ID/描述、锁定、隐藏、删点、边上加点、转换形状类型、撤销/重做 |
| 视图 | 滚轮缩放、适合窗口、适合选中、原始大小、亮度/对比度、中键/Alt 平移、十字准星、允许点在图像外 |
| 文件与工程 | 上传多图、上一张/下一张、自动保存、手动保存、另存 JSON、删除标注/图像、新建/改名/删除工程、工程 zip 打包 |
| 标签与 flags | 工程级 `labels.txt` 导入/导出、标签下拉补全、图像级 flags、工程级 `flags.txt` |
| 崩溃恢复 | `sessionStorage` → `localStorage` → 服务端 `/api/recent` → 固定工作空间四级恢复；未保存改动另有 600ms 草稿双写 |
| 多选与合并 | Shift 点选多个形状，合并为单个 mask；与桌面版行为保持一致 |
| 触控 | iPad / 触屏 Windows 沿用桌面三栏布局；单指标注、双指缩放平移、44pt 触控目标、画框手势 |
| 手机限制 | 手机访问登录页和标注页只显示阻断提示；`/admin` 和 `/login?next=/admin` 例外，可正常管理 |
| 账号与权限 | 本地注册/登录、bcrypt、会话超时、CSRF、管理员/标注员两级角色、批量创号 |
| 身份接入 | LDAP / AD 登录、OIDC SSO 登录、账号绑定、同名本地账号不自动接管 |
| 管理后台 | 账号管理、工程管理、文件资源管理器、上传流水、系统设置、LDAP/SSO、AI 配置 |
| 桌面兼容 | 标注 JSON、`labels.txt`、`flags.txt`、原图同目录；工程 zip 可直接作为桌面版 Annotation Folder |

### 当前限制

- 标注页的 `AI-Segment` / `AI-Find All` 仍置灰；管理后台可以保存 SAM / OpenAI 兼容配置并测试连通性，但 Web 端还没有接入分割推理，后端不加载 torch/SAM 权重。
- TIFF（`.tif` / `.tiff`）可以上传和列出，但浏览器不能解码，打开会提示加载失败；含 TIFF 的数据集请先转 PNG/JPEG。
- `epsilon`（mask 转多边形简化阈值）目前固定为 1.0，桌面版默认 10.0，Web 结果更贴近原始轮廓但点数更多。
- 同一浏览器两个标签页打开同一工程会收到提示；同时改同一张图仍是“后保存覆盖前保存”，没有实时协同锁。
- 手机端不是标注端，只保留管理后台；标注需要桌面浏览器或大屏触控设备。

## 技术架构

| 层次 | 技术 | 说明 |
| --- | --- | --- |
| 前端 | HTML + CSS + 原生 JS | `index.html`、`login.html`、`admin.html` 由 FastAPI 静态托管，无构建步骤 |
| 后端 | Python 3.10+ / FastAPI / Uvicorn | 单进程提供 API、页面路由和静态文件；只监听 `127.0.0.1:9631` |
| 网关 | NGINX | 对公网监听 `9630`，反代到 9631；502/503/504 有静态兜底页 |
| 数据 | 文件系统 + SQLite | 图片/JSON/标签/草稿在 `data/`；账号、设置、绑定、上传流水在 `var/labelme-web.db` |
| 认证 | bcrypt + 服务端会话 + CSRF | 支持本地账号、LDAP/AD、OIDC SSO；外部身份绑定到同一个本地账号 |
| 部署 | systemd + `deploy/install.sh` | 源码部署，无 Docker；服务用户 `labelme`，数据目录单独授权 |
| 自检 | Python mock + PowerShell smoke | LDAP/SSO/绑定用假服务跑控制流；管理后台用隔离库跑冒烟 |

```text
Browser
  ├─ index.html + app.js         标注页（登录后）
  ├─ login.html + login.js       登录 / 注册 / 绑定
  └─ admin.html + admin.js       /admin 管理后台
            │
            │ 同源 /api
            ▼
  NGINX :9630  ───────────────►  FastAPI :9631
                                    │
                                    ├─ data/<账号>/<工程>/
                                    │    ├─ 图片
                                    │    ├─ 同名 .json
                                    │    ├─ labels.txt / flags.txt
                                    │    └─ .drafts/
                                    └─ var/labelme-web.db
                                       var/.secret.key
```

### 目录结构

```text
labelme-web/
├─ backend/
│  ├─ app.py               # HTTP API、页面路由、静态托管、AI 配置接口
│  ├─ auth.py              # 账号库、bcrypt、会话、CSRF、/admin
│  ├─ ldap_auth.py         # LDAP / AD 认证
│  ├─ sso_auth.py          # OIDC SSO
│  ├─ workspace.py         # 工程磁盘布局、固定工作空间、老数据迁移
│  ├─ user_settings.py     # 标注页个人设置
│  ├─ settings_store.py    # 敏感配置加密与数据库设置
│  └─ requirements.txt
├─ frontend/
│  ├─ index.html / app.js  # 标注页
│  ├─ login.html / login.js
│  ├─ admin.html / admin.js
│  ├─ styles.css / auth.css / login.css
│  └─ 404.html / error.html / 50x.html
├─ dev/
│  ├─ serve.py             # 本地 9630 静态 + /api 反代
│  ├─ ldap_mock_check.py
│  ├─ bind_mock_check.py
│  ├─ sso_mock_check.py
│  └─ admin_smoke.ps1
├─ deploy/
│  ├─ install.sh
│  ├─ labelme-web.service
│  └─ nginx.conf
├─ data/                   # 运行时生成，不进 Git
├─ var/                    # 账号库与密钥，不进 Git
└─ README.md
```

### 数据布局

```text
data/<用户名>/<工程名>/
├─ 114.jpg                 # 原图，上传时不做压缩
├─ 114.json                # 与桌面版 labelme 7.11.0 同构
├─ labels.txt              # 工程级标签集
├─ flags.txt               # 工程级 flags 定义
└─ .drafts/                # 崩溃草稿，保存成功后自动清理
```

工作空间 id 由 `sha1(owner + project)` 前 12 位生成，存在 SQLite 的 `session_index` 表；索引丢失时可按账号/工程重算并补回。

## 快速开始

### 生产部署（Ubuntu / Debian）

```bash
sudo apt install -y python3 python3-venv nginx curl rsync
git clone https://github.com/GDUE-802D-YYDS/labelme-web.git
cd labelme-web
sudo bash deploy/install.sh
```

默认安装到 `/opt/labelme-web`，启动后：

- 前端：`http://<服务器IP>:9630/`
- 管理后台：`http://<服务器IP>:9630/admin`（桌面标注页没有入口，只能手敲或从手机阻断页进入）
- 健康检查：`http://127.0.0.1:9631/api/health`
- 服务日志：`journalctl -u labelme-web -f`

安装脚本会创建系统用户 `labelme`、venv、`data/`、`var/`、systemd 单元和 NGINX 站点；不安装 Docker。自定义目录：

```bash
sudo bash deploy/install.sh /srv/labelme-web
```

### 首次使用

1. 打开 `http://<host>:9630/`，进入登录页。
2. 点“注册”创建第一个账号；**第一个账号自动是管理员**。
3. 登录后进入标注页，先新建工程，再上传图片。
4. 管理后台地址是 `/admin`；手机端可以从阻断页的“前往管理员后台”进入。

生产 HTTPS 只需在 NGINX 的 server 块补证书；登录 cookie 会根据 `X-Forwarded-Proto: https` 自动加 `Secure`，也可用 `LABELME_WEB_COOKIE_SECURE=1` 强制。

## 配置说明

### 端口

| 角色 | 端口 | 监听方 |
| --- | --- | --- |
| 前端 / 网关 | **9630** | 开发：`dev/serve.py`；生产：NGINX |
| 后端 API | **9631** | uvicorn，只绑 `127.0.0.1` |

前端所有请求使用相对路径 `/api/...`，开发和生产都同源反代，不需要 CORS。

### 常用环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LABELME_WEB_DATA` | 项目下 `data/` | 标注数据根目录 |
| `LABELME_WEB_DB` | `var/labelme-web.db` | SQLite 账号库路径 |
| `LABELME_WEB_MAX_UPLOAD_MB` | `200` | 单文件上传上限；后台“系统设置”优先 |
| `LABELME_WEB_ALLOW_REGISTRATION` | `1` | 是否允许自助注册；后台设置优先 |
| `LABELME_WEB_SESSION_IDLE_MINUTES` | `30` | 无操作失效时间，5–1440 分钟 |
| `LABELME_WEB_SESSION_DAYS` | `7` | 登录态绝对上限，1–30 天 |
| `LABELME_WEB_COOKIE_SECURE` | 自动 | `1` / `true` 强制 Secure cookie |
| `LABELME_WEB_SECRET_KEY` | 空 | 敏感配置加密密钥；设置后覆盖密钥文件 |
| `LABELME_WEB_SECRET_KEY_FILE` | `var/.secret.key` | 密钥文件路径，首次启动自动生成 |
| `LABELME_WEB_AI_MODELS` | 空 | 收窄标注页可选 AI 模型，如 `sam2:base+,sam2:large` |

### 身份接入

- **LDAP / AD**：`LABELME_WEB_LDAP_URL` 配了才启用；支持 `USER_DN` 模板绑定或 `USER_FILTER` 搜索绑定，支持 StartTLS、管理员组、显示名。
- **OIDC SSO**：`LABELME_WEB_SSO_ISSUER` + `LABELME_WEB_SSO_CLIENT_ID` 两者都有才启用；支持 discovery、JWKS 验签、`state` / `nonce`、组映射和管理员组。
- 后台 `/admin` 的“身份接入”页面可以保存并测试这些配置，数据库设置优先于环境变量；点“恢复环境变量”会回退。
- 外部身份与本地同名账号**不会自动接管**：必须先用本地密码登录，再到“账号与安全”显式绑定。

### 上传与备份

- 上传上限有三处：NGINX `client_max_body_size`、后台“系统设置 → 单文件上传上限”、systemd 的 `LABELME_WEB_MAX_UPLOAD_MB` 兜底。
- 前端批量上传按“每批最多 20 张或 64 MB”分批发送。
- 备份必须同时拷走 `data/`、`var/labelme-web.db` 和 `var/.secret.key`；只拷数据库会导致 LDAP/SSO/AI 密钥解不开。

## 使用流程

1. **登录**：打开 `/login`，用本地账号登录；管理员也可以在登录页用 LDAP 或 SSO。
2. **建工程**：进入标注页，`在线 → 新建工程…`；工程名重复会自动加 `-2`、`-3`。
3. **上传**：`文件 → 打开文件` 或 `打开文件夹`，支持多选；上传后左侧图像列表出现缩略图。
4. **标注**：选左侧工具画形状；`Ctrl+N` 多边形、`Ctrl+R` 矩形、`Ctrl+E` 改标签；`Ctrl+Z` / `Ctrl+Y` 撤销重做。
5. **保存**：停手约 800ms 自动保存；也可以 `Ctrl+S` 手动保存。保存后的 JSON 与桌面版 7.11.0 同构。
6. **标签与 flags**：右侧“标签列表 → 编辑…”导入/导出 `labels.txt`；`文件 → 图像标记` 或工程 flags 写入 `flags.txt`。
7. **打包**：`在线 → 打包当前工程（zip）`，压缩包保留工程顶层目录，解压后可直接作为桌面版 Annotation Folder。
8. **异常恢复**：强杀、蓝屏或断电后重新打开，会按 session/localStorage/服务端 `/api/recent` 逐级恢复；未保存改动从 `.drafts/` 或本地草稿恢复。
9. **管理**：管理员进 `/admin` 管账号、工程、文件、上传流水、系统设置、身份接入和 AI 配置。

### 常用快捷键

| 快捷键 | 动作 |
| --- | --- |
| `Ctrl+O` / `Ctrl+U` | 打开文件 / 上传图像 |
| `Ctrl+S` / `Ctrl+Shift+S` | 保存 / 另存 JSON |
| `Ctrl+Z` / `Ctrl+Y` | 撤销 / 重做 |
| `Ctrl+N` / `Ctrl+R` | 新建多边形 / 矩形 |
| `Ctrl+E` | 修改当前形状标签 |
| `Ctrl+F` / `Ctrl+Shift+F` / `Ctrl+0` | 适合窗口 / 适合选中 / 原始大小 |
| `A` / `D` | 上一张 / 下一张 |
| `Delete` / `Ctrl+Delete` | 删除形状 / 删除图像 |
| `Ctrl+Shift+L` | 锁定/解锁当前形状 |

## 文档导航

| 想找什么 | 去哪里 |
| --- | --- |
| 本地跑起来 | [本地体验](#本地体验) |
| 生产安装 | [快速开始](#快速开始) |
| 功能与限制 | [功能介绍](#功能介绍) |
| 架构与数据布局 | [技术架构](#技术架构) |
| 环境变量与身份接入 | [配置说明](#配置说明) |
| 日常标注流程 | [使用流程](#使用流程) |
| HTTP API | [HTTP API](#http-api) / 运行时的 `/api/docs` |
| 部署文件 | `deploy/nginx.conf`、`deploy/labelme-web.service`、`deploy/install.sh` |
| 自检脚本 | `dev/*_check.py`、`dev/admin_smoke.ps1` |
| 上游项目 | <https://github.com/wkentaro/labelme> |

当前仓库没有单独的 `docs/` 目录；这份 README 是项目主文档，接口的实时 schema 以 FastAPI `/api/docs` 为准。

## HTTP API

除 `/api/health`、`/api/auth/register`、`/api/auth/login` 外，所有 `/api/*` 都要求登录 cookie；POST/PUT/PATCH/DELETE 还要带 `GET /api/auth/me` 返回的 `csrf_token`，放在 `X-CSRF-Token` 请求头。

| 分组 | 代表接口 | 说明 |
| --- | --- | --- |
| 健康检查 | `GET /api/health` | 返回 `{"status":"ok","labelme":"7.11.0"}` |
| 认证 | `POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`、`GET /api/auth/methods` | 本地账号、LDAP/SSO 登录方式与会话 |
| 绑定 | `GET /api/auth/bindings`、`POST /api/auth/bind/ldap`、`GET /api/auth/sso/bind/start`、`DELETE /api/auth/bindings/{provider}` | 外部身份绑定/解绑 |
| 工程 | `GET/POST/PUT/DELETE /api/projects`、`GET /api/projects/detail`、`POST /api/projects/package` | 工程列表、详情、改名、删除、打包 |
| 工作空间 | `POST/GET /api/sessions`、`GET /api/recent` | 固定工程空间与最近打开记录 |
| 图像 | `POST/GET /api/sessions/{s}/images`、`GET /api/sessions/{s}/raw/{name}`、`DELETE /api/sessions/{s}/images/{name}` | 上传、列表、原图、删除 |
| 标注 | `GET/PUT/DELETE /api/sessions/{s}/annotations/{name}` | 读/写/删除 labelme JSON |
| 草稿 | `PUT/GET/DELETE /api/sessions/{s}/drafts/{name}` | 崩溃草稿与过期判断 |
| 标签/flags | `GET/PUT /api/labels`、`GET/PUT /api/flags` | 工程级标签集与 flags |
| 管理 | `GET/POST/PATCH/DELETE /api/admin/users`、`GET /api/admin/overview`、`GET /api/admin/files`、`GET /api/admin/uploaders`、`GET/PUT /api/admin/system-config`、`GET/PUT/DELETE /api/admin/ai-config` | 账号、文件、上传流水、系统设置、AI 配置 |
| 身份配置 | `GET/PUT/DELETE /api/admin/auth-config`、`POST /api/admin/auth-config/test` | LDAP / SSO 配置与连通性测试 |

完整请求/响应以运行时 `/api/docs` 为准；脚本调用写接口时也必须先登录并携带 CSRF token。

## 许可证

本仓库当前**没有附带 LICENSE 文件**，GitHub 也未识别出许可证，因此徽章显示 `License Unspecified`，不是 Apache-2.0。上游 `labelme` 项目使用 GPL-3.0；在公开发布、分发或再许可前，请先确认本仓库与上游代码的许可证兼容性，再补充 `LICENSE` 和必要的 `NOTICE`。
