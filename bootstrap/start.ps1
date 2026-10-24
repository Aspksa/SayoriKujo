$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $serverPath = Join-Path $projectRoot 'server.py'
    if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
        throw 'Не найден server.py. Сначала распакуйте весь архив проекта.'
    }

    Write-Host '[SayoriKujo] Проверка Python 3.10 или новее...'
    $candidates = @(
        @{ Command = (Join-Path $projectRoot 'runtime\python.exe'); Prefix = @() },
        @{ Command = (Join-Path $projectRoot '.venv\Scripts\python.exe'); Prefix = @() }
    )
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $found = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $found) {
            $prefix = @()
            if ($name -eq 'py.exe') { $prefix = @('-3') }
            $candidates += @{ Command = $found.Source; Prefix = $prefix }
        }
    }

    $selected = $null
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Command -PathType Leaf)) { continue }
        $probe = New-Object System.Diagnostics.Process
        try {
            $probe.StartInfo.FileName = $candidate.Command
            $probe.StartInfo.Arguments = (($candidate.Prefix + @('-I', '-c', '"import sys,http.server,socket,threading,urllib.request,webbrowser;sys.exit(0 if sys.version_info >= (3,10) else 1)"')) -join ' ')
            $probe.StartInfo.UseShellExecute = $false
            $probe.StartInfo.CreateNoWindow = $true
            $probe.StartInfo.RedirectStandardOutput = $true
            $probe.StartInfo.RedirectStandardError = $true
            if (-not $probe.Start()) { continue }
            if (-not $probe.WaitForExit(10000)) {
                $probe.Kill()
                continue
            }
            if ($probe.ExitCode -eq 0) { $selected = $candidate; break }
        }
        catch { continue }
        finally { $probe.Dispose() }
    }

    if ($null -eq $selected) {
        throw 'Не найден рабочий Python 3.10+. Установите Python с python.org (Add python.exe to PATH), затем запустите BAT снова. Дополнительные пакеты не нужны.'
    }

    Write-Host ('[SayoriKujo] Python: ' + $selected.Command)
    $pythonArguments = @($selected.Prefix) + @('-I', '-X', 'utf8', '-u', $serverPath)
    & $selected.Command @pythonArguments
    exit $LASTEXITCODE
}
catch {
    Write-Host ('[SayoriKujo] Ошибка: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
