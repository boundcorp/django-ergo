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
