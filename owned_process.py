"""Own only the process tree we launch: Windows job / POSIX process group."""
import asyncio
import os
import signal


class WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        class Limits(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                        ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Limits), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        self.ctypes = c
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.resume = c.WinDLL('ntdll').NtResumeProcess
        self.resume.argtypes = [w.HANDLE]
        self.resume.restype = c.c_long
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, pid):
        # The child is born suspended so it cannot spawn outside the job before assignment.
        process = self.kernel.OpenProcess(0x0100 | 0x0001 | 0x0800, False, pid)
        if not process:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if self.resume(process) < 0:
                raise OSError('无法恢复已托管的子进程')
        finally:
            self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class OwnedProcess:
    def __init__(self):
        self.process = None
        self.job = None
        self._closed = False

    async def run(self, args, *, input_data=b'', cwd=None, env=None, timeout=120):
        options = {'creationflags': 0x00000004 | 0x00000200} if os.name == 'nt' else {'start_new_session': True}
        if os.name == 'nt':
            self.job = WindowsJob()
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options))
        try:
            try:
                self.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                self.process = await spawn
                raise
            if self.job:
                self.job.attach_and_resume(self.process.pid)
            stdout, stderr = await asyncio.wait_for(self.process.communicate(input_data), timeout)
            return self.process.returncode, stdout, stderr
        finally:
            await self.close()

    async def close(self):
        if self._closed:
            return
        if self.job:
            self.job.close()  # Kills descendants even if the root process already exited.
        elif self.process and os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process and self.process.returncode is None:
            # Also covers failure before a suspended Windows child was assigned to the job.
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(self.process.wait(), 5)
        self._closed = True
