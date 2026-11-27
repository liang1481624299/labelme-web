'use strict';

/* /admin 管理面板：账号增删改、角色、启用/禁用、重置密码、注册开关。
 * 该页面在标注页里没有任何入口，只能手敲 /admin；服务端还会再校验一次角色。 */

const $ = (id) => document.getElementById(id);
let me = null;
let csrfToken = '';

const HTTP_STATUS_TEXT = {
  400: '请求有误，请检查提交内容',
  401: '登录已过期，请重新登录',
  403: '没有权限执行这个操作',
  404: '请求的接口或文件不存在',
  409: '名称冲突，目标已存在',
  413: '文件超过上传上限',
  422: '参数格式不符合要求',
  429: '请求太频繁，请稍后再试',
  500: '服务器内部错误',
  502: '后端服务没有正常响应',
  503: '服务暂时不可用，请稍后再试',
  504: '后端响应超时，请稍后再试',
};

function httpStatusText(status) {
  return HTTP_STATUS_TEXT[status] || `请求失败（${status}）`;
}

function msg(el, text, ok) {
  el.textContent = text || '';
  el.className = 'admin-msg' + (text ? (ok ? ' ok' : ' err') : '');
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(String(method).toUpperCase())) {
    opts.headers['X-CSRF-Token'] = csrfToken;
  }
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
    const detail = typeof data.detail === 'string' ? data.detail : httpStatusText(res.status);
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

    const sourceCell = document.createElement('td');
    const sourceTag = document.createElement('span');
    const source = user.source || 'local';
    sourceTag.className = 'tag' + (source === 'local' ? '' : ' enabled');
    sourceTag.textContent = source === 'ldap' ? 'LDAP' : source === 'sso' ? 'SSO' : '本地';
    sourceTag.title = user.external_id || '';
    sourceCell.appendChild(sourceTag);

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
    row.append(nameCell, sourceCell, roleCell, stateCell, createdCell, loginCell, actionCell);
    box.appendChild(row);
  });
}

function renderProjects(projects) {
  const box = $('projectRows');
  box.innerHTML = '';
  if (!projects.length) {
    box.innerHTML = '<tr><td colspan="5" class="empty-cell">还没有工程</td></tr>';
    return;
  }
  projects.forEach((p) => {
    const row = document.createElement('tr');
    const ownerCell = document.createElement('td');
    ownerCell.textContent = p.owner;
    const nameCell = document.createElement('td');
    nameCell.textContent = p.name;
    const sessionsCell = document.createElement('td');
    sessionsCell.textContent = p.sessions ? '正常' : '目录缺失';
    const createdCell = document.createElement('td');
    createdCell.textContent = fmt(p.created_at);

    const actionCell = document.createElement('td');
    const detailBtn = document.createElement('button');
    detailBtn.className = 'btn btn-sm';
    detailBtn.textContent = '详情';
    detailBtn.addEventListener('click', () => showProjectDetail(p.owner, p.name));

    const renameBtn = document.createElement('button');
    renameBtn.className = 'btn btn-sm';
    renameBtn.textContent = '改名';
    renameBtn.style.marginLeft = '6px';
    renameBtn.addEventListener('click', async () => {
      const next = prompt(`把「${p.name}」改成：`, p.name);
      if (!next || next === p.name) return;
      try {
        await api('PUT', '/api/projects', { owner: p.owner, name: p.name, new_name: next.trim() });
        msg($('projectMsg'), `已改名为「${next.trim()}」`, true);
        await loadProjects();
      } catch (err) {
        msg($('projectMsg'), err.message, false);
      }
    });

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-sm btn-danger';
    delBtn.textContent = '删除';
    delBtn.style.marginLeft = '6px';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`确认删除 ${p.owner} 的工程「${p.name}」？\n其中所有图片和标注会一起删掉，不可恢复。`)) return;
      try {
        const res = await api('DELETE', '/api/projects', { owner: p.owner, name: p.name });
        msg($('projectMsg'), `已删除「${p.name}」`, true);
        await loadProjects();
      } catch (err) {
        msg($('projectMsg'), err.message, false);
      }
    });
    actionCell.append(detailBtn, renameBtn, delBtn);

    row.append(ownerCell, nameCell, sessionsCell, createdCell, actionCell);
    box.appendChild(row);
  });
}

