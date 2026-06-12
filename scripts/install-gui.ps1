#requires -version 5.1

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$PackageName = "codexproxy"
$RepoGitUrl = "git+https://github.com/chrometechhype/codexproxy.git"
$PythonVersion = "3.14.0"
$MinUvVersion = "0.11.0"
$UvInstallUrl = "https://astral.sh/uv/install.ps1"

$script:installCancelled = $false

function Write-Log {
    param([string] $Text, [string] $Color = "Black")
    $script:outputBox.SelectionStart = $script:outputBox.TextLength
    $script:outputBox.SelectionLength = 0
    $script:outputBox.SelectionColor = $Color
    $script:outputBox.AppendText($Text + "`r`n")
    $script:outputBox.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}

function Invoke-InstallStep {
    param([scriptblock] $Block, [string] $Label)
    if ($script:installCancelled) { return $false }
    $script:progressLabel.Text = $Label
    $script:progressBar.Value = 0
    [System.Windows.Forms.Application]::DoEvents()
    try {
        & $Block
        return $true
    }
    catch {
        Write-Log "ERROR: $_" "Red"
        return $false
    }
}

function Install-CodexProxyGui {
    $script:installBtn.Enabled = $false
    $script:cancelBtn.Text = "Cancel"
    $script:installCancelled = $false
    $voiceNim = $script:voiceNimChk.Checked
    $voiceLocal = $script:voiceLocalChk.Checked
    $voiceAll = $script:voiceAllChk.Checked
    $torchBackend = $script:torchBox.Text.Trim()
    $dryRun = $script:dryRunChk.Checked

    if ($voiceAll) { $voiceNim = $true; $voiceLocal = $true }

    $specArgs = @()
    if ($voiceNim -and $voiceLocal) { $spec = "$PackageName[voice,voice_local] @ $RepoGitUrl" }
    elseif ($voiceNim) { $spec = "$PackageName[voice] @ $RepoGitUrl" }
    elseif ($voiceLocal) { $spec = "$PackageName[voice_local] @ $RepoGitUrl" }
    else { $spec = $RepoGitUrl }

    $toolArgs = @("tool", "install", "--force")
    if (-not [string]::IsNullOrWhiteSpace($torchBackend)) { $toolArgs += @("--torch-backend", $torchBackend) }
    $toolArgs += $spec

    $ok = $true

    $ok = $ok -and (Invoke-InstallStep -Block {
        Write-Log "==> Installing Codex CLI if missing"
        $script:progressBar.Value = 10
        if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
            if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm is required" }
            & npm install -g @openai/codex 2>&1 | ForEach-Object { Write-Log $_ }
            if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
        }
        else { Write-Log "Codex CLI already installed, skipping" }
    } -Label "Installing Codex CLI...")

    $ok = $ok -and (Invoke-InstallStep -Block {
        Write-Log "==> Installing/updating uv"
        $script:progressBar.Value = 30
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Invoke-RestMethod $UvInstallUrl | Invoke-Expression
            $env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;$env:Path"
        }
        else {
            $updateOk = & uv self update --dry-run 2>$null
            if ($LASTEXITCODE -eq 0) { & uv self update 2>&1 | ForEach-Object { Write-Log $_ } }
            else { Write-Log "uv already updated, skipping" }
        }
        $version = & uv --version 2>$null
        Write-Log "uv version: $version"
    } -Label "Setting up uv...")

    $ok = $ok -and (Invoke-InstallStep -Block {
        Write-Log "==> Installing Python $PythonVersion"
        $script:progressBar.Value = 50
        & uv python install $PythonVersion 2>&1 | ForEach-Object { Write-Log $_ }
        if ($LASTEXITCODE -ne 0) { throw "Python install failed" }
    } -Label "Installing Python...")

    $ok = $ok -and (Invoke-InstallStep -Block {
        Write-Log "==> Installing CodexProxy"
        $script:progressBar.Value = 70
        if ($dryRun) {
            Write-Log "[DRY-RUN] uv $($toolArgs -join ' ')"
        }
        else {
            & uv $toolArgs 2>&1 | ForEach-Object { Write-Log $_ }
            if ($LASTEXITCODE -ne 0) { throw "Install failed" }
        }
    } -Label "Installing CodexProxy...")

    $script:progressBar.Value = 100
    if ($ok -and -not $script:installCancelled) {
        Write-Log ""
        Write-Log "=== CodexProxy installed successfully! ===" "Green"
        Write-Log "  Start the proxy with: cdx-server" "Blue"
        Write-Log "  Launch Codex CLI with: cdx-codex" "Blue"
    }
    elseif ($script:installCancelled) {
        Write-Log "Installation cancelled." "Orange"
    }

    $script:installBtn.Enabled = $true
    $script:cancelBtn.Text = "Close"
}

# --- GUI Setup ---
$form = New-Object System.Windows.Forms.Form
$form.Text = "CodexProxy Installer"
$form.Size = New-Object Drawing.Size(540, 500)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.Font = New-Object Drawing.Font("Segoe UI", 9)

