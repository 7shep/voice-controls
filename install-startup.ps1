# Starts voice controls at sign-in with no window, and starts it now.
# Undo: delete the "Brain voice controls" shortcut from shell:startup and run .venv\Scripts\python voice.py --stop
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
$script = Join-Path $here "voice.py"
if (-not (Test-Path $pythonw)) { throw "No .venv yet: see Setup in README.md." }

$link = Join-Path ([Environment]::GetFolderPath("Startup")) "Brain voice controls.lnk"
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$script`""
$shortcut.WorkingDirectory = $here
$shortcut.Save()

Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $here
"Installed $link and started listening. Logs: $(Join-Path $here 'voice.log')"
