<#
  hermes-status.ps1 -- bao cao trang thai expose that cua may nay.
  Goi cong khai bang cach double-click Hermes-Status.vbs tren Desktop.
  ASCII-only: Windows PowerShell 5.1 doc .ps1 khong BOM theo ANSI.
#>
param([switch]$NoPause)

$exe    = 'C:\Users\Ha Trung\AppData\Local\hermes\bin\hermes.exe'
$ts     = 'C:\Program Files\Tailscale\tailscale.exe'
$port   = 9119
$desktop= [Environment]::GetFolderPath('Desktop')
$log    = Join-Path $desktop 'hermes-expose.log'
$envFile= 'C:\Users\Ha Trung\AppData\Local\hermes\.env'

function H1([string]$t) { Write-Host ''; Write-Host ('=' * 66) -ForegroundColor DarkCyan; Write-Host "  $t" -ForegroundColor Cyan; Write-Host ('=' * 66) -ForegroundColor DarkCyan }
function Row([string]$k, [string]$v, [string]$c = 'Gray') { Write-Host ("  {0,-26} " -f $k) -NoNewline; Write-Host $v -ForegroundColor $c }
function Good([string]$t) { Write-Host "  [OK]   $t" -ForegroundColor Green }
function Bad([string]$t)  { Write-Host "  [NO]   $t" -ForegroundColor DarkRed }
function Warn([string]$t) { Write-Host "  [!!]   $t" -ForegroundColor Yellow }

Write-Host ''
Write-Host '  HERMES EXPOSURE STATUS' -ForegroundColor White
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')   may: $env:COMPUTERNAME" -ForegroundColor DarkGray

# ---------------- 1. backend ----------------
H1 '1. HERMES BACKEND (cong local)'
$conn = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
$bound = $false
if ($conn.Count -gt 0) {
    $bound = $true
    $addr = $conn[0].LocalAddress
    $pidN = $conn[0].OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidN" -ErrorAction SilentlyContinue
    Row 'Listening' 'YES' Green
    Row 'Bind address' $addr $(if ($addr -eq '0.0.0.0' -or $addr -eq '::') { 'Yellow' } else { 'Green' })
    Row 'PID' $pidN
    if ($proc) { Row 'Command line' ($proc.CommandLine -replace '.*hermes', 'hermes' | ForEach-Object { $_.Substring(0, [Math]::Min(48, $_.Length)) }) DarkGray }
    if ($addr -eq '0.0.0.0' -or $addr -eq '::') { Warn 'bind 0.0.0.0 = moi interface (LAN + VPN) deu cham duoc neu firewall cho' }
} else {
    Row 'Listening' 'NO' DarkRed
    Bad 'backend dang CHONG chay -> khong ai vao duoc'
}

# ---------------- 2. what it reports ----------------
if ($bound) {
    H1 '2. TINH TRANG AUTH (loai gate)'
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$port/api/status" -UseBasicParsing -TimeoutSec 8
        $j = $r.Content | ConvertFrom-Json
        Row 'HTTP local' ([string][int]$r.StatusCode) Green
        Row 'Hermes version' $j.version
        Row 'auth_required' $j.auth_required $(if ($j.auth_required) { 'Green' } else { 'Red' })
        Row 'auth_providers' (($j.auth_providers | ForEach-Object { "$_" }) -join ', ')
        Row 'profiles' (($j.profiles | ForEach-Object { "$_" }) -join ', ')
        Row 'gateway_running' $j.gateway_running $(if ($j.gateway_running) { 'Green' } else { 'Yellow' })
        if (-not $j.gateway_running) { Warn 'gateway messaging tat: Telegram/Discord khong chay (chat Desktop van binh thuong)' }
        if (-not $j.auth_required) { Bad 'AUTH GATE TAT -> bat ky ai cham duoc cong la vao thang' }
    } catch { Bad "khong doc duoc /api/status: $_" }
}

