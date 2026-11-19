"""Contain each worker command and its descendants on Linux and Windows."""

from contextlib import contextmanager
import os
import signal
import subprocess
import sys


class ProcessLaunchError(OSError):
    """The worker could not start its subprocess."""


def _spawn(cmd, **kwargs):
    try:
        return subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise ProcessLaunchError(str(exc)) from exc


# Wait for assignment to the Windows job before starting the actual command.
_WINDOWS_LAUNCHER = """
import subprocess
import sys

if sys.stdin.buffer.read(1) != b'1':
    sys.exit(1)
try:
    sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
except OSError as exc:
    print(f"Could not launch command: {exc}", file=sys.stderr, flush=True)
    sys.exit(1)
"""


@contextmanager
def _windows_job(cmd, kwargs):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL

    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    child = None
    assigned = False
    try:
        child = _spawn([sys.executable, "-c", _WINDOWS_LAUNCHER, *cmd], stdin=subprocess.PIPE, **kwargs)
        process_set_quota = 0x0100
        process_terminate = 0x0001
        handle = kernel.OpenProcess(process_set_quota | process_terminate, False, child.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            assigned = bool(kernel.AssignProcessToJobObject(job, handle))
            if not assigned:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
        with child.stdin as gate:
            gate.write(b"1")
        yield child
    finally:
        try:
            if assigned:
                if not kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            elif child is not None and child.poll() is None:
                child.kill()
            if child is not None:
                if child.stdin is not None and not child.stdin.closed:
                    child.stdin.close()
                child.wait(timeout=5)
        finally:
            kernel.CloseHandle(job)


@contextmanager
def job_process(cmd, **kwargs):
    """Reap the launcher and stop remaining job-owned processes on exit."""
    if os.name == "nt":
        with _windows_job(cmd, kwargs) as child:
            yield child
        return

    child = _spawn(cmd, stdin=subprocess.DEVNULL, start_new_session=True, **kwargs)
    try:
        yield child
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            child.wait(timeout=5)
