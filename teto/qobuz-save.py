#!/usr/bin/python3
"""Keep complete copies of Qobuz tracks played in the user's Strawberry session."""
import argparse
import concurrent.futures
import configparser
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import requests

HOME = Path.home()
PROFILE = Path(os.environ.get('STRAWBERRY_TETO_PROFILE', str(HOME / '.local/share/strawberry-teto/config/strawberry/strawberry.conf')))
STATE = Path(os.environ.get('STRAWBERRY_TETO_SAVE_STATE', str(HOME / '.local/state/strawberry-qobuz-save')))
DEST = Path(os.environ.get('STRAWBERRY_TETO_SAVE_DIR', str(HOME / 'Music/Qobuz')))
API = 'https://www.qobuz.com/api.json/0.2/'
BUS_NAME = 'org.mpris.MediaPlayer2.strawberry'
BUS_PATH = '/org/mpris/MediaPlayer2'
PLAYER = 'org.mpris.MediaPlayer2.Player'
RESERVE = 1024 ** 3
MAX_FILE = 2 * 1024 ** 3
log = logging.getLogger('qobuz-save')


class SaveError(Exception):
    pass


def safe_name(value):
    value = re.sub(r'[\x00-\x1f\x7f/\\]', '_', str(value)).strip(' .')
    # Limit UTF-8 bytes, not code points, for filesystem component limits.
    return value.encode('utf-8')[:120].decode('utf-8', 'ignore') or 'Unknown'


def settings():
    c = configparser.ConfigParser(interpolation=None)
    c.read(PROFILE)
    if not c.has_section('Qobuz'):
        raise SaveError('No Qobuz configuration')
    q = c['Qobuz']
    if not all(q.get(k) for k in ('app_id', 'app_secret', 'user_auth_token')):
        raise SaveError('Sign in to Qobuz in Strawberry first')
    return dict(q)


def api_get(session, q, method, params):
    # Match Strawberry's authenticated requests. Credentials never enter logs.
    with session.get(API + method, params={**params, 'app_id': q['app_id']},
                     headers={'X-App-Id': q['app_id'],
                              'X-User-Auth-Token': q['user_auth_token']},
                     timeout=(15, 60)) as reply:
        if reply.status_code != 200:
            raise SaveError(f'Qobuz {method}: HTTP {reply.status_code}')
        data = reply.json()
        if not isinstance(data, dict) or data.get('status') == 'error':
            raise SaveError(f'Qobuz {method}: request rejected')
        return data


def stream_params(q, track_id):
    params = {'format_id': q.get('format', '27'), 'intent': 'stream', 'track_id': track_id}
    timestamp = str(int(time.time()))
    message = 'trackgetFileUrl' + ''.join(k + params[k] for k in sorted(params))
    signature = hashlib.md5((message + timestamp + q['app_secret']).encode()).hexdigest()
    return {**params, 'request_ts': timestamp, 'request_sig': signature,
            'user_auth_token': q['user_auth_token']}


def saved_path(track_id, state=STATE):
    try:
        data = json.loads((state / f'{track_id}.json').read_text())
        path = Path(data['path'])
        if path.is_file() and path.stat().st_size == data['size']:
            return path
    except (OSError, ValueError, KeyError):
        pass
    return None


def download(session, url, path):
    if not url.startswith('https://'):
        raise SaveError('Refusing a non-HTTPS audio URL')
    # No account headers are sent to the audio CDN.
    with session.get(url, stream=True, timeout=(15, 60)) as reply:
        if reply.status_code != 200:
            raise SaveError(f'Audio server: HTTP {reply.status_code}')
        length = int(reply.headers.get('Content-Length', '0'))
        if length > MAX_FILE or shutil.disk_usage(path.parent).free < max(length * 2, RESERVE) + RESERVE:
            raise SaveError('Insufficient free space or track exceeds 2 GiB limit')
        size = 0
        with path.open('xb') as output:
            for block in reply.iter_content(256 * 1024):
                size += len(block)
                if size > MAX_FILE:
                    raise SaveError('Track exceeds 2 GiB limit')
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if not size or (length and size != length):
            raise SaveError('Incomplete audio download')


