# labelme-web 7.11.0

浏览器版 labelme：界面与标注能力对标桌面版 **labelme 7.11.0**，标注文件与该版本桌面版**完全同构**，
目录结构可以整包拷进桌面版当 Annotation Folder 打开。

- 后端：FastAPI + uvicorn，**源码部署**
- 网关：NGINX 反向代理，进程守护用 **systemd**，**不使用 docker**
- 前端：`frontend/` 下的静态页面与原生 JS（`index.html` / `login.html` / `admin.html` / `404.html`），无构建步骤

## 目录结构

```
labelme-web/
├── backend/
│   ├── app.py            # HTTP API + 静态托管 + 页面路由
│   ├── auth.py           # 账号库（SQLite）、bcrypt、登录态、/admin 接口
│   ├── workspace.py      # 工程磁盘布局、固定工作空间、老数据迁移
│   └── requirements.txt
├── frontend/
│   ├── index.html        # 标注页（需登录）
│   ├── styles.css
│   ├── app.js
│   ├── login.html        # 登录 / 注册页
│   ├── login.js
│   ├── admin.html        # /admin 管理面板（无入口，只能手敲链接）
│   ├── admin.js
│   ├── 404.html          # 不存在的页面（后端与 dev/serve 都返回真 404）
│   └── auth.css          # 登录页与管理面板样式
├── dev/
│   └── serve.py          # 本地开发前置：9630 静态 + /api 反代到 9631
├── data/                 # 标注数据，运行时生成，删掉会自动重建
│   └── <用户名>/
│       └── <工程名>/     # 图片、标注、标签集都直接放在工程里
│           ├── labels.txt
│           ├── 114.jpg
│           ├── 114.json
│           └── .drafts/  # 崩溃草稿，正常保存后自动删除
├── var/
│   └── labelme-web.db    # 账号库（bcrypt 哈希），运行时生成，不进 Git
├── deploy/
│   ├── nginx.conf
│   ├── labelme-web.service
│   └── install.sh
└── README.md
```

## 工程与数据布局

磁盘上是两层：**用户名 → 工程**。工程内部不再有「会话」这一层，图片、标注、标签集、草稿都直接放在工程根目录。

```
data/<用户名>/<工程名>/
data/shiroko/电路板A/114.jpg
data/shiroko/电路板A/114.json
```

- **用户名**来自登录账号：普通账号只能进出自己的目录树，管理员可以指定任意账号
  （`?owner=xxx`）；跨账号访问工程一律按 404 处理，不泄露「这个工程存在」。
- **工程**是标注的组织单位，用「文件 → 切换工程…」「文件 → 新建工程…」管理；
  工程目录缺失时会自动创建。标签集也跟着工程走：`data/<用户名>/<工程>/labels.txt`。
- 每个工程内部有一个**固定工作空间**（`workspace-xxxxxxxxxxxx`）。前端仍用
  `/api/sessions/{workspace}/...` 这组接口，但目录就是工程根目录，界面上看不到「会话」。

工作空间 id 由 `sha1(owner + project)` 前 12 位生成，存在 `var/labelme-web.db` 的
`session_index` 表里；改工程名时会同步换成新 id。索引丢了也不要紧：访问时按 owner/project
重算并补回，服务启动时也会扫 `data/*/*` 重建工程索引。

`data/` 是纯运行目录：**整个删掉也没关系**，下次列表/新建工程时后端会重建；
失效的索引行会被顺手清掉。备份/迁移只需要拷 `data/` 和 `var/labelme-web.db`。

### 老布局自动迁移

启动时会做两件事，重复执行不会二次搬动：

1. 历史 `data/<时间戳>/` 扁平目录 → 搬到 `data/<管理员>/默认工程/` 根目录，旧的
   `data/labels.txt` 一并搬进该工程；
2. 工程内旧的 `data/<用户名>/<工程>/<时间戳>/` 会话目录 → 拍平到工程根目录；同名文件
   自动加 `-<时间戳>` 后缀，并同步改写 JSON 的 `imagePath`。

