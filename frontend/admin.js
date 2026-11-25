'use strict';

/* /admin 管理面板：账号增删改、角色、启用/禁用、重置密码、注册开关。
 * 该页面在标注页里没有任何入口，只能手敲 /admin；服务端还会再校验一次角色。 */

const $ = (id) => document.getElementById(id);
let me = null;

function msg(el, text, ok) {
  el.textContent = text || '';
  el.className = 'admin-msg' + (text ? (ok ? ' ok' : ' err') : '');
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.replace('/login?next=/admin');
    throw new Error('未登录');
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `请求失败（${res.status}）`;
    throw new Error(detail);
  }
  return data;
}

function fmt(value) {
  return value ? String(value) : '—';
}

function renderUsers(users) {
  const box = $('userRows');
  box.innerHTML = '';
  users.forEach((user) => {
    const row = document.createElement('tr');

    const nameCell = document.createElement('td');
    nameCell.textContent = user.username;
    const isMe = me && user.id === me.id;
    if (isMe) {
      const badge = document.createElement('span');
      badge.className = 'tag';
      badge.style.marginLeft = '8px';
      badge.textContent = '我';
      nameCell.appendChild(badge);
    }

    const roleCell = document.createElement('td');
    const roleSelect = document.createElement('select');
    roleSelect.innerHTML = '<option value="annotator">标注员</option><option value="admin">管理员</option>';
    roleSelect.value = user.role;
    roleSelect.disabled = isMe;
    roleSelect.addEventListener('change', async () => {
      try {
        await api('PATCH', `/api/admin/users/${user.id}`, { role: roleSelect.value });
        await load();
      } catch (err) {
        msg($('listMsg'), err.message, false);
        roleSelect.value = user.role;
      }
    });
    roleCell.appendChild(roleSelect);

    const stateCell = document.createElement('td');
    const tag = document.createElement('span');
    tag.className = 'tag ' + (user.enabled ? 'enabled' : 'disabled');
    tag.textContent = user.enabled ? '启用' : '已禁用';
    stateCell.appendChild(tag);

    const createdCell = document.createElement('td');
    createdCell.textContent = fmt(user.created_at);

    const loginCell = document.createElement('td');
    loginCell.textContent = fmt(user.last_login_at);

    const actionCell = document.createElement('td');
    const resetBtn = document.createElement('button');
    resetBtn.className = 'btn btn-sm';
    resetBtn.textContent = '重置密码';
    resetBtn.addEventListener('click', async () => {
      const password = prompt(`为 ${user.username} 设置新密码（至少 8 位）`);
      if (!password) return;
      try {
        await api('PATCH', `/api/admin/users/${user.id}`, { password });
        msg($('listMsg'), `已重置 ${user.username} 的密码`, true);
      } catch (err) {
        msg($('listMsg'), err.message, false);
      }
    });

    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'btn btn-sm';
    toggleBtn.textContent = user.enabled ? '禁用' : '启用';
    toggleBtn.disabled = isMe;
    toggleBtn.style.marginLeft = '6px';
    toggleBtn.addEventListener('click', async () => {
      try {
        await api('PATCH', `/api/admin/users/${user.id}`, { enabled: !user.enabled });
        await load();
      } catch (err) {
        msg($('listMsg'), err.message, false);
      }
    });

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-sm btn-danger';
    delBtn.textContent = '删除';
    delBtn.disabled = isMe;
    delBtn.style.marginLeft = '6px';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`确认删除账号 ${user.username}？其登录态会立即失效。`)) return;
      try {
        await api('DELETE', `/api/admin/users/${user.id}`);
        await load();
      } catch (err) {
        msg($('listMsg'), err.message, false);
      }
    });

    actionCell.append(resetBtn, toggleBtn, delBtn);
    row.append(nameCell, roleCell, stateCell, createdCell, loginCell, actionCell);
    box.appendChild(row);
  });
}

async function load() {
  try {
    if (!me) {
      const res = await api('GET', '/api/auth/me');
      me = res.user;
      $('who').textContent = `${me.username} · ${me.role === 'admin' ? '管理员' : '标注员'}`;
    }
    const overview = await api('GET', '/api/admin/overview');
    $('statUsers').textContent = overview.users;
    $('statEnabled').textContent = overview.enabled;
    $('statAdmins').textContent = overview.admins;

    const settings = await api('GET', '/api/admin/settings');
    $('allowRegistration').checked = !!settings.allow_registration;

    const users = await api('GET', '/api/admin/users');
    renderUsers(users.users || []);
  } catch (err) {
    msg($('listMsg'), err.message, false);
  }
}

$('allowRegistration').addEventListener('change', async (event) => {
  const value = event.target.checked;
  try {
    const res = await api('PUT', '/api/admin/settings', { allow_registration: value });
    event.target.checked = !!res.allow_registration;
    msg($('listMsg'), value ? '已开启自助注册' : '已关闭自助注册', true);
  } catch (err) {
    event.target.checked = !value;
    msg($('listMsg'), err.message, false);
  }
});

$('createForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = $('newUsername').value.trim();
  const password = $('newPassword').value;
  const role = $('newRole').value;
  if (!username || !password) {
    msg($('createMsg'), '请填写账号和初始密码', false);
    return;
  }
  try {
    await api('POST', '/api/admin/users', { username, password, role });
    msg($('createMsg'), `已创建 ${username}`, true);
    $('newUsername').value = '';
    $('newPassword').value = '';
    $('newRole').value = 'annotator';
    await load();
  } catch (err) {
    msg($('createMsg'), err.message, false);
  }
});

$('btnBack').addEventListener('click', () => location.assign('/'));

$('btnLogout').addEventListener('click', async () => {
  try {
    await api('POST', '/api/auth/logout');
  } catch (err) { /* 失败也回登录页 */ }
  location.replace('/login');
});

load();