def probe(path):
    result = subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-show_format',
                             '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise SaveError('Downloaded file is not valid audio')
    data = json.loads(result.stdout)
    audio = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if not audio or audio.get('codec_name') not in ('flac', 'mp3'):
        raise SaveError('Unexpected audio format')
    return data, audio


def write_record(state, track_id, record):
    temp = state / f'{track_id}.json.tmp'
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    temp.replace(state / f'{track_id}.json')


def needs_cover(track_id, state=STATE):
    try:
        record = json.loads((state / f'{track_id}.json').read_text())
        return not (record.get('cover_embedded') and Path(record.get('cover_path', '')).is_file())
    except (OSError, ValueError):
        return True


def check_cover(path):
    result = subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, timeout=30)
    if result.returncode:
        raise SaveError('Invalid album cover image')
    streams = json.loads(result.stdout).get('streams', [])
    if not streams or streams[0].get('codec_name') not in ('mjpeg', 'png'):
        raise SaveError('Album cover must be JPEG or PNG')
    if not (0 < streams[0].get('width', 0) <= 10000 and 0 < streams[0].get('height', 0) <= 10000):
        raise SaveError('Invalid album cover dimensions')
    return '.jpg' if streams[0]['codec_name'] == 'mjpeg' else '.png'


def ensure_cover(track_id, state=STATE, session=None, track=None):
    target = saved_path(track_id, state)
    if not target:
        raise SaveError('Saved audio missing or changed; leaving it untouched')
    if not needs_cover(track_id, state):
        return target
    if session is None:
        with requests.Session() as own_session:
            return ensure_cover(track_id, state, own_session, track)
    record = json.loads((state / f'{track_id}.json').read_text())
    with tempfile.TemporaryDirectory(prefix='artwork-', dir=state) as work:
        cover = next((target.parent / name for name in ('cover.jpg', 'cover.png')
                      if (target.parent / name).is_file()), None)
        if cover:
            check_cover(cover)
        else:
            if track is None:
                track = api_get(session, settings(), 'track/get', {'track_id': track_id})
            images = (track.get('album') or {}).get('image') or {}
            url = next((images[k] for k in ('large', 'small', 'thumbnail') if images.get(k)), None)
            if not url or not url.startswith('https://'):
                raise SaveError('No HTTPS album cover available')
            image = Path(work) / 'cover.image'
            with session.get(url, stream=True, timeout=(15, 45)) as reply:
                if reply.status_code != 200:
                    raise SaveError(f'Album cover: HTTP {reply.status_code}')
                expected = int(reply.headers.get('Content-Length', '0'))
                if expected > 20 * 1024 ** 2:
                    raise SaveError('Album cover exceeds 20 MiB limit')
                size = 0
                with image.open('xb') as output:
                    for block in reply.iter_content(65536):
                        size += len(block)
                        if size > 20 * 1024 ** 2:
                            raise SaveError('Album cover exceeds 20 MiB limit')
                        output.write(block)
                if not size or (expected and size != expected):
                    raise SaveError('Incomplete album cover download')
            cover = target.parent / ('cover' + check_cover(image))
            os.link(image, cover)
        # Remux without re-encoding audio. Keep text tags and attach the front cover.
        output = Path(work) / ('with-cover' + target.suffix)
        command = ['/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-i', str(target),
                   '-i', str(cover), '-map', '0:a:0', '-map', '1:v:0',
                   '-map_metadata', '0', '-c', 'copy', '-disposition:v:0', 'attached_pic',
                   '-metadata:s:v:0', 'title=Album cover',
                   '-metadata:s:v:0', 'comment=Cover (front)']
        if target.suffix == '.mp3':
            command += ['-id3v2_version', '3']
        result = subprocess.run(command + [str(output)], capture_output=True, timeout=180)
        if result.returncode:
            raise SaveError('Could not embed album cover; original audio kept')
        data, _ = probe(output)
        if not any(s.get('disposition', {}).get('attached_pic') for s in data['streams']):
            raise SaveError('Embedded cover verification failed; original audio kept')
        output.chmod(target.stat().st_mode & 0o777)
        output.replace(target)
        record.update(size=target.stat().st_size, cover_path=str(cover), cover_embedded=True)
        write_record(state, track_id, record)
        log.info('Saved and embedded album cover: %s', target.name)
        return target


