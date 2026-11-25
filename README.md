# labelme-web 7.11.0

浏览器版 labelme：界面与标注能力对标桌面版 **labelme 7.11.0**，标注文件与该版本桌面版**完全同构**，
目录结构可以整包拷进桌面版当 Annotation Folder 打开。

- 后端：FastAPI + uvicorn，**源码部署**
- 网关：NGINX 反向代理，进程守护用 **systemd**，**不使用 docker**
- 前端：`frontend/` 三个文件（`index.html` / `styles.css` / `app.js`），原生 JS，无构建步骤

## 目录结构

```
labelme-web/
├── backend/
│   ├── app.py            # HTTP API + 静态托管 + 页面路由
│   ├── auth.py           # 账号库（SQLite）、bcrypt、登录态、/admin 接口
│   ├── workspace.py      # 工程/会话的磁盘布局、SQLite 索引、老数据迁移
│   └── requirements.txt
├── frontend/
│   ├── index.html        # 标注页（需登录）
│   ├── styles.css
│   ├── app.js
│   ├── login.html        # 登录 / 注册页
│   ├── login.js
│   ├── admin.html        # /admin 管理面板（无入口，只能手敲链接）
│   ├── admin.js
│   └── auth.css          # 登录页与管理面板样式
├── dev/
│   └── serve.py          # 本地开发前置：9630 静态 + /api 反代到 9631
├── data/                 # 标注数据，运行时生成，删掉会自动重建
│   └── <用户名>/
│       └── <工程名>/     # 工程级标签集 labels.txt 在这里
│           ├── labels.txt
│           └── 20260101013030/   # 一次浏览器会话一个文件夹
│               ├── 114.jpg
│               ├── 114.json
│               └── .drafts/      # 崩溃草稿，正常保存后自动删除
├── var/
│   └── labelme-web.db    # 账号库（bcrypt 哈希），运行时生成，不进 Git
├── deploy/
│   ├── nginx.conf
│   ├── labelme-web.service
│   └── install.sh
└── README.md
```

## 工程、会话与数据布局

磁盘上是三层：**用户名 → 工程 → 会话**。

```
data/<用户名>/<工程名>/<YYYYMMDDHHMMSS>/
data/shiroko/电路板A/20261125102550/114.jpg
data/shiroko/电路板A/20261125102550/114.json
```

- **用户名**来自登录账号：普通账号只能进出自己的目录树，管理员可以指定任意账号
  （`?owner=xxx`）；跨账号访问会话一律按 404 处理，不泄露“这个会话存在”。
- **工程**是标注的组织单位，用「文件 → 切换工程…」「文件 → 新建工程…」管理；
  工程目录缺失时会自动创建。标签集也跟着工程走：`data/<用户名>/<工程>/labels.txt`。
- **会话**对应一次连续标注（一个文件夹、一批图），用「文件 → 新建会话…」新建。

会话 id 仍然只是 14 位时间戳（同一秒重复加 `_2`、`_3`），前端 URL、草稿键、恢复链路都不变；
真实路径由 `var/labelme-web.db` 里的 `session_index` 表维护（`session_index(id, owner, project, created_at)`）。
索引丢了也不要紧：访问某个 id 时会全盘扫 `data/*/*/<id>` 并自动补回；服务启动时也会把磁盘上
存在但索引里没有的会话补进来（手工拷 `data/`、恢复备份都能直接用）。

`data/` 仍然是纯运行目录：**整个删掉也没关系**，下次新建会话/列表时后端会重建；
失效的索引行会被顺手清掉。备份/迁移只需要拷 `data/` 和 `var/labelme-web.db`。

### 老布局自动迁移

历史数据如果是 `data/<时间戳>/` 扁平结构，会在**第一个管理员出现后**自动搬到
`data/<管理员>/默认工程/<时间戳>/`，旧的 `data/labels.txt` 也一并搬进该工程；
迁移是 `shutil.move`，同一磁盘上很快，重复执行不会二次移动。用
`LABELME_WEB_DATA` 可以把整个数据根换到别处（默认就是项目下的 `data/`）。

会话内规则：**上传的原图和标注 JSON 同目录、同名**。

```
data/20260101013030/114.jpg     ← 原图（上传时的原文件，不做任何压缩）
data/20260101013030/114.json    ← 该图的标注
```

`114.json` 的字段与 labelme 7.11.0 写出的文件一致：

