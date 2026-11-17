#!/usr/bin/env pwsh
#Requires -Version 5.1
<#
Purpose: keep a private Magpie SSH tunnel with single-line traffic statistics.
Dependencies: Windows PowerShell 5.1 or PowerShell 7, Windows OpenSSH Client.
Effects: counts forwarded bytes on loopback, retries SSH; optional CLI settings update.
No service, scheduled task, password storage, or subscription-token copying.
#>
[CmdletBinding()]
param(
    [string]$SshHost = '',
    [string]$SshUser = '',
    [ValidateRange(1, 65535)][int]$SshPort = 22,
    [ValidateRange(1, 65535)][int]$LocalPort = 13425,
    [ValidateRange(1, 65535)][int]$RemotePort = 3425,
    [string]$ConfigPath,
    [string]$IdentityFile,
    [switch]$ConfigureClaude
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$sshProcess = $null
$lockStream = $null
$sshReaders = @()
$sshStarted = $false
$backendReady = $false
$trafficRelay = $null
$trafficEnabled = $false
$trafficLine = ''
$trafficLineLength = 0
$trafficLastUp = 0L
$trafficLastDown = 0L
$trafficLastSeconds = 0.0
$trafficClock = [System.Diagnostics.Stopwatch]::StartNew()

function ConvertTo-NativeArgument([string]$Value) {
    # ProcessStartInfo.Arguments uses Windows quoting, including in PowerShell 5.1.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Format-TrafficSize([double]$Bytes) {
    $units = @('B', 'KiB', 'MiB', 'GiB', 'TiB')
    $unit = 0
    while ($Bytes -ge 1024 -and $unit -lt $units.Count - 1) { $Bytes /= 1024; $unit++ }
    if ($unit -eq 0) { return ('{0:0} {1}' -f $Bytes, $units[$unit]) }
    return ('{0:0.0} {1}' -f $Bytes, $units[$unit])
}

function Clear-TrafficLine {
    if (-not $script:trafficEnabled -or $script:trafficLineLength -eq 0) { return }
    try {
        $width = [Math]::Max(0, [Console]::WindowWidth - 1)
        [Console]::Write("`r" + (' ' * [Math]::Min($script:trafficLineLength, $width)) + "`r")
    } catch { $script:trafficEnabled = $false }
    $script:trafficLineLength = 0
}

function Update-Traffic([switch]$Force) {
    if (-not $script:trafficEnabled -or $null -eq $script:trafficRelay) { return }
    $seconds = $script:trafficClock.Elapsed.TotalSeconds
    $elapsed = $seconds - $script:trafficLastSeconds
    if ($elapsed -ge 0.5 -or -not $script:trafficLine) {
        $up = $script:trafficRelay.Uploaded
        $down = $script:trafficRelay.Downloaded
        $upDelta = $up - $script:trafficLastUp
        $downDelta = $down - $script:trafficLastDown
        $interval = [Math]::Max(0.001, $elapsed)
        $upArrow = '-'; $downArrow = '-'
        if ($upDelta -gt 0) { $upArrow = [char]0x2191 }
        if ($downDelta -gt 0) { $downArrow = [char]0x2193 }
        $script:trafficLine = 'UP {0} {1}/s ({2})  DN {3} {4}/s ({5})' -f `
            $upArrow, (Format-TrafficSize ($upDelta / $interval)), (Format-TrafficSize $up), `
            $downArrow, (Format-TrafficSize ($downDelta / $interval)), (Format-TrafficSize $down)
        $script:trafficLastUp = $up
        $script:trafficLastDown = $down
        $script:trafficLastSeconds = $seconds
    } elseif (-not $Force) { return }
    Clear-TrafficLine
    if (-not $script:trafficEnabled) { return }
    try {
        $width = [Math]::Max(0, [Console]::WindowWidth - 1)
        $length = [Math]::Min($script:trafficLine.Length, $width)
        if ($length -gt 0) { [Console]::Write("`r" + $script:trafficLine.Substring(0, $length)) }
        $script:trafficLineLength = $length
    } catch { $script:trafficEnabled = $false }
}

function Wait-TrafficDelay([int]$Milliseconds) {
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($timer.ElapsedMilliseconds -lt $Milliseconds) {
        Update-Traffic
        $remaining = $Milliseconds - $timer.ElapsedMilliseconds
        if ($remaining -gt 0) { Start-Sleep -Milliseconds ([Math]::Min(100, $remaining)) }
    }
}

function Write-Log([string]$Message) {
    Clear-TrafficLine
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'), $Message)
    Update-Traffic -Force
}

function Initialize-TrafficRelay {
    if ('MagpieTrafficRelay' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;

// Counts only successfully copied application bytes. No payloads are retained or logged.
public sealed class MagpieTrafficRelay : IDisposable
{
    [DllImport("iphlpapi.dll", SetLastError = true)]
    private static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool order, int family, int tableClass, uint reserved);

    public static bool OwnsListener(int port, int processId)
    {
        int size = 0;
        uint error = GetExtendedTcpTable(IntPtr.Zero, ref size, false, 2, 3, 0);
        if (error != 122 && error != 0) throw new Win32Exception((int)error);
        for (int attempt = 0; attempt < 3; attempt++)
        {
            IntPtr table = Marshal.AllocHGlobal(size);
            try
            {
                error = GetExtendedTcpTable(table, ref size, false, 2, 3, 0);
                if (error == 122) continue;
                if (error != 0) throw new Win32Exception((int)error);
                int count = Marshal.ReadInt32(table);
                for (int index = 0; index < count; index++)
                {
                    int offset = 4 + index * 24;
                    uint address = unchecked((uint)Marshal.ReadInt32(table, offset + 4));
                    int encodedPort = Marshal.ReadInt32(table, offset + 8);
                    int localPort = ((encodedPort & 255) << 8) | ((encodedPort >> 8) & 255);
                    int owner = Marshal.ReadInt32(table, offset + 20);
                    if (owner == processId && localPort == port &&
                        new IPAddress(BitConverter.GetBytes(address)).Equals(IPAddress.Loopback)) return true;
                }
                return false;
            }
            finally { Marshal.FreeHGlobal(table); }
        }
        return false;
    }

    private sealed class Session
    {
        public readonly TcpClient Front;
        public readonly TcpClient Back = new TcpClient(AddressFamily.InterNetwork);
        public Task Work;
        private int closed;
        public Session(TcpClient front) { Front = front; }
        public void Close()
        {
            if (Interlocked.Exchange(ref closed, 1) != 0) return;
            try { Front.Close(); } catch (SocketException) { }
            try { Back.Close(); } catch (SocketException) { }
        }
    }

    private readonly object gate = new object();
    private readonly TcpListener listener;
    private readonly HashSet<Session> sessions = new HashSet<Session>();
    private Task accepting;
    private bool stopped;
    private int backendPort;
    private int backendProcessId;
    private long generation;
    private long uploaded;
    private long downloaded;

    public MagpieTrafficRelay(int localPort)
    {
        listener = new TcpListener(IPAddress.Loopback, localPort);
    }
    public int Port { get { return ((IPEndPoint)listener.LocalEndpoint).Port; } }
    public long Uploaded { get { return Interlocked.Read(ref uploaded); } }
    public long Downloaded { get { return Interlocked.Read(ref downloaded); } }

    public static int ChooseBackendPort()
    {
        TcpListener temporary = new TcpListener(IPAddress.Loopback, 0);
        try { temporary.Start(); return ((IPEndPoint)temporary.LocalEndpoint).Port; }
        finally { temporary.Stop(); }
    }

    public void Start()
    {
        try { listener.Start(); }
        catch (SocketException) { listener.Stop(); throw; }
        accepting = AcceptAsync();
    }

    public void SetBackend(int port)
    {
        SetBackend(port, 0);
    }

    public void SetBackend(int port, int processId)
    {
        if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException("port");
        lock (gate)
        {
            if (stopped) throw new ObjectDisposedException("MagpieTrafficRelay");
            generation++;
            backendPort = port;
            backendProcessId = processId;
        }
    }

    public void DisableBackend()
    {
        lock (gate) { backendPort = 0; backendProcessId = 0; generation++; }
    }

    private async Task AcceptAsync()
    {
        try
        {
            while (true)
            {
                TcpClient front = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                lock (gate)
                {
                    if (stopped || backendPort == 0) { front.Close(); continue; }
                    Session session = new Session(front);
                    sessions.Add(session);
                    int port = backendPort;
                    long version = generation;
                    int owner = backendProcessId;
                    session.Work = Task.Run(() => ForwardAsync(session, port, version, owner));
                }
            }
        }
        catch (ObjectDisposedException) { }
        catch (SocketException) { }
    }

    private static bool OwnerAvailable(int port, int processId)
    {
        return processId == 0 || OwnsListener(port, processId);
    }

    private async Task ForwardAsync(Session session, int port, long version, int owner)
    {
        try
        {
            if (!OwnerAvailable(port, owner)) return;
            lock (gate) { if (stopped || generation != version) return; }
            session.Front.NoDelay = true;
            session.Back.NoDelay = true;
            await session.Back.ConnectAsync(IPAddress.Loopback, port).ConfigureAwait(false);
            if (!OwnerAvailable(port, owner)) return;
            lock (gate) { if (stopped || generation != version) return; }
            NetworkStream frontStream = session.Front.GetStream();
            NetworkStream backStream = session.Back.GetStream();
            Socket frontSocket = session.Front.Client;
            Socket backSocket = session.Back.Client;
            Task up = CopyAsync(session, frontStream, backStream, backSocket, true);
            Task down = CopyAsync(session, backStream, frontStream, frontSocket, false);
            await Task.WhenAll(up, down).ConfigureAwait(false);
        }
        catch (IOException) { }
        catch (SocketException) { }
        catch (Win32Exception) { }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
        finally
        {
            session.Close();
            lock (gate) { sessions.Remove(session); }
        }
    }

    private async Task CopyAsync(Session session, NetworkStream input, NetworkStream output, Socket destination, bool upload)
    {
        byte[] buffer = new byte[65536];
        try
        {
            int count;
            while ((count = await input.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) != 0)
            {
                await output.WriteAsync(buffer, 0, count).ConfigureAwait(false);
                if (upload) Interlocked.Add(ref uploaded, count);
                else Interlocked.Add(ref downloaded, count);
            }
            // EOF closes this sending direction only; the response may still be streaming.
            try { destination.Shutdown(SocketShutdown.Send); }
            catch (SocketException) { }
            catch (ObjectDisposedException) { }
        }
        catch (IOException) { session.Close(); }
        catch (SocketException) { session.Close(); }
        catch (ObjectDisposedException) { session.Close(); }
    }

    public void Stop()
    {
        Session[] current;
        lock (gate)
        {
            if (stopped) return;
            stopped = true;
            backendPort = 0;
            backendProcessId = 0;
            generation++;
            current = new Session[sessions.Count];
            sessions.CopyTo(current);
        }
        listener.Stop();
        foreach (Session session in current) session.Close();
        List<Task> pending = new List<Task>();
        if (accepting != null) pending.Add(accepting);
        foreach (Session session in current) if (session.Work != null) pending.Add(session.Work);
        try { Task.WaitAll(pending.ToArray(), 1000); }
        catch (AggregateException) { }
    }

    public void Dispose() { Stop(); }
}
'@
}

function Test-SshListener([int]$Port, [int]$ProcessId) {
    return [MagpieTrafficRelay]::OwnsListener($Port, $ProcessId)
}

function Update-SshBackend {
    if ($script:backendReady -or -not $script:sshStarted -or $script:sshProcess.HasExited) { return }
    if (Test-SshListener $script:internalPort $script:sshProcess.Id) {
        $script:trafficRelay.SetBackend($script:internalPort, $script:sshProcess.Id)
        $script:backendReady = $true
        Write-Log 'SSH tunnel ready.'
    }
}

function Read-SshLogs {
    foreach ($entry in $script:sshReaders) {
        while ($null -ne $entry.Pending -and $entry.Pending.IsCompleted) {
            $line = $entry.Pending.GetAwaiter().GetResult()
            if ($null -eq $line) { $entry.Pending = $null; break }
            Write-Log $line
            $entry.Pending = $entry.Reader.ReadLineAsync()
        }
    }
}

function Assert-FreePort {
    # Query LISTEN sockets: a bind probe can mistake TIME_WAIT for a live listener.
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    foreach ($endpoint in $listeners) {
        $address = $endpoint.Address
        if ($address.IsIPv4MappedToIPv6) { $address = $address.MapToIPv4() }
        if ($endpoint.Port -eq $LocalPort -and
            ($address.Equals([System.Net.IPAddress]::Loopback) -or
             $address.Equals([System.Net.IPAddress]::Any))) {
            throw "Local port $LocalPort is occupied. No existing process was stopped."
        }
    }
    # IPv6 dual-stack conflicts and races are handled by SSH's bind-and-retry loop.
}

function Test-SshForkOption([string]$SshPath) {
    # Old Windows OpenSSH lacks this option. -G with NUL reads no user config or network.
    $probe = [System.Diagnostics.Process]::new()
    $probe.StartInfo.FileName = $SshPath
    $probe.StartInfo.Arguments = '-G -F NUL -o ForkAfterAuthentication=no probe.invalid'
    $probe.StartInfo.UseShellExecute = $false
    $probe.StartInfo.CreateNoWindow = $true
    $probe.StartInfo.RedirectStandardOutput = $true
    $probe.StartInfo.RedirectStandardError = $true
    $started = $false
    try {
        $started = $probe.Start()
        $stdout = $probe.StandardOutput.ReadToEndAsync()
        $stderr = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit(5000)) { throw 'SSH option check timed out.' }
        $null = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        return ($probe.ExitCode -eq 0)
    }
    finally {
        try {
            if ($started -and -not $probe.HasExited) { $probe.Kill(); $probe.WaitForExit() }
        } finally { $probe.Dispose() }
    }
}