def save_track(track_id, state=STATE, dest=DEST):
    if not re.fullmatch(r'\d+', track_id):
        raise SaveError('Invalid Qobuz track ID')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = saved_path(track_id, state)
    if existing:
        return ensure_cover(track_id, state)
    q = settings()
    with requests.Session() as session:
        track = api_get(session, q, 'track/get', {'track_id': track_id})
        stream = api_get(session, q, 'track/getFileUrl', stream_params(q, track_id))
        if str(stream.get('track_id')) != track_id or not stream.get('url'):
            raise SaveError('Qobuz returned no matching stream')
        if stream.get('sample') or stream.get('is_sample'):
            raise SaveError('Only a preview is available; no permanent copy saved')
        album = track.get('album') or {}
        album_artist = (album.get('artist') or {}).get('name', 'Unknown Artist')
        artist = (track.get('performer') or {}).get('name') or album_artist
        title = track.get('title', f'Track {track_id}')
        if track.get('version'):
            title += f" ({track['version']})"
        album_title = album.get('title', 'Unknown Album')
        number = int(track.get('track_number') or 0)
        disc = int(track.get('media_number') or 1)
        expected_duration = float(track.get('duration') or 0)
        log.info('Saving %s — %s [Qobuz %s]', artist, title, track_id)
        with tempfile.TemporaryDirectory(prefix='pending-', dir=state) as work:
            raw = Path(work) / 'audio.part'
            download(session, stream['url'], raw)
            data, audio = probe(raw)
            duration = float(data.get('format', {}).get('duration') or audio.get('duration') or 0)
            if duration <= 0 or (expected_duration and duration < expected_duration - 3):
                raise SaveError('Incomplete track or preview; no permanent copy saved')
            ext = 'flac' if audio['codec_name'] == 'flac' else 'mp3'
            tagged = Path(work) / f'tagged.{ext}'
            metadata = {'title': title, 'artist': artist, 'album_artist': album_artist,
                        'album': album_title, 'track': str(number), 'disc': str(disc),
                        'date': str(album.get('release_date_original') or ''),
                        'genre': (album.get('genre') or {}).get('name', ''),
                        'copyright': track.get('copyright', ''),
                        'comment': f'Qobuz track {track_id}'}
            command = ['/usr/bin/ffmpeg', '-nostdin', '-v', 'error', '-i', str(raw),
                       '-map', '0:a:0', '-c:a', 'copy']
            for key, value in metadata.items():
                if value:
                    command += ['-metadata', f'{key}={value}']
            result = subprocess.run(command + [str(tagged)], capture_output=True, timeout=180)
            if result.returncode:
                raise SaveError('Could not tag the downloaded audio')
            probe(tagged)
            directory = dest / safe_name(album_artist) / safe_name(f'{album_title} [{album.get("id", "unknown")}]')
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f'{disc:02d}-{number:02d} {safe_name(title)} [{track_id}].{ext}'
            # A complete, tagged file appears atomically in the watched collection.
            # Never overwrite an existing user file.
            if target.exists():
                raise SaveError(f'File already exists without a save record: {target.name}')
            os.link(tagged, target)
            record = {'track_id': track_id, 'path': str(target), 'size': target.stat().st_size,
                      'duration': duration, 'codec': audio['codec_name'],
                      'sample_rate': audio.get('sample_rate'), 'saved_at': int(time.time())}
            write_record(state, track_id, record)
            log.info('Saved %s', target)
            return ensure_cover(track_id, state, session, track)