# Header
$header = New-Object System.Windows.Forms.Label
$header.Text = "CodexProxy Installer"
$header.Font = New-Object Drawing.Font("Segoe UI", 14, [Drawing.FontStyle]::Bold)
$header.Location = New-Object Drawing.Point(12, 12)
$header.Size = New-Object Drawing.Size(500, 28)

# Options group
$group = New-Object System.Windows.Forms.GroupBox
$group.Text = "Voice Options"
$group.Location = New-Object Drawing.Point(12, 48)
$group.Size = New-Object Drawing.Size(500, 100)

$script:voiceNimChk = New-Object System.Windows.Forms.CheckBox
$script:voiceNimChk.Text = "NVIDIA NIM voice transcription"
$script:voiceNimChk.Location = New-Object Drawing.Point(12, 22)
$script:voiceNimChk.Size = New-Object Drawing.Size(240, 20)

$script:voiceLocalChk = New-Object System.Windows.Forms.CheckBox
$script:voiceLocalChk.Text = "Local Whisper voice transcription"
$script:voiceLocalChk.Location = New-Object Drawing.Point(12, 46)
$script:voiceLocalChk.Size = New-Object Drawing.Size(240, 20)

$script:voiceAllChk = New-Object System.Windows.Forms.CheckBox
$script:voiceAllChk.Text = "All voice backends"
$script:voiceAllChk.Location = New-Object Drawing.Point(12, 70)
$script:voiceAllChk.Size = New-Object Drawing.Size(240, 20)
$script:voiceAllChk.Add_CheckedChanged({
    $checked = $script:voiceAllChk.Checked
    $script:voiceNimChk.Checked = $checked
    $script:voiceLocalChk.Checked = $checked
    $script:voiceNimChk.Enabled = -not $checked
    $script:voiceLocalChk.Enabled = -not $checked
})

$torchLabel = New-Object System.Windows.Forms.Label
$torchLabel.Text = "Torch backend:"
$torchLabel.Location = New-Object Drawing.Point(270, 24)
$torchLabel.Size = New-Object Drawing.Size(90, 20)

$script:torchBox = New-Object System.Windows.Forms.TextBox
$script:torchBox.Location = New-Object Drawing.Point(360, 22)
$script:torchBox.Size = New-Object Drawing.Size(120, 20)
$script:torchBox.PlaceholderText = "e.g. cu130"

$script:dryRunChk = New-Object System.Windows.Forms.CheckBox
$script:dryRunChk.Text = "Dry run (show commands only)"
$script:dryRunChk.Location = New-Object Drawing.Point(270, 48)
$script:dryRunChk.Size = New-Object Drawing.Size(200, 20)

$group.Controls.AddRange(@($script:voiceNimChk, $script:voiceLocalChk, $script:voiceAllChk, $torchLabel, $script:torchBox, $script:dryRunChk))

# Buttons
$script:installBtn = New-Object System.Windows.Forms.Button
$script:installBtn.Text = "Install"
$script:installBtn.Location = New-Object Drawing.Point(12, 160)
$script:installBtn.Size = New-Object Drawing.Size(100, 28)
$script:installBtn.Add_Click({ Install-CodexProxyGui })

$script:cancelBtn = New-Object System.Windows.Forms.Button
$script:cancelBtn.Text = "Close"
$script:cancelBtn.Location = New-Object Drawing.Point(120, 160)
$script:cancelBtn.Size = New-Object Drawing.Size(100, 28)
$script:cancelBtn.Add_Click({
    if ($script:installBtn.Enabled) { $form.Close() }
    else { $script:installCancelled = $true; $script:cancelBtn.Enabled = $false }
})

# Progress
$script:progressLabel = New-Object System.Windows.Forms.Label
$script:progressLabel.Text = "Ready"
$script:progressLabel.Location = New-Object Drawing.Point(12, 198)
$script:progressLabel.Size = New-Object Drawing.Size(500, 20)

$script:progressBar = New-Object System.Windows.Forms.ProgressBar
$script:progressBar.Location = New-Object Drawing.Point(12, 220)
$script:progressBar.Size = New-Object Drawing.Size(500, 22)
$script:progressBar.Minimum = 0
$script:progressBar.Maximum = 100

# Output
$script:outputBox = New-Object System.Windows.Forms.RichTextBox
$script:outputBox.Location = New-Object Drawing.Point(12, 250)
$script:outputBox.Size = New-Object Drawing.Size(500, 200)
$script:outputBox.ReadOnly = $true
$script:outputBox.BackColor = [Drawing.Color]::FromKnownColor([Drawing.KnownColor]::Window)
$script:outputBox.Font = New-Object Drawing.Font("Consolas", 9)

$form.Controls.AddRange(@($header, $group, $script:installBtn, $script:cancelBtn, $script:progressLabel, $script:progressBar, $script:outputBox))
$form.ShowDialog() | Out-Null
