"""Main entry point for the ShadowPI moderation bot."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram import Chat, ChatPermissions, Message, Update
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .cas import CasClient
from .config import Settings, contains_blacklisted, format_attribution
from .database import Database
from .risk import RiskAssessment, RiskScorer, detect_link
from .sweep import run_member_sweep

logger = logging.getLogger(__name__)


def _patrol_enabled(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.bot_data.get("patrol_enabled", True))


def _set_patrol_state(context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    context.bot_data["patrol_enabled"] = enabled
    db: Database = context.bot_data["db"]
    db.set_flag(PATROL_STATE_KEY, enabled)

PATROL_STATE_KEY = "patrol_enabled"
ACTIVATION_STATE_KEY = "pin_activated"


def _pending_dm_requests(context: ContextTypes.DEFAULT_TYPE) -> dict[int, dict[str, Any]]:
    return context.bot_data.setdefault("pending_dm_requests", {})


def _user_is_deleted(user: Any) -> bool:
    if not user:
        return False
    if getattr(user, "is_deleted", False):
        return True
    return user.first_name == "Deleted Account" and not user.username


def _is_forwarded(message: Message) -> bool:
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
    )


def _message_type(message: Message, *, contains_link: bool, forwarded: bool) -> str:
    if forwarded:
        return "forward"
    if contains_link:
        return "link"
    if message.photo or message.video or message.document or message.animation:
        return "media"
    if message.sticker or message.voice or message.video_note or message.audio:
        return "media"
    return "text"


def _resolve_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    message = update.effective_message
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    if message and message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


def _bot_activated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.bot_data.get("activated", False))


def _set_activation_state(context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> None:
    context.bot_data["activated"] = enabled
    db: Database = context.bot_data["db"]
    db.set_flag(ACTIVATION_STATE_KEY, enabled)


async def _ensure_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _bot_activated(context):
        return True
    await update.effective_message.reply_text(
        "ShadowPI is locked. An admin must run /activate <pin>."
    )
    return False


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:  # pragma: no cover - network failures
        return False
    return member.status in ("administrator", "creator")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    attribution = format_attribution(settings)
    text = (
        "ShadowPI keeps watch over joins and live chat using CAS + behavior scoring.\n"
        "- Auto-check every join against CAS in real time.\n"
        "- Periodically refreshes the CAS export.csv into a local watchlist.\n"
        "- Tracks per-user activity to mute/ban flooders proactively."
    )
    if attribution:
        text += f"\n\n{attribution}"
    if not _bot_activated(context):
        text += "\n\nStatus: LOCKED — admins must /activate <pin>."
    await update.effective_message.reply_text(text)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_active(update, context):
        return
    db: Database = context.bot_data["db"]
    summary = db.counts_summary()
    text = (
        "ShadowPI totals:\n"
        f"- Users observed: {summary.get('total_users', 0)}\n"
        f"- Messages processed: {summary.get('total_messages', 0)}\n"
        f"- Warnings issued: {summary.get('total_warnings', 0)}\n"
        f"- Messages deleted: {summary.get('total_deletes', 0)}\n"
        f"- Watchlist size: {db.watchlist_size()}"
    )
    await update.effective_message.reply_text(text)


async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if _bot_activated(context):
        await update.effective_message.reply_text("ShadowPI is already unlocked.")
        return
    chat = update.effective_chat
    if not chat or chat.type == Chat.PRIVATE:
        await update.effective_message.reply_text(
            "Use /activate inside the target group so I can DM you a pin prompt."
        )
        return

    pending = _pending_dm_requests(context)
    pending[update.effective_user.id] = {
        "type": "pin",
        "chat_id": chat.id,
    }
    await update.effective_message.reply_text(
        "Check your DM—reply with the pin to unlock ShadowPI."
    )
    try:
        await context.bot.send_message(
            update.effective_user.id,
            "ShadowPI unlock requested. Reply to this message with the activation pin.",
        )
    except TelegramError as exc:
        logger.warning("Failed to DM activation pin request: %s", exc)
        await update.effective_message.reply_text(
            "I can't DM you. Please /start me in a private chat and try again."
        )


async def lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not _bot_activated(context):
        await update.effective_message.reply_text("ShadowPI is already locked.")
        return
    _set_activation_state(context, False)
    await update.effective_message.reply_text("ShadowPI locked. All automation paused until /activate.")
    await _notify_mods(
        context,
        f"{_username(update.effective_user)} locked ShadowPI.",
        fallback_chat=update.effective_chat.id if update.effective_chat else None,
    )


async def import_roster_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    chat = update.effective_chat
    if not chat or chat.type == Chat.PRIVATE:
        await update.effective_message.reply_text("Use /import_roster inside the group chat.")
        return

    pending = _pending_dm_requests(context)
    pending[update.effective_user.id] = {
        "type": "roster",
        "chat_id": chat.id,
    }
    await update.effective_message.reply_text("Check your DM for roster import instructions.")
    try:
        await context.bot.send_message(
            update.effective_user.id,
            "Send newline-separated entries like `123456789 @username Full Name`.\n"
            "Each line must include at least the numeric Telegram user ID. Optional @username and name help the risk scorer.",
        )
    except TelegramError as exc:
        logger.warning("Failed to DM roster instructions: %s", exc)
        await update.effective_message.reply_text(
            "I can't DM you. Please /start me privately and try again."
        )


async def _handle_dm_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != Chat.PRIVATE:
        return
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text or message.text.startswith("/"):
        return
    pending = _pending_dm_requests(context)
    request = pending.get(user.id)
    if not request:
        return

    chat_id = request.get("chat_id")
    req_type = request.get("type")
    settings: Settings = context.bot_data["settings"]

    if req_type == "pin":
        pin = message.text.strip()
        if pin != settings.activation_pin:
            await message.reply_text("Incorrect pin. Try again.")
            return

        pending.pop(user.id, None)
        if _bot_activated(context):
            await message.reply_text("ShadowPI is already unlocked.")
            return
        _set_activation_state(context, True)
        await message.reply_text("Correct pin. ShadowPI is now unlocked.")
        if chat_id:
            await context.bot.send_message(
                chat_id,
                f"{_username(user)} unlocked ShadowPI via DM. Use /patrol to resume enforcement.",
            )
            await _notify_mods(
                context,
                f"{_username(user)} unlocked ShadowPI.",
                fallback_chat=chat_id,
            )
        return

    if req_type == "roster":
        lines = [line.strip() for line in message.text.splitlines() if line.strip()]
        if not lines:
            await message.reply_text(
                "Send newline-separated entries like `123456789 @user Full Name`."
            )
            return
        db: Database = context.bot_data["db"]
        added = 0
        skipped: list[str] = []
        now_ts = int(time.time())
        for line in lines:
            tokens = line.replace(",", " ").split()
            user_id = None
            username = None
            remainder: list[str] = []
            for token in tokens:
                if user_id is None and token.lstrip("+-").isdigit():
                    try:
                        user_id = int(token)
                        continue
                    except ValueError:  # pragma: no cover
                        pass
                if username is None and token.startswith("@"):  # username
                    username = token.lstrip("@")
                    continue
                remainder.append(token)
            if not user_id:
                skipped.append(line)
                continue
            full_name = " ".join(remainder) if remainder else None
            db.record_user_seen(
                user_id,
                username,
                chat_id,
                now_ts,
                full_name=full_name,
                is_deleted=False,
            )
            added += 1

        pending.pop(user.id, None)
        summary = f"Imported {added} members."
        if skipped:
            summary += f" Skipped {len(skipped)} lines."
        await message.reply_text(summary)
        if skipped:
            await message.reply_text("Skipped lines:\n" + "\n".join(skipped[:5]))
        if chat_id:
            await context.bot.send_message(
                chat_id,
                f"{_username(user)} imported {added} roster entries.",
            )


async def cascheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /cascheck <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return

    cas_client: CasClient = context.bot_data["cas"]
    result = await cas_client.check_user(user_id)
    if result.should_ban:
        message = f"User {user_id} is CAS banned ({result.reason or 'no reason provided'})."
    elif result.ok:
        message = f"User {user_id} is not currently CAS banned."
    else:
        message = f"CAS lookup failed for {user_id}."
    await update.effective_message.reply_text(message)


async def _apply_override(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: command <user_id> [note]")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return
    note = " ".join(context.args[1:]) if len(context.args) > 1 else None
    db: Database = context.bot_data["db"]
    db.set_override(user_id, action, note)
    await update.effective_message.reply_text(
        f"Override stored for {user_id}: {action}"
    )


async def override_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _apply_override(update, context, "allow")


async def override_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _apply_override(update, context, "ban")


async def override_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /override_clear <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User ID must be numeric.")
        return
    db: Database = context.bot_data["db"]
    db.clear_override(user_id)
    await update.effective_message.reply_text(f"Cleared override for {user_id}.")


async def _toggle_patrol(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool,
) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    current = _patrol_enabled(context)
    if current == enabled:
        state = "Patrol is already active." if enabled else "Patrol already stood down."
        await update.effective_message.reply_text(state)
        return
    _set_patrol_state(context, enabled)
    if enabled:
        text = "Patrol mode enabled. ShadowPI will resume proactive enforcement."
    else:
        text = "Patrol mode disabled. Use /suspect on replies for manual checks."
    await update.effective_message.reply_text(text)
    actor = _username(update.effective_user)
    action = "enabled patrol" if enabled else "entered standdown"
    await _notify_mods(
        context,
        f"{actor} {action} mode.",
        fallback_chat=update.effective_chat.id if update.effective_chat else None,
    )


async def patrol_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_patrol(update, context, True)


async def standdown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_patrol(update, context, False)


async def suspect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    message = update.effective_message
    if not message or not message.reply_to_message:
        await update.effective_message.reply_text(
            "Reply to a suspicious message and use /suspect."
        )
        return
    target = message.reply_to_message
    assessment = await _process_message(
        target,
        context,
        manual=True,
        fallback_chat_id=target.chat_id,
    )
    if not assessment:
        await update.effective_message.reply_text("Unable to analyze that message.")
        return
    if assessment.actions:
        action_text = ", ".join(assessment.actions)
    else:
        action_text = "no automatic action"
    reasons = ", ".join(assessment.reasons) if assessment.reasons else "No risk factors."
    await update.effective_message.reply_text(
        f"Manual score {assessment.score}; actions: {action_text}. Reasons: {reasons}"
    )


async def shadowban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    user_id = _resolve_target_user_id(update, context)
    if not user_id:
        await update.effective_message.reply_text(
            "Provide a user ID or reply to a user to shadowban."
        )
        return
    db: Database = context.bot_data["db"]
    db.set_shadowban(user_id, True)
    await update.effective_message.reply_text(
        f"Shadowbanned user {user_id}. All future messages will auto-delete."
    )


async def shadowlift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    user_id = _resolve_target_user_id(update, context)
    if not user_id:
        await update.effective_message.reply_text(
            "Provide a user ID or reply to lift a shadowban."
        )
        return
    db: Database = context.bot_data["db"]
    db.set_shadowban(user_id, False)
    await update.effective_message.reply_text(f"Shadowban lifted for {user_id}.")


async def sweep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not await _ensure_active(update, context):
        return
    chat = update.effective_chat
    if not chat:
        return
    mode = (context.args[0].lower() if context.args else "report")
    if mode not in {"report", "clean"}:
        mode = "report"
    limit: int | None = None
    if len(context.args) > 1:
        try:
            limit = int(context.args[1])
        except ValueError:
            limit = None
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]

    progress = await update.effective_message.reply_text(
        f"Sweeping members ({mode})… this may take a while."
    )

    async def ban_member(user_id: int, reason: str) -> None:
        try:
            await context.bot.ban_chat_member(chat.id, user_id)
            await _notify_mods(
                context,
                f"Sweep {mode}: removed {user_id} ({reason})",
                fallback_chat=chat.id,
            )
        except TelegramError as exc:
            logger.warning("Sweep ban failed for %s: %s", user_id, exc)

    async def shadowban_member(user_id: int) -> None:
        db.set_shadowban(user_id, True)
        await _notify_mods(
            context,
            f"Sweep {mode}: shadowbanned {user_id}",
            fallback_chat=chat.id,
        )

    stats = await run_member_sweep(
        chat.id,
        chat.title or str(chat.id),
        db=db,
        settings=settings,
        mode=mode,
        limit=limit,
        ban_callback=ban_member if mode != "report" else None,
        shadowban_callback=shadowban_member if mode != "report" else None,
    )

    await progress.edit_text(stats.as_text())


async def _notify_mods(context: ContextTypes.DEFAULT_TYPE, text: str, *, fallback_chat: int | None = None) -> None:
    settings: Settings = context.bot_data["settings"]
    target = settings.mod_log_chat_id or fallback_chat
    if not target:
        logger.info("MOD LOG: %s", text)
        return
    try:
        await context.bot.send_message(target, text)
    except TelegramError as exc:  # pragma: no cover - network failures
        logger.warning("Failed to send mod log message: %s", exc)


def _username(user: Any) -> str:
    if not user:
        return "unknown"
    if user.username:
        return f"@{user.username}"
    if user.full_name:
        return user.full_name
    return str(user.id)


async def _enforce_actions(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    assessment: RiskAssessment,
) -> None:
    if not message or not message.from_user:
        return
    chat_id = message.chat_id
    user_id = message.from_user.id
    db: Database = context.bot_data["db"]
    actions = assessment.actions
    if not actions:
        return

    if "delete" in actions:
        try:
            await message.delete()
            db.increment_counters(user_id, deletions=1)
        except TelegramError:
            logger.debug("Message delete failed for %s", user_id)

    if "warn" in actions:
        warning = (
            "Please slow down—your activity triggered ShadowPI's anti-spam filters."
        )
        await message.reply_text(warning, quote=False)
        db.increment_counters(user_id, warnings=1)
        db.set_local_trust(user_id, "watch")

    if "mute" in actions:
        mute_seconds = 600
        until = int(time.time() + mute_seconds)
        permissions = ChatPermissions(can_send_messages=False)
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=permissions,
                until_date=until,
            )
            await _notify_mods(
                context,
                f"Muted {_username(message.from_user)} for spam score {assessment.score}",
                fallback_chat=chat_id,
            )
            db.set_local_trust(user_id, "muted")
        except TelegramError as exc:  # pragma: no cover - perms
            logger.warning("Mute failed: %s", exc)

    if "ban" in actions:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await _notify_mods(
                context,
                f"Banned {_username(message.from_user)} (score {assessment.score})",
                fallback_chat=chat_id,
            )
            db.set_local_trust(user_id, "banned")
        except TelegramError as exc:  # pragma: no cover - perms
            logger.warning("Ban failed: %s", exc)


async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not update.effective_chat:
        return
    if not _bot_activated(context):
        return
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    cas_client: CasClient = context.bot_data["cas"]
    settings: Settings = context.bot_data["settings"]
    timestamp = int(message.date.timestamp()) if message.date else int(time.time())

    patrol_active = _patrol_enabled(context)

    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        profile = db.record_user_seen(
            member.id,
            member.username,
            chat_id,
            timestamp,
            full_name=member.full_name,
            is_deleted=_user_is_deleted(member),
        )
        override = db.get_override(member.id)
        if override and override.get("action") == "ban":
            try:
                await context.bot.ban_chat_member(chat_id, member.id)
            except TelegramError as exc:
                logger.warning("Failed to apply override ban: %s", exc)
            continue

        cas_result = await cas_client.check_user(member.id)
        status = "banned" if cas_result.should_ban else "clean"
        db.update_cas_status(member.id, status)

        if cas_result.should_ban:
            if patrol_active:
                try:
                    await context.bot.ban_chat_member(chat_id, member.id)
                    await _notify_mods(
                        context,
                        f"CAS auto-ban: {_username(member)} ({cas_result.reason or 'no reason'})",
                        fallback_chat=chat_id,
                    )
                    db.set_local_trust(member.id, "banned")
                except TelegramError as exc:
                    logger.warning("Failed to auto-ban CAS hit: %s", exc)
            else:
                await _notify_mods(
                    context,
                    f"Standdown: CAS flagged {_username(member)} ({cas_result.reason or 'no reason'})",
                    fallback_chat=chat_id,
                )
            continue

        if not patrol_active:
            continue

        newbie_until = timestamp + settings.newbie_link_block_seconds
        db.set_newbie_until(member.id, newbie_until)
        if settings.newbie_link_block_seconds:
            await context.bot.send_message(
                chat_id,
                f"Welcome {_username(member)}! Links are locked for the first "
                f"{settings.newbie_link_block_seconds // 60} minutes while CAS clears.",
            )


async def _process_message(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    manual: bool,
    fallback_chat_id: int | None = None,
) -> RiskAssessment | None:
    if not message or not message.from_user or message.from_user.is_bot:
        return None

    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    risk: RiskScorer = context.bot_data["risk"]

    timestamp = int(message.date.timestamp()) if message.date else int(time.time())
    chat_id = message.chat_id
    profile = db.record_user_seen(
        message.from_user.id,
        message.from_user.username,
        chat_id,
        timestamp,
        full_name=message.from_user.full_name,
        is_deleted=_user_is_deleted(message.from_user),
    )

    if profile.get("shadowbanned"):
        try:
            await message.delete()
            db.increment_counters(message.from_user.id, deletions=1)
        except TelegramError:
            logger.debug("Shadowban delete failed for %s", message.from_user.id)
        if not manual:
            return None

    contains_link = detect_link(message)
    contains_blacklist = False
    content_parts = [message.text or "", message.caption or ""]
    for part in content_parts:
        if not part:
            continue
        if contains_blacklisted(part, settings.blacklisted_keywords) or contains_blacklisted(
            part,
            settings.blacklisted_domains,
        ):
            contains_blacklist = True
            break

    forwarded = _is_forwarded(message)
    db.increment_counters(
        message.from_user.id,
        messages=1,
        links=1 if contains_link else 0,
        forwards=1 if forwarded else 0,
    )

    msg_type = _message_type(message, contains_link=contains_link, forwarded=forwarded)
    db.record_first_message(
        message.from_user.id,
        timestamp,
        msg_type,
        forwarded,
    )

    if not manual and not _bot_activated(context):
        return None

    if not manual and not _patrol_enabled(context):
        return None

    newbie_restricted = bool(
        profile.get("newbie_until") and profile["newbie_until"] > timestamp
    )
    cas_banned = profile.get("cas_status") == "banned"
    watchlist_reason = db.in_watchlist(message.from_user.id)

    assessment = risk.evaluate(
        message,
        cas_banned=cas_banned,
        watchlist_reason=watchlist_reason,
        newbie_restricted=newbie_restricted,
        contains_link=contains_link,
        contains_blacklist=contains_blacklist,
    )

    if not assessment.actions:
        return assessment if manual else None

    reason_text = ", ".join(assessment.reasons)
    mode_label = "Manual /suspect" if manual else "Patrol"
    await _notify_mods(
        context,
        f"{mode_label} score {assessment.score} for {_username(message.from_user)}: {reason_text}",
        fallback_chat=fallback_chat_id or chat_id,
    )
    await _enforce_actions(context, message, assessment)
    return assessment


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not update.effective_chat:
        return
    await _process_message(
        message,
        context,
        manual=False,
        fallback_chat_id=update.effective_chat.id,
    )


async def refresh_cas_watchlist_job(context: CallbackContext) -> None:
    db: Database = context.job.data["db"]
    cas_client: CasClient = context.job.data["cas"]
    entries = await cas_client.fetch_bulk_user_ids()
    if not entries:
        return
    added = db.upsert_watchlist(entries, "cas_export")
    logger.info("CAS export sync complete (%d rows)", added)


def build_application(settings: Settings) -> Application:
    db = Database(settings.database_path)
    cas_client = CasClient(
        base_url=settings.cas_api_base,
        export_url=settings.cas_bulk_url,
        timeout=settings.http_timeout_seconds,
    )
    risk = RiskScorer(settings)
    patrol_enabled = db.get_flag(PATROL_STATE_KEY, True)
    activated = db.get_flag(ACTIVATION_STATE_KEY, False)

    request = HTTPXRequest(http_version="1.1", read_timeout=settings.http_timeout_seconds)
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .request(request)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["cas"] = cas_client
    application.bot_data["risk"] = risk
    application.bot_data["patrol_enabled"] = patrol_enabled
    application.bot_data["activated"] = activated

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("activate", activate_command))
    application.add_handler(CommandHandler("lock", lock_command))
    application.add_handler(CommandHandler("import_roster", import_roster_command))
    application.add_handler(CommandHandler("cascheck", cascheck_command))
    application.add_handler(CommandHandler("allow", override_allow))
    application.add_handler(CommandHandler("banlocal", override_ban))
    application.add_handler(CommandHandler("override_clear", override_clear))
    application.add_handler(CommandHandler("patrol", patrol_command))
    application.add_handler(CommandHandler("standdown", standdown_command))
    application.add_handler(CommandHandler("suspect", suspect_command))
    application.add_handler(CommandHandler("shadowban", shadowban_command))
    application.add_handler(CommandHandler("shadowlift", shadowlift_command))
    application.add_handler(CommandHandler("sweep", sweep_command))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            _handle_dm_message,
        )
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member)
    )
    application.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, handle_message)
    )

    refresh_interval = settings.cas_export_refresh_minutes * 60
    application.job_queue.run_repeating(
        refresh_cas_watchlist_job,
        interval=refresh_interval,
        first=5,
        data={"db": db, "cas": cas_client},
    )

    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    application = build_application(settings)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        cas_client = application.bot_data.get("cas")
        if isinstance(cas_client, CasClient):
            try:
                asyncio.run(cas_client.close())
            except RuntimeError:
                pass
        db = application.bot_data.get("db")
        if isinstance(db, Database):
            db.close()


if __name__ == "__main__":
    main()
