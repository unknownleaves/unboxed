# Unboxed installer for Windows.
#
#   irm https://raw.githubusercontent.com/unknownleaves/unboxed/main/install.ps1 | iex
#
# Installs the latest release for the current user (no admin needed) into
# %LOCALAPPDATA%\Programs\Unboxed, adds a Start Menu shortcut, the Explorer
# right-click menu, and an entry in Settings > Apps so it uninstalls normally.
# Running it again updates or repairs an existing install.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # makes downloads much faster in Windows PowerShell
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

    $repo  = 'unknownleaves/unboxed'
    $asset = 'Unboxed-windows-x64.zip'
    $dir   = Join-Path $env:LOCALAPPDATA 'Programs\Unboxed'
    $exe   = Join-Path $dir 'Unboxed.exe'

    function Step($msg) { Write-Host "  $msg" -ForegroundColor Gray }

    Write-Host ''
    Write-Host '  Installing Unboxed' -ForegroundColor Magenta
    Write-Host ''

    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Unboxed needs 64-bit Windows.' }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("unboxed-install-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp $asset
        if ($env:UNBOXED_ZIP) {
            # used by the automated tests
            Copy-Item $env:UNBOXED_ZIP $zip
            $version = if ($env:UNBOXED_VERSION) { $env:UNBOXED_VERSION } else { 'dev' }
        } else {
            Step 'Finding the latest version...'
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
                                     -Headers @{ 'User-Agent' = 'Unboxed-Installer' }
            $file = $rel.assets | Where-Object { $_.name -eq $asset } | Select-Object -First 1
            if (-not $file) { throw "The latest release has no Windows download yet. Try again in a few minutes." }
            $version = $rel.tag_name.TrimStart('v', 'V')
            Step "Downloading Unboxed $version ($([math]::Round($file.size / 1MB, 1)) MB)..."
            Invoke-WebRequest -Uri $file.browser_download_url -OutFile $zip -UseBasicParsing
        }

        Step 'Unpacking...'
        $stage = Join-Path $tmp 'app'
        Expand-Archive -Path $zip -DestinationPath $stage -Force
        if (-not (Test-Path (Join-Path $stage 'Unboxed.exe'))) { throw 'The download is incomplete. Please try again.' }

        # Close a running copy so its files can be replaced.
        Get-Process -Name Unboxed -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) } |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300

        Step "Installing to $dir"
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        foreach ($old in @('_internal', 'Unboxed.exe')) {
            $p = Join-Path $dir $old
            if (Test-Path $p) { Remove-Item -LiteralPath $p -Recurse -Force }
        }
        Get-ChildItem -LiteralPath $stage | Move-Item -Destination $dir -Force
        # Downloads can be flagged as "from the internet"; clear that so Windows doesn't nag.
        Get-ChildItem -LiteralPath $dir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

        Step 'Adding Start Menu shortcut...'
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Unboxed.lnk'))
        $lnk.TargetPath = $exe
        $lnk.WorkingDirectory = $dir
        $lnk.IconLocation = "$exe,0"
        $lnk.Description = 'Open and extract archives'
        $lnk.Save()
        if (-not $env:UNBOXED_NO_DESKTOP) {
            $desk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Unboxed.lnk'
            $lnk = $shell.CreateShortcut($desk)
            $lnk.TargetPath = $exe
            $lnk.WorkingDirectory = $dir
            $lnk.IconLocation = "$exe,0"
            $lnk.Save()
        }

        Step 'Adding "Extract Here" to the right-click menu...'
        Start-Process -FilePath $exe -ArgumentList '--register' -Wait

        Step 'Registering in Settings > Apps...'
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unboxed'
        New-Item -Path $key -Force | Out-Null
        $size = [int]((Get-ChildItem -LiteralPath $dir -Recurse -File | Measure-Object Length -Sum).Sum / 1KB)
        $props = @{
            DisplayName     = 'Unboxed'
            DisplayVersion  = $version
            Publisher       = 'Unboxed'
            DisplayIcon     = "$exe,0"
            InstallLocation = $dir
            UninstallString = "`"$exe`" --uninstall"
            QuietUninstallString = "`"$exe`" --uninstall --quiet"
            URLInfoAbout    = "https://github.com/$repo"
        }
        foreach ($k in $props.Keys) { Set-ItemProperty -Path $key -Name $k -Value $props[$k] }
        Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
        Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
        Set-ItemProperty -Path $key -Name EstimatedSize -Value $size -Type DWord

        Write-Host ''
        Write-Host "  Unboxed $version is installed." -ForegroundColor Green
        Write-Host '  Find it in the Start Menu, or right-click any archive > Extract Here (Unboxed).' -ForegroundColor Gray
        Write-Host '  (On Windows 11 the right-click items are under "Show more options".)' -ForegroundColor Gray
        Write-Host ''
        if (-not $env:UNBOXED_NO_LAUNCH) { Start-Process -FilePath $exe }
    }
    catch {
        Write-Host ''
        Write-Host "  Install failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host ''
        if ($env:UNBOXED_ZIP) { exit 1 }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
