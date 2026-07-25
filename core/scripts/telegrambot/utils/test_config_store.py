import json
import os
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class TestConfigStoreError(RuntimeError):
    pass


_store_lock = threading.RLock()


def _sqlite_modules(path):
    try:
        from utils import database, state_store
    except ImportError:
        return None
    if not state_store.is_managed_path(path):
        return None
    return database, state_store


def _parent_dir(path):
    return os.path.dirname(os.path.abspath(path))


@contextmanager
def _file_lock(path):
    parent = _parent_dir(path)
    os.makedirs(parent, exist_ok=True)
    lock_file = open(f"{path}.lock", "a")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _read_unlocked(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise TestConfigStoreError(f"Failed to read test config database: {exc}") from exc
    if not isinstance(data, dict):
        raise TestConfigStoreError("Test config database must contain a JSON object.")
    return data


def _write_unlocked(path, configs):
    if not isinstance(configs, dict):
        raise TestConfigStoreError("Test config database must contain a JSON object.")

    parent = _parent_dir(path)
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(configs, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def load_test_configs(path):
    sqlite_modules = _sqlite_modules(path)
    if sqlite_modules:
        _database, state_store = sqlite_modules
        data = state_store.read_state(path, {})
        if not isinstance(data, dict):
            raise TestConfigStoreError("Test config database must contain a JSON object.")
        return data
    with _store_lock:
        with _file_lock(path):
            return _read_unlocked(path)


def save_test_configs(path, configs):
    sqlite_modules = _sqlite_modules(path)
    if sqlite_modules:
        _database, state_store = sqlite_modules
        if not isinstance(configs, dict):
            raise TestConfigStoreError("Test config database must contain a JSON object.")
        state_store.write_state(path, configs)
        return
    with _store_lock:
        with _file_lock(path):
            _write_unlocked(path, configs)


def update_test_configs(path, mutator):
    sqlite_modules = _sqlite_modules(path)
    if sqlite_modules:
        database, state_store = sqlite_modules
        descriptor = state_store.describe_path(path)
        try:
            with database.transaction() as connection:
                configs = state_store.load_descriptor(connection, descriptor, {})
                if not isinstance(configs, dict):
                    raise TestConfigStoreError("Test config database must contain a JSON object.")
                result = mutator(configs)
                state_store.save_descriptor(connection, descriptor, configs)
                return result
        except TestConfigStoreError:
            raise
        except Exception as exc:
            raise TestConfigStoreError(f"Failed to update test config database: {exc}") from exc
    with _store_lock:
        with _file_lock(path):
            configs = _read_unlocked(path)
            result = mutator(configs)
            _write_unlocked(path, configs)
            return result


def upsert_recovered_test_users(path, recovered_users):
    def mutate(configs):
        created = 0
        history_added = 0
        for recovered in recovered_users or []:
            telegram_id = recovered.get("telegram_id")
            if telegram_id is None:
                continue
            user_key = str(telegram_id)
            history_record = dict(recovered.get("history_record") or {})
            existed = user_key in configs and isinstance(configs.get(user_key), dict)
            entry = dict(configs.get(user_key) or {})
            entry.setdefault("telegram_id", int(telegram_id))
            if not existed:
                entry["used_at"] = recovered.get("used_at")
                entry["recovery_source"] = "verified_orphan_test"
                entry["recovered_at"] = recovered.get("recovered_at")
                created += 1

            history = [item for item in entry.get("historical_configs", []) if isinstance(item, dict)]
            target = (
                str(history_record.get("server_id") or "primary").lower(),
                str(history_record.get("username") or "").lower(),
            )
            if not any(
                (
                    str(item.get("server_id") or "primary").lower(),
                    str(item.get("username") or "").lower(),
                ) == target
                for item in history
            ):
                history.append(history_record)
                history_added += 1
            entry["historical_configs"] = history
            configs[user_key] = entry
        return {"created": created, "history_added": history_added}

    return update_test_configs(path, mutate)


def upsert_recovered_test_user(path, telegram_id, history_record, used_at, recovered_at):
    return upsert_recovered_test_users(path, [{
        "telegram_id": telegram_id,
        "history_record": history_record,
        "used_at": used_at,
        "recovered_at": recovered_at,
    }])
