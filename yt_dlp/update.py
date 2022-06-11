import hashlib
import json
import os
import subprocess
import sys
import traceback
import platform
from zipimport import zipimporter

from .compat import compat_realpath, functools
from .utils import Popen, encode_compat_str, write_string
from .version import __version__
try:
    from .build_config import variant
except ImportError:
    variant = 'red'

try:
    from .build_config import is_brew
except ImportError:
    is_brew = False


@functools.cache
def _get_variant_and_executable_path():
    """@returns (variant, executable_path)"""
    if hasattr(sys, 'frozen'):
        path = sys.executable
        prefix = 'mac' if sys.platform == 'darwin' else 'win'
        if getattr(sys, '_MEIPASS', None):
            if sys._MEIPASS == os.path.dirname(sys.executable):
                return f'{prefix}_dir', path
            if prefix == 'win' and variant:
                return f'exe_{variant}', path
            return f'{prefix}_exe', path
        return 'py2exe'

    path = os.path.dirname(__file__)
    if isinstance(__loader__, zipimporter):
        return 'zip', os.path.join(path, '..')
    elif (os.path.basename(sys.argv[0]) in ('__main__.py', '-m')
          and os.path.exists(os.path.join(path, '../.git/HEAD'))):
        return 'source', path
    elif is_brew:
        return 'homebrew', path
    return 'unknown', path


@functools.cache
def detect_variant():
    return _get_variant_and_executable_path()[0]


_NON_UPDATEABLE_REASONS = {
    'exe_red': None,
    'exe_white': None,
    'win_exe': None,
    'zip': None,
    'mac_exe': None,
    'py2exe': None,
    'homebrew': None,
    'win_dir': 'Auto-update is not supported for unpackaged windows executable; Re-download the latest release',
    'mac_dir': 'Auto-update is not supported for unpackaged MacOS executable; Re-download the latest release',
    'source': 'You cannot update when running from source code; Use git to pull the latest changes',
    'unknown': 'It looks like you installed yt-dlp with a package manager, pip or setup.py; Use that to update',
}


def is_non_updateable():
    return _NON_UPDATEABLE_REASONS.get(detect_variant(), _NON_UPDATEABLE_REASONS['unknown'])


def get_version_info(ydl):
    JSON_URL = 'https://api.github.com/repos/ytdl-patched/ytdl-patched/releases/latest'
    version_info = ydl._opener.open(JSON_URL).read().decode()
    return json.loads(version_info)

# def get_version_info(ydl):
#     # this is for when it needs to look into pre-prelease versions
#     for page_num in range(1, 4):
#         try:
#             JSON_URL = 'https://api.github.com/repos/nao20010128nao/ytdl-patched/releases?page=%d' % page_num
#             releases = json.loads(ydl._opener.open(JSON_URL).read().decode('utf-8'))
#         except BaseException:
#             JSON_URL = 'https://api.github.com/repos/ytdl-patched/ytdl-patched/releases?page=%d' % page_num
#             releases = json.loads(ydl._opener.open(JSON_URL).read().decode('utf-8'))
#         for release in releases:
#             if release.get('prerelease'):
#                 return release
#     raise Exception('can\'t find pre-release.')


