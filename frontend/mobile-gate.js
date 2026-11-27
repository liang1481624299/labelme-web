/* 手机访问限制：仅用于登录页和标注页；管理员后台及其登录入口不受限制。 */
(function () {
  'use strict';

  var ADMIN_PATH = /^\/admin(?:\.html)?\/?$/i;

  function isAdminPath(path) {
    return ADMIN_PATH.test(path || '');
  }

  function isAdminEntry() {
    if (isAdminPath(location.pathname)) return true;
    if (!/^\/login(?:\/|\.html)?$/i.test(location.pathname)) return false;
    var next = new URLSearchParams(location.search).get('next') || '';
    if (!next) return false;
    try {
      return isAdminPath(new URL(next, location.href).pathname);
    } catch (err) {
      return false;
    }
  }

  function isPhoneScreen() {
    var shortSide = Math.min(window.innerWidth || 0, window.innerHeight || 0);
    var coarse = false;
    try {
      coarse = !!(window.matchMedia && (
        window.matchMedia('(pointer: coarse)').matches
        || window.matchMedia('(any-pointer: coarse)').matches
      ));
    } catch (err) { /* 老浏览器没有 matchMedia 时按 UA 判断 */ }
    var phoneUa = /iPhone|iPod|Android.+Mobile|Mobile.+Android|Windows Phone|BlackBerry|Opera Mini|IEMobile/i.test(navigator.userAgent || '');
    var uaMobile = !!(navigator.userAgentData && navigator.userAgentData.mobile);
    var touch = coarse || ((navigator.maxTouchPoints || 0) > 1);
    var mobileUa = phoneUa || uaMobile;
    if (mobileUa) return shortSide < 700;
    // 没有移动 UA 的触屏设备（iPad 桌面 UA、触屏笔记本、Windows 平板）按宽度判断，避免误伤触屏笔记本
    return touch && (window.innerWidth || 0) < 700;
  }

  if (isAdminEntry()) return;

  var gate = null;
  var current = null;

  function buildGate() {
    gate = document.createElement('div');
    gate.id = 'mobileGate';
    gate.innerHTML =
      '<div class="mg-card" role="dialog" aria-modal="true" aria-labelledby="mgTitle" tabindex="-1">'
      + '<div class="mg-kicker"><span class="mg-logo">L</span><span>Labelme · 屏幕限制</span></div>'
      + '<h1 id="mgTitle">手机屏幕暂不支持标注</h1>'
      + '<p>标注页需要完整的左右面板和精确的指针操作，手机宽度会把画布挤到无法使用。</p>'
      + '<ul>'
      + '<li>请改用电脑浏览器，或 iPad / 大屏平板（屏幕短边不小于 700px）。</li>'
      + '<li>手机只保留管理员后台；管理员登录不受此限制。</li>'
      + '</ul>'
      + '<div class="mg-actions">'
      + '<a class="mg-primary" href="/admin">前往管理员后台</a>'
      + '</div>'
      + '<p class="mg-note">在电脑或大屏设备上打开当前地址即可继续标注。</p>'
      + '<div class="mg-current" aria-live="polite"></div>'
      + '</div>';
    document.body.appendChild(gate);
    current = gate.querySelector('.mg-current');
  }

  function setInert(on) {
    var app = document.getElementById('app');
    var card = document.querySelector('.auth-card');
    [app, card].forEach(function (el) {
      if (!el) return;
      el.inert = on;
      if (on) el.setAttribute('aria-hidden', 'true');
      else el.removeAttribute('aria-hidden');
    });
  }

  function sync() {
    var blocked = isPhoneScreen();
    window.LabelmeMobileGateBlocked = blocked;
    document.documentElement.dataset.mobileGate = blocked ? '1' : '0';
    if (blocked && !gate) buildGate();
    if (gate) {
      gate.hidden = !blocked;
      if (blocked && current) {
        current.textContent = '当前窗口 ' + window.innerWidth + ' × ' + window.innerHeight + 'px';
        setTimeout(function () {
          var card = gate.querySelector('.mg-card');
          if (card) card.focus();
        }, 0);
      }
    }
    setInert(blocked);
  }

  document.addEventListener('keydown', function (ev) {
    if (!window.LabelmeMobileGateBlocked) return;
    var target = ev.target;
    if (target && target.closest && target.closest('#mobileGate')) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, true);

  window.addEventListener('resize', function () { sync(); }, { passive: true });
  window.addEventListener('orientationchange', function () { setTimeout(sync, 120); }, { passive: true });
  window.addEventListener('pageshow', function () { sync(); });

  sync();
})();