# ---------------- 3. funnel ----------------
H1 '3. TAILSCALE FUNNEL (cong public)'
$funnelUrl = $null
if (Test-Path $ts) {
    $v = & $ts version 2>&1 | Select-Object -First 1
    Row 'Tailscale' "$v" Green
    $st = & $ts status --peers=false 2>&1 | Out-String
    if ($st -match '(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\S+)\s+(\w+)') {
        Row 'Node' ("{0}  ({1})" -f $matches[2], $matches[1])
        Row 'Login' $matches[3] DarkGray
    }
    $fs = & $ts funnel status 2>&1 | Out-String
    $all = [regex]::Matches($fs, 'https://[a-z0-9.-]+\.ts\.net') | ForEach-Object { $_.Value } | Sort-Object -Unique
    $node = $null
    $node = [regex]::Match($st, '^\s*\d+\.\d+\.\d+\.\d+\s+(\S+)', 'Multiline').Groups[1].Value
    Row 'Node name' $node Green
    if ($all.Count -gt 1) { Warn ("funnel status tra " + $all.Count + " URL (entry cu sau khi doi ten): " + ($all -join ' , ')) }
    $cand = @($all | Where-Object { $_ -match ('^https://' + [regex]::Escape($node) + '\.') })
    if ($cand.Count) { $funnelUrl = $cand[0] } elseif ($all.Count) { $funnelUrl = $all[-1] }

    if ($funnelUrl) {
        Row 'Funnel' 'ON' Green
        Row 'Public URL' $funnelUrl Cyan
        try {
            $pr = Invoke-WebRequest "$funnelUrl/api/status" -UseBasicParsing -TimeoutSec 20
            Row 'Goi tu may nay' ("HTTP {0}" -f [int]$pr.StatusCode) Green
            Good 'backend tra loi qua URL funnel. Luu y: do tu may nay nen van co the tu tra loi minh; nguoi ngoai xac nhan lan dau la du.'
        } catch {
            Row 'Goi tu may nay' 'FAIL' Red
            Bad 'URL khong tra loi -> chay lai Hermes-Expose.vbs'
        }
    } else {
        Row 'Funnel' 'OFF' DarkGray
        Good 'khong co cong public qua Tailscale'
    }
    Row 'MagicDNS (KHONG phai funnel)' "$($node).<tailnet>.ts.net:9119" DarkGray
    Warn 'URL co :9119 hoac phan giai ve 100.x = CHI thiet bi trong tailnet vao duoc, dung de chia se ra ngoai'
} else {
    Bad 'khong thay tailscale.exe'
}

# ---------------- 4. other paths ----------------
H1 '4. DUONG KHAC (LAN / Radmin VPN)'
$vpn = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '26.*' })
if ($vpn.Count) { Row 'Radmin VPN IP' ($vpn | ForEach-Object { $_.IPAddress }) Green } else { Row 'Radmin VPN IP' 'khong co (VPN chua ket noi)' DarkGray }
$lan = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '192.168.*' })
foreach ($l in $lan) { Row 'LAN' "$($l.IPAddress) ($($l.InterfaceAlias))" DarkGray }
$fw = Get-NetFirewallRule -DisplayName 'Hermes_RadminVPN' -ErrorAction SilentlyContinue
if ($fw) {
    $ra = $fw | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
    Row 'Firewall rule' ("OPEN cho {0}" -f ($ra.RemoteAddress -join ',')) Yellow
    Warn 'rule nay cho peer VPN vao 9119 - xoa neu khong dung nua'
} elseif ($bound) {
    Row 'Firewall rule' 'khong doc duoc (can admin) hoac khong ton tai' DarkGray
}

# ---------------- 5. credentials ----------------
H1 '5. CREDENTIALS (chi hien ten, khong hien mat khau)'
if (Test-Path $envFile) {
    $t = Get-Content $envFile -Raw
    $u = [regex]::Match($t, '(?m)^HERMES_DASHBOARD_BASIC_AUTH_USERNAME=(.*)$').Groups[1].Value
    $p = [regex]::Match($t, '(?m)^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=(.*)$').Groups[1].Value
    $s = [regex]::Match($t, '(?m)^HERMES_DASHBOARD_BASIC_AUTH_SECRET=(.*)$').Groups[1].Value
    Row 'Username' $u Green
    Write-Host ("  {0,-26} (do dai {1} ky tu)" -f 'Password', $p.Length) -ForegroundColor $(if ($p.Length -ge 14) { 'Green' } else { 'Yellow' })
    Row 'Signing secret' $(if ($s.Length -ge 32) { 'co, du dai' } elseif ($s) { 'NGAN - session mat khi reboot' } else { 'KHONG CO - bi da login moi lan reboot' }) $(if ($s.Length -ge 32) { 'Green' } else { 'Red' })
} else { Bad "khong thay $envFile" }

# ---------------- 6. verdict ----------------
H1 'KET LUAN'
$publicOpen = ($null -ne $funnelUrl)
$lanOpen = ($bound -and ($conn[0].LocalAddress -in @('0.0.0.0', '::')))
if ($publicOpen) {
    Write-Host '  >>> MO PUBLIC qua Tailscale Funnel' -ForegroundColor Red
    Write-Host "  >>> URL dung duoc cho may la: $funnelUrl" -ForegroundColor Cyan
    Write-Host '  >>> Khong dung dang co :9119 - dang do chi trong tailnet' -ForegroundColor Yellow
    Write-Host '  >>> Tat: double-click Hermes-Unexpose.vbs' -ForegroundColor Yellow
} elseif ($lanOpen) {
    Write-Host '  >>> KHONG public, nhung bind 0.0.0.0 -> LAN/VPN cham duoc (firewall quyet dinh)' -ForegroundColor Yellow
} elseif ($bound) {
    Write-Host '  >>> AN TOAN: chi loopback' -ForegroundColor Green
} else {
    Write-Host '  >>> TAT HOAN TOAN - khong co cong nao mo' -ForegroundColor Green
}

Write-Host ''
Write-Host '  Log gan nhat:' -ForegroundColor DarkGray
if (Test-Path $log) { Get-Content $log -Tail 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } } else { Write-Host '    (chua co log)' -ForegroundColor DarkGray }
Write-Host ''
if (-not $NoPause) { Read-Host '  Bam Enter de dong' }
