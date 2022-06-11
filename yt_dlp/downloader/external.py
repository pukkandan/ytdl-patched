import enum
import os
import os.path
import re
import subprocess
import sys
import time
import signal

from .fragment import FragmentFD
from ..compat import functools  # isort: split
from ..compat import compat_setenv
from ..postprocessor.ffmpeg import EXT_TO_OUT_FORMATS, FFmpegPostProcessor
from ..postprocessor._attachments import RunsFFmpeg
from ..utils import (
    Popen,
    _configuration_args,
    check_executable,
    classproperty,
    cli_bool_option,
    cli_option,
    cli_valueless_option,
    determine_ext,
    encodeArgument,
    encodeFilename,
    find_available_port,
    handle_youtubedl_headers,
    remove_end,
    sanitized_Request,
    try_get,
    variadic,
    traverse_obj,
)
from ..longname import split_longname


class Features(enum.Enum):
    TO_STDOUT = enum.auto()
    MULTIPLE_FORMATS = enum.auto()


class ExternalFD(FragmentFD):
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps')
    SUPPORTED_FEATURES = ()

    def real_download(self, filename, info_dict):
        self.report_destination(filename)
        tmpfilename = self.temp_name(filename)

        try:
            started = time.time()
            retval = self._call_downloader(tmpfilename, info_dict)
        except KeyboardInterrupt:
            if not info_dict.get('is_live'):
                raise
            # Live stream downloading cancellation should be considered as
            # correct and expected termination thus all postprocessing
            # should take place
            retval = 0
            self.to_screen('[%s] Interrupted by user' % self.get_basename())

        if retval == 0:
            status = {
                'filename': filename,
                'status': 'finished',
                'elapsed': time.time() - started,
            }
            if filename != '-':
                fsize = self.ydl.getsize(encodeFilename(tmpfilename))
                self.to_screen(f'\r[{self.get_basename()}] Downloaded {fsize} bytes')
                self.try_rename(tmpfilename, filename)
                status.update({
                    'downloaded_bytes': fsize,
                    'total_bytes': fsize,
                })
            self._hook_progress(status, info_dict)
            return True
        else:
            self.to_stderr('\n')
            self.report_error('%s exited with code %d' % (
                self.get_basename(), retval))
            return False

    @classmethod
    def get_basename(cls):
        return cls.__name__[:-2].lower()

    @classproperty
    def EXE_NAME(cls):
        return cls.get_basename()

    @functools.cached_property
    def exe(self):
        return self.EXE_NAME

    @classmethod
    def available(cls, path=None):
        path = check_executable(
            cls.EXE_NAME if path in (None, cls.get_basename()) else path,
            [cls.AVAILABLE_OPT])
        if not path:
            return False
        cls.exe = path
        return path

    @classmethod
    def supports(cls, info_dict):
        return all((
            not info_dict.get('to_stdout') or Features.TO_STDOUT in cls.SUPPORTED_FEATURES,
            '+' not in info_dict['protocol'] or Features.MULTIPLE_FORMATS in cls.SUPPORTED_FEATURES,
            all(proto in cls.SUPPORTED_PROTOCOLS for proto in info_dict['protocol'].split('+')),
        ))

    @classmethod
    def can_download(cls, info_dict, path=None):
        return cls.available(path) and cls.supports(info_dict)

    def _option(self, command_option, param):
        return cli_option(self.params, command_option, param)

    def _bool_option(self, command_option, param, true_value='true', false_value='false', separator=None):
        return cli_bool_option(self.params, command_option, param, true_value, false_value, separator)

    def _valueless_option(self, command_option, param, expected_value=True):
        return cli_valueless_option(self.params, command_option, param, expected_value)

    def _configuration_args(self, keys=None, *args, **kwargs):
        return _configuration_args(
            self.get_basename(), self.params.get('external_downloader_args'), self.EXE_NAME,
            keys, *args, **kwargs)

    def _call_downloader(self, tmpfilename, info_dict):
        """ Either overwrite this or implement _make_cmd """
        cmd = [encodeArgument(a) for a in self._make_cmd(tmpfilename, info_dict)]

        self._debug_cmd(cmd)

        if 'fragments' not in info_dict:
            _, stderr, retcode = self._call_process(cmd, info_dict)
            if retcode == 0:
                self.to_stderr(stderr.decode('utf-8', 'replace'))
            return retcode

        fragment_retries = self.params.get('fragment_retries', 0)
        skip_unavailable_fragments = self.params.get('skip_unavailable_fragments', True)

        count = 0
        while count <= fragment_retries:
            _, stderr, retcode = self._call_process(cmd, info_dict)
            if retcode == 0:
                break
            # TODO: Decide whether to retry based on error code
            # https://aria2.github.io/manual/en/html/aria2c.html#exit-status
            self.to_stderr(stderr.decode('utf-8', 'replace'))
            count += 1
            if count <= fragment_retries:
                self.to_screen(
                    '[%s] Got error. Retrying fragments (attempt %d of %s)...'
                    % (self.get_basename(), count, self.format_retries(fragment_retries)))
                self.sleep_retry('fragment', count)
        if count > fragment_retries:
            if not skip_unavailable_fragments:
                self.report_error('Giving up after %s fragment retries' % fragment_retries)
                return -1

        decrypt_fragment = self.decrypter(info_dict)
        dest, _ = self.sanitize_open(tmpfilename, 'wb')
        for frag_index, fragment in enumerate(info_dict['fragments']):
            fragment_filename = '%s-Frag%d' % (tmpfilename, frag_index)
            try:
                src, _ = self.sanitize_open(fragment_filename, 'rb')
            except OSError as err:
                if skip_unavailable_fragments and frag_index > 1:
                    self.report_skip_fragment(frag_index, err)
                    continue
                self.report_error(f'Unable to open fragment {frag_index}; {err}')
                return -1
            dest.write(decrypt_fragment(fragment, src.read()))
            src.close()
            if not self.params.get('keep_fragments', False):
                self.try_remove(encodeFilename(fragment_filename))
        dest.close()
        self.try_remove(encodeFilename('%s.frag.urls' % tmpfilename))
        return 0

    def _call_process(self, cmd, info_dict):
        p = Popen(cmd, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate_or_kill()
        return stdout, stderr, p.returncode


class CurlFD(ExternalFD):
    AVAILABLE_OPT = '-V'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '--location', '-o', tmpfilename, '--compressed']
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', f'{key}: {val}']

        cmd += self._bool_option('--continue-at', 'continuedl', '-', '0')
        cmd += self._valueless_option('--silent', 'noprogress')
        cmd += self._valueless_option('--verbose', 'verbose')
        cmd += self._option('--limit-rate', 'ratelimit')
        retry = self._option('--retry', 'retries')
        if len(retry) == 2:
            if retry[1] in ('inf', 'infinite'):
                retry[1] = '2147483647'
            cmd += retry
        cmd += self._option('--max-filesize', 'max_filesize')
        cmd += self._option('--interface', 'source_address')
        cmd += self._option('--proxy', 'proxy')
        cmd += self._valueless_option('--insecure', 'nocheckcertificate')
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd

    def _call_downloader(self, tmpfilename, info_dict):
        cmd = [encodeArgument(a) for a in self._make_cmd(tmpfilename, info_dict)]

        self._debug_cmd(cmd)

        # curl writes the progress to stderr so don't capture it.
        p = Popen(cmd)
        p.communicate_or_kill()
        return p.returncode


