"""Strict child ownership, matching Codex process-group / Windows Job Object semantics."""

import ctypes
import os
import signal
import subprocess
from threading import RLock
from time import monotonic, sleep

from agent.runtime.errors import ExecutionTerminated


# 管理隔离的子进程及其后代；close 会终止并等待进程退出。
class OwnedProcess:
    def __init__(self, command, *, cwd):
        self.lock, self.closed, self.job = RLock(), False, None
        self.process = None
        try:
            if os.name == "nt":
                import win32job

                self.job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
                flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                import win32api
                import win32con

                # 复制已创建进程的句柄并加入 Job，避免使用可能被复用的 PID 查找进程。
                current = win32api.GetCurrentProcess()
                handle = win32api.DuplicateHandle(
                    current, int(self.process._handle), current, 0, False, win32con.DUPLICATE_SAME_ACCESS
                )
                try:
                    win32job.AssignProcessToJobObject(self.job, handle)
                    resume = ctypes.WinDLL("ntdll").NtResumeProcess
                    resume.argtypes, resume.restype = [ctypes.c_void_p], ctypes.c_long
                    if resume(int(handle)) < 0:
                        raise OSError("Could not resume the contained worker")
                finally:
                    handle.Close()
            elif os.name == "posix":
                self.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                raise OSError("Unsupported process containment platform")
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.closed:
                return
            process = self.process
            try:
                if self.job is not None:
                    import win32job

                    win32job.TerminateJobObject(self.job, 1)
                elif process is not None and os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                if self.job is not None:
                    deadline = monotonic() + 5
                    while win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)[
                        "ActiveProcesses"
                    ]:
                        if monotonic() >= deadline:
                            raise TimeoutError("Contained descendants did not exit")
                        sleep(0.01)
            except Exception as exc:
                raise ExecutionTerminated("Owned worker process could not be reaped") from exc
            finally:
                if self.job is not None:
                    self.job.Close()
                    self.job = None
                self.closed = process is None or process.poll() is not None
