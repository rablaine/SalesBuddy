$ErrorActionPreference = 'Stop'

Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class SalesBuddyInputFocusNative {
    [StructLayout(LayoutKind.Sequential)]
    public struct Rect {
        public int left, top, right, bottom;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct GuiThreadInfo {
        public uint size, flags;
        public IntPtr active, focus, capture, menuOwner, moveSize, caret;
        public Rect caretRect;
    }
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    public static extern bool IsWindowEnabled(IntPtr window);
    [DllImport("user32.dll")]
    public static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")]
    public static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr window, StringBuilder name, int count);
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetGUIThreadInfo(uint threadId, ref GuiThreadInfo info);

    public static object ReadGuiThread(uint threadId) {
        var info = new GuiThreadInfo();
        info.size = (uint)Marshal.SizeOf(info);
        bool available = threadId != 0 && GetGUIThreadInfo(threadId, ref info);
        int error = available || threadId == 0 ? 0 : Marshal.GetLastWin32Error();
        return new {
            available, error, flags = info.flags,
            activeHandle = info.active.ToInt64().ToString(),
            focusHandle = info.focus.ToInt64().ToString(),
            captureHandle = info.capture.ToInt64().ToString(),
            menuOwnerHandle = info.menuOwner.ToInt64().ToString(),
            moveSizeHandle = info.moveSize.ToInt64().ToString()
        };
    }
}
'@

function Get-SafeWindowClass([IntPtr]$Window) {
    if ($Window -eq [IntPtr]::Zero) { return 'none' }
    $name = [Text.StringBuilder]::new(256)
    if ([SalesBuddyInputFocusNative]::GetClassName($Window, $name, $name.Capacity) -eq 0) {
        return 'unavailable'
    }
    $allowed = @(
        'Chrome_WidgetWin_0', 'Chrome_WidgetWin_1', 'Chrome_RenderWidgetHostHWND',
        '#32768', '#32770', 'ComboLBox', 'tooltips_class32'
    )
    if ($allowed -ccontains $name.ToString()) { return $name.ToString() }
    return 'other'
}

while ($null -ne ($line = [Console]::ReadLine())) {
    $request = $line | ConvertFrom-Json
    $window = [IntPtr]::new([long]::Parse($request.hwnd))
    $foreground = [SalesBuddyInputFocusNative]::GetForegroundWindow()
    $foregroundPid = [uint32]0
    $foregroundThread = [SalesBuddyInputFocusNative]::GetWindowThreadProcessId(
        $foreground, [ref]$foregroundPid)
    $windowPid = [uint32]0
    $windowThread = [SalesBuddyInputFocusNative]::GetWindowThreadProcessId(
        $window, [ref]$windowPid)
    $owner = [SalesBuddyInputFocusNative]::GetWindow($window, 4)
    $result = @{
        id = $request.id
        capturedAt = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        windowHandle = $window.ToInt64().ToString()
        foregroundHandle = $foreground.ToInt64().ToString()
        ownerHandle = $owner.ToInt64().ToString()
        foregroundPid = $foregroundPid
        windowThreadId = $windowThread
        foregroundThreadId = $foregroundThread
        foregroundClass = Get-SafeWindowClass $foreground
        foregroundOwnerHandle = (
            [SalesBuddyInputFocusNative]::GetWindow($foreground, 4)).ToInt64().ToString()
        foregroundRootOwnerHandle = (
            [SalesBuddyInputFocusNative]::GetAncestor($foreground, 3)).ToInt64().ToString()
        windowGui = [SalesBuddyInputFocusNative]::ReadGuiThread($windowThread)
        foregroundGui = [SalesBuddyInputFocusNative]::ReadGuiThread($foregroundThread)
        enabled = [SalesBuddyInputFocusNative]::IsWindowEnabled($window)
        foregroundIsSelf = ($foreground -eq $window)
    }
    [Console]::Out.WriteLine(($result | ConvertTo-Json -Compress -Depth 4))
    [Console]::Out.Flush()
}