迁移使用 `shutil.move`，同一磁盘上很快。真实数据迁移前建议先整包备份 `data/` 与
`var/labelme-web.db`（仓库里的 `backup/pre-flat-*` 就是这样一份）。用 `LABELME_WEB_DATA`
可以把整个数据根换到别处（默认就是项目下的 `data/`）。

工程内规则：**上传的原图和标注 JSON 同目录、同名**。

```
data/shiroko/电路板A/114.jpg     ← 原图（上传时的原文件，不做任何压缩）
data/shiroko/电路板A/114.json    ← 该图的标注
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
把整个工程目录拷进桌面版 labelme →「打开文件夹」，回读的标注与 Web 端一致，反之亦然。

## 工程失效与自愈

工程工作空间 id 存在浏览器 `sessionStorage`，目录存在服务器，两边可能对不上：清空 `data/`、
换机器、别人清理工程之后，老标签页手里的 id 就成了悬空引用（报 `session not found`）。现在的处理是：

- 页面启动时探活：工程不存在就直接进入当前工程的固定工作空间，不会停在错误页；
- 上传文件夹前先探活（`ensureSessionAlive()`），发现目录没了就重建工作空间再上传；
- 保存标注收到 404 时就地重建工作空间并提示「原工程目录已不存在」，不再把 404 丢给你；
- 用 `?session=xxx` 打开一个已被删掉的工程，会提示并重建，而不是卡住。

界面上会弹一句「原工程目录已不存在，已重新连接工程空间」，不打断操作。

> 顺带说明为什么不用浏览器指纹：指纹会随浏览器升级、隐私开关、多设备而变，
> 会导致「同一个人的标注突然分裂成好几个文件夹」；而且它属于隐私敏感信息，不适合拿来当目录主键。
> 现在一个工程一个固定工作空间，比随机会话 id 更稳、更可控。
>
> 注意：两个标签页现在会进入同一个工程空间，不再自动分出新会话；同时改同一张图会互相覆盖，
> 前端会提示「另一个标签页也打开了同一个工程」。

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
角色（未登录跳登录页，非管理员 403）。面板是「左侧菜单 + 右侧详情」布局（顶部可搜索菜单，
`#ldap` 这类锚点可直达，浏览器前进/后退也能用）：

- **账号与权限**：概览（账号数 / 启用数 / 管理员数 / 已记录上传数 + 常用操作入口）、账号管理
  （新建账号、账号列表，多一列「来源」区分 本地 / LDAP / SSO，可改角色、启停、重置密码）、
  批量创号（8 位数字账号 + 导出 CSV）；
- **身份接入**：LDAP 设置（服务器地址、Base DN、两种绑定方式、服务账号、管理员组、StartTLS、
  超时、登录页显示名，带「保存 / 测试连接 / 恢复环境变量」）；SSO 设置（issuer、client id/secret、
  scopes、回调地址、管理员组、账号名/组字段、显示名、secret 传递方式，带「保存 / 测试 discovery /
  恢复环境变量」）；
- **工程与文件**：工程管理（工程表：详情 / 改名 / 删除、批量建同名工程）、文件资源管理器、
  上传流水（谁上传了什么）；
- **系统与 AI**：系统设置（站点名称、单文件上传上限、自助注册开关，保存后即时生效）、
  AI 模型（SAM 提供方与 OpenAI 兼容接口两套配置；SAM 内置与桌面版一致的 10 个模型目录：
  SAM 2 Base+ / SAM 3 / EfficientSAM / SAM 2 / SAM / YOLO-World，可选模型、填推理服务地址、
  API Key 遮罩与超时，带「保存 / 测试当前提供方 / 恢复默认」；OpenAI 兼容接口只预留连接信息，
  暂不参与推理）。

不存在的页面路径返回独立 `frontend/404.html`（HTTP 状态码真为 404），不会悄悄回退成首页；
`/api/*` 的未知路径仍然返回 JSON 404。开发用的 `dev/serve.py` 与生产 FastAPI 静态托管行为一致。

文件资源管理器按 `data/ → 账号 → 工程 → 文件` 逐层点进去，每层给出大小、条目数 /
图片数、修改时间；到文件层会显示**最后一个动这个文件的人**（上传 / 保存标注 / 删除），
也可以预览、下载、单个删除。只在管理员身份下可用，路径一律限制在 `data/` 内，越界直接 400。

