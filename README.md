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
│   ├── app.py            # HTTP API + 静态托管（唯一后端文件）
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── dev/
│   └── serve.py          # 本地开发前置：9630 静态 + /api 反代到 9631
├── data/                 # 会话数据，运行时生成，删掉会自动重建
│   └── 20260101013030/   # 一个浏览器会话一个文件夹
│       ├── 114.jpg
│       └── 114.json
├── deploy/
│   ├── nginx.conf
│   ├── labelme-web.service
│   └── install.sh
└── README.md
```

## 会话与数据布局

浏览器第一次打开页面时，后端在 `data/` 下创建 `YYYYMMDDHHMMSS` 目录（同一秒重复则加 `_2`、`_3`），
会话 id 存在 **sessionStorage**：新开标签页/新窗口 = 新会话目录，刷新页面仍是当前会话。
也可从「文件 → 切换会话…」切换或新建；用 `?session=20260101013030` 可固定打开某个会话（便于分享链接）。

`data/` 是纯粹的运行目录：**整个删掉也没关系**，下次新建会话（或读会话列表）时后端会自己
`mkdir` 重建，跑着的服务不用重启。所以备份/迁移只需要拷 `data/` 本身。

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

浏览器打开 <http://127.0.0.1:9630>。改 `frontend/` 下的文件刷新即生效，不用重启；
改 `backend/app.py` 由 uvicorn `--reload` 自动重载。

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

HTTPS：把证书 `ssl_certificate` / `ssl_certificate_key` 加进该 server 块即可，后端不需要改。

## 功能对标

已实现（对标 7.11 桌面版）：

- 布局：菜单栏 / 工具栏 / 左侧竖排工具条 / 右侧「标签列表 · 形状列表 · 图像列表」三个 dock / 底部状态栏
- 形状：`polygon`、`rectangle`、`oriented_rectangle`、`circle`、三点画圆（存成两点 `circle`）、`point`、`line`、`linestrip`
- 编辑：选中、拖动整体、拖顶点、复制（Ctrl+D）、删除、修改标签、撤销/重做（快照栈）
- 视图：滚轮缩放、适合窗口（Ctrl+F）、原始大小（Ctrl+0）、亮度/对比度、中键/Alt 拖拽平移
- 文件：上传图像、多图列表、上一张/下一张、自动保存（停手 0.8 s 落盘）、手动保存、另存为 JSON、删除图像
- 快捷键：Ctrl+O / Ctrl+S / Ctrl+Z / Ctrl+Y / Ctrl+D / Ctrl+N / Ctrl+R / Delete / A、D / Enter / Esc

**未对标**：`AI-Segment`、`AI-Find All`、SAM 模型下拉。这几项依赖服务端 SAM 权重与 GPU，
纯 Web 源码部署没有模型可用，界面上保留入口但置灰，鼠标悬停会说明。普通标注流程不受影响。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查，返回 `{"status":"ok","labelme":"7.11.0"}` |
| POST | `/api/sessions` | 新建会话目录，返回 `{"session":"20260101013030"}` |
| GET | `/api/sessions` | 会话列表（图像数 / 已标注数 / 创建时间） |
| POST | `/api/sessions/{s}/images` | 上传图像，multipart 字段名 `files`，可多文件，重名自动加 `-1` |
| GET | `/api/sessions/{s}/images` | 会话内图像列表 |
| GET | `/api/sessions/{s}/raw/{name}` | 取原图 |
| DELETE | `/api/sessions/{s}/images/{name}` | 删除图像及其同名 `.json` |
| GET | `/api/sessions/{s}/annotations/{name}` | 读标注（无则 404） |
| PUT | `/api/sessions/{s}/annotations/{name}` | 写标注，原子替换，强制 `imagePath=name` |

`/api/docs` 有 FastAPI 自带的可交互文档（若不想暴露，可在 NGINX 里单独 `location /api/docs { deny all; }`）。