```json
{
  "version": "7.11.0",
  "flags": {},
  "shapes": [
    {
      "label": "resistor",
      "points": [[120.5, 340.1], [210.0, 340.1], [210.0, 400.0]],
      "group_id": null,
      "description": "",
      "shape_type": "polygon",
      "flags": {},
      "mask": null
    }
  ],
  "imagePath": "114.jpg",
  "imageData": null,
  "imageHeight": 1080,
  "imageWidth": 1920
}
```

`imagePath` 始终是同目录的文件名（相对），`imageData` 为 `null`（原图就在旁边，不重复存 base64）。
把整个会话目录拷进桌面版 labelme →「打开文件夹」，回读的标注与 Web 端一致，反之亦然。

## 会话失效与自愈

会话 id 存在浏览器 `sessionStorage`，目录存在服务器，两边可能对不上：清空 `data/`、换机器、
别人清理旧会话之后，老标签页手里的 id 就成了悬空引用（报 `session not found`）。现在的处理是：

- 页面启动时探活：会话不存在就直接新建一个，不会停在错误页；
- 上传文件夹前先探活（`ensureSessionAlive()`），发现目录没了就新建会话再上传；
- 保存标注收到 404 时就地重建会话并提示「原会话目录已不存在」，不再把 404 丢给你；
- 用 `?session=xxx` 打开一个已被删掉的会话，会提示并新建，而不是卡住。

界面上会弹一句「原会话目录已不存在，已切换到新会话 2026…」，不打断操作。

> 顺带说明为什么不用浏览器指纹：指纹会随浏览器升级、隐私开关、多设备而变，
> 会导致「同一个人的标注突然分裂成好几个文件夹」；而且它属于隐私敏感信息，不适合拿来当目录主键。
> 这里要的语义是「一次标注会话一个文件夹」，随机会话 id + 失效自愈比指纹更稳、更可控。

## 账号、登录与权限

打开 `http://<host>:9630/` 先看到登录页，登录后才进标注页；未登录访问任何页面都会跳
`/login?next=...`，所有 `/api/*`（除登录/注册/健康检查）都要求登录。

- 账号库：`var/labelme-web.db`（SQLite，可用 `LABELME_WEB_DB` 改路径）。放在 `var/` 而不是
  `data/`，因为 `data/` 是标注运行目录、删了会自愈，账号不能跟着一起没。
- 密码：**只存 bcrypt 哈希**（`$2b$12$...`，cost 12），库里没有明文，日志也不打密码。
  密码 8 位起，最长 72 字节（bcrypt 上限，超了直接拒绝，不悄悄截断）。
- 登录态：服务端随机 token，cookie `lmweb_session`（HttpOnly / SameSite=Lax，7 天），
  库里只存 token 的 sha256；改角色、禁用、改密码都会立刻作废旧登录态。
- 角色两级：
  - `admin`：能进 `/admin` 管账号；
  - `annotator`：只能标注，访问 `/admin` 返回 403。
- **第一个注册的账号自动成为管理员**；之后注册的都是标注员。管理员可以关掉“允许自助注册”，
  也可以直接建号。
- 失败次数限制：同一 IP + 账号 5 分钟内失败 6 次，锁 60 秒。

### /admin 管理面板

得手敲 `http://<host>:9630/admin`，**标注页和登录页都没有任何入口/链接**；服务端还会再校验一次
角色（未登录跳登录页，非管理员 403）。面板能做：

- 看账号总数 / 启用数 / 管理员数；
- 新建账号（指定角色与初始密码）；
- 改角色、禁用/启用（禁用会立即踢掉该账号已登录的会话）；
- 重置密码（同样踢掉旧会话，自己改自己保留当前登录）；
- 删除账号；
- 开/关“允许自助注册”；
- **批量创号**：填数量（1–200）和可选起始 ID，账号名 = 8 位数字 ID、密码默认 = 同一个 ID
  （纯数字 8 位正好满足密码下限）。明文密码只在那一次响应里出现，库里只有 bcrypt 哈希；
  面板会把结果列出来并可 **导出 CSV**（账号,密码），刷新页面就再也拿不到明文；
- **工程管理**：列出所有账号的工程与会话数、给任意账号新建工程。

批量创号每个账号要算一次 bcrypt（cost 12，约 0.3 秒/个），200 个大约一分钟，
按钮会一直置灰到请求返回；超时上限受 NGINX `proxy_read_timeout` 约束，别一次拉太满。

两个保护：不能禁用/删除自己；系统至少保留一个启用的管理员。

