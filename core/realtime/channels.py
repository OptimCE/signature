"""THE ONLY PLACE A REALTIME CHANNEL STRING IS BUILT (Python side).

This is a security control, not a style rule. A producer that accidentally
publishes a per-user thing onto a community tier is a cross-tenant leak, and
``tier`` below is a required argument with no default precisely so that mistake
cannot be made by omission. A test greps each service for the literal
``notify:v1:`` outside this module and fails on a hit.

Grammar (fixed arity, so Redis 6 ACLs can later be granted per prefix without a
redesign) — byte-identical to
``crm-backend/src/shared/realtime/realtime.channels.ts``::

    notify:v1:u:{internal_app_user_id}
    notify:v1:c:{internal_community_id}:{MEMBER|MANAGER}

Ids are the INTERNAL integer keys (``app_user.id``, ``community.id``) — what
every producer here already holds, and what ``notification.id_user`` is. They are
NOT Keycloak subs or org uuids.

The community family is what lets a worker with NO user attribution at all (the
generation and simulation jobs carry only ``id_community``) address exactly the
right audience with zero database lookups. Its safety comes from the subscribe
side: crm-backend only ever subscribes a connection to tiers the ticket mint
proved the user holds, from gateway-verified claims.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

_PREFIX = "notify:v1"


class Tier(str, Enum):
    """Channel tiers. Deliberately coarser than a role: there is no ADMIN bucket."""

    #: Everyone in the community, managers included.
    MEMBER = "MEMBER"
    #: ADMIN and MANAGER only.
    MANAGER = "MANAGER"


def user_channel(internal_user_id: int) -> str:
    """Channel for one user, in every community and outside all of them."""
    return f"{_PREFIX}:u:{internal_user_id}"


def community_channel(internal_community_id: int, tier: Tier) -> str:
    """Channel for one tier of one community."""
    return f"{_PREFIX}:c:{internal_community_id}:{tier.value}"


@dataclass(frozen=True)
class UserAudience:
    """One recipient, addressed by internal ``app_user.id``."""

    user_id: int

    def channels(self) -> Sequence[str]:
        return (user_channel(self.user_id),)


@dataclass(frozen=True)
class UsersAudience:
    """An explicit set of recipients. Duplicates are collapsed."""

    user_ids: Iterable[int]

    def channels(self) -> Sequence[str]:
        return tuple(user_channel(uid) for uid in dict.fromkeys(self.user_ids))


@dataclass(frozen=True)
class CommunityAudience:
    """One tier of one community.

    ``Tier.MANAGER`` reaches ADMIN and MANAGER only; ``Tier.MEMBER`` reaches
    everyone in the community, managers included — a manager's connection
    subscribes to both tiers, so "everyone" is one publish, not two.
    """

    community_id: int
    tier: Tier

    def channels(self) -> Sequence[str]:
        return (community_channel(self.community_id, self.tier),)


Audience = UserAudience | UsersAudience | CommunityAudience
