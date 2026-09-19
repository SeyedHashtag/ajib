"""Release gate for generated client artifacts (not private source paths)."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_branding', ROOT / 'core/scripts/telegrambot/utils/public_branding.py')
branding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(branding)


def check(directory):
    directory = Path(directory)
    if not (directory / 'index.html').is_file():
        raise ValueError('Production assets have not been built')
    failures = []
    for path in directory.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if path.suffix == '.map' or branding.contains_private(relative):
            failures.append(relative)
        if path.suffix in {'.html', '.js', '.css', '.json', '.svg', '.txt', '.webmanifest'}:
            if branding.contains_private(path.read_text(encoding='utf-8')):
                failures.append(relative)
    if failures:
        raise ValueError('Private branding or source maps in public assets: ' + ', '.join(sorted(set(failures))))


if __name__ == '__main__':
    check(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'web/dist')
    print('Public production artifacts passed')
