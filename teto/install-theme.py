#!/usr/bin/env python3
"""Install the Kasane Teto theme into a separate Strawberry profile on Linux."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True, help='Built Strawberry executable')
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local/share/strawberry-teto')
    args = parser.parse_args()
    binary = args.binary.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('--binary must point to an executable Strawberry build')
    root = args.prefix.expanduser().resolve()
    source = Path(__file__).resolve().parent
    config = root / 'config'
    data = root / 'data'
    theme = data / 'theme'
    settings = config / 'strawberry/strawberry.conf'
    settings.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'theme', theme, dirs_exist_ok=True)
    shutil.copytree(theme / 'icons', data / 'icons/kasane-teto', dirs_exist_ok=True)
    # Preserve settings and service logins on subsequent installations.
    if not settings.exists():
        settings.write_text((theme / 'strawberry.conf.in').read_text().replace('@THEME_DIR@', str(theme)))
        settings.chmod(0o600)
    if not (config / 'kdeglobals').exists():
        shutil.copy2(theme / 'kdeglobals', config / 'kdeglobals')
    font_root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'fonts/strawberry-teto'
    shutil.copytree(theme / 'fonts', font_root, dirs_exist_ok=True)
    if shutil.which('fc-cache'):
        subprocess.run(['fc-cache', str(font_root)], check=True)
    helpers = root / 'helpers'
    helpers.mkdir(exist_ok=True)
    shutil.copy2(source / 'qobuz-save.py', helpers / 'qobuz-save.py')
    launcher = root / 'bin/strawberry-teto'
    launcher.parent.mkdir(exist_ok=True)
    quote = lambda path: shlex.quote(str(path))
    launcher.write_text(
        '#!/bin/sh\nset -eu\n'
        'export QT_QPA_PLATFORMTHEME=kde\n'
        f'export XDG_CONFIG_HOME={quote(config)}\n'
        f'export XDG_DATA_HOME={quote(data)}\n'
        f'export XDG_CACHE_HOME={quote(root / "cache")}\n'
        f'export STRAWBERRY_TETO_PROFILE={quote(settings)}\n'
        'if [ "${STRAWBERRY_TETO_QOBUZ_SAVE:-0}" = 1 ]; then\n'
        f'  python3 {quote(helpers / "qobuz-save.py")} --watch </dev/null >/dev/null 2>&1 &\n'
        'fi\n'
        f'exec {quote(binary)} "$@"\n'
    )
    launcher.chmod(0o755)
    print(f'Installed. Close other Strawberry instances, then run: {launcher}')
    print('Add your music folders in Settings > Collection; this profile starts with an empty library.')


if __name__ == '__main__':
    main()