标注页只多了一个「文件 → 退出登录」和状态栏右下角的当前账号名，没有管理相关按钮。

## 崩溃 / 强杀 / 蓝屏后的会话恢复

浏览器被任务管理器强杀、渲染进程崩溃、笔记本蓝屏或直接断电，都不是「正常关闭」，
`beforeunload` 不保证执行，`sessionStorage` 一定会丢，`localStorage` 也不一定落盘
（本机实测：写入后 1.5 秒直接 `taskkill /F`，重开 `localStorage` 已经是空的）。
所以恢复链路分四层，任何一层命中都能接着做：

| 顺序 | 来源 | 能扛住什么 |
| --- | --- | --- |
| 1 | `sessionStorage` | 刷新、同标签页重开 |
| 2 | `localStorage`（`labelme.web.lastSession`） | 标签页崩溃、正常关浏览器 |
| 3 | 服务器 `GET /api/recent` | 强杀进程、蓝屏、断电（只看磁盘上最后一次改动） |
| 4 | 新建会话 | 上面都没有（首次使用 / 会话被删） |

启动时按顺序探活，命中 2 或 3 会弹一句「已恢复上次会话 2026…」，并把上次那张图直接打开；
会话目录在服务器上已经没了就自动落到下一层，不会停在错误页。

第 2 层还有一道 **BroadcastChannel 探活**（`labelme-web-live`，等 450 ms）：
如果同一个浏览器里已经有标签页活着并用着这个会话，就不抢，直接新建会话，避免两个页面同时写同一份 JSON。

### 未保存的编辑怎么找回

标注本身是「停手 800 ms 自动保存」，崩溃往往发生在保存之前，所以另存一份**草稿**，双写：

- `localStorage`：改动后 250 ms 写 `labelme.web.draft.<session>.<图名>`；
- 服务器：改动后 600 ms `PUT /api/sessions/{s}/drafts/{图名}`，落在
  `data/<session>/.drafts/<图名>.json`，写入走「临时文件 + `fsync` + rename」，断电也不容易坏。

标注 JSON 本身也走同一套 `fsync` 落盘，所以一次保存要么是旧的、要么是新的，不会出现半截文件。

打开一张图时先读 `localStorage` 草稿，没有再读服务器草稿；只有「尺寸一致且形状与已保存的 JSON 不同」
才恢复，并立刻重新自动保存。标注保存成功后两份草稿都会被删掉，所以草稿存在就等价于「有没保存的东西」。
读服务器草稿时后端会比对 mtime，标注 JSON 更新则判定草稿过期，直接丢弃，不会把旧数据盖回来。

其余几处补漏：

- 关标签页/关浏览器时 `beforeunload` 用 `keepalive` 把标注和草稿各补一枪；
- 切到后台、合盖（`visibilitychange` / `pagehide`）立刻 flush 一次草稿，笔记本睡眠当崩溃处理；
- 换会话/换图时取消未触发的草稿定时器，避免把 A 图的形状写到 B 图。

最坏情况只丢最后 600 ms 内的改动；`data/<session>/.drafts/` 是隐藏子目录，不影响「原图和 JSON 同目录」，
把会话目录拷进桌面版时可以直接连 `.drafts` 一起删掉。

## 标签集：导入 / 导出 / 跨端复用

标签集是**工程级**的，落在 `data/<用户名>/<工程名>/labels.txt`（UTF-8，每行一个标签）：

- 界面位置：右侧「标签列表」标题栏的 **编辑…** → 弹窗里有 **导入…** / **导出 labels.txt**；
- 导入支持 `.txt` 和 `.json`：
  - `.txt`：每行一个；忽略空行、`#` 与 `//` 注释；
  - 兼容带颜色的行，下面三种都会只取标签名：`sensor, 0, 128, 0`、`pin,255,0,0,255`、`led: #ff0000`；
  - `.json`：`["a","b"]` 或 `{"labels": ["a","b"]}`；
- 导入是**合并**（已有的不重复、不覆盖），导出就是一份标准 `labels.txt`；
- 标注时输入框带下拉补全（datalist），也可直接点下方常用标签；
- 打开已有 `.json` 标注时，里面出现过的 label 会自动汇总进标签集，等于「从旧标注反推标签表」；
- 桌面版 labelme 可以直接复用同一份标签：

```bash
labelme data/<用户名>/<工程名>/20260101013030 --labels data/<用户名>/<工程名>/labels.txt
```

