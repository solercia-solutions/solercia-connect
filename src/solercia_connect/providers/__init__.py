from solercia_connect.providers.bitbucket import BitbucketAdapter
from solercia_connect.providers.glpi import GlpiAdapter
from solercia_connect.providers.google import GoogleCalendarAdapter, GoogleGmailAdapter
from solercia_connect.providers.google_meet import GoogleMeetAdapter
from solercia_connect.providers.jira import JiraAdapter
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.providers.search_console import GoogleSearchConsoleAdapter

__all__ = [
    "BitbucketAdapter",
    "GlpiAdapter",
    "GoogleCalendarAdapter",
    "GoogleGmailAdapter",
    "GoogleMeetAdapter",
    "GoogleSearchConsoleAdapter",
    "JiraAdapter",
    "MicrosoftGraphAdapter",
]
