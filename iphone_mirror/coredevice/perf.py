"""Windows tweaks that cut latency for the app and the server child.

  * 1 ms timer resolution (timeBeginPeriod): Qt and asyncio timers otherwise tick on the
    15.6 ms default, e.g. the touch-move flush and the server's RTCP/HID timers.
  * no power throttling (EcoQoS): Windows 11 slows down and ignores the timer request
    of windowless or background processes such as the server child.
  * "above normal" priority: decoding and input are scheduled first when the PC is busy.
  * tune_tunnel_socket: the TCP connection that carries the Wi-Fi tunnel sends at once
    (TCP_NODELAY) and acknowledges every segment at once (SIO_TCP_SET_ACK_FREQUENCY = 1).
    With delayed ACKs the iPhone's TCP waits for an ACK before sending the tail of a
    frame, so video arrived in pairs: one frame late, the next on time.

Everything is best effort: a failing call is logged and ignored.
"""
import ctypes
import logging
import socket
import sys

log = logging.getLogger(__name__)

ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
ProcessPowerThrottling = 4
SIO_TCP_SET_ACK_FREQUENCY = 0x98000017  # _WSAIOW(IOC_VENDOR, 23)


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def boost_process():
    if sys.platform != "win32":
        return
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    me = k32.GetCurrentProcess()
    try:
        state = _PowerThrottlingState(
            PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            PROCESS_POWER_THROTTLING_EXECUTION_SPEED | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
            0,  # 0 = never throttle
        )
        if not k32.SetProcessInformation(me, ProcessPowerThrottling, ctypes.byref(state), ctypes.sizeof(state)):
            log.debug("SetProcessInformation failed: %d", ctypes.get_last_error())
    except Exception as e:
        log.debug("power throttling: %s", e)
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception as e:
        log.debug("timeBeginPeriod: %s", e)
    try:
        k32.SetPriorityClass(me, ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception as e:
        log.debug("SetPriorityClass: %s", e)


def tune_tunnel_socket(sock):
    """Low-latency options for the TCP socket that carries the tunnel."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        log.debug("TCP_NODELAY: %s", e)
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes

        ws2 = ctypes.WinDLL("ws2_32", use_last_error=True)
        ws2.WSAIoctl.argtypes = [
            ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p,
        ]
        freq = wintypes.DWORD(1)
        done = wintypes.DWORD(0)
        rc = ws2.WSAIoctl(sock.fileno(), SIO_TCP_SET_ACK_FREQUENCY, ctypes.byref(freq), ctypes.sizeof(freq),
                          None, 0, ctypes.byref(done), None, None)
        if rc != 0:
            log.info("SIO_TCP_SET_ACK_FREQUENCY failed: %d", ws2.WSAGetLastError())
        else:
            log.info("tunnel socket: TCP_NODELAY + immediate ACKs")
    except Exception as e:
        log.info("SIO_TCP_SET_ACK_FREQUENCY: %s", e)
