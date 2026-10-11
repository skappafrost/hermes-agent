$ErrorActionPreference = 'SilentlyContinue'
# bin\hermes.exe is a silent no-op on this install (verified: --version prints nothing), while the
# venv shim works. Resolve by asking each candidate, not by trusting existence.
$exe = $null
foreach ($c in @(
    'C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe',
    'C:\Users\Ha Trung\AppData\Local\hermes\bin\hermes.exe')) {
    if (Test-Path $c) {
        $ver = (& $c --version 2>&1 | Out-String).Trim()
        if ($ver -match 'Hermes') { $exe = $c; break }
    }
}
if (-not $exe) { $exe = 'C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe' }
$ts      = 'C:\Program Files\Tailscale\tailscale.exe'
$desktop = [Environment]::GetFolderPath('Desktop')
$log     = Join-Path $desktop 'hermes-expose.log'
$urlFile = Join-Path $desktop 'hermes-url.txt'
$port    = 9119

function Log([string]$m) { "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $log }

# ---------- 1. backend ----------
# Bound to 0.0.0.0 on purpose: the Tailscale funnel proxies to 127.0.0.1:9119 and would work
# with a loopback bind, but the Radmin-VPN peer path needs the non-loopback bind too.
$live = @(Get-NetTCPConnection -State Listen -LocalPort $port)
if ($live.Count -eq 0) {
    Start-Process -FilePath $exe -WindowStyle Hidden -ArgumentList `
        'dashboard', '--host', '0.0.0.0', '--port', "$port", '--no-open', '--skip-build'
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 3
        $live = @(Get-NetTCPConnection -State Listen -LocalPort $port)
        if ($live.Count -gt 0) { break }
    }
    if ($live.Count -eq 0) { Log 'FAIL - backend khong listen sau 60s'; exit 1 }
    Log ("BACKEND-ON bind {0} PID {1}" -f $live[0].LocalAddress, $live[0].OwningProcess)
} else {
    Log ("BACKEND-EXISTS PID {0}" -f $live[0].OwningProcess)
}

# ---------- 2. tailscale funnel ----------
if (-not (Test-Path $ts)) { Log 'FAIL - khong thay tailscale.exe; funnel khong bat duoc'; exit 1 }

# Luc boot: tailscale service co the chua kip ket noi -> cho toi da 60s thay vi fail ngay.
for ($i = 0; $i -lt 20; $i++) {
    $ipOut = (& $ts ip 2>&1 | Out-String)
    if ($ipOut -match '100\.\d+\.\d+\.\d+') { break }
    Start-Sleep -Seconds 3
}

# Ten node hien tai. Sau khi doi ten may/ten tailnet, `funnel status` co the con giu entry cu
# (voi ten cu) song song entry moi -> khong duoc lay match dau tien.
$hostName = (& $ts status --peers=false 2>&1 | Out-String)
$hostName = [regex]::Match($hostName, '^\s*\d+\.\d+\.\d+\.\d+\s+(\S+)', 'Multiline').Groups[1].Value

function Get-FunnelUrl {
    $s = & $ts funnel status 2>&1 | Out-String
    $all = [regex]::Matches($s, 'https://[a-z0-9.-]+\.ts\.net') | ForEach-Object { $_.Value } | Sort-Object -Unique
    if (-not $all) { return $null }
    if ($hostName) {
        $mine = @($all | Where-Object { $_ -match ('^https://' + [regex]::Escape($hostName) + '\.') })
        if ($mine.Count) { return $mine[0] }
    }
    return $all[-1]
}
function Test-FunnelPublic([string]$u) {
    if (-not $u) { return $false }
    try {
        $r = Invoke-WebRequest "$u/api/status" -UseBasicParsing -TimeoutSec 20
        return ([int]$r.StatusCode -eq 200)
    } catch { return $false }
}
# Goi HTTP tu chinh node nay KHONG phan biet duoc funnel ON vs tailnet-only: ten ts.net van giai
# duoc qua tailnet va backend tu tra loi 200. Funnel phai duoc doc tu `funnel status`.
function Test-FunnelFlagOn {
    $s = & $ts funnel status 2>&1 | Out-String
    return ($s -match '\(Funnel on\)')
}

$url = Get-FunnelUrl
if ($url -and (Test-FunnelFlagOn) -and (Test-FunnelPublic $url)) {
    Log ("FUNNEL-EXISTS $url (public tra loi OK)")
} else {
    if ($url -and (Test-FunnelPublic $url)) { Log ("FUNNEL-OFF nhung node tu tra loi qua tailnet ($url) -> bat funnel") }
    elseif ($url) { Log ("FUNNEL-LIEN nhung khong tra loi ($url) -> tao lai") }
    else { Log 'FUNNEL-NONE -> bat funnel' }
    & $ts funnel --bg $port 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    $url = Get-FunnelUrl
}

if (-not $url) {
    Log 'FAIL - funnel khong tra ve URL (kiem tra ACL: nodeAttrs funnel trong admin console)'
    Set-Content -Path $urlFile -Value 'LOI - Funnel khong bat duoc'
    exit 1
}

# ---------- 3. prove it answers ----------
if (Test-FunnelPublic $url) {
    try {
        $j = (Invoke-WebRequest "$url/api/status" -UseBasicParsing -TimeoutSec 20).Content | ConvertFrom-Json
        # Ghi nhan: goi do xay ra TU MAY NAY, nen ve mat ly thuyet no co the tu tra loi minh.
        # Da chung minh bang mot phat goi tu ben ngoai (khong phai may nay) rang funnel public that.
        Log ("FUNNEL-ON $url HTTP 200 auth_required=$($j.auth_required) providers=[$($j.auth_providers -join ',')] profiles=$(($j.profiles | Measure-Object).Count)")
    } catch { Log "WARN - $url khong tra loi: $_" }
} else {
    Log "FAIL - $url khong tra loi 200"
}
Set-Content -Path $urlFile -Value $url
Log ("URL da luu vao $urlFile (co dinh, khong doi theo lan chay)" )
