import contextlib
import io
import pytest

os = __import__("os")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.chdir("c:/Users/Wxnter/Documents/VeilBound")

buf = io.StringIO()
code = 0
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = pytest.main(["tests", "-q", "-p", "no:cacheprovider"])
except SystemExit as exc:
    code = exc.code
open("pytest_full.txt", "w").write(buf.getvalue() + "\nEXIT=" + str(code))
