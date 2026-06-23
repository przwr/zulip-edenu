import logging
import zoneinfo
from email.utils import format_datetime as email_format_datetime
from typing import Any

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.utils.timezone import get_current_timezone_name as timezone_get_current_timezone_name
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from ua_parser import parse_os, parse_user_agent

from confirmation.models import one_click_unsubscribe_link
from zerver.lib.queue import queue_json_publish_rollback_unsafe
from zerver.lib.send_email import FromAddress
from zerver.lib.timestamp import format_datetime_to_string
from zerver.lib.timezone import canonicalize_timezone
from zerver.models import UserProfile
from zerver.models.custom_profile_fields import CustomProfileFieldValue

JUST_CREATED_THRESHOLD = 60


def get_device_browser(user_agent: str) -> str | None:
    if "zulip" in user_agent.lower():
        return "Zulip"

    if browser := parse_user_agent(user_agent):
        browser_family = browser.family
        if browser_family == "IE":
            return "Internet Explorer"
        elif browser_family != "Other":
            return browser_family

    return None


def get_device_os(user_agent: str) -> str | None:
    if os := parse_os(user_agent):
        os_family = os.family
        if os_family == "Mac OS X":
            return "macOS"
        elif os_family != "Other":
            return os_family
    return None


@receiver(user_logged_in, dispatch_uid="only_on_login")
def email_on_new_login(sender: Any, user: UserProfile, request: Any, **kwargs: Any) -> None:
    if not user.enable_login_emails:
        return

    if user.delivery_email == "":
        # Do not attempt to send new login emails for users without an email address.
        # The assertions here are to help document the only circumstance under which
        # this condition should be possible.
        assert (
            user.realm.demo_organization_scheduled_deletion_date is not None and user.is_realm_owner
        )
        return

    # We import here to minimize the dependencies of this module,
    # since it runs as part of `manage.py` initialization
    from zerver.context_processors import common_context

    if not settings.SEND_LOGIN_EMAILS:
        return

    if request:
        # If the user's account was just created, avoid sending an email.
        if (timezone_now() - user.date_joined).total_seconds() <= JUST_CREATED_THRESHOLD:
            return

        user_agent = request.headers.get("User-Agent", "")

        context = common_context(user)
        context["user_email"] = user.delivery_email
        user_tz = user.timezone
        if user_tz == "":
            user_tz = timezone_get_current_timezone_name()
        local_time = timezone_now().astimezone(zoneinfo.ZoneInfo(canonicalize_timezone(user_tz)))
        context["login_time"] = format_datetime_to_string(local_time, user.twenty_four_hour_time)
        context["device_ip"] = request.META.get("REMOTE_ADDR") or _("Unknown IP address")
        context["device_os"] = get_device_os(user_agent) or _("an unknown operating system")
        context["device_browser"] = get_device_browser(user_agent) or _("An unknown browser")
        context["unsubscribe_link"] = one_click_unsubscribe_link(user, "login")

        email_dict = {
            "template_prefix": "zerver/emails/notify_new_login",
            "to_user_ids": [user.id],
            "from_name": FromAddress.security_email_from_name(user_profile=user),
            "from_address": FromAddress.NOREPLY,
            "context": context,
            "date": email_format_datetime(local_time),
        }
        queue_json_publish_rollback_unsafe("email_senders", email_dict)


@receiver(user_logged_in, dispatch_uid="refresh_avatar_on_login")
def refresh_avatar_on_login(sender: Any, **kwargs: Any) -> None:
    # PORTAL EDENU: refresh this user's Authentik avatar + rank badge shortly
    # after login, so a changed picture shows within seconds rather than the
    # next hourly cron tick. Fire-and-forget daemon thread: the sync reads the
    # Authentik media dir off disk and must not block the login response.
    # Reuses the same sync_user_avatar() the cron uses.
    # ponytail: unbounded threads under a login storm is the ceiling; fine at
    # this org size — upgrade to queue_json_publish("email_senders"-style)
    # worker if logins ever scale.
    # Production-only: gated off in tests (PORTAL_EDENU defaults False) so the
    # daemon thread never races with assert_database_query_count blocks.
    if not settings.PORTAL_EDENU:
        return
    user = kwargs.get("user")
    if user is None or getattr(user, "is_bot", False):
        return  # nocoverage

    from threading import Thread

    from django.db import connection

    def _sync() -> None:
        try:
            # Deferred import: signals.py loads at Django startup.
            from zerver.management.commands.sync_avatars_from_authentik import (
                get_admin_user,
                get_ranks_for_users,
                sync_user_avatar,
            )

            # No-op for users without a Profilowe avatar (service accounts, etc.)
            # without raising DoesNotExist inside sync_user_avatar.
            has_avatar = CustomProfileFieldValue.objects.filter(
                field__name="Profilowe", user_profile=user
            ).exclude(value="")
            if not has_avatar.exists():
                return

            rank = get_ranks_for_users([user]).get(user.id, "Normal")  # nocoverage
            sync_user_avatar(user, admin_user=get_admin_user(), rank=rank)  # nocoverage
        except Exception:  # nocoverage
            # Never break login: log and move on.
            logging.getLogger("avatar_sync").exception(
                "login-triggered avatar sync failed for %s", getattr(user, "delivery_email", "?")
            )
        finally:
            # Threads own their DB connection; return it to the pool.
            connection.close()

    Thread(target=_sync, name="avatar-sync-on-login", daemon=True).start()


@receiver(user_logged_out)
def clear_call_tokens_on_logout(
    sender: object, *, user: UserProfile | None, **kwargs: object
) -> None:
    # Loaded lazily so django.setup() succeeds before static asset generation
    from zerver.actions.video_calls import do_set_video_call_provider_token

    if user is not None:
        if user.third_party_api_state.get("zoom") is not None:
            do_set_video_call_provider_token(user, "zoom", None)
        if user.third_party_api_state.get("webex") is not None:
            do_set_video_call_provider_token(user, "webex", None)
