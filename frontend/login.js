'use strict';

/* 登录 / 注册页。成功后跳回 ?next= 指向的页面（默认标注页 /）。 */

const $ = (id) => document.getElementById(id);

const params = new URLSearchParams(location.search);
let next = params.get('next') || '/';
if (!next.startsWith('/') || next.startsWith('//')) next = '/';

let mode = 'login';

function setMode(value) {
  mode = value;
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.tab === value);
  });
  $('registerHint').hidden = value !== 'register';
  $('authSubmit').textContent = value === 'register' ? '注册并登录' : '登录';
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
      const detail = typeof data.detail === 'string' ? data.detail : `请求失败（${res.status}）`;
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

if (next === '/admin') {
  $('registerHint').hidden = true;
}

setMode('login');
$('username').focus();