function Set-Property($Object, [string]$Name, $Value) {
    $Object | Add-Member -MemberType NoteProperty -Name $Name -Value $Value -Force
}

function Get-Catalog {
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$LocalPort/v1/models")
    $request.Proxy = $null
    $request.Timeout = 3000
    $request.ReadWriteTimeout = 3000
    $response = $request.GetResponse()
    try {
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
        finally { $reader.Dispose() }
    }
    finally { $response.Close() }
}

function Update-ClaudeSettings($Catalog) {
    $models = @($Catalog.data | Where-Object { $_.id -like 'claude/*' })
    $families = @('OPUS', 'SONNET', 'HAIKU', 'FABLE')
    $primary = @(
        foreach ($family in $families) {
            $candidates = @(
                foreach ($model in $models) {
                    if ($model.id -cmatch "^claude/claude-$($family.ToLower())-([0-9]+(?:-[0-9]+)*)(?:-latest)?$") {
                        $parts = @($Matches[1].Split('-'))
                        $date = 0L
                        if ($parts[-1].Length -eq 8) {
                            $date = [long]$parts[-1]
                            $parts = @($parts | Select-Object -SkipLast 1)
                        }
                        $version = ($parts | ForEach-Object { ([long]$_).ToString('D20') }) -join '-'
                        [pscustomobject]@{ Id = $model.id; Version = $version; Undated = ($date -eq 0); Date = $date }
                    }
                }
            )
            if ($candidates.Count -eq 0) { throw "No versioned $family model in catalog. Settings were not changed." }
            ($candidates | Sort-Object Version, Undated, Date, Id -Descending | Select-Object -First 1).Id
        }
    )
    $directory = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.claude'
    $path = Join-Path $directory 'settings.json'
    $original = $null
    if (Test-Path -LiteralPath $path) {
        $original = [System.IO.File]::ReadAllText($path)
        $settings = $original | ConvertFrom-Json
    } else { $settings = [pscustomobject]@{} }
    if ($null -eq $settings -or $settings -isnot [pscustomobject]) {
        throw 'Claude settings must contain a JSON object.'
    }
    if ($null -eq $settings.PSObject.Properties['env']) {
        Set-Property $settings 'env' ([pscustomobject]@{})
    }
    if ($settings.env -isnot [pscustomobject]) { throw 'Claude settings.env must be an object.' }
    Set-Property $settings.env 'ANTHROPIC_BASE_URL' "http://127.0.0.1:$LocalPort"
    Set-Property $settings.env 'ANTHROPIC_AUTH_TOKEN' 'magpie'
    $families = @('OPUS', 'SONNET', 'HAIKU', 'FABLE')
    for ($i = 0; $i -lt $families.Count; $i++) {
        Set-Property $settings.env "ANTHROPIC_DEFAULT_$($families[$i])_MODEL" $primary[$i]
    }
    foreach ($name in @('ANTHROPIC_SMALL_FAST_MODEL', 'CLAUDE_CODE_MODEL_CAPABILITIES')) {
        $settings.env.PSObject.Properties.Remove($name)
    }
    Set-Property $settings 'model' $primary[0]
    $ordered = @($models | Sort-Object @{ Expression = {
        $rank = [Array]::IndexOf($primary, $_.id)
        if ($rank -lt 0) { 100 } else { $rank }
    } }, id)
    foreach ($name in @('modelOverrides', 'modelPicker')) {
        if ($null -eq $settings.PSObject.Properties[$name]) {
            Set-Property $settings $name ([pscustomobject]@{})
        }
        if ($settings.$name -isnot [pscustomobject]) { throw "Claude settings.$name must be an object." }
    }
    $options = @($ordered | ForEach-Object {
        Set-Property $settings.modelOverrides $_.id.Substring(7) $_.id
        $label = $_.id
        if ($null -ne $_.PSObject.Properties['display_name']) { $label = $_.display_name }
        [ordered]@{ model = $_.id; label = $label; description = 'UK Magpie via SSH' }
    })
    Set-Property $settings.modelPicker 'options' $options
    $serialized = $settings | ConvertTo-Json -Depth 100
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    if ($null -ne $original -and [System.IO.File]::ReadAllText($path) -cne $original) {
        throw 'Settings changed concurrently; stopped without replacing them.'
    }
    if ($null -eq $original -and (Test-Path -LiteralPath $path)) {
        throw 'Settings were created concurrently; stopped without replacing them.'
    }
    $temporary = Join-Path $directory ([System.IO.Path]::GetRandomFileName())
    try {
        [System.IO.File]::WriteAllText($temporary, $serialized, [System.Text.UTF8Encoding]::new($false))
        if ($null -ne $original) {
            $backup = "$path.before-magpie-$([Guid]::NewGuid().ToString('N')).bak"
            [System.IO.File]::Replace($temporary, $path, $backup)
            Write-Log "Settings backup: $backup (may contain private configuration; do not share)."
        } else { [System.IO.File]::Move($temporary, $path) }
    }
    finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
    Write-Log "CLI settings updated: $path ($($models.Count) Claude models)."
    Write-Log 'Claude Desktop uses separate third-party inference settings.'
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'Use magpie_tunnel.py on Linux; this script requires Windows.' }
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $identityArgs = @()
    if ($PSBoundParameters.ContainsKey('IdentityFile')) {
        if ([string]::IsNullOrWhiteSpace($IdentityFile)) { throw 'IdentityFile cannot be empty.' }
        $key = Get-Item -LiteralPath $IdentityFile -ErrorAction Stop
        if ($key -isnot [System.IO.FileInfo] -or $key.Extension -ieq '.pub') {
            throw 'IdentityFile must be a readable private key file, not a .pub file.'
        }
        $keyHandle = [System.IO.File]::OpenRead($key.FullName)
        $keyHandle.Dispose()
        $identityArgs = @('-i', $key.FullName, '-o', 'IdentitiesOnly=yes')
    }
    if (-not $ConfigPath) {
        $besideScript = Join-Path $PSScriptRoot 'connection.json'
        if (Test-Path -LiteralPath $besideScript) { $ConfigPath = $besideScript }
    }
    if ($ConfigPath) {
        $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        if ($null -eq $config -or $config -isnot [pscustomobject]) {
            throw 'Connection configuration must contain a JSON object.'
        }
        foreach ($mapping in @(@('SshHost', 'host'), @('SshUser', 'user'),
            @('SshPort', 'ssh_port'), @('LocalPort', 'local_port'), @('RemotePort', 'remote_port'))) {
            if (-not $PSBoundParameters.ContainsKey($mapping[0])) {
                $property = $config.PSObject.Properties[$mapping[1]]
                if ($null -ne $property) { Set-Variable -Name $mapping[0] -Value $property.Value }
            }
        }
    }
    if (-not $SshHost) { $SshHost = Read-Host 'SSH host' }
    if (-not $SshUser) { $SshUser = Read-Host 'SSH user' }
    if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._:-]*$' -or
        $SshUser -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$') {
        throw 'Invalid SSH host or user.'
    }
    $lockPath = Join-Path ([System.IO.Path]::GetTempPath()) "claude-magpie-$LocalPort.lock"
    try {
        $lockStream = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    } catch { throw 'Another copy of this tunnel is running, or its lock cannot be opened.' }
    Assert-FreePort
    $sshLifecycleArgs = @('-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ControlPersist=no')
    if (Test-SshForkOption $ssh) { $sshLifecycleArgs += @('-o', 'ForkAfterAuthentication=no') }
    Initialize-TrafficRelay
    $trafficRelay = [MagpieTrafficRelay]::new($LocalPort)
    $waitingForPort = $false
    while ($true) {
        try { $trafficRelay.Start(); break }
        catch {
            $socketError = $_.Exception
            while ($null -ne $socketError.InnerException) { $socketError = $socketError.InnerException }
            if ($socketError -isnot [System.Net.Sockets.SocketException] -or
                $socketError.SocketErrorCode -ne [System.Net.Sockets.SocketError]::AddressAlreadyInUse) { throw }
            Assert-FreePort
            if (-not $waitingForPort) {
                Write-Log "Local port $LocalPort has no IPv4 listener but is not yet reusable; waiting. Ctrl+C to stop."
                $waitingForPort = $true
            }
            Wait-TrafficDelay 1000
        }
    }
    # This chooses a port, not a reservation: SSH still checks the actual bind.
    $internalPort = [MagpieTrafficRelay]::ChooseBackendPort()
    try {
        $trafficEnabled = $Host.Name -eq 'ConsoleHost' -and -not [Console]::IsOutputRedirected -and -not [Console]::IsErrorRedirected
    } catch { $trafficEnabled = $false }
    Write-Log "Forwarding localhost:$LocalPort to remote Magpie. Keep this terminal open."
    Write-Log 'Requires a working SSH key and a verified known_hosts entry on this Windows machine.'
    Write-Log 'Run claude in another terminal. Press Ctrl+C to stop.'
    $configurePending = $ConfigureClaude.IsPresent
    while ($true) {
        # SSH reports temporary bind failures without terminating this retry loop.
        $backendReady = $false
        $sshArgs = @('-NT', '-p', "$SshPort") + $identityArgs + $sshLifecycleArgs + @('-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3', '-L',
            "127.0.0.1:${internalPort}:127.0.0.1:${RemotePort}", "${SshUser}@${SshHost}")
        $sshProcess = [System.Diagnostics.Process]::new()
        $sshProcess.StartInfo.FileName = $ssh
        $sshProcess.StartInfo.Arguments = ($sshArgs | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '
        $sshProcess.StartInfo.UseShellExecute = $false
        $sshProcess.StartInfo.CreateNoWindow = $true
        $sshProcess.StartInfo.RedirectStandardOutput = $true
        $sshProcess.StartInfo.RedirectStandardError = $true
        $sshStarted = $sshProcess.Start()
        $script:sshReaders = @($sshProcess.StandardOutput, $sshProcess.StandardError) | ForEach-Object {
            @{ Reader = $_; Pending = $_.ReadLineAsync() }
        }
        if ($configurePending) {
            $catalog = $null
            for ($attempt = 0; $attempt -lt 15 -and -not $sshProcess.HasExited; $attempt++) {
                Read-SshLogs
                Update-SshBackend
                Update-Traffic
                if ($backendReady) {
                    try { $catalog = Get-Catalog; break }
                    catch { }
                }
                Wait-TrafficDelay 1000
            }
            if ($null -ne $catalog) {
                Update-ClaudeSettings $catalog
                $configurePending = $false
            } else { Write-Log 'WARNING: Gateway not ready; CLI settings have not been changed.' }
        }
        # Poll rather than block in WaitForExit so PowerShell can process Ctrl+C.
        while (-not $sshProcess.HasExited) { Read-SshLogs; Update-SshBackend; Update-Traffic; Start-Sleep -Milliseconds 100 }
        # Existing sockets may still contain response bytes; let both copy loops drain.
        $trafficRelay.DisableBackend()
        $backendReady = $false
        while (@($script:sshReaders | Where-Object { $null -ne $_.Pending }).Count -gt 0) {
            Read-SshLogs
            Update-Traffic
            Start-Sleep -Milliseconds 20
        }
        $exitCode = $sshProcess.ExitCode
        $sshProcess.Dispose()
        $sshProcess = $null
        $sshStarted = $false
        Write-Log "SSH exited ($exitCode); reconnecting in 5 seconds. Ctrl+C to stop."
        Wait-TrafficDelay 5000
    }
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    throw
}
finally {
    Clear-TrafficLine
    $trafficEnabled = $false
    if ($null -ne $trafficRelay) {
        $trafficRelay.Stop()
        Write-Log ('Traffic total: UP {0}; DN {1}.' -f (Format-TrafficSize $trafficRelay.Uploaded), (Format-TrafficSize $trafficRelay.Downloaded))
    }
    if ($null -ne $sshProcess) {
        try {
            if ($sshStarted -and -not $sshProcess.HasExited) { $sshProcess.Kill(); $sshProcess.WaitForExit() }
        } finally { $sshProcess.Dispose() }
    }
    if ($null -ne $lockStream) { $lockStream.Dispose() }
}