「上传流水」按人汇总上传数量与累计体积，并列出最近 200 条动作。这些记录写在同一个 SQLite 的
`file_events` 表里（`actor / event / owner / project / session / name / size / at`），
所以备份数据库时痕迹一起走；记录失败不会影响上传或保存本身。

批量创号每个账号要算一次 bcrypt（cost 12，约 0.3 秒/个），200 个大约一分钟，
按钮会一直置灰到请求返回；超时上限受 NGINX `proxy_read_timeout` 约束，别一次拉太满。

两个保护：不能禁用/删除自己；系统至少保留一个启用的管理员。

标注页只多了一个「文件 → 退出登录」和状态栏右下角的当前账号名，没有管理相关按钮。

## LDAP / AD 接入（可选）

不配就是现在这套本地账号，一行代码都不会走到 LDAP；配上之后，**本地密码仍然优先**，
本地过不了才去 LDAP 验证 —— 这样 LDAP 挂了管理员还能从本地账号进来救场。

配置有两处来源：**后台页面 /admin → LDAP 设置（存在 SQLite，优先）**，以及下面的环境变量（兜底）。
页面里点「恢复环境变量」会清掉页面保存的值，回退成纯环境变量模式。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LABELME_WEB_LDAP_URL` | 是 | `ldap://host:389` 或 `ldaps://host:636`；配了它才算开启 |
| `LABELME_WEB_LDAP_BASE_DN` | 视情况 | 搜索基准，如 `ou=people,dc=lab,dc=local` |
| `LABELME_WEB_LDAP_USER_DN` | 二选一 | 直接拼 DN 的模板，`{username}` / `{base_dn}` 会被替换 |
| `LABELME_WEB_LDAP_USER_FILTER` | 二选一 | AD 常用：`(sAMAccountName={username})`，先搜 DN 再绑 |
| `LABELME_WEB_LDAP_BIND_DN` / `_BIND_PASSWORD` | 搜索模式 | 用来搜索的服务账号；匿名可搜就留空 |
| `LABELME_WEB_LDAP_ADMIN_GROUP` | 否 | 组名（`labelme-admins`）或组 DN；命中就把影子账号设成管理员 |
| `LABELME_WEB_LDAP_STARTTLS` | 否 | `1` 时先 StartTLS 再绑定（`ldap://` 场景强烈建议） |
| `LABELME_WEB_LDAP_TIMEOUT` | 否 | 连接/接收超时秒数，默认 5 |
| `LABELME_WEB_LDAP_LABEL` | 否 | 登录页上的名字，默认「LDAP / 域账号」 |

两种典型配置：

```bash
# 1) OpenLDAP / 通用目录：模板绑定，不需要服务账号
LABELME_WEB_LDAP_URL=ldaps://ldap.lab.local:636
LABELME_WEB_LDAP_BASE_DN=ou=people,dc=lab,dc=local
LABELME_WEB_LDAP_USER_DN=uid={username},ou=people,dc=lab,dc=local
LABELME_WEB_LDAP_ADMIN_GROUP=labelme-admins

# 2) Windows AD：按 sAMAccountName 搜 DN 再绑定
LABELME_WEB_LDAP_URL=ldap://dc01.lab.local:389
LABELME_WEB_LDAP_STARTTLS=1
LABELME_WEB_LDAP_BASE_DN=dc=lab,dc=local
LABELME_WEB_LDAP_USER_FILTER=(sAMAccountName={username})
LABELME_WEB_LDAP_BIND_DN=cn=svc-labelme,ou=services,dc=lab,dc=local
LABELME_WEB_LDAP_BIND_PASSWORD=********
LABELME_WEB_LDAP_ADMIN_GROUP=CN=labelme-admins,OU=groups,DC=lab,DC=local
```

行为约定（这几条是刻意设计，不是实现细节）：

- 认证成功后在 `users` 里落一条影子账号（`source='ldap'`，`password_hash='!ldap'` 占位），
  之后工程、`/admin`、批量建号全部照旧；账号名必须符合本站命名规则（它同时是 `data/` 的目录名）
