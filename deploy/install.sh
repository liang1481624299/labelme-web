#!/usr/bin/env bash
# labelme-web 源码部署脚本（Ubuntu / Debian，无 docker）
#
#   sudo ./deploy/install.sh                 # 把当前源码部署到 /opt/labelme-web
#   sudo ./deploy/install.sh /srv/labelme-web  # 自定义安装目录
#
# 做的事：系统用户 → venv + 依赖 → data/ → systemd 单元 → NGINX 站点 → 启动
set -euo pipefail

APP_DIR="${1:-/opt/labelme-web}"
SERVICE_NAME="labelme-web"
SERVICE_USER="labelme"
NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo $0"
[[ -f "${SRC_DIR}/backend/app.py" ]] || die "找不到 backend/app.py，请在源码目录内运行"
[[ -f "${SRC_DIR}/frontend/index.html" ]] || die "找不到 frontend/index.html"

command -v python3 >/dev/null || die "缺少 python3"
python3 - <<'PY' || die "需要 Python 3.10+（python3-venv 也要装：apt install python3-venv）"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
command -v nginx >/dev/null || die "缺少 nginx：apt install -y nginx"

# ---------------------------------------------------------------- 系统用户
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  log "创建系统用户 ${SERVICE_USER}"
  useradd --system --shell /usr/sbin/nologin --home-dir "${APP_DIR}" "${SERVICE_USER}"
fi

# ---------------------------------------------------------------- 拷贝源码
if [[ "${SRC_DIR}" != "${APP_DIR}" ]]; then
  log "同步源码 ${SRC_DIR} → ${APP_DIR}"
  mkdir -p "${APP_DIR}"
  if command -v rsync >/dev/null; then
    rsync -a --delete \
      --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
      "${SRC_DIR}/" "${APP_DIR}/"
  else
    cp -a "${SRC_DIR}/." "${APP_DIR}/"
  fi
else
  log "源码已位于 ${APP_DIR}"
fi

mkdir -p "${APP_DIR}/data"

# ---------------------------------------------------------------- Python 环境
log "创建 venv 并安装依赖"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel >/dev/null
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/backend/requirements.txt"

# ---------------------------------------------------------------- 权限
log "设置归属与权限"
chown -R root:root "${APP_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/data"
chmod 755 "${APP_DIR}"
chmod 755 "${APP_DIR}/data"

# ---------------------------------------------------------------- systemd
log "安装 systemd 单元"
sed "s|/opt/labelme-web|${APP_DIR}|g" "${SRC_DIR}/deploy/${SERVICE_NAME}.service" \
  > "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

# ---------------------------------------------------------------- nginx
log "安装 NGINX 站点"
install -m 644 "${SRC_DIR}/deploy/nginx.conf" "${NGINX_SITE}"
ln -sf "${NGINX_SITE}" "${NGINX_LINK}"
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl reload nginx

# ---------------------------------------------------------------- 自检
sleep 2
if curl -fsS "http://127.0.0.1:9631/api/health" >/dev/null; then
  log "后端健康检查通过"
else
  die "后端未响应，查看：journalctl -u ${SERVICE_NAME} -n 60 --no-pager"
fi

cat <<EOF

部署完成。

  安装目录   ${APP_DIR}
  会话数据   ${APP_DIR}/data/<YYYYMMDDHHMMSS>/
  前端端口   9630（NGINX）
  后端端口   9631（systemd，仅本机 127.0.0.1）
  服务单元   systemctl status ${SERVICE_NAME}
  日志       journalctl -u ${SERVICE_NAME} -f
  访问       http://<服务器IP>:9630/

EOF
