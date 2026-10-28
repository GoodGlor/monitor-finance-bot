"""Materialise the Google credential files from environment variables.

A host with an ephemeral filesystem has nowhere durable to keep `creds/`, and
the directory is gitignored so it is not in the deployed slug either. The two
Google credential files are therefore passed as config vars and written to
disk at startup, before the OAuth client reads them.

This is sound for the *token* specifically because `auth_service` never writes
a refreshed token back on the success path - it re-refreshes from the stored
refresh_token on every run. The materialised token file is effectively
read-only, so nothing is lost when the dyno's disk is discarded.

Local machines set neither variable: an existing file is never overwritten, so
running from a laptop keeps using the real `creds/` directory untouched.

Nothing here is ever logged beyond a variable NAME. The values are an OAuth
client secret and a refresh token.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

# env var -> filename inside the creds directory
CREDENTIAL_VARS = {
    'GOOGLE_CREDENTIALS_JSON': 'credentials.json',
    'GOOGLE_TOKEN_JSON': 'token.json',
}


def materialize_credentials(creds_dir: str) -> list:
    """Write any credential config vars to files. Returns the names written.

    Never raises: a bad or absent variable is logged and skipped so the caller
    can fall through to whatever is already on disk and produce a useful error
    of its own.
    """
    written = []
    for var, filename in CREDENTIAL_VARS.items():
        raw = os.getenv(var)
        if not raw:
            continue

        target = os.path.join(creds_dir, filename)
        if os.path.exists(target):
            # A real file on disk wins: on a laptop the config var is usually
            # absent, but if both exist the local file is the one being
            # actively refreshed and must not be clobbered.
            logger.info(f'{filename} already exists; ignoring {var}')
            continue

        try:
            # Parse before writing: a truncated paste should fail here, with a
            # clear message, rather than inside the OAuth library later.
            json.loads(raw)
        except ValueError:
            logger.error(f'{var} is not valid JSON; ignoring it')
            continue

        try:
            os.makedirs(creds_dir, exist_ok=True)
            with open(target, 'w') as fh:
                fh.write(raw)
        except OSError as error:
            logger.error(f'Could not write {filename} ({error.__class__.__name__}); ignoring {var}')
            continue

        written.append(filename)

    if written:
        logger.info(f'Materialised {len(written)} credential file(s) from the environment')
    return written