- 组命中管理员时**只升不降**，避免 LDAP 组配错把唯一的管理员降权锁死
- LDAP 连不上 → HTTP 503「LDAP 暂不可用，请先用本地账号登录」，且**不累加登录失败次数**，
  免得 LDAP 抖动把本地账号一起锁了
- 密码错 → 401，和本地密码错同一个提示，不区分「账号不存在 / 密码错」
- `/api/auth/methods` 只返回「开没开」和显示名，不返回服务器地址；登录页据此显示一句提示

离线自检（不需要真的 LDAP 服务器，用假连接跑控制流）：

```bash
python dev/ldap_mock_check.py     # 13 项：模板/搜索绑定、密码错、组映射、影子账号、幂等
```

## SSO 接入（OIDC，可选）

通用 OpenID Connect，不绑定某一家 IdP：Authentik / Keycloak / Microsoft Entra / 学校统一认证
都走同一套 discovery + authorization code + JWKS 验签。同样**页面可配、环境变量兜底**，
`issuer` + `client_id` 两者都有才算开启。

| 变量 / 页面字段 | 必填 | 说明 |
| --- | --- | --- |
| `LABELME_WEB_SSO_ISSUER` | 是 | IdP 的 issuer，如 `https://auth.lab.local/application/o/labelme/` |
| `LABELME_WEB_SSO_CLIENT_ID` | 是 | 应用 client id |
| `LABELME_WEB_SSO_CLIENT_SECRET` | 否 | 机密客户端才需要；公开客户端（PKCE）留空 |
| `LABELME_WEB_SSO_SCOPES` | 否 | 默认 `openid profile email` |
| `LABELME_WEB_SSO_REDIRECT_URI` | 否 | 空 = 按访问域名拼 `/api/auth/sso/callback`；反代/公网部署建议显式填 |
| `LABELME_WEB_SSO_ADMIN_GROUP` | 否 | 命中组 → 影子账号设为管理员（只升不降） |
| `LABELME_WEB_SSO_USERNAME_CLAIM` | 否 | 默认 `preferred_username`，可改 `email` 等 |
| `LABELME_WEB_SSO_GROUPS_CLAIM` | 否 | 默认 `groups`，没有时自动尝试 `roles` / `realm_access.roles` |
| `LABELME_WEB_SSO_LABEL` | 否 | 登录页按钮文字，默认「统一登录（SSO）」 |
| `LABELME_WEB_SSO_TOKEN_AUTH` | 否 | `post`（默认）或 `basic`（Authorization: Basic） |

流程与约定：

1. 登录页出现「用<显示名>登录」按钮 → `GET /api/auth/sso/start` → 302 到 IdP 授权页
   （带 `state` / `nonce`，内存里存 10 分钟）；
2. IdP 回调 `GET /api/auth/sso/callback` → 用 code 换 token → 用 JWKS 校验
   `id_token`（`iss` / `aud` / `exp` / `nonce`）→ 取 claims 里的账号名；
3. 落一条影子账号（`source='sso'`，`password_hash='!sso'` 占位），之后权限、工程、
   `/admin` 全部照旧；同名本地账号不覆盖密码，只按组升权限；
4. 任何失败都 302 回 `/login?sso_error=...`，`SsoUnavailable` 与 `SsoError` 分开给原因
   （连不上 IdP / 校验不过），不会把用户卡在空白页；
5. `/api/auth/methods` 只多一个 `sso: {enabled, label}`，不泄露 issuer 与 client id。

自检（本地起一个假 IdP，跑完整链路，不需要真 IdP）：

```bash
python dev/sso_mock_check.py      # 25 项：discovery 缓存、state 一次性、验签/过期/换 key、
                                  #       组映射、basic/post、影子账号、methods
```

管理后台冒烟（隔离库 + 端口 9632，覆盖配置保存/测试、文件浏览器、上传流水）：

```bash
pwsh dev/admin_smoke.ps1          # 18 项
```


## 崩溃 / 强杀 / 蓝屏后的编辑恢复