> 注意：标签集在工程目录里，所以清空 `data/` 会连同标签集一起清掉——
> 这也意味着「拷走 `data/`」就等于带走全部标注和标签表。

接口：`GET /api/labels?owner=&project=`、`PUT /api/labels?owner=&project=`
（body `{"labels": [...]}`，服务端会去空、去重、截断超长）。

## 端口约定

| 角色 | 端口 | 由谁监听 |
| --- | --- | --- |
| 前端 | **9630** | 开发：`dev/serve.py`；生产：NGINX |
| 后端 API | **9631** | 开发/生产都是 uvicorn，只绑 `127.0.0.1` |

前端代码里所有请求都写相对路径 `/api/...`，开发环境由 `dev/serve.py` 转发到 9631，
生产环境由 NGINX 转发到 9631，两边行为一致，也不需要 CORS。

## 本地开发（两个进程）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt

# 终端 1：后端 9631
python -m uvicorn backend.app:app --host 127.0.0.1 --port 9631 --reload

# 终端 2：前端 9630（静态托管 + /api 反代）
python dev/serve.py            # 默认 --port 9630 --api http://127.0.0.1:9631
```

浏览器打开 <http://127.0.0.1:9630> 会先到登录页：第一次用点「注册」建管理员账号（第一个账号自动是
管理员），之后就能进标注页。改 `frontend/` 下的文件刷新即生效，不用重启；
改 `backend/` 下的 Python 由 uvicorn `--reload` 自动重载（Windows 上偶尔会卡在 Reloading，
直接杀掉重开更省事）。

Windows 上把 `python3` 换成 `python`（或解释器绝对路径），其余命令一样：

```powershell
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt
.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 9631 --reload
.venv\Scripts\python dev\serve.py
```

## Linux 服务器部署（NGINX + systemd）

```bash
# 依赖
sudo apt install -y python3 python3-venv nginx curl
# 部署（默认装到 /opt/labelme-web）
sudo bash ./deploy/install.sh
# 或指定目录
sudo bash ./deploy/install.sh /srv/labelme-web
```

脚本会做：建系统用户 `labelme` → 同步源码 → 建 venv 装依赖 → 建 `data/` 并授权 →
安装 `labelme-web.service`（监听 `127.0.0.1:9631`）→ 安装 NGINX 站点（**9630** 反代到 9631）→ 健康检查。

常用运维：

```bash
systemctl status labelme-web
journalctl -u labelme-web -f
sudo systemctl restart labelme-web
sudo nginx -t && sudo systemctl reload nginx
```

上传体积上限改两处：`/etc/nginx/sites-available/labelme-web` 的 `client_max_body_size`，
与 `labelme-web.service` 里的 `LABELME_WEB_MAX_UPLOAD_MB`（默认 200 MB）。

HTTPS：把证书 `ssl_certificate` / `ssl_certificate_key` 加进该 server 块即可，后端不需要改；
登录 cookie 会自动按 `X-Forwarded-Proto: https` 加上 `Secure`（也可用
`LABELME_WEB_COOKIE_SECURE=1` 强制）。

账号库在 `var/labelme-web.db`（systemd 单元里 `LABELME_WEB_DB` 指过去，`ReadWritePaths` 含 `var`），
备份时和 `data/` 一起拷走就行。

## 功能对标

已实现（对标 7.11 桌面版）：

- 布局：菜单栏 / 工具栏 / 左侧竖排工具条 / 右侧「标签列表 · 形状列表 · 图像列表」三个 dock / 底部状态栏
- 形状：`polygon`、`rectangle`、`oriented_rectangle`、`circle`、三点画圆（存成两点 `circle`）、`point`、`line`、`linestrip`
- 编辑：选中、拖动整体、拖顶点、复制/粘贴/副本（Ctrl+C/V/D）、删除、修改标签与 Group ID/描述、锁定、隐藏/显示、删除顶点、边上加点、转换形状类型、方向选择、撤销/重做（快照栈）
- 多边形手势：单击加点、点回起点闭合、双击闭合、Enter/Space 闭合、Backspace/Ctrl+Z 撤销当前点
- 视图：滚轮缩放、适合窗口（Ctrl+F）、适合选中（Ctrl+Shift+F）、原始大小（Ctrl+0）、亮度/对比度、中键/Alt 拖拽平移
- 文件：上传图像、多图列表、上一张/下一张、自动保存（停手 0.8 s 落盘）、手动保存、另存为 JSON、关闭图像（可保留上一张标注与缩放）、删除标注文件、删除图像
- 快捷键：Ctrl+O/U/S/Shift+S/W、Delete/Ctrl+Delete、A/D 切图、Ctrl+N 多边形、Ctrl+R 矩形、Ctrl+E 改标签、Ctrl+Shift+L 锁定、T 切换显示、Ctrl+F/Ctrl+Shift+F 适合窗口/选中、Ctrl+0 原始大小、Ctrl+↑/↓/←/→ 选邻近形状
- 快捷键：Ctrl+O / Ctrl+S / Ctrl+Z / Ctrl+Y / Ctrl+D / Ctrl+N / Ctrl+R / Delete / A、D / Enter / Esc

**未对标**：`AI-Segment`、`AI-Find All`、SAM 模型下拉。这几项依赖服务端 SAM 权重与 GPU，
纯 Web 源码部署没有模型可用，界面上保留入口但置灰，鼠标悬停会说明。普通标注流程不受影响。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查，返回 `{"status":"ok","labelme":"7.11.0"}`（无需登录） |
| POST | `/api/auth/register` | 注册，body `{username,password}`；首个账号 = 管理员 |
| POST | `/api/auth/login` | 登录，成功后下发 `lmweb_session` cookie |
| POST | `/api/auth/logout` | 退出登录并删掉服务端 token |
| GET | `/api/auth/me` | 当前账号 `{user:{id,username,role,enabled}}` |
| GET | `/api/admin/users` | 账号列表（仅管理员） |
| POST | `/api/admin/users` | 新建账号 `{username,password,role}` |
| PATCH | `/api/admin/users/{id}` | 改角色 / 启停 / 重置密码 |
| DELETE | `/api/admin/users/{id}` | 删除账号 |
| GET | `/api/admin/settings` | `{allow_registration}` |
| PUT | `/api/admin/settings` | 开关自助注册 |
| GET | `/api/admin/overview` | 账号总数 / 启用数 / 管理员数 |
| POST | `/api/admin/users/batch` | 批量创号 `{count,start?}`，账号名与初始密码都是 8 位数字 ID（仅管理员） |
| GET | `/api/projects` | 当前账号的工程列表；`?owner=` 指定账号，管理员可用 `?scope=all` 看全部 |
| POST | `/api/projects` | 新建工程 `{name,owner?}`（普通账号只能建自己的） |
| POST | `/api/sessions` | 在 `?owner=&project=` 下新建会话目录，返回 `{session,owner,project}` |
| GET | `/api/sessions` | 会话列表（**JSON 数组**），支持 `?owner=&project=`（普通账号只能看自己的；管理员不传 owner 看全部） |
| POST | `/api/sessions/{s}/images` | 上传图像，multipart 字段名 `files`，可多文件，重名自动加 `-1` |
| GET | `/api/sessions/{s}/images` | 会话内图像列表 |
| GET | `/api/sessions/{s}/raw/{name}` | 取原图 |
| DELETE | `/api/sessions/{s}/images/{name}` | 删除图像及其同名 `.json` |
| GET | `/api/sessions/{s}/annotations/{name}` | 读标注（无则 404） |
| PUT | `/api/sessions/{s}/annotations/{name}` | 写标注，原子替换，强制 `imagePath=name` |
| DELETE | `/api/sessions/{s}/annotations/{name}` | 删除标注 JSON 与草稿，保留图像 |
| PUT | `/api/sessions/{s}/drafts/{name}` | 写崩溃草稿（`fsync` 落盘），body `{shapes,flags,imageWidth,imageHeight,at}` |
| GET | `/api/sessions/{s}/drafts/{name}` | 读草稿，附带 `stale`（标注比草稿新则为 true） |
| DELETE | `/api/sessions/{s}/drafts/{name}` | 删除草稿（保存成功后前端会调用） |
| GET | `/api/recent` | 最近一次动过的会话/图像，供强杀、蓝屏后接回；支持 `?owner=` |
| GET | `/api/labels` | 读**工程级**标签集（`data/<owner>/<project>/labels.txt`），支持 `?owner=&project=` |
| PUT | `/api/labels` | 写工程标签集，body `{"labels":[...]}`，支持 `?owner=&project=` |

`/api/docs` 有 FastAPI 自带的可交互文档（若不想暴露，可在 NGINX 里单独 `location /api/docs { deny all; }`）。
