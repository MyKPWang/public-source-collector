class CollectorError(Exception):
    """Base error for expected collection failures."""


class MissingCredentials(CollectorError):
    pass


class RunInProgress(CollectorError):
    pass


class CooldownActive(CollectorError):
    def __init__(self, until: int):
        self.until = until
        super().__init__(f"account is cooling down until {until}")


class WeChatRateLimited(CollectorError):
    pass


class WeChatSessionExpired(CollectorError):
    pass


class WeChatResponseError(CollectorError):
    pass


class WeChatAccessBlocked(CollectorError):
    """Operation refused for entitlement/permission reasons; not transient.

    ``/cgi-bin/appmsgpublish`` returns ``ret=200013`` for accounts without the
    "list another account's articles" capability (e.g. an unverified personal
    subscription account). The block persists across arbitrarily long waits, so it
    must not be treated as a rate limit that a cooldown would eventually clear.
    """