浏览器被任务管理器强杀、渲染进程崩溃、笔记本蓝屏或直接断电，都不是「正常关闭」，
`beforeunload` 不保证执行，`sessionStorage` 一定会丢，`localStorage` 也不一定落盘
（本机实测：写入后 1.5 秒直接 `taskkill /F`，重开 `localStorage` 已经是空的）。
所以恢复链路分四层，任何一层命中都能接着做：

| 顺序 | 来源 | 能扛住什么 |
| --- | --- | --- |
| 1 | `sessionStorage` | 刷新、同标签页重开 |
| 2 | `localStorage`（`labelme.web.lastSession`） | 标签页崩溃、正常关浏览器 |
| 3 | 服务器 `GET /api/recent` | 强杀进程、蓝屏、断电（只看磁盘上最后一次改动） |
| 4 | 进入当前工程的固定工作空间 | 上面都没有（首次使用 / 工程被删） |

启动时按顺序探活，命中 2 或 3 会弹一句「已恢复上次工程」，并把上次那张图直接打开；
工程目录在服务器上已经没了就自动落到下一层，不会停在错误页。

第 2 层还有一道 **BroadcastChannel 探活**（`labelme-web-live`，等 450 ms）：
如果同一个浏览器里已经有标签页活着并用着这个工程，现在不会另开目录，只提示「注意不要同时改同一张图」。

### 未保存的编辑怎么找回

标注本身是「停手 800 ms 自动保存」，崩溃往往发生在保存之前，所以另存一份**草稿**，双写：

- `localStorage`：改动后 250 ms 写 `labelme.web.draft.<session>.<图名>`；
- 服务器：改动后 600 ms `PUT /api/sessions/{s}/drafts/{图名}`，落在
  `data/<用户名>/<工程>/.drafts/<图名>.json`，写入走「临时文件 + `fsync` + rename」，断电也不容易坏。

标注 JSON 本身也走同一套 `fsync` 落盘，所以一次保存要么是旧的、要么是新的，不会出现半截文件。

打开一张图时先读 `localStorage` 草稿，没有再读服务器草稿；只有「尺寸一致且形状与已保存的 JSON 不同」
才恢复，并立刻重新自动保存。标注保存成功后两份草稿都会被删掉，所以草稿存在就等价于「有没保存的东西」。
读服务器草稿时后端会比对 mtime，标注 JSON 更新则判定草稿过期，直接丢弃，不会把旧数据盖回来。

其余几处补漏：

- 关标签页/关浏览器时 `beforeunload` 用 `keepalive` 把标注和草稿各补一枪；
- 切到后台、合盖（`visibilitychange` / `pagehide`）立刻 flush 一次草稿，笔记本睡眠当崩溃处理；
- 换工程/换图时取消未触发的草稿定时器，避免把 A 图的形状写到 B 图。

最坏情况只丢最后 600 ms 内的改动；`data/<用户名>/<工程>/.drafts/` 是隐藏子目录，不影响「原图和 JSON 同目录」，
把工程目录拷进桌面版时可以直接连 `.drafts` 一起删掉。

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
labelme data/<用户名>/<工程名> --labels data/<用户名>/<工程名>/labels.txt
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

上传体积上限有三处：NGINX 的 `client_max_body_size`、后台「系统设置 → 单文件上传上限」（存数据库，优先），
以及 `labelme-web.service` 里的 `LABELME_WEB_MAX_UPLOAD_MB`（默认 200 MB，作为环境变量兜底）。

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
- 掩码（`mask`）：渲染二值 PNG、`rectangle/circle/polygon/oriented_rectangle` → `mask` 栅格化、`mask` → 多边形（轮廓追踪）/ 矩形
- 多选与合并：Shift + 点选多个形状，转换菜单「合并所选形状」= 并集栅格化成单个 mask（与桌面版一致）
- 工程级配置：label 对话框带 Group ID、描述、flags 勾选；图像级 image flags；`GET/PUT /api/flags` 存到工程 `flags.txt`
- 保存选项：自动保存开关、撤销步数（默认 20）、把图像 base64 内嵌到 JSON（`imageData`）、切换图像时保留亮度/对比度
- 形状锁定（Ctrl+Shift+L）：锁定状态写入 JSON 的 `locked` 字段，重开图像后仍然生效
- 标签集：菜单「导出 / 导入标签集（labels.txt）」
- 视图：十字准星开关、允许点在图像外（`allow_out_of_bounds_points`）、显示/隐藏侧边面板、重置面板布局；
  滚轮与按钮缩放固定以图像中心为锚点，画面不随鼠标位置漂移；绘制中的矩形/圆/多边形预览实时裁到图像边界