async function showProjectDetail(owner, name) {
  try {
    const d = await api('GET', `/api/projects/detail?owner=${encodeURIComponent(owner)}&name=${encodeURIComponent(name)}`);
    const lines = [];
    lines.push(`${d.owner} / ${d.name}`);
    lines.push(`图片 ${d.images} 张 · 已标注 ${d.annotations} 张`);
    if (d.labels.length) lines.push(`标签集：${d.labels.join('、')}`);
    alert(lines.join('\n'));
  } catch (err) {
    msg($('projectMsg'), err.message, false);
  }
}

async function loadProjects() {
  try {
    const res = await api('GET', '/api/projects?scope=all');
    renderProjects(res.projects || []);
  } catch (err) {
    msg($('projectMsg'), err.message, false);
  }
}

async function load() {
  try {
    if (!me) {
      const res = await api('GET', '/api/auth/me');
      me = res.user;
      if (res.csrf_token) csrfToken = res.csrf_token;
      $('who').textContent = `${me.username} · ${me.role === 'admin' ? '管理员' : '标注员'}`;
      $('sideUser').textContent = me.username;
    }
    const overview = await api('GET', '/api/admin/overview');
    $('statUsers').textContent = overview.users;
    $('statEnabled').textContent = overview.enabled;
    $('statAdmins').textContent = overview.admins;

    const settings = await api('GET', '/api/admin/settings');
    $('allowRegistration').checked = !!settings.allow_registration;

    const users = await api('GET', '/api/admin/users');
    renderUsers(users.users || []);
    await loadProjects();
    try {
      const up = await api('GET', '/api/admin/uploaders?limit=1');
      $('statUploads').textContent = up.stats.uploads;
    } catch (e) {
      $('statUploads').textContent = '—';
    }
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

let batchRows = [];

$('batchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const count = Number($('batchCount').value || 0);
  const start = $('batchStart').value.trim();
  if (!count || count < 1 || count > 200) {
    msg($('batchMsg'), '数量要在 1–200 之间', false);
    return;
  }
  const btn = $('btnBatch');
  btn.disabled = true;
  msg($('batchMsg'), `正在生成 ${count} 个账号（bcrypt 每号约 0.3 秒）…`, true);
  try {
    const res = await api('POST', '/api/admin/users/batch',
      start ? { count, start } : { count });
    batchRows = res.created || [];
    $('batchOut').hidden = !batchRows.length;
    $('batchOut').textContent = batchRows
      .map((r) => `${r.username}  ${r.password}`)
      .join('\n');
    $('btnBatchExport').disabled = !batchRows.length;
    msg($('batchMsg'),
      `已创建 ${res.count} 个账号：${res.first} – ${res.last}（密码同账号，请立刻导出）`, true);
    $('projectOwner').value = batchRows.length ? batchRows[0].username : $('projectOwner').value;
    await load();
  } catch (err) {
    msg($('batchMsg'), err.message, false);
  } finally {
    btn.disabled = false;
  }
});

$('btnBatchExport').addEventListener('click', () => {
  if (!batchRows.length) return;
  const csv = ['账号,密码'].concat(batchRows.map((r) => `${r.username},${r.password}`)).join('\r\n');
  const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `labelme-accounts-${batchRows[0].username}-${batchRows[batchRows.length - 1].username}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
});

$('projectForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const owner = $('projectOwner').value.trim();
  const name = $('projectName').value.trim();
  if (!owner || !name) {
    msg($('projectMsg'), '请填写所属账号和工程名', false);
    return;
  }
  try {
    await api('POST', '/api/projects', { owner, name });
    msg($('projectMsg'), `已为 ${owner} 新建工程「${name}」`, true);
    $('projectName').value = '';
    await loadProjects();
  } catch (err) {
    msg($('projectMsg'), err.message, false);
  }
});

$('projectBatchForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const owners = $('projectBatchOwners').value
    .split(/[\s,，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const name = $('projectName').value.trim();
  if (!owners.length) {
    msg($('projectMsg'), '请先填账号列表', false);
    return;
  }
  if (!name) {
    msg($('projectMsg'), '请在上面的「工程名」里填一个名字', false);
    return;
  }
  const btn = $('btnBatchProjects');
  btn.disabled = true;
  let ok = 0;
  const failed = [];
  for (const owner of owners) {
    try {
      await api('POST', '/api/projects', { owner, name });
      ok += 1;
    } catch (err) {
      failed.push(`${owner}：${err.message}`);
    }
  }
  btn.disabled = false;
  msg($('projectMsg'),
    `已为 ${ok} 个账号建好「${name}」` + (failed.length ? `；失败 ${failed.length} 个：${failed.slice(0, 3).join('；')}` : ''),
    !failed.length);
  await loadProjects();
});

$('btnBack').addEventListener('click', () => location.assign('/'));

$('btnLogout').addEventListener('click', async () => {
  try {
    await api('POST', '/api/auth/logout');
  } catch (err) { /* 失败也回登录页 */ }
  location.replace('/login');
});

/* ---------------------------------------------------------------- 视图切换 */
const PAGES = ['overview', 'accounts', 'batch', 'ldap', 'sso', 'projects', 'files', 'uploads', 'settings', 'ai'];
const loadedViews = new Set();
let navReady = false;

function showView(name) {
  if (!PAGES.includes(name)) name = 'overview';
  document.querySelectorAll('.nav-item').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === name);
  });
  document.querySelectorAll('.page').forEach((view) => {
    view.hidden = view.dataset.page !== name;
  });
  if ((name === 'ldap' || name === 'sso') && !loadedViews.has('auth')) {
    loadedViews.add('auth');
    loadAuthConfig();
  }
  if (name === 'files' && !loadedViews.has('files')) {
    loadedViews.add('files');
    loadFiles({});
  }
  if (name === 'uploads' && !loadedViews.has('uploads')) {
    loadedViews.add('uploads');
    loadUploaders();
  }
  if (name === 'projects' && !loadedViews.has('projects')) loadedViews.add('projects');
  if (name === 'settings' && !loadedViews.has('settings')) {
    loadedViews.add('settings');
    loadSystemConfig();
  }
  if (name === 'ai' && !loadedViews.has('ai')) {
    loadedViews.add('ai');
    loadAiConfig();
  }
  if (location.hash !== '#' + name) {
    if (navReady) history.pushState(null, '', '#' + name);
    else history.replaceState(null, '', '#' + name);
  }
  navReady = true;
}

document.querySelectorAll('.nav-item').forEach((tab) => {
  tab.addEventListener('click', () => showView(tab.dataset.view));
});

document.querySelectorAll('.quick-item').forEach((item) => {
  item.addEventListener('click', () => showView(item.dataset.goto));
});

window.addEventListener('hashchange', () => {
  showView((location.hash || '').replace('#', '') || 'overview');
});
window.addEventListener('popstate', () => {
  showView((location.hash || '').replace('#', '') || 'overview');
});

/* 菜单搜索：按文字过滤左侧菜单 */
$('adminMenuSearch').addEventListener('input', (event) => {
  const q = event.target.value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('.nav-item').forEach((item) => {
    const hit = !q || item.textContent.toLowerCase().includes(q);
    item.hidden = !hit;
    if (hit) shown += 1;
  });
  $('navEmpty').hidden = shown > 0;
  document.querySelectorAll('.nav-group').forEach((group) => {
    let next = group.nextElementSibling;
    let any = false;
    while (next && !next.classList.contains('nav-group')) {
      if (next.classList.contains('nav-item') && !next.hidden) any = true;
      next = next.nextElementSibling;
    }
    group.hidden = !any;
  });
});

/* ------------------------------------------------------------- LDAP / SSO */
let authConfig = null;

function fillSrcMap(section) {
  document.querySelectorAll(`.src[data-src^="${section}."]`).forEach((el) => {
    const field = el.dataset.src.split('.')[1];
    const from = (authConfig[section].source || {})[field];
    if (!from) { el.textContent = ''; el.title = ''; return; }
    el.textContent = from === 'db' ? '（页面保存）' : '（环境变量）';
  });
}

async function loadAuthConfig() {
  try {
    authConfig = await api('GET', '/api/admin/auth-config');
    applyLdap(authConfig.ldap);
    applySso(authConfig.sso);
    fillSrcMap('ldap');
    fillSrcMap('sso');
  } catch (err) {
    msg($('ldapMsg'), err.message, false);
  }
}

function applyLdap(v) {
  const on = !!v.enabled;
  $('ldapState').textContent = on ? `已启用 · ${v.label || 'LDAP'}` : '未启用';
  $('ldapState').className = 'tag ' + (on ? 'enabled' : 'disabled');
  $('ldapUrl').value = v.url || '';
  $('ldapBaseDn').value = v.base_dn || '';
  $('ldapUserDn').value = v.user_dn || '';
  $('ldapUserFilter').value = v.user_filter || '';
  $('ldapBindDn').value = v.bind_dn || '';
  $('ldapBindPassword').value = '';
  $('ldapBindPassword').placeholder = v.bind_password_set ? '已设置（留空不修改）' : '留空 = 不修改';
  $('ldapAdminGroup').value = v.admin_group || '';
  $('ldapLabel').value = v.label || '';
  $('ldapTimeout').value = v.timeout || '5';
  $('ldapStarttls').checked = !!v.starttls;
}

function applySso(v) {
  const on = !!v.enabled;
  $('ssoState').textContent = on ? `已启用 · ${v.label || 'SSO'}` : '未启用';
  $('ssoState').className = 'tag ' + (on ? 'enabled' : 'disabled');
  $('ssoIssuer').value = v.issuer || '';
  $('ssoClientId').value = v.client_id || '';
  $('ssoClientSecret').value = '';
  $('ssoClientSecret').placeholder = v.client_secret_set ? '已设置（留空不修改）' : '留空 = 不使用 secret（公开客户端）';
  $('ssoScopes').value = v.scopes || '';
  $('ssoRedirect').value = v.redirect_uri || '';
  $('ssoAdminGroup').value = v.admin_group || '';
  $('ssoUsernameClaim').value = v.username_claim || '';
  $('ssoGroupsClaim').value = v.groups_claim || '';
  $('ssoLabel').value = v.label || '';
  $('ssoTokenAuth').value = v.token_auth || 'post';
  $('ssoCallback').textContent = v.redirect_uri || (location.origin + '/api/auth/sso/callback');
}

async function saveSection(section, payload, msgId) {
  try {
    const res = await api('PUT', '/api/admin/auth-config', { [section]: payload });
    authConfig = { ldap: res.ldap, sso: res.sso };
    if (section === 'ldap') applyLdap(res.ldap); else applySso(res.sso);
    fillSrcMap('ldap'); fillSrcMap('sso');
    msg($(msgId), `已保存 ${res.saved.length} 项`, true);
  } catch (err) {
    msg($(msgId), err.message, false);
  }
}

$('ldapForm').addEventListener('submit', (event) => {
  event.preventDefault();
  saveSection('ldap', {
    url: $('ldapUrl').value, base_dn: $('ldapBaseDn').value,
    user_dn: $('ldapUserDn').value, user_filter: $('ldapUserFilter').value,
    bind_dn: $('ldapBindDn').value, bind_password: $('ldapBindPassword').value,
    admin_group: $('ldapAdminGroup').value, label: $('ldapLabel').value,
    timeout: $('ldapTimeout').value, starttls: $('ldapStarttls').checked,
  }, 'ldapMsg');
});

$('btnTestLdap').addEventListener('click', async () => {
  const btn = $('btnTestLdap');
  btn.disabled = true;
  msg($('ldapMsg'), '正在连接 LDAP…', true);
  try {
    const r = await api('POST', '/api/admin/auth-config/test', { kind: 'ldap' });
    msg($('ldapMsg'), r.detail, r.ok === true);
  } catch (err) {
    msg($('ldapMsg'), err.message, false);
  } finally {
    btn.disabled = false;
  }
});

$('btnResetLdap').addEventListener('click', async () => {
  if (!confirm('清掉页面里保存的 LDAP 设置，回退到环境变量？')) return;
  try {
    const res = await api('DELETE', '/api/admin/auth-config?kind=ldap');
    authConfig = { ldap: res.ldap, sso: res.sso };
    applyLdap(res.ldap); fillSrcMap('ldap');
    msg($('ldapMsg'), '已回退到环境变量', true);
  } catch (err) {
    msg($('ldapMsg'), err.message, false);
  }
});

$('ssoForm').addEventListener('submit', (event) => {
  event.preventDefault();
  saveSection('sso', {
    issuer: $('ssoIssuer').value, client_id: $('ssoClientId').value,
    client_secret: $('ssoClientSecret').value, scopes: $('ssoScopes').value,
    redirect_uri: $('ssoRedirect').value, admin_group: $('ssoAdminGroup').value,
    username_claim: $('ssoUsernameClaim').value, groups_claim: $('ssoGroupsClaim').value,
    label: $('ssoLabel').value, token_auth: $('ssoTokenAuth').value,
  }, 'ssoMsg');
});

$('btnTestSso').addEventListener('click', async () => {
  const btn = $('btnTestSso');
  btn.disabled = true;
  msg($('ssoMsg'), '正在拉 discovery 文档…', true);
  try {
    const r = await api('POST', '/api/admin/auth-config/test', { kind: 'sso' });
    const lines = [r.detail];
    if (r.ok) {
      lines.push(`authorization：${r.authorization_endpoint}`);
      lines.push(`token：${r.token_endpoint}`);
      lines.push(`jwks：${r.jwks_uri}`);
    }
    msg($('ssoMsg'), lines.join('\n'), !!r.ok);
  } catch (err) {
    msg($('ssoMsg'), err.message, false);
  } finally {
    btn.disabled = false;
  }
});

$('btnResetSso').addEventListener('click', async () => {
  if (!confirm('清掉页面里保存的 SSO 设置，回退到环境变量？')) return;
  try {
    const res = await api('DELETE', '/api/admin/auth-config?kind=sso');
    authConfig = { ldap: res.ldap, sso: res.sso };
    applySso(res.sso); fillSrcMap('sso');
    msg($('ssoMsg'), '已回退到环境变量', true);
  } catch (err) {
    msg($('ssoMsg'), err.message, false);
  }
});

/* ------------------------------------------------------------ 文件浏览器 */
const fsPlace = { owner: '', project: '' };

function fmtSize(n) {
  if (!n) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function crumb() {
  const bits = ['data/'];
  if (fsPlace.owner) bits.push(fsPlace.owner + '/');
  if (fsPlace.project) bits.push(fsPlace.project + '/');
  $('fsCrumb').textContent = bits.join('');
  $('fsPath').textContent = bits.join('');
}

async function loadFiles(place) {
  Object.assign(fsPlace, { owner: '', project: '' }, place || {});
  crumb();
  const q = new URLSearchParams();
  if (fsPlace.owner) q.set('owner', fsPlace.owner);
  if (fsPlace.project) q.set('project', fsPlace.project);
  try {
    const res = await api('GET', '/api/admin/files?' + q.toString());
    renderFiles(res.entries || []);
  } catch (err) {
    msg($('fsMsg'), err.message, false);
  }
}

function renderFiles(entries) {
  const box = $('fsRows');
  box.innerHTML = '';
  if (!entries.length) {
    box.innerHTML = '<tr><td colspan="7" class="empty-cell">这里是空的</td></tr>';
    $('fsSummary').textContent = '';
    return;
  }
  const dirs = entries.filter((e) => e.type === 'dir').length;
  const files = entries.length - dirs;
  const bytes = entries.reduce((sum, e) => sum + (e.type === 'file' ? e.size : 0), 0);
  $('fsSummary').textContent = `${dirs} 个目录 · ${files} 个文件 · ${fmtSize(bytes)}`;

  entries.forEach((entry) => {
    const row = document.createElement('tr');
    const nameCell = document.createElement('td');
    const openBtn = document.createElement('button');
    openBtn.className = 'fs-name';
    openBtn.type = 'button';
    openBtn.textContent = (entry.type === 'dir' ? '📁 ' : entry.kind === 'image' ? '🖼 ' : '📄 ') + entry.name;
    if (entry.type === 'dir') {
      openBtn.addEventListener('click', () => {
        if (!fsPlace.owner) loadFiles({ owner: entry.name });
        else if (!fsPlace.project) loadFiles({ owner: fsPlace.owner, project: entry.name });
      });
    } else {
      openBtn.addEventListener('click', () => {
        window.open('/api/admin/files/raw?path=' + encodeURIComponent(entry.path), '_blank');
      });
    }
    nameCell.appendChild(openBtn);

    const typeCell = document.createElement('td');
    typeCell.textContent = entry.type === 'dir'
      ? (fsPlace.project ? '工程目录' : fsPlace.owner ? '工程' : '账号目录')
      : (entry.kind === 'image' ? '图片' : entry.kind === 'annotation' ? '标注' : entry.kind === 'labels' ? '标签集' : '文件');

    const sizeCell = document.createElement('td');
    sizeCell.textContent = entry.type === 'dir' ? '—' : fmtSize(entry.size);

    const countCell = document.createElement('td');
    if (entry.type === 'dir') {
      if (entry.images !== undefined) {
        const parts = [`${entry.images} 张图`];
        if (entry.annotations !== undefined) parts.push(`${entry.annotations} 个标注`);
        if (entry.projects !== undefined) parts.push(`${entry.projects} 个工程`);
        countCell.textContent = parts.join(' / ');
      } else {
        countCell.textContent = `${entry.entries ?? 0} 项`;
      }
    } else {
      countCell.textContent = '—';
    }

    const timeCell = document.createElement('td');
    timeCell.textContent = entry.mtime ? new Date(entry.mtime * 1000).toLocaleString() : '—';

    const whoCell = document.createElement('td');
    if (entry.last_actor) {
      whoCell.textContent = `${entry.last_actor} · ${entry.last_event === 'annotate' ? '标注' : entry.last_event === 'upload' ? '上传' : '删除'}`;
    } else if (entry.actors && entry.actors.length) {
      whoCell.textContent = entry.actors.join('、');
    } else {
      whoCell.textContent = '—';
    }

    const actionCell = document.createElement('td');
    if (entry.type === 'file') {
      const dl = document.createElement('button');
      dl.className = 'btn btn-sm';
      dl.textContent = '下载';
      dl.addEventListener('click', () => {
        location.href = '/api/admin/files/raw?path=' + encodeURIComponent(entry.path) + '&download=1';
      });
      const del = document.createElement('button');
      del.className = 'btn btn-sm btn-danger';
      del.textContent = '删除';
      del.style.marginLeft = '6px';
      del.addEventListener('click', async () => {
        if (!confirm(`确认删除 ${entry.name}？不可恢复。`)) return;
        try {
          await api('DELETE', '/api/admin/files?path=' + encodeURIComponent(entry.path));
          msg($('fsMsg'), `已删除 ${entry.name}`, true);
          await loadFiles(fsPlace);
        } catch (err) {
          msg($('fsMsg'), err.message, false);
        }
      });
      actionCell.append(dl, del);
    }
    row.append(nameCell, typeCell, sizeCell, countCell, timeCell, whoCell, actionCell);
    box.appendChild(row);
  });
}

$('fsUp').addEventListener('click', () => {
  if (fsPlace.project) loadFiles({ owner: fsPlace.owner });
  else loadFiles({});
});

async function loadUploaders() {
  try {
    const res = await api('GET', '/api/admin/uploaders?limit=120');
    $('uploaderStats').innerHTML = '';
    const stats = res.stats || {};
    [['上传总数', stats.uploads], ['涉及账号', stats.uploaders],
     ['累计体积', fmtSize(stats.uploaded_bytes)], ['全部流水', stats.events]]
      .forEach(([label, value]) => {
        const box = document.createElement('div');
        box.className = 'stat';
        const b = document.createElement('b');
        b.textContent = value ?? '—';
        const span = document.createElement('span');
        span.textContent = label;
        box.append(b, span);
        $('uploaderStats').appendChild(box);
      });

    const rows = res.recent || [];
    $('eventRows').innerHTML = rows.length ? '' : '<tr><td colspan="6" class="empty-cell">还没有记录（新上传的图片会出现在这里）</td></tr>';
    rows.forEach((ev) => {
      const row = document.createElement('tr');
      [
        ev.at,
        ev.actor,
        ev.event === 'upload' ? '上传' : ev.event === 'annotate' ? '保存标注' : ev.event === 'delete' ? '删除' : ev.event,
        [ev.owner, ev.project].filter(Boolean).join(' / '),
        ev.name,
        ev.size ? fmtSize(ev.size) : '—',
      ].forEach((text) => {
        const td = document.createElement('td');
        td.textContent = text || '—';
        row.appendChild(td);
      });
      $('eventRows').appendChild(row);
    });
  } catch (err) {
    msg($('eventMsg'), err.message, false);
  }
}

/* ------------------------------------------------------------ 系统设置 */
async function loadSystemConfig() {
  try {
    const cfg = await api('GET', '/api/admin/system-config');
    $('siteName').value = cfg.site_name || 'Labelme';
    $('uploadMaxMb').value = cfg.upload_max_mb || 200;
    $('systemAllowRegistration').checked = !!cfg.allow_registration;
    $('systemSuffixes').textContent = (cfg.image_suffixes || []).join('、') || '—';
    $('systemVersion').textContent = cfg.labelme_version || '—';
    $('systemState').textContent = '已加载';
    $('systemState').className = 'tag enabled';
    document.title = `管理后台 · ${cfg.site_name || 'Labelme'}`;
    const brand = document.querySelector('.admin-brand b');
    if (brand) brand.textContent = cfg.site_name || 'Labelme';
  } catch (err) {
    msg($('systemMsg'), err.message, false);
  }
}

$('systemForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const res = await api('PUT', '/api/admin/system-config', {
      site_name: $('siteName').value.trim(),
      upload_max_mb: Number($('uploadMaxMb').value),
      allow_registration: $('systemAllowRegistration').checked,
    });
    msg($('systemMsg'), `已保存：${(res.saved || []).join('、')}`, true);
    await loadSystemConfig();
  } catch (err) {
    msg($('systemMsg'), err.message, false);
  }
});

/* -------------------------------------------------------------- AI 模型 */
let aiConfig = null;
let aiProvider = 'sam';

function selectedSamModel() {
  const picked = document.querySelector('#aiModelGrid input[name="samModel"]:checked');
  return picked ? picked.value : (aiConfig?.sam?.model || 'sam2:base+');
}

function samModelName(id) {
  const hit = (aiConfig?.catalog || []).find((m) => m.id === id);
  return hit ? hit.name : id;
}

function renderAiCatalog(cfg) {
  const box = $('aiModelGrid');
  const catalog = cfg.catalog || [];
  const current = cfg.sam?.model || 'sam2:base+';
  box.innerHTML = '';
  catalog.forEach((model) => {
    const card = document.createElement('label');
    card.className = 'model-card' + (model.id === current ? ' selected' : '');
    const rec = model.recommended ? '<span class="rec">推荐</span>' : '';
    card.innerHTML = `${rec}
      <input type="radio" name="samModel" value="${model.id}" ${model.id === current ? 'checked' : ''} />
      <b>${model.name}</b>
      <span class="caps">${model.caps || ''}</span>
      <p class="note">${model.note || ''}</p>
      <span class="lic">许可：${model.license || ''}</span>`;
    card.querySelector('input').addEventListener('change', () => {
      box.querySelectorAll('.model-card').forEach((el) => el.classList.toggle('selected', el === card));
      updateAiState();
    });
    box.appendChild(card);
  });
  if (!catalog.length) {
    box.innerHTML = '<div class="empty-cell">后端没有返回模型目录</div>';
  }
}

function setAiProvider(provider) {
  aiProvider = provider === 'openai' ? 'openai' : 'sam';
  $('aiProviders').querySelectorAll('.provider-card').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.provider === aiProvider);
  });
  document.querySelectorAll('[data-ai-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.aiPanel !== aiProvider;
  });
  $('aiProviderNote').innerHTML = aiProvider === 'openai'
    ? 'OpenAI 兼容接口只保存连接信息，不会向标注页发起请求；测试连接会请求 <code>/models</code>。'
    : 'SAM 服务需实现 <code>GET /health</code>；Web 后端只保存配置并做连通测试，真正的分割推理由该服务完成。';
  updateAiState();
}

function updateAiState() {
  const el = $('aiState');
  if (!aiConfig) return;
  if (!aiConfig.enabled) {
    el.textContent = '未启用';
    el.className = 'tag disabled';
    return;
  }
  if (aiProvider === 'openai') {
    el.textContent = '已配置 · OpenAI 兼容接口预留';
  } else {
    el.textContent = `已配置 · 待接入推理（${samModelName(selectedSamModel())}）`;
  }
  el.className = 'tag enabled';
}

async function loadAiConfig() {
  try {
    const cfg = await api('GET', '/api/admin/ai-config');
    aiConfig = cfg;
    $('aiEnabled').checked = !!cfg.enabled;
    renderAiCatalog(cfg);
    $('aiSamEndpoint').value = cfg.sam?.endpoint || '';
    $('aiSamTimeout').value = cfg.sam?.timeout || 30;
    $('aiSamApiKey').value = '';
    $('aiSamApiKey').placeholder = cfg.sam?.api_key_set ? '已设置（留空不修改）' : '留空 = 不设置';
    $('aiOpenaiBase').value = cfg.openai?.base_url || '';
    $('aiOpenaiModel').value = cfg.openai?.model || '';
    $('aiOpenaiTimeout').value = cfg.openai?.timeout || 30;
    $('aiOpenaiApiKey').value = '';
    $('aiOpenaiApiKey').placeholder = cfg.openai?.api_key_set ? '已设置（留空不修改）' : '留空 = 不设置';
    setAiProvider(cfg.provider || 'sam');
  } catch (err) {
    msg($('aiMsg'), err.message, false);
  }
}

$('aiProviders').addEventListener('click', (event) => {
  const btn = event.target.closest('.provider-card');
  if (btn) setAiProvider(btn.dataset.provider);
});

$('aiEnabled').addEventListener('change', () => {
  if (aiConfig) {
    aiConfig.enabled = $('aiEnabled').checked;
    updateAiState();
  }
});

$('aiForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const body = {
      enabled: $('aiEnabled').checked,
      provider: aiProvider,
      sam: {
        endpoint: $('aiSamEndpoint').value.trim(),
        model: selectedSamModel(),
        timeout: Number($('aiSamTimeout').value || 30),
      },
      openai: {
        base_url: $('aiOpenaiBase').value.trim(),
        model: $('aiOpenaiModel').value.trim(),
        timeout: Number($('aiOpenaiTimeout').value || 30),
      },
    };
    const samKey = $('aiSamApiKey').value.trim();
    if (samKey) body.sam.api_key = samKey;
    const openaiKey = $('aiOpenaiApiKey').value.trim();
    if (openaiKey) body.openai.api_key = openaiKey;
    const res = await api('PUT', '/api/admin/ai-config', body);
    msg($('aiMsg'), `已保存：${(res.saved || []).join('、')}`, true);
    await loadAiConfig();
  } catch (err) {
    msg($('aiMsg'), err.message, false);
  }
});

$('btnTestAi').addEventListener('click', async () => {
  msg($('aiMsg'), `正在测试 ${aiProvider === 'openai' ? 'OpenAI 兼容接口' : 'SAM 服务'}…`, true);
  try {
    const res = await api('POST', '/api/admin/ai-config/test', { provider: aiProvider });
    const label = res.provider === 'openai' ? 'OpenAI 兼容接口' : 'SAM 服务';
    msg($('aiMsg'), res.ok
      ? `${label}可达（HTTP ${res.status}）：${res.detail}`
      : `${label}连接失败：${res.detail}`, res.ok);
  } catch (err) {
    msg($('aiMsg'), err.message, false);
  }
});

$('btnClearAi').addEventListener('click', async () => {
  if (!confirm('恢复默认会清掉 SAM 与 OpenAI 两套地址、模型和 API Key，确定？')) return;
  try {
    await api('DELETE', '/api/admin/ai-config');
    msg($('aiMsg'), '已恢复默认', true);
    await loadAiConfig();
  } catch (err) {
    msg($('aiMsg'), err.message, false);
  }
});

/* 首次进入：按 hash 定位，默认概览 */
showView((location.hash || '').replace('#', '') || 'overview');
load();

