import json
import os
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for tests
    fcntl = None


_process_lock = threading.RLock()


@contextmanager
def locked_json(path, default=None, mode=0o600):
    """Yield mutable JSON data while holding a process and filesystem lock."""
    default = {} if default is None else default
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = f"{path}.lock"
    with _process_lock:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            try:
                os.chmod(lock_path, mode)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                data = read_json(path, default)
                yield data
                write_json(path, data, mode=mode)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        if isinstance(default, dict):
            return dict(default)
        if isinstance(default, list):
            return list(default)
        return default


def write_json(path, data, mode=0o600):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
