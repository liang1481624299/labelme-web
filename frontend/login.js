'use strict';

/* 登录 / 注册页。成功后跳回 ?next= 指向的页面（默认标注页 /）。 */

const $ = (id) => document.getElementById(id);

const HTTP_STATUS_TEXT = {
  400: '请求有误，请检查账号和密码',
  401: '账号或密码不正确',
  403: '没有权限执行这个操作',
  404: '请求的接口不存在',
  409: '账号已存在',
  413: '提交内容超过上限',
  422: '账号或密码格式不符合要求',
  429: '尝试太频繁，请稍后再试',
  500: '服务器内部错误',
  502: '后端服务没有正常响应',
  503: '服务暂时不可用，请稍后再试',
  504: '后端响应超时，请稍后再试',
};

function httpStatusText(status) {
  return HTTP_STATUS_TEXT[status] || `请求失败（${status}）`;
}

const params = new URLSearchParams(location.search);

/* 与后端 auth._safe_next 对齐：只允许本站绝对路径。浏览器会把反斜杠
   当成路径分隔符，所以 /\evil 也必须拦掉，否则登录成功后会被带去外站。 */
function safeNext(raw) {
  const text = String(raw || '');
  if (!text || /[\u0000-\u001f]/.test(text) || text.includes('\\') || /%2f|%5c/i.test(text)) {
    return '/';
  }
  if (!text.startsWith('/') || text.startsWith('//')) return '/';
  try {
    const parsed = new URL(text, location.origin);
    if (parsed.origin !== location.origin) return '/';
    return parsed.pathname + parsed.search + parsed.hash;
  } catch (err) {
    return '/';
  }
}

let next = safeNext(params.get('next'));

let mode = 'login';
let ldapHintText = '';
let ssoEnabled = false;

function setMode(value) {
  mode = value;
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    const active = tab.dataset.tab === value;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  $('registerHint').hidden = value !== 'register';
  if (value === 'register') $('ldapHint').hidden = true;
  if (ssoEnabled) $('ssoBox').hidden = value !== 'login';
  if (value === 'login' && ldapHintText) {
    $('ldapHint').textContent = ldapHintText;
    $('ldapHint').hidden = false;
  }
  $('authTitle').textContent = value === 'register' ? '创建账号' : '用户登录';
  $('authSub').textContent = value === 'register'
    ? '注册后即可进入标注工作台'
    : '欢迎回来，请登录以继续使用';
  $('authSubmitLabel').textContent = value === 'register' ? '注册并登录' : '登录';
  $('password').autocomplete = value === 'register' ? 'new-password' : 'current-password';
  showError('');
}

function showError(text) {
  const box = $('authError');
  box.textContent = text || '';
  box.hidden = !text;
}

async function submit(event) {
  event.preventDefault();
  showError('');
  const username = $('username').value.trim();
  const password = $('password').value;
  if (!username || !password) {
    showError('请输入账号和密码');
    return;
  }
  const btn = $('authSubmit');
  btn.disabled = true;
  try {
    const res = await fetch(mode === 'register' ? '/api/auth/register' : '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : httpStatusText(res.status);
      showError(detail);
      return;
    }
    location.replace(next);
  } catch (err) {
    showError(`无法连接服务端：${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

document.querySelectorAll('.auth-tab').forEach((tab) => {
  tab.addEventListener('click', () => setMode(tab.dataset.tab));
});
$('authForm').addEventListener('submit', submit);

/* 密码可见性切换，图标随状态切换；仅改 input 类型，不碰提交逻辑 */
const passwordInput = $('password');
const togglePassword = $('togglePassword');
togglePassword.addEventListener('click', () => {
  const visible = passwordInput.type === 'password';
  passwordInput.type = visible ? 'text' : 'password';
  togglePassword.classList.toggle('is-on', visible);
  togglePassword.setAttribute('aria-pressed', visible ? 'true' : 'false');
  togglePassword.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  passwordInput.focus({ preventScroll: true });
});

if (next === '/admin') {
  $('registerHint').hidden = true;
}

setMode('login');
$('username').focus();

/* 从 SSO 回调失败回来时，后端会把原因塞在 sso_error 里 */
if (params.get('sso_error')) {
  showError(params.get('sso_error'));
}

/* 后端如果开了 LDAP 接入层，就在登录框下面写一句提示；
   只拿开关和显示名，拿不到服务器地址（那是运维配置，不该出现在页面上）。 */
(async () => {
  try {
    const res = await fetch('/api/auth/methods', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    const siteName = String((data && data.site_name) || 'Labelme').trim() || 'Labelme';
    document.title = `登录 · ${siteName}`;
    const brand = document.querySelector('.auth-brand span:not(.dot):not(.ver)');
    if (brand) brand.textContent = siteName;
    /* 左侧品牌区标题同样跟随站点名，避免两边不一致 */
    const heroSite = $('heroSite');
    if (heroSite) heroSite.textContent = siteName;
    const ldap = (data && data.ldap) || {};
    const sso = (data && data.sso) || {};
    if (sso.enabled) {
      ssoEnabled = true;
      const btn = $('ssoBtn');
      btn.textContent = `用 ${sso.label || '统一登录'} 登录`;
      $('ssoBox').hidden = mode !== 'login';
      btn.addEventListener('click', () => {
        location.href = '/api/auth/sso/start?next=' + encodeURIComponent(next);
      });
    }
    if (!ldap.enabled) return;
    ldapHintText = `可以直接用${ldap.label || 'LDAP'}账号登录；如果没有，仍可用本站账号密码登录。`;
    if (mode === 'login') {
      const hint = $('ldapHint');
      hint.textContent = ldapHintText;
      hint.hidden = false;
    }
  } catch (err) { /* 拿不到就当作没开 */ }
})();
