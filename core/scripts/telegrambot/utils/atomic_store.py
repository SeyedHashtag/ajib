import json
import os
import threading
from copy import deepcopy
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for tests
    fcntl = None


_process_lock = threading.RLock()


def _default_copy(default):
    return deepcopy({} if default is None else default)


def _read_json_for_update(path, default):
    """Read state for a locked update without hiding damaged persisted data."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return _default_copy(default)


@contextmanager
def locked_json(path, default=None, mode=0o600):
    """Yield mutable JSON data while holding a process and filesystem lock.

    Read-only helpers deliberately tolerate a missing or damaged file, but a
    locked mutation must never replace damaged state with an empty default.
    JSON and I/O errors therefore propagate before the caller can write.
    """
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
                data = _read_json_for_update(path, default)
                yield data
                write_json(path, data, mode=mode)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return _default_copy(default)


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
        # fsyncing only the file does not make the directory entry durable
        # across a power loss. Sync the parent after the atomic rename too.
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
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