class AxelFD(ExternalFD):
    AVAILABLE_OPT = '-V'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-o', tmpfilename]
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['-H', f'{key}: {val}']
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd


class WgetFD(ExternalFD):
    AVAILABLE_OPT = '--version'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-O', tmpfilename, '-nv', '--no-cookies', '--compression=auto']
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', f'{key}: {val}']
        cmd += self._option('--limit-rate', 'ratelimit')
        retry = self._option('--tries', 'retries')
        if len(retry) == 2:
            if retry[1] in ('inf', 'infinite'):
                retry[1] = '0'
            cmd += retry
        cmd += self._option('--bind-address', 'source_address')
        proxy = self.params.get('proxy')
        if proxy:
            for var in ('http_proxy', 'https_proxy'):
                cmd += ['--execute', f'{var}={proxy}']
        cmd += self._valueless_option('--no-check-certificate', 'nocheckcertificate')
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd


class Aria2cFD(ExternalFD):
    AVAILABLE_OPT = '-v'
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps', 'dash_frag_urls', 'm3u8_frag_urls')

    @staticmethod
    def supports_manifest(manifest):
        UNSUPPORTED_FEATURES = [
            r'#EXT-X-BYTERANGE',  # playlists composed of byte ranges of media files [1]
            # 1. https://tools.ietf.org/html/draft-pantos-http-live-streaming-17#section-4.3.2.2
        ]
        check_results = (not re.search(feature, manifest) for feature in UNSUPPORTED_FEATURES)
        return all(check_results)

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-c',
               '--console-log-level=warn', '--summary-interval=0', '--download-result=hide',
               '--http-accept-gzip=true', '--file-allocation=none', '-x16', '-j16', '-s16']
        if 'fragments' in info_dict:
            cmd += ['--allow-overwrite=true', '--allow-piece-length-change=true']
        else:
            cmd += ['--min-split-size', '1M']

        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', f'{key}: {val}']
        cmd += self._option('--max-overall-download-limit', 'ratelimit')
        cmd += self._option('--interface', 'source_address')

        proxy = self.params.get('proxy')
        if isinstance(proxy, (str, bytes)) and re.match(r'^socks[\da-zA-Z]*://', proxy):
            self.report_warning(
                '%s does not support SOCKS proxies. Downloading is likely to fail. '
                'Consider adding --hls-prefer-native to your command.' % self.get_basename())

        cmd += self._option('--all-proxy', 'proxy')
        cmd += self._bool_option('--check-certificate', 'nocheckcertificate', 'false', 'true', '=')
        cmd += self._bool_option('--remote-time', 'updatetime', 'true', 'false', '=')
        cmd += self._bool_option('--show-console-readout', 'noprogress', 'false', 'true', '=')
        cmd += self._configuration_args()

        if info_dict.get('__rpc_port'):
            cmd += ['--enable-rpc', f'--rpc-listen-port={info_dict["__rpc_port"]}']

        # aria2c strips out spaces from the beginning/end of filenames and paths.
        # We work around this issue by adding a "./" to the beginning of the
        # filename and relative path, and adding a "/" at the end of the path.
        # See: https://github.com/yt-dlp/yt-dlp/issues/276
        # https://github.com/ytdl-org/youtube-dl/issues/20312
        # https://github.com/aria2/aria2/issues/1373
        dn = self.ydl.dirname(tmpfilename)
        if dn:
            if not self.ydl.isabs(dn):
                dn = f'.{os.path.sep}{dn}'
            cmd += ['--dir', dn + os.path.sep]
        if 'fragments' not in info_dict:
            cmd += ['--out', f'.{os.path.sep}{self.ydl.basename(tmpfilename)}']
        cmd += ['--auto-file-renaming=false']

        if 'fragments' in info_dict:
            cmd += ['--file-allocation=none', '--uri-selector=inorder']
            url_list_file = '%s.frag.urls' % tmpfilename
            url_list = []
            for frag_index, fragment in enumerate(info_dict['fragments']):
                fragment_filename = '%s-Frag%d' % (self.ydl.basename(tmpfilename), frag_index)
                url_list.append('%s\n\tout=%s' % (fragment['url'], fragment_filename))
            stream, _ = self.sanitize_open(url_list_file, 'wb')
            stream.write('\n'.join(url_list).encode())
            stream.close()
            cmd += ['-i', url_list_file]
        else:
            cmd += ['--', info_dict['url']]
        return cmd

    def _call_downloader(self, tmpfilename, info_dict):
        info_dict.pop('__rpc_port', None)

        # aria2c does not support livestreams and stdout redirection,
        # so that's okay
        use_native_progress = (
            self.params.get('enable_native_progress', False)
            and not self.params.get('verbose', False))

        if use_native_progress:
            info_dict = info_dict.copy()
            info_dict['__rpc_port'] = find_available_port() or 19190
        return super()._call_downloader(tmpfilename, info_dict)

    def _call_process(self, cmd, info_dict):
        if '__rpc_port' not in info_dict:
            return super()._call_process(cmd, info_dict)

        from tempfile import TemporaryFile
        import json
        import uuid

        rpc_port = info_dict['__rpc_port']
        nr_frags = len(info_dict['fragments']) if 'fragments' in info_dict else -1

        def aria2c_rpc(method, params):
            # note: there's no need to be UUID (it can even a numeric value), but that's easier
            sanitycheck = str(uuid.uuid4())
            d = json.dumps({
                'jsonrpc': '2.0',
                'id': sanitycheck,
                'method': method,
                'params': params,
            }).encode('utf-8')
            request = sanitized_Request(
                f'http://localhost:{rpc_port}/jsonrpc',
                headers={
                    'Content-Type': 'application/json',
                    'Content-Length': f'{len(d)}',
                    'Ytdl-request-proxy': '__noproxy__',
                },
                data=d)
            with self.ydl.urlopen(request) as r:
                resp = json.load(r)
            # failing at this assertion means that the RPC server went wrong
            # (KeyEror includes)
            assert resp['id'] == sanitycheck
            return resp['result']

        started = time.time()
        status = {
            'filename': info_dict.get('_filename'),
            'status': 'downloading',
            'elapsed': 0,
            'downloaded_bytes': 0,
        }
        if nr_frags >= 0:
            status.update({
                'fragment_count': nr_frags,
                'fragment_index': 0,
            })
        self._hook_progress(status, info_dict)

        with TemporaryFile() as so, TemporaryFile() as se, \
             Popen(cmd, stdout=so.fileno(), stderr=se.fileno()) as p:
            # make a short wait so that RPC client can receive response,
            # or the connection stalls infinitely
            time.sleep(0.2)
            retval = p.poll()
            while retval is None:
                try:
                    # https://aria2.github.io/manual/en/html/libaria2.html#c.DOWNLOAD_WAITING
                    # https://aria2.github.io/manual/en/html/aria2c.html#aria2.tellActive

                    # use tellActive as we won't know the GID without reading stdout
                    # that is a mess in Python
                    aktiva = aria2c_rpc('aria2.tellActive', [])
                    completed = aria2c_rpc('aria2.tellStopped', [0, abs(nr_frags)])
                    if not aktiva and len(completed) == abs(nr_frags):
                        # no active downloads, we'll exit the loop after shutdown
                        aria2c_rpc('aria2.shutdown', [])
                        retval = p.wait()
                        break

                    if nr_frags < 0:
                        # single file
                        active = aktiva[0]
                        cl, ds, tl = int(active['completedLength']), int(active['downloadSpeed']), int(active['totalLength'])
                        status.update({
                            'downloaded_bytes': cl,
                            'speed': ds,
                            'eta': try_get(0, lambda x: (tl - cl) / ds),
                            'total_bytes': tl,
                        })
                        continue

                    # fragmented
                    total_bytes = sum(map(int, traverse_obj([aktiva, completed], (..., ..., 'totalLength'), default=[])))
                    if completed or aktiva:
                        total_bytes = total_bytes * nr_frags / (len(completed) + len(aktiva))
                    total_completed = sum(map(int, traverse_obj(completed, (..., 'totalLength'), default=[])))
                    dled_aktiva = sum(map(int, traverse_obj(aktiva, (..., 'completedLength'), default=[])))
                    total_speed = sum(map(float, traverse_obj(aktiva, (..., 'downloadSpeed'), default=[])))
                    dl_all = dled_aktiva + total_completed

                    status.update({
                        'downloaded_bytes': dl_all,
                        'speed': total_speed,
                        'eta': try_get(0, lambda x: (total_bytes - dl_all) / total_speed),
                        'total_bytes': total_bytes,
                        'fragment_index': len(completed) + len(aktiva) // 2,
                    })
                finally:
                    status.update({'elapsed': time.time() - started})
                    self._hook_progress(status, info_dict)
                    time.sleep(0.1)
                    retval = p.poll()

            if retval == 0:
                status.update({
                    'status': 'finished',
                    'downloaded_bytes': status.get('total_bytes'),
                })
                self._hook_progress(status, info_dict)

            so.seek(0)
            se.seek(0)
            # it's expected to be bytes here!
            stdout, stderr = so.read(), se.read()

            return stdout, stderr, retval


