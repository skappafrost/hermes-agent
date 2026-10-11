<#
  hermes-setpass.ps1 -- doi mat khau dashboard Hermes nhanh.
  Double-click Hermes-SetPass.vbs tren Desktop.
  Mat khau KHONG bao gio bi in ra man hinh hay ghi vao log.
  ASCII-only cho Windows PowerShell 5.1.
#>
param([switch]$NoPause)

# bin\hermes.exe is a silent no-op on this install; resolve by asking each candidate.
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
$envFile = 'C:\Users\Ha Trung\AppData\Local\hermes\.env'
$port    = 9119
$log     = Join-Path ([Environment]::GetFolderPath('Desktop')) 'hermes-expose.log'

function Log([string]$m) { "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $log }
function Line { Write-Host ('-' * 62) -ForegroundColor DarkGray }

Write-Host ''
Write-Host '  DOI MAT KHAU HERMES DASHBOARD' -ForegroundColor Cyan
Line

if (-not (Test-Path $envFile)) { Write-Host "  KHONG THAY $envFile" -ForegroundColor Red; if (-not $NoPause) { Read-Host '  Enter de dong' }; exit 1 }

Write-Host '  [1] Nhap mat khau moi' -ForegroundColor White
Write-Host '  [2] Sinh mat khau ngau nhien manh (khuyen dung)' -ForegroundColor White
Write-Host '  [3] Huy' -ForegroundColor DarkGray
$choice = (Read-Host '  Chon').Trim()

$new = $null
if ($choice -eq '2') {
    $alpha = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_.@%+='
    $bytes = New-Object byte[] 22
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $new = -join ($bytes | ForEach-Object { $alpha[$_ % $alpha.Length] })
    Write-Host ''
    Write-Host '  Mat khau moi da sinh. No se hien DU ONE lan duy nhat:' -ForegroundColor Yellow
    Line
    Write-Host "    $new" -ForegroundColor White
    Line
    Write-Host '  Copy ngay bay gio - khong hien lai lan nao nua.' -ForegroundColor Yellow
    $ok = Read-Host '  Da copy xong? (y/N)'
    if ($ok -notmatch '^[yY]') { Write-Host '  Huy. Khong doi gi.' -ForegroundColor Gray; if (-not $NoPause) { Read-Host '  Enter' }; exit 0 }
} elseif ($choice -eq '1') {
    do {
        $a = Read-Host '  Mat khau moi' -AsSecureString
        $b = Read-Host '  Nhap lai'    -AsSecureString
        $sa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
        $sb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
        if ($sa -ne $sb) { Write-Host '  Khong khop, lam lai.' -ForegroundColor Red }
        elseif ($sa.Length -lt 8) { Write-Host '  Qua ngan (toi thieu 8 ky tu).' -ForegroundColor Red }
        else { $new = $sa }
    } while (-not $new)
} else {
    Write-Host '  Huy.' -ForegroundColor Gray
    if (-not $NoPause) { Read-Host '  Enter de dong' }
    exit 0
}

# ---------- backup + ghi .env ----------
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bak = "$envFile.bak-$stamp"
Copy-Item -LiteralPath $envFile -Destination $bak -Force
Write-Host ''
Write-Host "  Da backup: $(Split-Path $bak -Leaf)" -ForegroundColor DarkGray

# PS 5.1: Get-Content mac dinh doc theo ANSI khi file khong co BOM -> mojibake toan bo
# comment UTF-8; va Set-Content -Encoding UTF8 lai them BOM. Doc/ghi tuong minh bang .NET
# UTF8 khong BOM de file khong bi nhiem ban.
$t = [IO.File]::ReadAllText($envFile, [Text.UTF8Encoding]::new($false))
if ($t -match '(?m)^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=.*$') {
    $t = [regex]::Replace($t, '(?m)^HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=.*$', 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=' + $new)
    $how = 'da cap nhat'
} else {
    $t = $t.TrimEnd("`r", "`n") + "`r`nHERMES_DASHBOARD_BASIC_AUTH_PASSWORD=$new`r`n"
    $how = 'da them moi'
}
[IO.File]::WriteAllText($envFile, $t, [Text.UTF8Encoding]::new($false))
Write-Host "  .env: $how (username va signing secret gi nguyen)" -ForegroundColor Green

# ---------- restart backend de no co hieu luc ----------
# The running dashboard read .env at process start, so it still authenticates against the OLD
# password until it is restarted. This is the step most people forget.
$running = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host '  Dang restart backend de mat khau co hieu luc...' -ForegroundColor Gray
    & $exe dashboard --stop 2>&1 | Out-Null
    Start-Sleep -Seconds 5
    $addr = if ($running[0].LocalAddress -in @('0.0.0.0', '::')) { '0.0.0.0' } else { '127.0.0.1' }
    Start-Process -FilePath $exe -WindowStyle Hidden -ArgumentList `
        'dashboard', '--host', $addr, '--port', "$port", '--no-open', '--skip-build'
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 3
        if (@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).Count) { break }
    }
    Write-Host '  Backend da song lai.' -ForegroundColor Green
} else {
    Write-Host '  Backend dang TAT -> mat khau moi se ap dung lan bat ke tiep.' -ForegroundColor Yellow
}

# ---------- verify that it really took effect ----------
Write-Host ''
Write-Host '  Verify:' -ForegroundColor Gray
try {
    $body = @{ provider = 'basic'; username = '__khong_ton_tai__'; password = $new; next = '' } | ConvertTo-Json
    $r1 = $null
    try {
        $r1 = Invoke-WebRequest "http://127.0.0.1:$port/auth/password-login" -Method Post -ContentType 'application/json' `
              -Body $body -UseBasicParsing -TimeoutSec 10
        $code1 = [int]$r1.StatusCode
    } catch { $code1 = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
    if ($code1 -eq 401) { Write-Host '    [OK] username la + mat khau moi bi tu choi  -> mat khau khong phai master key' -ForegroundColor Green }
    else { Write-Host "    [!!] username la tra HTTP $code1 (mong doi 401)" -ForegroundColor Yellow }

    $user = [regex]::Match([IO.File]::ReadAllText($envFile, [Text.UTF8Encoding]::new($false)), '(?m)^HERMES_DASHBOARD_BASIC_AUTH_USERNAME=(.*)$').Groups[1].Value
    $body2 = @{ provider = 'basic'; username = $user; password = $new; next = '' } | ConvertTo-Json
    try {
        $r2 = Invoke-WebRequest "http://127.0.0.1:$port/auth/password-login" -Method Post -ContentType 'application/json' `
              -Body $body2 -UseBasicParsing -TimeoutSec 10
        if ([int]$r2.StatusCode -eq 200) { Write-Host "    [OK] login $user + mat khau moi -> 200. Thanh cong." -ForegroundColor Green }
        else { Write-Host "    [!!] login tra HTTP $([int]$r2.StatusCode)" -ForegroundColor Yellow }
    } catch {
        $c = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        Write-Host "    [FAIL] login mat khau moi bi tu choi (HTTP $c)" -ForegroundColor Red
        Write-Host '    Kiem tra: backend da restart that chua?' -ForegroundColor Yellow
    }
    Log 'PASS-CHANGED - mat khau dashboard da doi va verify'
} finally {
    if ($new) { $new = $null }
}

Write-Host ''
Write-Host '  NHO: gui mat khau moi cho may B, mat khau CU DUNG NGAY LAP TUC.' -ForegroundColor Cyan
if (-not $NoPause) { Read-Host '  Enter de dong' }
