"""iOS 27+ mirroring and live control through Apple's CoreDevice display service.

pymobiledevice3's ``serve-web`` server does the device side: the iOS 17+ tunnel, the
HEVC screen stream, HID touch / keyboard / buttons, audio and the pasteboard. It runs
in a child process (server.py, which also patches it for Windows) and this package is
its client: stream.py decodes the video with FFmpeg and plays the sound, control.py
sends input, window.py is the iPhone-shaped window with live touch.
"""
