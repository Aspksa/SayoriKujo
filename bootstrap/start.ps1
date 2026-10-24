$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$launchClock = [System.Diagnostics.Stopwatch]::StartNew()
$stageClock = [System.Diagnostics.Stopwatch]::StartNew()
$bootStages = New-Object 'System.Collections.Generic.List[object]'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sessionId = [Guid]::NewGuid().ToString('N')
$startedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz')
$sessionDir = Join-Path $projectRoot 'data\sessions'
$logDir = Join-Path $projectRoot 'data\logs'
$recordPath = Join-Path $sessionDir ($sessionId + '.json')
$logPath = Join-Path $logDir ($sessionId + '.log')
$storageReady = $false
$currentStage = 'preparation'
$currentLabel = 'Подготовка'
$currentNumber = 1
$env:SAYORI_SESSION_ID = $sessionId

function Write-Launch([string]$Message) {
    Write-Host $Message
    if ($script:storageReady) {
        try {
            [IO.File]::AppendAllText($script:logPath, ([DateTimeOffset]::UtcNow.ToString('o') + ' [BOOT] ' + $Message + [Environment]::NewLine), $script:utf8)
        }
        catch {
            $script:storageReady = $false
            Write-Host '[Журнал] Не удалось записать лог. Запуск продолжается без сохранения.'
        }
    }
}

function Save-Boot([string]$Status = 'starting', [int]$ExitCode = 0) {
    if (-not $script:storageReady) { return }
    $temporary = $script:recordPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $record = [ordered]@{
            schema_version = 1; id = $script:sessionId; started_at = $script:startedAt
            last_seen_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz')
            status = $Status; duration_seconds = [Math]::Round($script:launchClock.Elapsed.TotalSeconds, 3)
            duration_is_final = ($Status -ne 'starting'); stages = @($script:bootStages.ToArray())
        }
        if ($Status -ne 'starting') {
            $record['exit_code'] = $ExitCode
            $record['ended_at'] = $record.last_seen_at
        }
        [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 8), $script:utf8)
        if ([IO.File]::Exists($script:recordPath)) {
            [IO.File]::Replace($temporary, $script:recordPath, [NullString]::Value)
        }
        else { [IO.File]::Move($temporary, $script:recordPath) }
    }
    catch {
        $script:storageReady = $false
        Write-Host ('[Журнал] Историю не удалось сохранить. Запуск продолжается: ' + $_.Exception.Message)
    }
    finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Complete-Stage([string]$Status = 'ok') {
    $seconds = [Math]::Round($script:stageClock.Elapsed.TotalSeconds, 4)
    $script:bootStages.Add(@{name=$script:currentStage; label=$script:currentLabel; seconds=$seconds; status=$Status})
    $result = 'готово'
    if ($Status -ne 'ok') { $result = 'ошибка' }
    Write-Launch ('[' + $script:currentNumber + '/5] ' + $script:currentLabel + ' — ' + $result + ', ' + $seconds.ToString('F3') + ' с.')
    Save-Boot
}

function Remove-OldDiagnostics {
    if (-not $script:storageReady) { return }
    try {
        $completed = 0
        $cutoff = [DateTime]::UtcNow.AddDays(-30)
        foreach ($file in (Get-ChildItem -LiteralPath $script:sessionDir -Filter '*.json' -File | Sort-Object LastWriteTimeUtc -Descending)) {
            if ($file.BaseName -notmatch '^[a-f0-9]{32}$') { continue }
            try {
                $record = [IO.File]::ReadAllText($file.FullName, $script:utf8) | ConvertFrom-Json
                if ($record.status -in @('starting', 'running')) { continue }
                $completed++
                if ($completed -le 200 -and $file.LastWriteTimeUtc -ge $cutoff) { continue }
                Remove-Item -LiteralPath $file.FullName -Force
                foreach ($suffix in @('.log', '.log.1', '.log.2')) {
                    $oldLog = Join-Path $script:logDir ($file.BaseName + $suffix)
                    if (Test-Path -LiteralPath $oldLog) { Remove-Item -LiteralPath $oldLog -Force }
                }
                $oldLock = Join-Path $script:sessionDir ($file.BaseName + '.lock')
                if (Test-Path -LiteralPath $oldLock) { Remove-Item -LiteralPath $oldLock -Force }
            }
            catch { continue }
        }
    }
    catch { Write-Host '[Журнал] Очистка старых записей пропущена.' }
}

