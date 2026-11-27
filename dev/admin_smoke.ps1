# 管理后台分区 + LDAP/SSO 配置 + 文件浏览器 冒烟测试（隔离库，端口 9632）
$ErrorActionPreference = 'Stop'
$root = 'C:\Users\Shiroko\Desktop\labelme-web'
$py = Join-Path $root '.venv\Scripts\python.exe'
$tmp = Join-Path $env:TEMP ("lmweb-admin-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path (Join-Path $tmp 'var') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $tmp 'data') -Force | Out-Null

$env:LABELME_WEB_DB = Join-Path $tmp 'var\smoke.db'
$env:LABELME_WEB_DATA = Join-Path $tmp 'data'
$env:LABELME_WEB_ALLOW_REGISTRATION = '1'

$log = Join-Path $tmp 'uvicorn.log'
$err = Join-Path $tmp 'uvicorn.err'
$proc = Start-Process -FilePath $py -ArgumentList @('-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '9632') `
  -WorkingDirectory $root -RedirectStandardOutput $log -RedirectStandardError $err -PassThru
Start-Sleep -Seconds 4
$base = 'http://127.0.0.1:9632'
$pass = 0; $total = 0
$script:csrf = ''
function Check($cond, $name) {
  $script:total++
  if ($cond) { $script:pass++; Write-Host "PASS $name" } else { Write-Host "FAIL $name" }
}
function J($method, $url, $body) {
  $args = @{ Method = $method; Uri = "$base$url"; WebSession = $script:sess; ContentType = 'application/json' }
  if ($script:csrf) { $args.Headers = @{ 'X-CSRF-Token' = $script:csrf } }
  if ($null -ne $body) { $args.Body = ($body | ConvertTo-Json -Compress) }
  Invoke-RestMethod @args
}

try {
  $sess = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  Invoke-RestMethod -Method Post -Uri "$base/api/auth/register" -ContentType 'application/json' `
    -Body (@{ username = 'tester'; password = 'Passw0rd!2026' } | ConvertTo-Json) -WebSession $sess | Out-Null
  Check ((J GET '/api/auth/me' $null).user.role -eq 'admin') '第一个注册账号是管理员'
  $script:csrf = (J GET '/api/auth/me' $null).csrf_token
  Check ([bool]$script:csrf) 'GET /api/auth/me 返回 CSRF token'

  $methods = J GET '/api/auth/methods' $null
  Check ($methods.sso.enabled -eq $false) '/api/auth/methods 带 sso 且默认关闭'

  $cfg = J GET '/api/admin/auth-config' $null
  Check ($null -ne $cfg.ldap.url -and $null -ne $cfg.sso.client_secret) 'GET auth-config 返回两段'

  $r = J PUT '/api/admin/auth-config' @{ ldap = @{ url = 'ldap://127.0.0.1:1'; timeout = 2; label = '测试 LDAP' } }
  Check (($r.saved -contains 'ldap.url') -and $r.ldap.enabled -eq $true) 'PUT 保存 LDAP 配置后即启用'

  $t = J POST '/api/admin/auth-config/test' @{ kind = 'ldap' }
  Check ($t.ok -eq $false -and $t.detail -match '连接失败') 'LDAP 测试连接对不可达地址返回失败原因'

  $s = J POST '/api/admin/auth-config/test' @{ kind = 'sso' }
  Check ($s.ok -eq $null -and $s.detail -match '未启用') '未配置 SSO 时测试返回「未启用」'

  $r2 = J PUT '/api/admin/auth-config' @{ sso = @{ issuer = 'http://127.0.0.1:1/oidc'; client_id = 'lmweb'; client_secret = 'abc' } }
  Check ($r2.sso.enabled -eq $true -and $r2.sso.client_secret_set -eq $true) 'PUT 保存 SSO 配置后即启用'
  Check ($r2.sso.client_secret -notmatch 'abc') 'client_secret 回显被遮罩'
  $s2 = J POST '/api/admin/auth-config/test' @{ kind = 'sso' }
  Check ($s2.ok -eq $false) 'SSO discovery 对不可达 issuer 返回失败'

  $clr = J DELETE '/api/admin/auth-config?kind=sso' $null
  Check ($clr.sso.enabled -eq $false) '清掉 SSO 配置后回退为未启用'

  # 工程 / 会话 / 上传
  J POST '/api/projects' @{ owner = 'tester'; name = '冒烟工程' } | Out-Null
  $cs = J POST '/api/sessions?owner=tester&project=冒烟工程' $null
  $sid = $cs.session
  $img = Join-Path $tmp 'sample.jpg'
  [IO.File]::WriteAllBytes($img, [byte[]](0xFF, 0xD8, 0xFF, 0xD9))
  Invoke-RestMethod -Method Post -Uri "$base/api/sessions/$sid/images" -Form @{ files = Get-Item $img } -WebSession $sess -Headers @{ 'X-CSRF-Token' = $script:csrf } | Out-Null
  Check $true '上传一张图'

  $files = J GET '/api/admin/files' $null
  Check (($files.entries | Where-Object { $_.name -eq 'tester' }).type -eq 'dir') '文件浏览器根目录列出账号目录'
  $f2 = J GET '/api/admin/files?owner=tester' $null
  Check (($f2.entries | Where-Object { $_.name -eq '冒烟工程' }).projects -eq 1) 'owner 层列出工程并带工程数'
  $f3 = J GET "/api/admin/files?owner=tester&project=%E5%86%92%E7%83%9F%E5%B7%A5%E7%A8%8B" $null
  $sample = $f3.entries | Where-Object { $_.name -eq 'sample.jpg' }
  Check ($sample.kind -eq 'image' -and $sample.last_actor -eq 'tester' -and $sample.last_event -eq 'upload') '工程层直接列出文件并带「谁上传的」'

  $raw = Invoke-WebRequest -Uri "$base/api/admin/files/raw?path=tester/%E5%86%92%E7%83%9F%E5%B7%A5%E7%A8%8B/sample.jpg" -WebSession $sess
  Check ($raw.StatusCode -eq 200 -and $raw.RawContentLength -ge 4) 'raw 下载可读'
  $bad = $null
  try { Invoke-WebRequest -Uri "$base/api/admin/files/raw?path=../backend/app.py" -WebSession $sess | Out-Null } catch { $bad = $_.Exception.Response.StatusCode.value__ }
  Check ($bad -eq 400) '路径越界被拒（400）'

  $up = J GET '/api/admin/uploaders?limit=10' $null
  Check ($up.stats.uploads -eq 1 -and @($up.by_actor)[0].actor -eq 'tester') '上传流水按人汇总'

  $del = J DELETE "/api/admin/files?path=tester/%E5%86%92%E7%83%9F%E5%B7%A5%E7%A8%8B/sample.jpg" $null
  Check ($del.size -ge 4) '管理员删除单个文件并记录'
} finally {
  Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
}

Write-Host "`n$pass/$total PASS   临时目录：$tmp"
if (Test-Path $err) { Get-Content $err -Tail 12 }

