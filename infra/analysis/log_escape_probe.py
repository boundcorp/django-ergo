# Analysis sandbox log-escape probe (blocker B7). Submitted as user code to the
# real executor by tests/test_analysis_log_isolation.py and by the runner image
# smoke test in .github/workflows/analysis-images.yml. It tries every stdout-like
# path a script can reach and writes a marker to each; the caller asserts that
# the container log holds nothing but the executor's one capped record.
import errno
import json
import os

results = {}


def attempt(name):
    try:
        fd = os.open(name, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as exc:
        results[name] = "blocked:" + errno.errorcode.get(exc.errno, str(exc.errno))
        return
    try:
        os.write(fd, b"ESCAPED " + name.encode() + b"\n")
        results[name] = "wrote"
    except OSError as exc:
        results[name] = "write-failed:" + errno.errorcode.get(exc.errno, "")
    finally:
        os.close(fd)


# The executor (the parent; PID 1 in a pod). Guess fds when listing fails.
parent = "/proc/%d" % os.getppid()
try:
    fds = os.listdir(parent + "/fd")
except OSError as exc:
    results["list parent fds"] = "blocked:" + errno.errorcode[exc.errno]
    fds = [str(n) for n in range(32)]
for fd in fds:
    attempt(parent + "/fd/" + fd)
for leaf in ("mem", "environ"):
    try:
        open(parent + "/" + leaf, "rb").close()
        results[parent + "/" + leaf] = "opened"
    except OSError as exc:
        results[parent + "/" + leaf] = "blocked:" + errno.errorcode[exc.errno]
results["parent"] = parent
# The kernel calls besides /proc that could reach the parent's descriptors.
# Both need ptrace access, which a non-dumpable parent denies to the same uid.
try:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.ptrace.restype = ctypes.c_long

    def call(value):
        if value == -1:
            code = ctypes.get_errno()
            return "blocked:" + errno.errorcode.get(code, str(code))
        return None

    PTRACE_SEIZE = 0x4206
    outcome = call(
        libc.ptrace(
            ctypes.c_long(PTRACE_SEIZE), ctypes.c_long(os.getppid()), None, None
        )
    )
    # Exiting detaches a tracer, so a successful seize needs no cleanup.
    results["ptrace parent"] = outcome or "attached"
    pidfd = libc.syscall(
        ctypes.c_long(434), ctypes.c_long(os.getppid()), 0
    )  # pidfd_open
    outcome = call(pidfd)
    if outcome is None:
        for number in range(32):
            fd = libc.syscall(ctypes.c_long(438), pidfd, number, 0)  # pidfd_getfd
            outcome = call(fd)
            if outcome is None:
                try:
                    os.write(fd, b"ESCAPED pidfd_getfd\n")
                    outcome = "wrote"
                except OSError:
                    outcome = "obtained"
                os.close(fd)
                break
        os.close(pidfd)
    results["pidfd_getfd parent"] = outcome
except OSError as exc:
    results["ptrace parent"] = "blocked:" + str(exc)
for path in (
    "/dev/stdout",
    "/dev/stderr",
    "/proc/self/fd/1",
    "/proc/self/fd/2",
    "/dev/fd/1",
    "/dev/tty",
):
    attempt(path)
# A job pod has no terminal (admission forbids tty), so no console. The local
# tests skip it on terminal-attached dev containers, where it is a real tty.
CHECK_CONSOLE = True
if CHECK_CONSOLE:
    attempt("/dev/console")
os.write(1, b"INSIDE fd1\n")
os.write(2, b"INSIDE fd2\n")
inherited = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
        inherited.append(fd)
    except OSError:
        pass
results["inherited"] = inherited
print("RESULTS " + json.dumps(results))