class HttpieFD(ExternalFD):
    AVAILABLE_OPT = '--version'
    EXE_NAME = 'http'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = ['http', '--download', '--output', tmpfilename, info_dict['url']]

        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += [f'{key}:{val}']
        return cmd


class FFmpegFD(ExternalFD, RunsFFmpeg):
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps', 'm3u8', 'm3u8_native', 'rtsp', 'rtmp', 'rtmp_ffmpeg', 'mms', 'http_dash_segments')
    SUPPORTED_FEATURES = (Features.TO_STDOUT, Features.MULTIPLE_FORMATS)

    @classmethod
    def available(cls, path=None):
        # TODO: Fix path for ffmpeg
        # Fixme: This may be wrong when --ffmpeg-location is used
        return FFmpegPostProcessor().available

    def on_process_started(self, proc, stdin):
        """ Override this in subclasses  """
        pass

    @classmethod
    def can_merge_formats(cls, info_dict, params):
        return (
            info_dict.get('requested_formats')
            and info_dict.get('protocol')
            and not params.get('allow_unplayable_formats')
            and 'no-direct-merge' not in params.get('compat_opts', [])
            and cls.can_download(info_dict))

    def _call_downloader(self, tmpfilename, info_dict):
        urls = [f['url'] for f in info_dict.get('requested_formats', [])] or [info_dict['url']]
        ffpp = FFmpegPostProcessor(downloader=self)
        if not ffpp.available:
            self.report_error('m3u8 download detected but ffmpeg could not be found. Please install')
            return False
        ffpp.check_version()

        if self.ydl.params.get('escape_long_names', False):
            tmpfilename = split_longname(tmpfilename)

        args = [ffpp.executable, '-y']

        for log_level in ('quiet', 'verbose'):
            if self.params.get(log_level, False):
                args += ['-loglevel', log_level]
                break
        verbose = self.params.get('verbose')
        if not verbose:
            args += ['-hide_banner']

        live = info_dict.get('live') or info_dict.get('is_live')
        args += traverse_obj(info_dict, ('downloader_options', 'ffmpeg_args'), default=[])

        # These exists only for compatibility. Extractors should use
        # info_dict['downloader_options']['ffmpeg_args'] instead
        args += info_dict.get('_ffmpeg_args') or []
        seekable = info_dict.get('_seekable')
        if seekable is not None:
            # setting -seekable prevents ffmpeg from guessing if the server
            # supports seeking(by adding the header `Range: bytes=0-`), which
            # can cause problems in some cases
            # https://github.com/ytdl-org/youtube-dl/issues/11800#issuecomment-275037127
            # http://trac.ffmpeg.org/ticket/6125#comment:10
            args += ['-seekable', '1' if seekable else '0']

        http_headers = None
        if info_dict.get('http_headers'):
            youtubedl_headers = handle_youtubedl_headers(info_dict['http_headers'])
            # drop Accept-Encoding from request header; it should be added by each client rather than forcing from ytdl-patched itself
            youtubedl_headers.pop(next((x for x in youtubedl_headers.keys() if x.lower() == 'accept-encoding'), None), None)
            http_headers = [
                # Trailing \r\n after each HTTP header is important to prevent warning from ffmpeg/avconv:
                # [http @ 00000000003d2fa0] No trailing CRLF found in HTTP header.
                '-headers',
                ''.join(f'{key}: {val}\r\n' for key, val in youtubedl_headers.items())
            ]

        env = None
        proxy = self.params.get('proxy')
        if proxy:
            if not re.match(r'^[\da-zA-Z]+://', proxy):
                proxy = 'http://%s' % proxy

            if proxy.startswith('socks'):
                self.report_warning(
                    '%s does not support SOCKS proxies. Downloading is likely to fail. '
                    'Consider adding --hls-prefer-native to your command.' % self.get_basename())

            # Since December 2015 ffmpeg supports -http_proxy option (see
            # http://git.videolan.org/?p=ffmpeg.git;a=commit;h=b4eb1f29ebddd60c41a2eb39f5af701e38e0d3fd)
            # We could switch to the following code if we are able to detect version properly
            # args += ['-http_proxy', proxy]
            env = os.environ.copy()
            compat_setenv('HTTP_PROXY', proxy, env=env)
            compat_setenv('http_proxy', proxy, env=env)

        protocol = info_dict.get('protocol')

        if protocol == 'ffmpeg':
            self.report_warning('Calling this downloader with "ffmpeg" is deprecated. Please fix code.')

        if protocol == 'rtmp':
            player_url = info_dict.get('player_url')
            page_url = info_dict.get('page_url')
            app = info_dict.get('app')
            play_path = info_dict.get('play_path')
            tc_url = info_dict.get('tc_url')
            flash_version = info_dict.get('flash_version')
            live = info_dict.get('rtmp_live', False)
            conn = info_dict.get('rtmp_conn')
            if player_url is not None:
                args += ['-rtmp_swfverify', player_url]
            if page_url is not None:
                args += ['-rtmp_pageurl', page_url]
            if app is not None:
                args += ['-rtmp_app', app]
            if play_path is not None:
                args += ['-rtmp_playpath', play_path]
            if tc_url is not None:
                args += ['-rtmp_tcurl', tc_url]
            if flash_version is not None:
                args += ['-rtmp_flashver', flash_version]
            if live:
                args += ['-rtmp_live', 'live']
            if isinstance(conn, list):
                for entry in conn:
                    args += ['-rtmp_conn', entry]
            elif isinstance(conn, str):
                args += ['-rtmp_conn', conn]

        def get_infodict_list(keys):
            result = []
            for k in variadic(keys):
                o = info_dict.get(k)
                if not o:
                    continue
                result.extend(variadic(o))
            return result

        start_time, end_time = info_dict.get('section_start') or 0, info_dict.get('section_end')

        for i, url in enumerate(urls):
            if http_headers is not None and re.match(r'^https?://', url):
                args += http_headers
            if start_time:
                args += ['-ss', str(start_time)]
            if end_time:
                args += ['-t', str(end_time - start_time)]

            args += get_infodict_list((f'input_params_{i + 1}', 'input_params'))
            args += self._configuration_args((f'_i{i + 1}', '_i')) + ['-i', url]

        if not (start_time or end_time) or not self.params.get('force_keyframes_at_cuts'):
            args += ['-c', 'copy']

        if info_dict.get('requested_formats') or protocol == 'http_dash_segments':
            for (i, fmt) in enumerate(info_dict.get('requested_formats') or [info_dict]):
                stream_number = fmt.get('manifest_stream_number', 0)
                args.extend(['-map', f'{i}:{stream_number}'])

        if self.params.get('test', False):
            args += ['-fs', str(self._TEST_FILE_SIZE)]

        ext = info_dict['ext']
        if protocol in ('m3u8', 'm3u8_native'):
            use_mpegts = (tmpfilename == '-') or self.params.get('hls_use_mpegts')
            if use_mpegts is None:
                use_mpegts = info_dict.get('is_live')
            if use_mpegts:
                args += ['-f', 'mpegts']
            else:
                args += ['-f', 'mp4']
                if (ffpp.basename == 'ffmpeg' and ffpp._features.get('needs_adtstoasc')) and (not info_dict.get('acodec') or info_dict['acodec'].split('.')[0] in ('aac', 'mp4a')):
                    args += ['-bsf:a', 'aac_adtstoasc']
        elif protocol == 'rtmp':
            args += ['-f', 'flv']
        elif ext == 'mp4' and tmpfilename == '-':
            args += ['-f', 'mpegts']
        elif ext == 'unknown_video':
            ext = determine_ext(remove_end(tmpfilename, '.part'))
            if ext == 'unknown_video':
                self.report_warning(
                    'The video format is unknown and cannot be downloaded by ffmpeg. '
                    'Explicitly set the extension in the filename to attempt download in that format')
            else:
                self.report_warning(f'The video format is unknown. Trying to download as {ext} according to the filename')
                args += ['-f', EXT_TO_OUT_FORMATS.get(ext, ext)]
        else:
            args += ['-f', EXT_TO_OUT_FORMATS.get(ext, ext)]

        args += get_infodict_list((f'output_params_{i + 1}', 'output_params')) + self._configuration_args(('_o1', '_o', ''))

        use_native_progress = (
            self.params.get('enable_native_progress')
            and not verbose
            and not live
            and url not in ('-', 'pipe:'))

        args = [encodeArgument(opt) for opt in args]
        args.append(encodeFilename(ffpp._ffmpeg_filename_argument(tmpfilename), True))
        if use_native_progress:
            args.extend(['-progress', 'pipe:1', '-stats_period', '0.1'])
        self._debug_cmd(args)

        if use_native_progress:
            proc = Popen(
                args, env=env, universal_newlines=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        else:
            proc = Popen(args, stdin=subprocess.PIPE, env=env)

        if url in ('-', 'pipe:'):
            self.on_process_started(proc, proc.stdin)
        try:
            retval = -1
            if use_native_progress:
                try:
                    retval = self.read_ffmpeg_status(info_dict, proc, False)
                except KeyboardInterrupt:
                    # forward SIGINT and get return value
                    proc.send_signal(signal.SIGINT.value)
                    retval = proc.wait()
                    raise
            else:
                retval = proc.wait()
        except BaseException as e:
            # subprocces.run would send the SIGKILL signal to ffmpeg and the
            # mp4 file couldn't be played, but if we ask ffmpeg to quit it
            # produces a file that is playable (this is mostly useful for live
            # streams). Note that Windows is not affected and produces playable
            # files (see https://github.com/ytdl-org/youtube-dl/issues/8300).
            if isinstance(e, KeyboardInterrupt) and live:
                retval = 0
            if isinstance(e, KeyboardInterrupt) and sys.platform != 'win32' and url not in ('-', 'pipe:'):
                proc.communicate_or_kill(b'q')
            else:
                proc.kill()
                proc.wait()
                raise
        return retval


class AVconvFD(FFmpegFD):
    pass


_BY_NAME = {
    klass.get_basename(): klass
    for name, klass in globals().items()
    if name.endswith('FD') and name not in ('ExternalFD', 'FragmentFD')
}

_BY_EXE = {klass.EXE_NAME: klass for klass in _BY_NAME.values()}


def list_external_downloaders():
    return sorted(_BY_NAME.keys())


def get_external_downloader(external_downloader):
    """ Given the name of the executable, see whether we support the given
        downloader . """
    # Drop .exe extension on Windows
    bn = os.path.splitext(os.path.basename(external_downloader))[0]
    return _BY_NAME.get(bn, _BY_EXE.get(bn))
