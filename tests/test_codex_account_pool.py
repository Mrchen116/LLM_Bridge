import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import token_auth


def test_codex_store_migrates_legacy_single_account(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('.codex_oauth.json').write_text(
        json.dumps(
            {
                'codex_oauth': {
                    'access_token': 'legacy-access',
                    'refresh_token': 'legacy-refresh',
                    'expires_at': 4102444800,
                    'account_id': 'org-legacy',
                    'updated_at': 1,
                }
            }
        ),
        encoding='utf-8',
    )

    status = asyncio.run(token_auth.list_codex_accounts())
    assert status['default_label'] == 'primary'
    assert len(status['accounts']) == 1
    assert status['accounts'][0]['label'] == 'primary'


def test_codex_pool_marks_429_and_switches_to_backup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('.codex_oauth.json').write_text(
        json.dumps(
            {
                'schema_version': 2,
                'default_label': 'primary',
                'accounts': [
                    {
                        'label': 'primary',
                        'account_id': 'org-primary',
                        'priority': 100,
                        'enabled': True,
                        'access_token': 'token-primary',
                        'refresh_token': 'refresh-primary',
                        'expires_at': 4102444800,
                        'cooldown_until': 0,
                        'last_error': '',
                        'updated_at': 1,
                    },
                    {
                        'label': 'backup',
                        'account_id': 'org-backup',
                        'priority': 200,
                        'enabled': True,
                        'access_token': 'token-backup',
                        'refresh_token': 'refresh-backup',
                        'expires_at': 4102444800,
                        'cooldown_until': 0,
                        'last_error': '',
                        'updated_at': 1,
                    },
                ],
            }
        ),
        encoding='utf-8',
    )

    profile = {
        'provider': 'codex_oauth',
        'auth': {'accountPoolPolicy': {'cooldownSeconds': 300}},
    }

    first = asyncio.run(token_auth.get_codex_upstream_headers(profile))
    assert first['Authorization'] == 'Bearer token-primary'

    asyncio.run(
        token_auth.mark_codex_account_rate_limited(
            headers=first,
            status_code=429,
            error_text='rate limited',
            profile=profile,
        )
    )

    second = asyncio.run(token_auth.get_codex_upstream_headers(profile))
    assert second['Authorization'] == 'Bearer token-backup'


def test_codex_pool_switch_priority_remove_enable_disable_by_label(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('.codex_oauth.json').write_text(
        json.dumps(
            {
                'schema_version': 2,
                'default_label': 'a',
                'accounts': [
                    {
                        'label': 'a',
                        'account_id': 'org-a',
                        'priority': 100,
                        'enabled': True,
                        'access_token': 'token-a',
                        'refresh_token': 'refresh-a',
                        'expires_at': 4102444800,
                        'cooldown_until': 0,
                        'last_error': '',
                        'updated_at': 1,
                    },
                    {
                        'label': 'b',
                        'account_id': 'org-b',
                        'priority': 200,
                        'enabled': True,
                        'access_token': 'token-b',
                        'refresh_token': 'refresh-b',
                        'expires_at': 4102444800,
                        'cooldown_until': 0,
                        'last_error': '',
                        'updated_at': 1,
                    },
                ],
            }
        ),
        encoding='utf-8',
    )

    switched = asyncio.run(token_auth.switch_codex_default_account('b'))
    assert switched['label'] == 'b'

    disabled = asyncio.run(token_auth.set_codex_account_enabled('b', False))
    assert disabled['enabled'] is False

    enabled = asyncio.run(token_auth.set_codex_account_enabled('b', True))
    assert enabled['enabled'] is True

    updated_priority = asyncio.run(token_auth.set_codex_account_priority('b', 50))
    assert updated_priority['priority'] == 50

    ordered = asyncio.run(token_auth.list_codex_accounts())
    assert [item['label'] for item in ordered['accounts']] == ['b', 'a']

    removed = asyncio.run(token_auth.remove_codex_account('a'))
    assert removed['label'] == 'a'

    state = asyncio.run(token_auth.list_codex_accounts())
    assert state['default_label'] == 'b'
    assert [item['label'] for item in state['accounts']] == ['b']
