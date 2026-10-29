"""Opt-in OS-vault credential reuse; never fall back to a plaintext file."""
import hashlib
import os


def launch_token(root, remember=False, *, token=None, service='nexus-hud'):
    token = token if token is not None else os.environ.get('NEXUS_HUD_TOKEN', '')
    if token and not remember:
        return token
    import keyring
    if type(keyring.get_keyring()).__module__ not in {
        'keyring.backends.SecretService', 'keyring.backends.macOS', 'keyring.backends.Windows'
    }:
        raise ValueError('A native OS credential vault is required')
    account = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    if remember:
        if not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('Set a valid local access token before remembering it')
    try:
        if remember:
            keyring.set_password(service, account, token)
            return token
        return keyring.get_password(service, account) or ''
    except Exception:
        raise ValueError('Native OS credential vault unavailable; supply the configured local access token explicitly') from None