**未对标**：`AI-Segment`、`AI-Find All`、SAM 模型下拉与「模型管理」（`open_models`）。这些依赖服务端 SAM 权重与 GPU，
纯 Web 源码部署没有模型可用，标注页的 AI 入口仍置灰。管理后台已经提供 **AI 模型**配置页：
SAM 这一段内置了与桌面版一致的模型目录（SAM 2 Base+、SAM 3、EfficientSAM Tiny/Small、
SAM 2 Small/Large、SAM Base/Large/Huge、YOLO-World），可以选择默认模型、保存推理服务地址、
API Key 与超时，并用 `GET <endpoint>/health` 做连通性测试；另有一套 OpenAI 兼容接口配置
（地址、模型、Key、超时）作为预留，只保存不参与标注。真正的分割推理尚未接入 Web 标注页
（后端不加载 torch/SAM 权重）。
要打通需要在服务端部署一个实现 `/health` 的推理服务，再补 `/api/ai/segment` 代理与前端点选交互。

其余有意不做或形态不同、不影响标注结果的差异：

- 输出目录（`change_output_dir`）：Web 端固定写服务端 `data/`，没有本地输出目录概念。
- 自动化接口（`labelme/_automation`）：桌面版给脚本调用；Web 用 HTTP API 代替。
- 配置文件（`open_config`）：桌面版编辑 `default_config.yaml`；Web 的相应选项放进了菜单，不读 YAML。
- 教程页：Web 用「帮助 → 快捷键与关于」代替，没有独立教程窗口。
- 唯一标签列表 / 未使用标签区：Web 只按形状列标签，不做「已定义但未使用」视图。
- 图像列表「隐藏已标注」筛选：Web 只有文件名搜索，已标注状态用行首勾选框展示。
- TIFF（`.tif/.tiff`）：后端允许上传和列出，但浏览器不能解码，打开时会提示「图像加载失败」；
  桌面版可直接打开。含 TIFF 的数据集请先转成 PNG/JPEG，或后续加一层服务端转码（需要 Pillow）。