function Finish-Launcher([int]$ExitCode) {
    if (-not $script:storageReady) { return }
    try {
        $current = [IO.File]::ReadAllText($script:recordPath, $script:utf8) | ConvertFrom-Json
        if ($current.status -notin @('starting', 'running')) { return }
        # Python normally finalizes the same record. Preserve its richer traffic data.
        $current.status = 'failed'
        if ($ExitCode -eq 0) { $current.status = 'stopped' }
        $current.duration_seconds = [Math]::Round($script:launchClock.Elapsed.TotalSeconds, 3)
        $current.last_seen_at = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz')
        $current | Add-Member -NotePropertyName ended_at -NotePropertyValue $current.last_seen_at -Force
        $current | Add-Member -NotePropertyName exit_code -NotePropertyValue $ExitCode -Force
        $current | Add-Member -NotePropertyName duration_is_final -NotePropertyValue $true -Force
        $temporary = $script:recordPath + '.finish.tmp'
        [IO.File]::WriteAllText($temporary, ($current | ConvertTo-Json -Depth 10), $script:utf8)
        [IO.File]::Replace($temporary, $script:recordPath, [NullString]::Value)
    }
    catch { Write-Host '[Журнал] Итог запуска не удалось сохранить.' }
}

try {
    try {
        [IO.Directory]::CreateDirectory($sessionDir) | Out-Null
        [IO.Directory]::CreateDirectory($logDir) | Out-Null
        $storageReady = $true
    }
    catch { Write-Host '[Журнал] Папка данных недоступна. Запуск продолжается без сохранения.' }
    Write-Launch '[1/5] Подготовка...'
    Save-Boot
    Remove-OldDiagnostics
    $serverPath = Join-Path $projectRoot 'server.py'
    foreach ($required in @('server.py', 'bootstrap\traffic.py', 'bootstrap\diagnostics.py', 'modules\brain\__init__.py', 'modules\brain\store.py', 'modules\brain\provider.py', 'modules\brain\ui.html', 'modules\brain\ui.js', 'modules\brain\ui.css')) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $required) -PathType Leaf)) {
            throw ('Не найден ' + $required + '. Сначала распакуйте весь архив проекта.')
        }
    }
    Complete-Stage
    $currentStage = 'python'; $currentLabel = 'Проверка Python'; $currentNumber = 2
    $stageClock.Restart()
    Write-Launch '[2/5] Проверка Python 3.10 или новее...'
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
            $probe.StartInfo.Arguments = (($candidate.Prefix + @('-I', '-c', '"import sys,http.server,socket,threading,urllib.request,webbrowser,ctypes,sqlite3;sys.exit(0 if sys.version_info >= (3,10) else 1)"')) -join ' ')
            $probe.StartInfo.UseShellExecute = $false
            $probe.StartInfo.CreateNoWindow = $true
            $probe.StartInfo.RedirectStandardOutput = $true
            $probe.StartInfo.RedirectStandardError = $true
            if (-not $probe.Start()) { continue }
            if (-not $probe.WaitForExit(10000)) {
                $probe.Kill()
                Write-Launch '[2/5] Кандидат Python не ответил за 10 секунд; проверяется следующий.'
                continue
            }
            if ($probe.ExitCode -eq 0) { $selected = $candidate; break }
            Write-Launch '[2/5] Кандидат Python не прошёл проверку; проверяется следующий.'
        }
        catch { Write-Launch '[2/5] Кандидат Python не запустился; проверяется следующий.' }
        finally { $probe.Dispose() }
    }
    if ($null -eq $selected) {
        throw 'Не найден рабочий Python 3.10+. Установите Python с python.org (Add python.exe to PATH), затем запустите BAT снова. Дополнительные пакеты не нужны.'
    }
    Write-Launch ('[SayoriKujo] Python: ' + $selected.Command)
    Complete-Stage
    $currentStage = 'server'; $currentLabel = 'Подготовка сервера'; $currentNumber = 3
    $stageClock.Restart()
    $env:SAYORI_BOOT_SECONDS = $launchClock.Elapsed.TotalSeconds.ToString('F4', [Globalization.CultureInfo]::InvariantCulture)
    $pythonArguments = @($selected.Prefix) + @('-I', '-X', 'utf8', '-u', $serverPath)
    & $selected.Command @pythonArguments
    $serverExit = $LASTEXITCODE
    if ($serverExit -ne 0) { Write-Launch ('[SayoriKujo] Сервер завершился с кодом ' + $serverExit + '.') }
    Finish-Launcher $serverExit
    exit $serverExit
}
catch {
    $launchError = $_
    Complete-Stage 'error'
    Write-Launch ('[SayoriKujo] Ошибка: ' + $launchError.Exception.Message)
    Save-Boot 'failed' 1
    exit 1
}
