from __future__ import annotations


class CollectorDomainError(Exception):
    """Base class for expected production-collector failures."""


class CooldownActive(CollectorDomainError):
    pass


class RequestBudgetExhausted(CollectorDomainError):
    pass


class WeChatRateLimited(CollectorDomainError):
    pass


class WeChatSessionExpired(CollectorDomainError):
    pass


class WeChatProtocolChanged(CollectorDomainError):
    pass


class WeChatNetworkFailed(CollectorDomainError):
    pass


class WeChatAccessBlocked(CollectorDomainError):
    """WeChat refused the operation for entitlement/permission reasons (not transient).

    e.g. ``/cgi-bin/appmsgpublish`` returning ``ret=200013`` for an unverified
    subscription account: the "list another account's articles" capability is not
    granted, and it stays blocked no matter how long we wait.
    """
