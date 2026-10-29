"""Shared native WG_Panel Telegram card presentation; no credentials or IO."""
import re
from html import escape
from urllib.parse import urlsplit


def safe_peer_name(peer_id, name):
    raw = str(name or f'peer-{peer_id}').strip()
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', raw).strip('._')[:100] or f'peer-{peer_id}'


def bundle_keyboard(peer_id):
    return {'inline_keyboard': [
        [{'text': '↻ Refresh', 'callback_data': f'peer:bundle:{int(peer_id)}'},
         {'text': '◇ Edit', 'callback_data': f'peer:edit:{int(peer_id)}'}],
        [{'text': '← Back', 'callback_data': 'peers:menu'}]]}


def _length(value):
    return len(value.encode('utf-16-le')) // 2


def peer_card_caption(name, config, short_url, qr=False):
    """Native two-card caption, conservatively bounded including HTML markup.

    Only the preview is shortened. The downloadable configuration is untouched.
    """
    title = str(name or 'Peer')[:100]
    url = str(short_url or '')
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme in {'http', 'https'} and bool(parsed.netloc) and _length(escape(url)) <= 700
    except ValueError:
        valid_url = False
    if not valid_url:
        url = 'Unavailable'
    heading = ('▦', 'QR code', 'Scan with a WireGuard client') if qr else ('◇', 'Configuration', 'WireGuard client profile')
    prefix = f'{heading[0]} <b>{escape(title)} · {heading[1]}</b>\n<i>{heading[2]}</i>\n\n'
    suffix = f'\n⌁ <b>Short link</b>\n{escape(url)}'
    config = config.decode("utf-8", errors="replace") if isinstance(config, bytes) else str(config)
    def render(preview):
        return prefix + '<pre><code>' + escape(preview) + '</code></pre>' + suffix
    if _length(render(config)) <= 1024:
        return render(config)
    # Extremely escaped names/links must not push the fixed frame over the limit.
    if _length(render('\n…')) > 1024:
        title = title[:32]
        prefix = f'{heading[0]} <b>{escape(title)} · {heading[1]}</b>\n<i>{heading[2]}</i>\n\n'
    if _length(render('\n…')) > 1024:
        suffix = '\n⌁ <b>Short link</b>\nUnavailable'
    low, high = 0, len(config)
    while low < high:
        mid = (low + high + 1) // 2
        if _length(render(config[:mid] + '\n…')) <= 1024:
            low = mid
        else:
            high = mid - 1
    return render(config[:low].rstrip() + '\n…')
