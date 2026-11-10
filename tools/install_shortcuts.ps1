# Creates the "iPhone Mirror Plus" shortcuts (Desktop + Start menu) with the app icon and
# the AppUserModelID the app sets on itself (iphone_mirror/coredevice/__main__.py), so the
# taskbar shows "iPhone Mirror Plus" with its own icon instead of Python, and a pinned
# taskbar button starts the app again. Run again after moving the folder:
#   powershell -ExecutionPolicy Bypass -File tools\install_shortcuts.ps1
param(
    [string]$AppDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Venv = (Join-Path $env:LOCALAPPDATA 'iPhoneMirror\venv')
)
$ErrorActionPreference = 'Stop'
$AppId = 'iPhoneMirrorPlus.App'
$SourceIcon = Join-Path $AppDir 'iphone_mirror\coredevice\app.ico'
# Windows caches shortcut icons by file path: a changed app.ico would keep the old picture.
# Point the shortcuts at a copy named after the icon's content hash instead.
$hash = (Get-FileHash $SourceIcon -Algorithm SHA256).Hash.Substring(0, 10).ToLower()
$IconDir = Join-Path $env:LOCALAPPDATA 'iPhoneMirror\icons'
New-Item -ItemType Directory -Force $IconDir | Out-Null
Get-ChildItem $IconDir -Filter 'app-*.ico' | Where-Object { $_.Name -ne "app-$hash.ico" } | Remove-Item -Force -ErrorAction SilentlyContinue
$Icon = Join-Path $IconDir "app-$hash.ico"
Copy-Item $SourceIcon $Icon -Force
$Target = Join-Path $Venv 'Scripts\pythonw.exe'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class LnkAppId {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PROPERTYKEY { public Guid fmtid; public uint pid; }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    struct PROPVARIANT { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        int GetCount(out uint c);
        int GetAt(uint i, out PROPERTYKEY k);
        int GetValue(ref PROPERTYKEY k, out PROPVARIANT v);
        int SetValue(ref PROPERTYKEY k, ref PROPVARIANT v);
        int Commit();
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bc, int flags, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    public static void Set(string lnk, string appId) {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out store);
        PROPERTYKEY key = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        PROPVARIANT v = new PROPVARIANT { vt = 31 /* VT_LPWSTR */, p = Marshal.StringToCoTaskMemUni(appId) };
        try {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref v));
            Marshal.ThrowExceptionForHR(store.Commit());
        } finally {
            Marshal.FreeCoTaskMem(v.p);
            Marshal.ReleaseComObject(store);
        }
    }
}
'@

$ws = New-Object -ComObject WScript.Shell
$dirs = @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
foreach ($dir in $dirs) {
    $path = Join-Path $dir 'iPhone Mirror Plus.lnk'
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $Target
    $s.Arguments = '-m iphone_mirror.coredevice'
    $s.WorkingDirectory = $AppDir
    $s.IconLocation = "$Icon,0"
    $s.Description = 'iPhone tren may tinh: hinh, tieng, cham truc tiep, clipboard, gia lap GPS'
    $s.Save()
    [LnkAppId]::Set($path, $AppId)
    Write-Output "ok: $path"
}
