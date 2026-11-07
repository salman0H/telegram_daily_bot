"""Daily vocabulary review from the user's committed, offline-first word bank."""
from datetime import datetime
import json
import logging
from pathlib import Path
import re
from urllib.parse import quote
from src import config
from src.http_client import request_json
from src.news import safe_url
from src.writer import plain_field

LOGGER = logging.getLogger(__name__)
WORD_BANK = Path(__file__).resolve().parents[1] / 'data' / 'vocabulary.json'
STOP_WORDS = set('a an the of to and or in for on with by is are be as that which something someone do have make very'.split())


def load_entries(path=WORD_BANK):
    with Path(path).open(encoding='utf-8') as file:
        data = json.load(file)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
        raise ValueError('Invalid vocabulary bank')
    entries, seen_ids = [], set()
    for entry in data['entries']:
        if (not isinstance(entry, dict) or type(entry.get('id')) is not int or entry['id'] in seen_ids
                or not isinstance(entry.get('word'), str) or not entry['word'].strip()
                or not isinstance(entry.get('results'), list) or not entry['results']
                or not isinstance(entry['results'][0], dict)
                or not isinstance(entry['results'][0].get('definition'), str)):
            raise ValueError('Invalid vocabulary entry')
        seen_ids.add(entry['id'])
        entries.append(entry)
    return entries


def select_daily(entries, now=None, count=5):
    day = (now or datetime.now(config.LOCAL_TZ)).astimezone(config.LOCAL_TZ).date().toordinal()
    unique = {}
    for entry in entries:
        unique.setdefault(entry['word'].strip().casefold(), entry)
    pool = list(unique.values())
    if not pool:
        return []
    offset = (day * count) % len(pool)
    return (pool[offset:] + pool[:offset])[:count]


def _tokens(text):
    return set(re.findall(r'[a-z]+', text.casefold())) - STOP_WORDS


def dictionary_details(entry):
    """Only single words are looked up; the user's meaning is never overwritten."""
    if not config.ENABLE_VOCABULARY_DICTIONARY or not re.fullmatch(r'[A-Za-z]+(?:-[A-Za-z]+)?', entry['word']):
        return {}
    url = 'https://api.dictionaryapi.dev/api/v2/entries/en/' + quote(entry['word'].lower(), safe='')
    try:
        response = request_json(url, timeout=5, attempts=1)
        if not isinstance(response, list):
            return {}
        for result in response:
            if not isinstance(result, dict) or result.get('word', '').casefold() != entry['word'].casefold():
                continue
            details = {'source': url}
            ipa = plain_field(result.get('phonetic', ''), 100)
            for phonetic in result.get('phonetics') or []:
                if not isinstance(phonetic, dict):
                    continue
                ipa = ipa or plain_field(phonetic.get('text', ''), 100)
                audio = phonetic.get('audio', '')
                if isinstance(audio, str) and audio.startswith('//'):
                    audio = 'https:' + audio
                audio = safe_url(audio)
                if audio and 'audio' not in details:
                    details['audio'] = audio
            if ipa:
                details['ipa'] = ipa
            # Conservative lexical overlap: avoid examples from a clearly different sense.
            source_tokens = _tokens(entry['results'][0]['definition'])
            best_score, example = 0, ''
            for meaning in result.get('meanings') or []:
                for definition in meaning.get('definitions') or []:
                    tokens = _tokens(definition.get('definition', ''))
                    overlap = len(tokens & source_tokens)
                    score = overlap / max(1, len(source_tokens))
                    candidate = plain_field(definition.get('example', ''), 400)
                    if candidate and overlap >= 2 and score >= 0.25 and score > best_score:
                        best_score, example = score, candidate
            if example:
                details['example'] = example
            return details
    except Exception as exc:
        LOGGER.info('Dictionary enrichment unavailable (%s)', type(exc).__name__)
    return {}


def render_card(entry, index, total, details=None):
    details = details or {}
    result = entry['results'][0]
    word = plain_field(entry['word'], 180)
    meaning = plain_field(entry.get('translation'), 400) or 'در فهرست شما ثبت نشده است.'
    pos = plain_field(result.get('partOfSpeech'), 180) or 'در فهرست ثبت نشده'
    synonyms = ' · '.join(plain_field(item, 120) for item in result.get('synonyms', []) if isinstance(item, str))
    blocks = [f'📘 **مرور واژگان · {index} از {total}**\n\n**{word}**\n{pos}',
              f'🇮🇷 **معنی فارسی**\n{meaning}',
              f'📖 **English definition**\n{plain_field(result["definition"], 900)}',
              f'🔁 **Synonyms / Equivalents**\n{synonyms or "در فهرست ثبت نشده"}']
    local_ipa = entry.get('pronunciation')
    local_ipa = plain_field(local_ipa.get('all', ''), 100) if isinstance(local_ipa, dict) else ''
    ipa = local_ipa or details.get('ipa')
    if ipa:
        blocks.append(f'🗣 **Pronunciation (IPA)**\n{ipa}')
    if details.get('audio'):
        blocks.append('🔊 تلفظ صوتی\n' + details['audio'])
    local_examples = result.get('examples') or []
    examples = [plain_field(example, 400) for example in local_examples[:2] if isinstance(example, str)]
    if examples:
        blocks.append('✍️ **Examples · فهرست شما**\n' + '\n'.join(examples))
    elif details.get('example'):
        blocks.append('✍️ **Example · فرهنگ لغت**\n' + details['example'])
    syllables = entry.get('syllables')
    if isinstance(syllables, dict) and type(syllables.get('count')) is int and 0 < syllables['count'] <= 20:
        parts = syllables.get('list') or []
        parts = ' · '.join(plain_field(part, 80) for part in parts if isinstance(part, str))
        blocks.append(f'🔤 **Syllables**\n{syllables["count"]}' + (f' · {parts}' if parts else ''))
    frequency = entry.get('frequency')
    if type(frequency) in (int, float) and 0 <= frequency <= 100:
        blocks.append(f'📊 **Frequency · مقدار ثبت‌شده در فهرست**\n{frequency:g}')
    blocks.append('🧠 **تمرین یادآوری**\nمعنی را بپوشانید؛ واژه را به خاطر بیاورید و با همین معنا یک جمله بسازید.')
    blocks.append(f'فهرست شخصی شما · ردیف {entry["id"]}')
    if details.get('ipa') or details.get('audio') or details.get('example'):
        blocks.append('دادهٔ تکمیلی: Free Dictionary API\n' + details['source'])
    return {'text': '\n\n'.join(blocks), 'image_url': ''}


def get_vocabulary_cards(now=None):
    if not config.ENABLE_VOCABULARY:
        return []
    selected = select_daily(load_entries(), now, config.VOCABULARY_PER_DAY)
    return [render_card(entry, index, len(selected), dictionary_details(entry))
            for index, entry in enumerate(selected, 1)]