另：`epsilon`（掩码转多边形时的简化阈值）目前固定 1.0，界面上没放滑块；桌面版默认是 10.0，
Web 版结果更贴近原始轮廓，但点数更多。需要完全一致可后续做成配置项。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查，返回 `{"status":"ok","labelme":"7.11.0"}`（无需登录） |
| POST | `/api/auth/register` | 注册，body `{username,password}`；首个账号 = 管理员 |
| POST | `/api/auth/login` | 登录，成功后下发 `lmweb_session` cookie |
| POST | `/api/auth/logout` | 退出登录并删掉服务端 token |
| GET | `/api/auth/me` | 当前账号 `{user:{id,username,role,enabled}}` |
| GET | `/api/auth/methods` | 登录方式（`{password:true, ldap:{enabled,label}, sso:{enabled,label}, site_name, upload_max_mb}`），不泄露地址与 client |
| GET | `/api/auth/sso/start` | 302 到 IdP 授权页；未启用 SSO 时 404 |
| GET | `/api/auth/sso/callback` | IdP 回调：换 token、验 `id_token`、发本地会话、回 `next`；失败 302 到登录页带 `sso_error` |
| GET | `/api/admin/auth-config` | LDAP / SSO 当前生效配置（密钥遮罩，带每个值的来源） |
| PUT | `/api/admin/auth-config` | 保存 LDAP / SSO 配置到数据库（优先于环境变量） |
| DELETE | `/api/admin/auth-config` | `?kind=ldap|sso` 清掉页面保存的值，回退环境变量 |
| POST | `/api/admin/auth-config/test` | 连通性测试 `{kind:'ldap'|'sso'}` |
| GET | `/api/admin/system-config` | 站点名称、上传上限、注册开关、支持格式（仅管理员） |
| PUT | `/api/admin/system-config` | 保存 `{site_name?, upload_max_mb?, allow_registration?}`，即时生效 |
| GET | `/api/admin/ai-config` | AI 配置：`provider`、`sam` / `openai` 两段、10 个模型目录（API Key 遮罩，仅管理员） |
| PUT | `/api/admin/ai-config` | 保存 `{enabled, provider, sam:{endpoint,model,timeout,api_key?}, openai:{base_url,model,timeout,api_key?}}`；旧扁平字段仍兼容 |
| DELETE | `/api/admin/ai-config` | 清掉 AI 配置，恢复默认 |
| POST | `/api/admin/ai-config/test` | `{provider:'sam'|'openai'}`：SAM 请求 `/health`，OpenAI 兼容请求 `/models` |
| GET | `/api/admin/files` | 文件浏览器：`?owner=&project=` 逐层列目录（带大小/时间/上传人），已无会话层 |
| GET | `/api/admin/files/raw` | 读取 `data/` 下的单个文件 `?path=`，`&download=1` 带附件头；越界 400 |
| DELETE | `/api/admin/files` | 删除 `data/` 下的单个文件 `?path=` |
| GET | `/api/admin/uploaders` | 谁上传了什么：按人汇总 + 最近动作流水 `?limit=` |
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
| PUT | `/api/projects` | 工程改名 `{name,new_name,owner?}`，目录与索引一起改；重名 409 |
| DELETE | `/api/projects` | 删除工程 `{name,owner?}`（连同图片、标注），返回 1 |
| GET | `/api/projects/detail` | 工程概览 `?owner=&name=`：图片/标注数、标签集、固定工作空间 id |
| POST | `/api/sessions` | 返回 `?owner=&project=` 对应的**固定工作空间**（幂等，不会新建目录） |
| GET | `/api/sessions` | 工作空间列表（**JSON 数组**）；一个工程一条，id 形如 `workspace-xxxxxxxxxxxx` |
| POST | `/api/sessions/{s}/images` | 上传图像，multipart 字段名 `files`，可多文件，重名自动加 `-1` |
| GET | `/api/sessions/{s}/images` | 当前工作空间（工程根目录）图像列表 |
| GET | `/api/sessions/{s}/raw/{name}` | 取原图 |
| DELETE | `/api/sessions/{s}/images/{name}` | 删除图像及其同名 `.json` |
| GET | `/api/sessions/{s}/annotations/{name}` | 读标注（无则 404） |
| PUT | `/api/sessions/{s}/annotations/{name}` | 写标注，原子替换，强制 `imagePath=name` |
| DELETE | `/api/sessions/{s}/annotations/{name}` | 删除标注 JSON 与草稿，保留图像 |
| PUT | `/api/sessions/{s}/drafts/{name}` | 写崩溃草稿（`fsync` 落盘），body `{shapes,flags,imageWidth,imageHeight,at}` |
| GET | `/api/sessions/{s}/drafts/{name}` | 读草稿，附带 `stale`（标注比草稿新则为 true） |
| DELETE | `/api/sessions/{s}/drafts/{name}` | 删除草稿（保存成功后前端会调用） |
| GET | `/api/recent` | 最近一次动过的工程/图像，供强杀、蓝屏后接回；支持 `?owner=` |
| GET | `/api/labels` | 读**工程级**标签集（`data/<owner>/<project>/labels.txt`），支持 `?owner=&project=` |
| PUT | `/api/labels` | 写工程标签集，body `{"labels":[...]}`，支持 `?owner=&project=` |
| GET | `/api/flags` | 读工程级 flag 定义（`data/<owner>/<project>/flags.txt`），支持 `?owner=&project=` |
| PUT | `/api/flags` | 写工程 flag 定义，body `{"flags":[...]}`，支持 `?owner=&project=` |

`/api/docs` 有 FastAPI 自带的可交互文档（若不想暴露，可在 NGINX 里单独 `location /api/docs { deny all; }`）。
