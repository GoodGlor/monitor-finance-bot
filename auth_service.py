import os
import sys
from creds_bootstrap import materialize_credentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.discovery import HttpError
from google.auth.exceptions import RefreshError
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

load_dotenv()


class AuthService:
    def __init__(self):
        self._prj_path = str(os.path.dirname(__file__))

        self.spreadsheet_id = os.getenv('SPREADSHEET_ID')
        self._scopes = ('https://www.googleapis.com/auth/gmail.modify',
                        'https://www.googleapis.com/auth/spreadsheets')

        self.service_gmail = None
        self.service_sheets = None

        self._login()

    @staticmethod
    def _interactive_login_possible() -> bool:
        """Whether a browser-based OAuth flow could actually complete here.

        `run_local_server` opens a port and blocks until a browser hits the
        redirect. With no terminal there is no browser and no one to click,
        so it blocks forever - and under `flock` a wedged run holds the lock
        and silently kills every scheduled run after it.
        """
        return sys.stdin.isatty()

    def _login(self):

        creds = None
        # Config vars first: on a host with an ephemeral filesystem this is the
        # only way the credential files exist at all.
        materialize_credentials(os.path.join(self._prj_path, 'creds'))

        token_path = os.path.join(self._prj_path, 'creds/token.json')
        credentials_path = os.path.join(self._prj_path, 'creds/credentials.json')

        if os.path.exists(token_path):
            try:
                creds = Credentials.from_authorized_user_file(token_path, self._scopes)
                creds.refresh(Request())
            except RefreshError as error:
                logger.error(f'An error occurred: {error}')
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.warning('Token expired')
                creds.refresh(Request())
                logger.info('Token refreshed')
            else:
                if not self._interactive_login_possible():
                    # Fail loudly instead of hanging. The scheduler logs this,
                    # the process exits, and the next run gets a clean shot.
                    raise RuntimeError(
                        'Google credentials are missing or expired and no browser is '
                        'available to re-authorise. Run this project on a machine with '
                        'a terminal to regenerate creds/token.json, then redeploy it '
                        '(locally: copy the file across; on a hosted runner: set the '
                        'GOOGLE_TOKEN_JSON config var to its contents). If this keeps '
                        'recurring weekly, the Google OAuth consent screen is still in '
                        'Testing mode, which expires refresh tokens after 7 days.')
                logger.info('Need to loging through Google')
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, self._scopes)
                creds = flow.run_local_server(port=0)
            # Use the absolute path so a refreshed token is written next to the
            # project regardless of the current working directory (e.g. cron).
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
                logger.info('New cred created')

        try:
            self.service_gmail = build(serviceName='gmail', version='v1', credentials=creds)
            labels_result = self.service_gmail.users().labels().list(userId='me').execute().get('labels', [])
            logger.info(f'Service gmail created and test labels found {len(labels_result)}')

            self.service_sheets = build(serviceName='sheets', version='v4', credentials=creds)
            sheet = self.service_sheets.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id,
                                                                    range='data!A:A').execute()
            logger.info(f'Service sheet created and test values found {len(sheet.get("values") or [])}')

        except HttpError as error:
            logger.error(f'Error: {error}')

        finally:
            logger.info('Login Successful')


if __name__ == '__main__':
    google_service = AuthService()
    # print(google_service.get_emails())
