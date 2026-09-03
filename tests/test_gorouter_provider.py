import json

from utils.config import AccountConfig, AppConfig, ProviderConfig, load_accounts_config


def test_builtin_gorouter_provider_config(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()
	gorouter = config.get_provider('gorouter')

	assert gorouter is not None
	assert gorouter.domain == 'https://gorouter.app'
	assert gorouter.sign_in_path == '/api/user/checkin'
	assert gorouter.user_info_path == '/api/user/self'
	assert gorouter.turnstile is True
	assert gorouter.bypass_method is None
	assert gorouter.use_proxy is False


def test_builtin_old_providers_unchanged(monkeypatch):
	"""旧版 provider 行为不得受 gorouter 接入影响"""
	monkeypatch.delenv('PROVIDERS', raising=False)

	config = AppConfig.load_from_env()

	anyrouter = config.get_provider('anyrouter')
	assert anyrouter.sign_in_path == '/api/user/sign_in'
	assert anyrouter.api_user_key == 'new-api-user'
	assert anyrouter.needs_waf_cookies() is True
	assert anyrouter.turnstile is False
	assert anyrouter.persist_profile is True

	agentrouter = config.get_provider('agentrouter')
	assert agentrouter.sign_in_path is None
	assert agentrouter.needs_manual_check_in() is False
	assert agentrouter.needs_waf_cookies() is True
	assert agentrouter.turnstile is False


def test_gorouter_can_be_overridden_via_providers_env(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps({'gorouter': {'domain': 'https://self-hosted.example.com', 'turnstile': False}}),
	)

	config = AppConfig.load_from_env()
	gorouter = config.get_provider('gorouter')

	assert gorouter.domain == 'https://self-hosted.example.com'
	assert gorouter.turnstile is False
	assert gorouter.sign_in_path == '/api/user/checkin'  # 未覆盖的字段继承内置默认


def test_provider_from_dict_turnstile_inheritance():
	defaults = ProviderConfig(name='custom', domain='https://old.example.com', turnstile=True)

	provider = ProviderConfig.from_dict('custom', {'domain': 'https://new.example.com'}, defaults=defaults)

	assert provider.turnstile is True
	assert provider.turnstile_site_key is None


def test_provider_from_dict_turnstile_site_key_override(monkeypatch):
	monkeypatch.setenv(
		'PROVIDERS',
		json.dumps({'gorouter': {'domain': 'https://gorouter.app', 'turnstile_site_key': '0xABC'}}),
	)

	config = AppConfig.load_from_env()

	assert config.get_provider('gorouter').turnstile_site_key == '0xABC'


def test_access_token_account_without_api_user_or_cookies(monkeypatch):
	"""PAT 账号：无需 api_user 与 cookies"""
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'name': 'GoRouter', 'provider': 'gorouter', 'access_token': 'token123'}]),
	)

	accounts = load_accounts_config()

	assert accounts is not None
	assert len(accounts) == 1
	assert accounts[0].access_token == 'token123'
	assert accounts[0].has_access_token() is True
	assert accounts[0].has_login_credentials() is False


def test_legacy_cookie_account_still_requires_api_user(monkeypatch):
	"""旧行为保持不变：session cookies 账号仍必须提供 api_user"""
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'provider': 'gorouter', 'cookies': {'session': 'abc'}}]),
	)

	assert load_accounts_config() is None


def test_legacy_cookie_account_with_api_user_still_loads(monkeypatch):
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'cookies': {'session': 'abc'}, 'api_user': '12345'}]),
	)

	accounts = load_accounts_config()

	assert accounts is not None
	assert accounts[0].api_user == '12345'
	assert accounts[0].has_access_token() is False


def test_empty_access_token_is_ignored(monkeypatch):
	"""access_token 为空串等同于未配置，不能绕过校验"""
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'provider': 'gorouter', 'access_token': '', 'cookies': {'session': 'abc'}}]),
	)

	# cookies 存在但没有 api_user -> 仍按旧规则报错
	assert load_accounts_config() is None


def test_account_from_dict_parses_access_token():
	account = AccountConfig.from_dict({'access_token': 'tok', 'provider': 'gorouter'}, 0)

	assert account.access_token == 'tok'
