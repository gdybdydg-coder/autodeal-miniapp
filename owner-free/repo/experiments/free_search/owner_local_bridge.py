import json, os, time, uuid
from pathlib import Path
def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(10):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)

def child_environment(env):
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'COMSPEC', 'PATHEXT', 'HOME', 'LANG', 'AUTODEAL_OWNER_STATE_DIR', 'AUTODEAL_OWNER_BROWSER_BINARIES'}
    return {k: v for k, v in env.items() if k.upper() in allowed}