def current_player(bus):
    from gi.repository import Gio, GLib
    result = bus.call_sync(BUS_NAME, BUS_PATH, 'org.freedesktop.DBus.Properties',
                           'GetAll', GLib.Variant('(s)', (PLAYER,)), None,
                           Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
    metadata = result.get('Metadata', {})
    match = re.fullmatch(r'qobuz:(\d+)', metadata.get('xesam:url', ''))
    return (match.group(1) if match else None), result.get('PlaybackStatus')


def watch(bus):
    from gi.repository import GLib
    attempts, pending, retry_at = {}, {}, {}
    missing_since = None
    log.info('Watching Strawberry. Permanent files: %s', DEST)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
        while True:
            if (STATE / 'disabled').exists():
                log.info('Automatic saving disabled; finishing queued downloads')
                return
            for track_id, future in list(pending.items()):
                if not future.done():
                    continue
                del pending[track_id]
                try:
                    future.result()
                except Exception as error:
                    # requests exceptions can include signed URLs; never log them.
                    reason = str(error) if isinstance(error, SaveError) else type(error).__name__
                    log.error('Track %s: %s', track_id, reason)
                    retry_at[track_id] = time.monotonic() + 60
            try:
                track_id, status = current_player(bus)
                missing_since = None
            except GLib.Error:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > 60 and not pending:
                    log.info('Strawberry closed; saver exiting')
                    return
                time.sleep(2)
                continue
            if (track_id and status == 'Playing' and track_id not in pending
                    and attempts.get(track_id, 0) < 3
                    and time.monotonic() >= retry_at.get(track_id, 0)
                    and (not saved_path(track_id) or needs_cover(track_id))):
                attempts[track_id] = attempts.get(track_id, 0) + 1
                pending[track_id] = worker.submit(save_track, track_id)
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--current', action='store_true', help='Save current Qobuz track, even if paused')
    parser.add_argument('--disable', action='store_true', help='Disable automatic saving')
    parser.add_argument('--enable', action='store_true', help='Enable automatic saving on Strawberry launch')
    parser.add_argument('--backfill-covers', action='store_true', help='Download and embed covers for previously saved tracks')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.disable:
        (STATE / 'disabled').touch()
        print('Automatic Qobuz saving disabled. Existing files are kept.')
        return
    if args.enable:
        (STATE / 'disabled').unlink(missing_ok=True)
        print('Automatic Qobuz saving enabled for Strawberry launches.')
        return
    if not (args.watch or args.current or args.backfill_covers):
        parser.error('Choose --watch, --current or --backfill-covers')
    if args.watch and (STATE / 'disabled').exists():
        return
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(STATE / 'saver.log', maxBytes=1_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    from gi.repository import Gio
    with (STATE / 'watch.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.current or args.backfill_covers:
                raise SaveError('Saver is running; disable it temporarily before manual updates')
            return
        if args.backfill_covers:
            failures = 0
            for record in sorted(STATE.glob('*.json')):
                if not record.stem.isdigit():
                    continue
                try:
                    print(ensure_cover(record.stem), flush=True)
                except Exception as error:
                    reason = str(error) if isinstance(error, SaveError) else type(error).__name__
                    log.error('Cover for %s: %s', record.stem, reason)
                    print(f'Cover for {record.stem}: {reason}', flush=True)
                    failures += 1
            if failures:
                raise SaveError(f'{failures} cover update(s) failed')
            return
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if args.current:
            track_id, _ = current_player(bus)
            if not track_id:
                raise SaveError('Current Strawberry track is not from Qobuz')
            print(save_track(track_id))
        else:
            watch(bus)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        reason = str(error) if isinstance(error, SaveError) else type(error).__name__
        log.error('%s', reason)
        raise SystemExit(reason)