def run_update(ydl):
    """
    Update the program file with the latest version from the repository
    Returns whether the program should terminate
    """
    def report_error(msg, expected=False):
        ydl.report_error(msg, tb='' if expected else None)

    def report_unable(action, expected=False):
        report_error(f'Unable to {action}', expected)

    def report_permission_error(file):
        report_unable(f'write to {file}; Try running as administrator', True)

    def report_network_error(action, delim=';'):
        report_unable(f'{action}{delim} Visit  https://github.com/yt-dlp/yt-dlp/releases/latest', True)

    def calc_sha256sum(path):
        h = hashlib.sha256()
        b = bytearray(128 * 1024)
        mv = memoryview(b)
        with open(os.path.realpath(path), 'rb', buffering=0) as f:
            for n in iter(lambda: f.readinto(mv), 0):
                h.update(mv[:n])
        return h.hexdigest()

    # Download and check versions info
    try:
        version_info = get_version_info(ydl)
    except Exception:
        return report_network_error('obtain version info', delim='; Please try again later or')

    def version_tuple(version_str):
        return tuple(map(int, version_str.split('.')))

    version_id = version_info['name']
    ydl.to_screen(f'Latest version: {version_id}, Current version: {__version__}')
    if version_tuple(__version__) >= version_tuple(version_id):
        ydl.to_screen(f'ytdl-patched is up to date ({__version__})')
        return

    err = is_non_updateable()
    if err:
        return report_error(err, True)

    # sys.executable is set to the full pathname of the exe-file for py2exe
    # though symlinks are not followed so that we need to do this manually
    # with help of realpath
    filename = compat_realpath(sys.executable if hasattr(sys, 'frozen') else sys.argv[0])

    ydl.to_screen(f'Current Build Hash {calc_sha256sum(filename)}')
    ydl.to_screen(f'Updating to version {version_id} ...')

    version_labels = {
        'zip_3': '',
        'exe_red_64': '-red.exe',
        'exe_white_64': '-white.exe',
        'win_exe_64': '.exe',
        'py2exe_64': '_min.exe',
        'win_exe_32': '_x86.exe',
        'mac_exe_64': '_macos',
    }

    def get_bin_info(bin_or_exe, version):
        label = version_labels['%s_%s' % (bin_or_exe, version)]
        return next((i for i in version_info['assets'] if i['name'] == f'ytdl-patched{label}'), {})

    def get_sha256sum(bin_or_exe, version):
        filename = 'ytdl-patched%s' % version_labels['%s_%s' % (bin_or_exe, version)]
        urlh = next(
            (i for i in version_info['assets'] if i['name'] in ('SHA2-256SUMS')),
            {}).get('browser_download_url')
        if not urlh:
            return None
        hash_data = ydl._opener.open(urlh).read().decode()
        return dict(ln.split()[::-1] for ln in hash_data.splitlines()).get(filename)

    if not os.access(filename, os.W_OK):
        return report_permission_error(filename)

    # PyInstaller
    variant = detect_variant()
    if variant in ('win_exe', 'exe_red', 'exe_white', 'py2exe'):
        directory = os.path.dirname(filename)
        if not os.access(directory, os.W_OK):
            return report_permission_error(directory)
        try:
            if os.path.exists(filename + '.old'):
                os.remove(filename + '.old')
        except OSError:
            return report_unable('remove the old version')

        try:
            arch = platform.architecture()[0][:2]
            url = get_bin_info(variant, arch).get('browser_download_url')
            if not url:
                return report_network_error('fetch updates')
            urlh = ydl._opener.open(url)
            newcontent = urlh.read()
            urlh.close()
        except OSError:
            return report_network_error('download latest version')

        try:
            with open(filename + '.new', 'wb') as outf:
                outf.write(newcontent)
        except OSError:
            return report_permission_error(f'{filename}.new')

        expected_sum = get_sha256sum(variant, arch)
        if not expected_sum:
            ydl.report_warning('no hash information found for the release')
        elif calc_sha256sum(filename + '.new') != expected_sum:
            report_network_error('verify the new executable')
            try:
                os.remove(filename + '.new')
            except OSError:
                return report_unable('remove corrupt download')

        try:
            os.rename(filename, filename + '.old')
        except OSError:
            return report_unable('move current version')
        try:
            os.rename(filename + '.new', filename)
        except OSError:
            report_unable('overwrite current version')
            os.rename(filename + '.old', filename)
            return
        try:
            # Continues to run in the background
            Popen(
                'ping 127.0.0.1 -n 5 -w 1000 & del /F "%s.old"' % filename,
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ydl.to_screen('Updated ytdl-patched to version %s' % version_id)
            return True  # Exit app
        except OSError:
            report_unable('delete the old version')

    elif variant in ('zip', 'mac_exe'):
        pack_type = '3' if variant == 'zip' else '64'
        try:
            url = get_bin_info(variant, pack_type).get('browser_download_url')
            if not url:
                return report_network_error('fetch updates')
            urlh = ydl._opener.open(url)
            newcontent = urlh.read()
            urlh.close()
        except OSError:
            return report_network_error('download the latest version')

        expected_sum = get_sha256sum(variant, pack_type)
        if not expected_sum:
            ydl.report_warning('no hash information found for the release')
        elif hashlib.sha256(newcontent).hexdigest() != expected_sum:
            return report_network_error('verify the new package')

        try:
            with open(filename + '.new', 'wb') as outf:
                os.chmod(outf.fileno(), os.stat(filename).st_mode)
                outf.write(newcontent)
        except (IOError, OSError):
            return report_unable('download current version')

        try:
            os.replace(filename + '.new', filename)
        except (IOError, OSError):
            return report_unable('overwrite current version')

        ydl.to_screen('Updated ytdl-patched to version %s; Restart yt-dlp to use the new version' % version_id)
        return

    elif variant == 'homebrew':
        stdout = next(filter(None, Popen(['brew', 'tap'], stdout=subprocess.PIPE, encoding='utf-8').communicate()), '')
        if 'nao20010128nao/my' in stdout:
            ydl.to_screen('Fixing taps to point to new one')
            ret = Popen(['brew', 'untap', '-f', 'nao20010128nao/my']).wait()
            if ret != 0:
                return report_unable('untap old tap')
            ret = Popen(['brew', 'tap', 'lesmiscore/my']).wait()
            if ret != 0:
                return report_unable('tap new tap')
        os.execvp('brew', ['brew', 'upgrade', 'lesmiscore/my/ytdl-patched'])

    assert False, f'Unhandled variant: {variant}'


# Deprecated
def update_self(to_screen, verbose, opener):

    printfn = to_screen

    write_string(
        'DeprecationWarning: "yt_dlp.update.update_self" is deprecated and may be removed in a future version. '
        'Use "yt_dlp.update.run_update(ydl)" instead\n')

    class FakeYDL():
        _opener = opener
        to_screen = printfn

        @staticmethod
        def report_warning(msg, *args, **kwargs):
            return printfn('WARNING: %s' % msg, *args, **kwargs)

        @staticmethod
        def report_error(msg, tb=None):
            printfn('ERROR: %s' % msg)
            if not verbose:
                return
            if tb is None:
                # Copied from YoutubeDl.trouble
                if sys.exc_info()[0]:
                    tb = ''
                    if hasattr(sys.exc_info()[1], 'exc_info') and sys.exc_info()[1].exc_info[0]:
                        tb += ''.join(traceback.format_exception(*sys.exc_info()[1].exc_info))
                    tb += encode_compat_str(traceback.format_exc())
                else:
                    tb_data = traceback.format_list(traceback.extract_stack())
                    tb = ''.join(tb_data)
            if tb:
                printfn(tb)

    return run_update(FakeYDL())
