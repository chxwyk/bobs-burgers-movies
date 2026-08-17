from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from .config import ConfigError, Settings
from .database import ActiveOrderExistsError, Database
from .models import GuildSettings, MovieOrder, PaymentMethod
from .pricing import (
    InputError,
    format_cents,
    parse_money,
    parse_seats,
    percentage_share_cents,
    validate_zip_code,
)
from .transcripts import (
    TranscriptAttachment,
    TranscriptMessage,
    render_transcript_html,
    save_transcript,
)

LOGGER = logging.getLogger("oxy_movies")
BRAND_AVATAR_PATH = Path(__file__).parent / "assets" / "bobs-burgers-direct-movies-pfp.png"
BRAND_AVATAR_STATE_KEY = "brand_avatar_sha256"
PURPLE = discord.Color.from_rgb(145, 70, 255)
SUCCESS = discord.Color.from_rgb(46, 204, 113)
WARNING = discord.Color.from_rgb(241, 196, 15)
ERROR = discord.Color.from_rgb(231, 76, 60)
COMPLETE_TICKET_RETENTION = timedelta(seconds=5)
DONE_TICKET_RETENTION = timedelta(hours=1)
SCHEDULED_CLOSE_RETRY_SECONDS = 5 * 60
KNOWN_PAYMENT_METHODS = (
    "Cash App",
    "Apple Pay",
    "Zelle",
    "Venmo",
    "PayPal",
    "Stripe Payment Link",
    "Cryptocurrency",
)


def _safe_channel_fragment(value: str) -> str:
    fragment = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return fragment[:22] or "customer"


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_administrator(member: discord.Member | discord.User) -> bool:
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def _is_staff(member: discord.Member | discord.User, settings: GuildSettings) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator or any(
        role.id == settings.staff_role_id for role in member.roles
    )


async def _ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "content": content,
        "embed": embed,
        "view": view,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _configured_settings(
    bot: MovieOrdersBot, interaction: discord.Interaction
) -> GuildSettings | None:
    if interaction.guild_id is None:
        await _ephemeral(interaction, "This can only be used inside the Discord server.")
        return None
    settings = await bot.db.get_guild_settings(interaction.guild_id)
    if settings is None:
        await _ephemeral(
            interaction,
            "The movie bot has not been configured. An administrator must run `/setup`.",
        )
    return settings


async def _ticket_order(
    bot: MovieOrdersBot, interaction: discord.Interaction
) -> MovieOrder | None:
    if interaction.channel_id is None:
        await _ephemeral(interaction, "Use this inside a movie order ticket.")
        return None
    order = await bot.db.get_order_by_channel(interaction.channel_id)
    if order is None:
        await _ephemeral(interaction, "This channel is not a movie order ticket.")
    return order


def _panel_embed(settings: GuildSettings, orders_open: bool) -> discord.Embed:
    status = (
        "🟢 **MOVIE ORDERS ARE OPEN** — New movie orders are being accepted."
        if orders_open
        else "🔴 **MOVIE ORDERS ARE CLOSED** — New orders are temporarily paused."
    )
    embed = discord.Embed(
        title=f"🎬 {settings.brand_name}",
        description=(
            f"{status}\n\n"
            "Get **50% off movie tickets** at supported theaters. Build your cart on "
            "the theater's official checkout page, then use **Order Movie** below.\n\n"
            "Upload a clear screenshot showing the complete cart and final checkout total "
            "inside your private ticket. **Do not place the order yourself.**"
        ),
        color=SUCCESS if orders_open else ERROR,
    )
    embed.add_field(
        name="💸 50% Off",
        value="You pay exactly half of the verified final checkout total.",
        inline=True,
    )
    embed.add_field(
        name="💵 Order Limit",
        value="Final checkout total must be **$40–$250**.",
        inline=True,
    )
    embed.add_field(
        name="🎟️ Tickets",
        value="Receive your movie ticket QR codes securely inside your private ticket.",
        inline=False,
    )
    embed.add_field(
        name="🎭 Theater Requests",
        value="Request any theater; Movie Staff confirms availability before payment.",
        inline=False,
    )
    embed.add_field(
        name="🍿 Food & Drinks",
        value="Snack and drink orders are available for **AMC orders only**.",
        inline=False,
    )
    embed.set_footer(text="Never share passwords, card numbers, or account login codes.")
    if settings.banner_url and _valid_http_url(settings.banner_url):
        embed.set_image(url=settings.banner_url)
    return embed


def _how_it_works_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🍿 How Movie Orders Work",
        description="Three quick steps—your Movie Staff member handles the rest.",
        color=PURPLE,
    )
    embed.add_field(
        name="1 — Build your cart and take a screenshot",
        value=(
            "Use the theater's official website or app. Pick the movie, theater, date, "
            "showtime, seats, and any eligible AMC snacks. Continue until the **entire final "
            "checkout total** is visible, then take a clear screenshot. Do not submit payment."
        ),
        inline=False,
    )
    embed.add_field(
        name="2 — Open a private movie ticket",
        value=(
            "Choose **Order Movie**, complete the short form, and upload your full cart "
            "screenshot in the ticket. Final totals must be between **$40 and $250**."
        ),
        inline=False,
    )
    embed.add_field(
        name="3 — Pay half and receive your tickets",
        value=(
            "Movie Staff verifies the cart, sends your exact 50%-off invoice, and provides "
            "your QR ticket codes. AMC snack orders include pickup information when available."
        ),
        inline=False,
    )
    embed.add_field(
        name="Important",
        value="Availability can change. Never pay until Movie Staff verifies your screenshot.",
        inline=False,
    )
    return embed


def _order_summary_embed(order: MovieOrder) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎟️ Movie Order #{order.id:06d}",
        description=(
            "Upload a **full screenshot of the theater checkout page** showing the movie, "
            "showtime, seats, snacks, and final total. Movie Staff will verify it before payment."
        ),
        color=PURPLE,
    )
    embed.add_field(name="Movie & Showtime", value=order.movie_showtime[:1024], inline=False)
    embed.add_field(name="ZIP Code", value=order.zip_code, inline=True)
    embed.add_field(name="Seats", value=str(order.seats), inline=True)
    embed.add_field(name="Snacks", value=order.snacks[:1024], inline=False)
    embed.add_field(
        name="Submitted Final Total",
        value=format_cents(order.submitted_total_cents),
        inline=True,
    )
    embed.add_field(
        name="Estimated 50%-Off Price",
        value=f"**{format_cents(order.customer_price_cents)}**",
        inline=True,
    )
    embed.set_footer(text="Food and drinks are available for AMC orders only.")
    return embed


def _invoice_embed(order: MovieOrder) -> discord.Embed:
    embed = discord.Embed(
        title=f"💳 Invoice • Movie Order #{order.id:06d}",
        description="Your cart was reviewed. Choose a payment method below.",
        color=SUCCESS,
    )
    embed.add_field(
        name="Verified Final Total",
        value=format_cents(order.submitted_total_cents),
        inline=True,
    )
    embed.add_field(
        name="You Pay — 50%",
        value=f"**{format_cents(order.customer_price_cents)}**",
        inline=True,
    )
    embed.set_footer(text="Only use payment details shown inside this private ticket.")
    return embed


class MainPanelView(discord.ui.View):
    def __init__(self, bot: MovieOrdersBot, *, orders_open: bool = True) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.order_movie.disabled = not orders_open
        self.order_movie.emoji = "🟢" if orders_open else "🔴"
        self.order_movie.style = (
            discord.ButtonStyle.success if orders_open else discord.ButtonStyle.danger
        )
        self.order_movie.label = "Order Movie" if orders_open else "Movie Orders Closed"

    @discord.ui.button(
        label="Order Movie",
        emoji="🎟️",
        style=discord.ButtonStyle.success,
        custom_id="oxy_movies:v1:order",
    )
    async def order_movie(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not await self.bot.db.get_store_open(interaction.guild_id):
            await _ephemeral(interaction, "🔴 Movie orders are currently closed.")
            return
        existing = await self.bot.db.get_active_order_for_customer(
            interaction.guild_id, interaction.user.id
        )
        if existing:
            location = (
                f"<#{existing.channel_id}>" if existing.channel_id else "your pending order"
            )
            await _ephemeral(
                interaction, f"You already have an active movie order: {location}."
            )
            return
        await interaction.response.send_modal(MovieOrderModal(self.bot))

    @discord.ui.button(
        label="How It Works",
        emoji="🍿",
        style=discord.ButtonStyle.secondary,
        custom_id="oxy_movies:v1:how_it_works",
    )
    async def how_it_works(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await _ephemeral(interaction, embed=_how_it_works_embed())


class MovieOrderModal(discord.ui.Modal, title="🎬 Movie Ticket Form"):
    movie_showtime = discord.ui.TextInput(
        label="Movie name and showtime",
        placeholder="Example: Superman — 8:30 PM",
        min_length=3,
        max_length=200,
    )
    zip_code = discord.ui.TextInput(
        label="ZIP code",
        placeholder="Example: 89109",
        min_length=5,
        max_length=10,
    )
    seats = discord.ui.TextInput(
        label="How many seats?",
        placeholder="Example: 3",
        min_length=1,
        max_length=2,
    )
    snacks = discord.ui.TextInput(
        label="Any snacks? AMC only",
        placeholder="Large popcorn and 2 drinks, or None",
        style=discord.TextStyle.paragraph,
        min_length=2,
        max_length=500,
    )
    final_total = discord.ui.TextInput(
        label="Final checkout total ($40–$250)",
        placeholder="Example: $86.42 after taxes and fees",
        min_length=2,
        max_length=20,
    )

    def __init__(self, bot: MovieOrdersBot) -> None:
        super().__init__(timeout=600)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild is None:
            return
        if not await self.bot.db.get_store_open(interaction.guild.id):
            await _ephemeral(
                interaction,
                "Movie orders closed while you were completing the form. No ticket was created.",
            )
            return
        try:
            zip_code = validate_zip_code(str(self.zip_code))
            seats = parse_seats(str(self.seats))
            final_total_cents = parse_money(str(self.final_total))
        except InputError as exc:
            await _ephemeral(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            order = await self.bot.db.create_order(
                guild_id=interaction.guild.id,
                customer_id=interaction.user.id,
                movie_showtime=str(self.movie_showtime).strip(),
                zip_code=zip_code,
                seats=seats,
                snacks=str(self.snacks).strip(),
                submitted_total_cents=final_total_cents,
            )
        except ActiveOrderExistsError:
            await interaction.followup.send(
                "You already have an active movie order.", ephemeral=True
            )
            return

        category = interaction.guild.get_channel(settings.ticket_category_id)
        staff_role = interaction.guild.get_role(settings.staff_role_id)
        bot_member = interaction.guild.me
        if (
            not isinstance(category, discord.CategoryChannel)
            or staff_role is None
            or bot_member is None
        ):
            await self.bot.db.cancel_order_creation(order.id)
            await interaction.followup.send(
                "The ticket setup is missing a valid category or Movie Staff role. "
                "Ask an administrator to rerun `/setup`.",
                ephemeral=True,
            )
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            staff_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        channel: discord.TextChannel | None = None
        try:
            channel = await interaction.guild.create_text_channel(
                name=(
                    f"movie-{order.id:04d}-"
                    f"{_safe_channel_fragment(interaction.user.display_name)}"
                ),
                category=category,
                overwrites=overwrites,
                topic=f"Movie order #{order.id:06d} • Customer {interaction.user.id}",
                reason=f"Movie order #{order.id:06d}",
            )
            order = await self.bot.db.attach_channel(order.id, channel.id)
            await channel.send(
                content=f"{interaction.user.mention} {staff_role.mention} — new movie order",
                embed=_order_summary_embed(order),
                view=TicketControlsView(self.bot),
                allowed_mentions=discord.AllowedMentions(
                    users=[interaction.user],
                    roles=[staff_role],
                    everyone=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not create movie ticket for order %s", order.id)
            if channel is not None:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await channel.delete(reason="Movie ticket setup failed")
            await self.bot.db.cancel_order_creation(order.id)
            await interaction.followup.send(
                "I could not create the private ticket. Check the bot's channel permissions.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Your private movie order ticket is ready: {channel.mention}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Movie order modal failed", exc_info=error)
        await _ephemeral(interaction, "Something went wrong. Please try the form again.")


class TicketControlsView(discord.ui.View):
    def __init__(self, bot: MovieOrdersBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="oxy_movies:v1:close_ticket",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if interaction.user.id != order.customer_id and not _is_staff(
            interaction.user, settings
        ):
            await _ephemeral(
                interaction, "Only the customer or Movie Staff can close this ticket."
            )
            return
        await interaction.response.send_modal(CloseReasonModal(self.bot))


class CloseReasonModal(discord.ui.Modal, title="Close Movie Ticket"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Example: Tickets delivered successfully",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: MovieOrdersBot) -> None:
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = str(self.reason).strip() or "Closed from the ticket button"
        await _close_ticket(self.bot, interaction, reason=reason)


class InvoiceView(discord.ui.View):
    def __init__(self, bot: MovieOrdersBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Choose Payment Method",
        emoji="💳",
        style=discord.ButtonStyle.primary,
        custom_id="oxy_movies:v1:choose_payment",
    )
    async def choose_payment(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        if order is None or interaction.guild_id is None:
            return
        if interaction.user.id != order.customer_id:
            await _ephemeral(interaction, "Only the customer can choose a payment method.")
            return
        if order.assigned_staff_id is None:
            await _ephemeral(interaction, "Movie Staff must claim this order first.")
            return
        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, order.assigned_staff_id
        )
        if not methods:
            await _ephemeral(
                interaction,
                "The assigned staff member has not configured any payment methods yet.",
            )
            return
        await _ephemeral(
            interaction,
            "Choose how you want to pay:",
            view=PaymentMethodView(self.bot, interaction.user.id, methods),
        )


class PaymentMethodSelect(discord.ui.Select):
    def __init__(
        self,
        bot: MovieOrdersBot,
        owner_id: int,
        methods: list[PaymentMethod],
    ) -> None:
        self.bot = bot
        self.owner_id = owner_id
        self.methods = {str(method.id): method for method in methods}
        options = [
            discord.SelectOption(label=method.name[:100], value=str(method.id), emoji="💳")
            for method in methods[:25]
        ]
        super().__init__(
            placeholder="Select a payment method…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await _ephemeral(interaction, "This payment menu belongs to another customer.")
            return
        order = await _ticket_order(self.bot, interaction)
        if order is None or not isinstance(interaction.channel, discord.TextChannel):
            return
        method = self.methods[self.values[0]]
        if order.assigned_staff_id != method.staff_user_id:
            await _ephemeral(
                interaction, "This invoice is no longer current. Ask staff to resend it."
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        order = await self.bot.db.set_order_payment_method(
            order.id, method.name, actor_id=interaction.user.id
        )
        embed = discord.Embed(
            title=f"💳 Pay with {method.name}",
            description=(
                f"Send exactly **{format_cents(order.customer_price_cents)}** using the "
                "instructions below. Then select **I've Paid** and upload payment proof."
            ),
            color=SUCCESS,
        )
        embed.add_field(
            name="Payment Instructions",
            value=method.instructions[:1024],
            inline=False,
        )
        embed.set_footer(
            text="Never share passwords, bank logins, card numbers, security codes, or seed phrases."
        )
        await interaction.channel.send(
            content=f"<@{order.customer_id}>",
            embed=embed,
            view=PaymentSubmittedView(self.bot),
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False, replied_user=False
            ),
        )
        await interaction.followup.send(
            f"Selected **{method.name}**. The instructions were posted in the ticket.",
            ephemeral=True,
        )


class PaymentMethodView(discord.ui.View):
    def __init__(
        self,
        bot: MovieOrdersBot,
        owner_id: int,
        methods: list[PaymentMethod],
    ) -> None:
        super().__init__(timeout=300)
        self.add_item(PaymentMethodSelect(bot, owner_id, methods))


class PaymentSubmittedView(discord.ui.View):
    def __init__(self, bot: MovieOrdersBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="I've Paid",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="oxy_movies:v1:payment_submitted",
    )
    async def payment_submitted(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None or interaction.guild is None:
            return
        if interaction.user.id != order.customer_id:
            await _ephemeral(interaction, "Only the customer can submit payment.")
            return
        if not order.payment_method:
            await _ephemeral(interaction, "Choose a payment method first.")
            return
        if order.status in {"payment_submitted", "paid", "tickets_sent", "completed"}:
            await _ephemeral(interaction, "Your payment status was already submitted.")
            return

        await self.bot.db.set_order_status(
            order.id,
            "payment_submitted",
            actor_id=interaction.user.id,
            details={"payment_method": order.payment_method},
        )
        staff_role = interaction.guild.get_role(settings.staff_role_id)
        staff_mention = (
            f"<@{order.assigned_staff_id}>"
            if order.assigned_staff_id is not None
            else (staff_role.mention if staff_role is not None else "Movie Staff")
        )
        await interaction.response.send_message(
            f"✅ Payment submitted. {staff_mention}, please verify the proof before fulfilling.",
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=[staff_role] if staff_role is not None else False,
                everyone=False,
                replied_user=False,
            ),
        )


async def _collect_transcript_messages(
    channel: discord.TextChannel,
) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    async for message in channel.history(limit=None, oldest_first=True):
        content = message.content
        if not content and message.embeds:
            embed_parts: list[str] = []
            for embed in message.embeds:
                if embed.title:
                    embed_parts.append(embed.title)
                if embed.description:
                    embed_parts.append(embed.description)
                for field in embed.fields:
                    embed_parts.append(f"{field.name}: {field.value}")
            content = "\n".join(embed_parts)
        messages.append(
            TranscriptMessage(
                author_name=str(message.author),
                author_id=message.author.id,
                avatar_url=(
                    str(message.author.display_avatar.url)
                    if getattr(message.author, "display_avatar", None)
                    else None
                ),
                created_at=message.created_at,
                content=content,
                attachments=[
                    TranscriptAttachment(filename=item.filename, url=item.url)
                    for item in message.attachments
                ],
            )
        )
    return messages


async def _close_ticket(
    bot: MovieOrdersBot,
    interaction: discord.Interaction,
    *,
    reason: str,
    owner_share_percent: int | None = None,
) -> None:
    order = await _ticket_order(bot, interaction)
    settings = await _configured_settings(bot, interaction)
    if (
        order is None
        or settings is None
        or interaction.guild is None
        or not isinstance(interaction.channel, discord.TextChannel)
    ):
        return
    if interaction.user.id != order.customer_id and not _is_staff(interaction.user, settings):
        await _ephemeral(interaction, "Only the customer or Movie Staff can close this ticket.")
        return
    if owner_share_percent is not None and not _is_staff(interaction.user, settings):
        await _ephemeral(interaction, "Only Movie Staff can complete and record an order.")
        return
    if interaction.channel.id in bot.closing_channels:
        await _ephemeral(interaction, "This ticket is already closing.")
        return

    bot.closing_channels.add(interaction.channel.id)
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        messages = await _collect_transcript_messages(interaction.channel)
        archived_order = replace(order, status="completed") if owner_share_percent else order
        calculated_share_cents = (
            percentage_share_cents(order.customer_price_cents, owner_share_percent)
            if owner_share_percent is not None
            else None
        )
        transcript_html = render_transcript_html(
            guild_name=interaction.guild.name,
            channel_name=interaction.channel.name,
            order=archived_order,
            messages=messages,
        )
        transcript_path = save_transcript(
            bot.settings.transcript_dir / f"movie-order-{order.id:06d}.html",
            transcript_html,
        )
        log_channel = interaction.guild.get_channel(settings.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await interaction.followup.send(
                "The transcript channel is missing, so this ticket was left open.",
                ephemeral=True,
            )
            return

        log_embed = discord.Embed(
            title=f"🔒 Movie Order #{order.id:06d} Closed",
            description=f"**Reason:** {reason[:1000]}",
            color=ERROR,
        )
        log_embed.add_field(name="Customer", value=f"<@{order.customer_id}>", inline=True)
        log_embed.add_field(name="Movie", value=order.movie_showtime[:1024], inline=False)
        log_embed.add_field(name="Closed By", value=f"<@{interaction.user.id}>", inline=True)
        if owner_share_percent is not None:
            assert calculated_share_cents is not None
            log_embed.add_field(
                name="Customer Price — Share Base",
                value=format_cents(order.customer_price_cents),
                inline=True,
            )
            log_embed.add_field(
                name=f"Owner Share ({owner_share_percent}%)",
                value=f"**{format_cents(calculated_share_cents)}**",
                inline=True,
            )
        try:
            log_message = await log_channel.send(
                embed=log_embed,
                file=discord.File(transcript_path),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not upload transcript for movie order %s", order.id)
            await interaction.followup.send(
                "The transcript could not be uploaded, so this ticket was left open. "
                "Check the bot's transcript-channel permissions.",
                ephemeral=True,
            )
            return

        share_recorded = False
        try:
            if owner_share_percent is not None:
                (
                    _,
                    share_recorded,
                    recorded_share_cents,
                ) = await bot.db.close_order_with_owner_share(
                    order.id,
                    guild_id=interaction.guild.id,
                    share_percent=owner_share_percent,
                    actor_id=interaction.user.id,
                    owed_by_staff_id=order.assigned_staff_id or interaction.user.id,
                    reason=reason,
                )
                assert calculated_share_cents == recorded_share_cents
                log_embed.set_footer(
                    text=(
                        "Owner share recorded automatically by /done."
                        if share_recorded
                        else "Owner share was already recorded for this order."
                    )
                )
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await log_message.edit(embed=log_embed)
            else:
                await bot.db.set_order_status(
                    order.id,
                    "closed",
                    actor_id=interaction.user.id,
                    details={"reason": reason},
                )
        except Exception:
            LOGGER.exception("Could not finalize movie order %s", order.id)
            await interaction.followup.send(
                "The transcript was uploaded, but the order calculation could not be "
                "saved. The ticket was left open; please run `/done` again.",
                ephemeral=True,
            )
            return
        customer = interaction.guild.get_member(order.customer_id)
        if customer is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await customer.send(
                    content=f"Your movie order #{order.id:06d} was closed.",
                    file=discord.File(transcript_path),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        confirmation = "Transcript saved."
        if owner_share_percent is not None and calculated_share_cents is not None:
            confirmation += (
                f" Your {owner_share_percent}% share is "
                f"**{format_cents(calculated_share_cents)}** and was recorded automatically."
                if share_recorded
                else " This order's owner share was already recorded, so it was not counted twice."
            )
        confirmation += " This ticket will close in 5 seconds."
        await interaction.followup.send(confirmation, ephemeral=True)
        await interaction.channel.send(
            "🔒 Transcript saved. Closing this ticket in 5 seconds.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.sleep(5)
        await interaction.channel.delete(
            reason=f"Movie order #{order.id:06d} archived by {interaction.user}"
        )
    finally:
        bot.closing_channels.discard(interaction.channel.id)


def _scheduled_close_datetime(value: str) -> datetime:
    close_at = datetime.fromisoformat(value)
    return close_at.replace(tzinfo=UTC) if close_at.tzinfo is None else close_at


async def _archive_completed_movie_order(bot: MovieOrdersBot, order_id: int) -> bool:
    order = await bot.db.get_order(order_id)
    if order is None or order.status in {"closed", "cancelled"}:
        return True
    if order.status != "completed" or order.channel_id is None:
        return True

    guild = bot.get_guild(order.guild_id)
    if guild is None:
        return False
    channel = guild.get_channel(order.channel_id)
    if not isinstance(channel, discord.TextChannel):
        await bot.db.set_order_status(
            order.id,
            "closed",
            actor_id=order.completed_by,
            details={"reason": "Scheduled ticket channel no longer exists"},
        )
        return True
    if channel.id in bot.closing_channels:
        return False

    settings = await bot.db.get_guild_settings(order.guild_id)
    if settings is None:
        return False
    log_channel = guild.get_channel(settings.log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        return False

    bot.closing_channels.add(channel.id)
    try:
        messages = await _collect_transcript_messages(channel)
        transcript_html = render_transcript_html(
            guild_name=guild.name,
            channel_name=channel.name,
            order=replace(order, status="completed"),
            messages=messages,
        )
        transcript_path = save_transcript(
            bot.settings.transcript_dir / f"movie-order-{order.id:06d}.html",
            transcript_html,
        )
        share = await bot.db.get_owner_share_for_order(order.id)
        log_embed = discord.Embed(
            title=f"🎬 Movie Order #{order.id:06d} Auto-Closed",
            description="The customer access window ended.",
            color=discord.Color.dark_grey(),
        )
        log_embed.add_field(name="Customer", value=f"<@{order.customer_id}>", inline=True)
        log_embed.add_field(name="Movie", value=order.movie_showtime[:1024], inline=False)
        log_embed.add_field(
            name="Verified Order Total",
            value=format_cents(order.submitted_total_cents),
            inline=True,
        )
        log_embed.add_field(
            name="Customer Price — Share Base",
            value=format_cents(order.customer_price_cents),
            inline=True,
        )
        if share is not None:
            share_percent, share_cents = share
            log_embed.add_field(
                name=f"Owner Share ({share_percent}%)",
                value=f"**{format_cents(share_cents)}**",
                inline=True,
            )
        log_embed.set_footer(text="Transcript captured after the customer access window.")
        try:
            await log_channel.send(
                embed=log_embed,
                file=discord.File(transcript_path),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not archive scheduled movie order %s", order.id)
            return False

        customer = guild.get_member(order.customer_id)
        if customer is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await customer.send(
                    content=f"Your movie order #{order.id:06d} was closed.",
                    file=discord.File(transcript_path),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await channel.send(
                "🔒 The customer access window has ended. Transcript saved; closing "
                "this ticket in 5 seconds.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Movie order #{order.id:06d} access window ended")
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not delete scheduled movie ticket %s", channel.id)
            return False
        await bot.db.set_order_status(
            order.id,
            "closed",
            actor_id=order.completed_by,
            details={"reason": "Completed-ticket customer access window ended"},
        )
        return True
    finally:
        bot.closing_channels.discard(channel.id)


async def _scheduled_close_worker(
    bot: MovieOrdersBot, order_id: int, scheduled_close_at: str
) -> None:
    close_at = _scheduled_close_datetime(scheduled_close_at)
    delay = max(0.0, (close_at - datetime.now(UTC)).total_seconds())
    await asyncio.sleep(delay)
    while True:
        try:
            if await _archive_completed_movie_order(bot, order_id):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Scheduled close failed for movie order %s", order_id)
        await asyncio.sleep(SCHEDULED_CLOSE_RETRY_SECONDS)


async def _refresh_saved_panel(
    bot: MovieOrdersBot, guild: discord.Guild, settings: GuildSettings
) -> bool:
    if settings.panel_channel_id is None or settings.panel_message_id is None:
        return False
    channel = guild.get_channel(settings.panel_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        message = await channel.fetch_message(settings.panel_message_id)
        orders_open = await bot.db.get_store_open(guild.id)
        await message.edit(
            embed=_panel_embed(settings, orders_open),
            view=MainPanelView(bot, orders_open=orders_open),
            attachments=[],
        )
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


async def _announce_store_open(
    bot: MovieOrdersBot, guild: discord.Guild, settings: GuildSettings
) -> str:
    if settings.notification_role_id is None:
        return "No customer notification role is configured. Rerun `/setup` to add one."
    role = guild.get_role(settings.notification_role_id)
    if role is None or role.is_default():
        return "The configured customer notification role is missing or invalid."
    channel = (
        guild.get_channel(settings.panel_channel_id)
        if settings.panel_channel_id is not None
        else None
    )
    if not isinstance(channel, discord.TextChannel):
        return "The saved movie storefront channel is missing, so customers were not pinged."
    bot_member = guild.me
    if bot_member is None:
        return "The bot member could not be found, so customers were not pinged."
    permissions = channel.permissions_for(bot_member)
    if not permissions.send_messages:
        return f"I cannot send the opening notification in {channel.mention}."
    if not role.mentionable and not permissions.mention_everyone:
        return (
            f"{role.mention} is not mentionable. Make it mentionable or give the bot "
            "**Mention @everyone, @here, and All Roles** permission."
        )
    try:
        await channel.send(
            content=(
                f"{role.mention} 🟢 **MOVIE ORDERS ARE OPEN!**\n"
                "Get **50% off movie tickets**. Final checkout totals must be "
                "**$40–$250**. Use the **Order Movie** button above to start."
            ),
            allowed_mentions=discord.AllowedMentions(
                users=False,
                roles=[role],
                everyone=False,
                replied_user=False,
            ),
        )
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.exception("Could not send movie storefront opening ping in guild %s", guild.id)
        return f"The store opened, but I could not ping customers in {channel.mention}."
    return f"Customer notification sent to {role.mention} in {channel.mention}."


class MovieCommands(commands.Cog):
    store = app_commands.Group(name="store", description="Open or close movie orders")
    payments = app_commands.Group(
        name="payments", description="Configure Movie Staff payment methods"
    )

    def __init__(self, bot: MovieOrdersBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Configure the movie order system")
    @app_commands.guild_only()
    @app_commands.describe(
        panel_channel="Where customers will see the movie order panel",
        ticket_category="Category where private movie tickets are created",
        movie_staff_role="Role allowed to view and handle movie orders",
        transcript_channel="Private channel where closed-ticket transcripts are saved",
        notification_role="Customer role pinged when movie orders open",
        brand_name="Storefront title",
        banner_url="Optional HTTPS image URL for the movie storefront",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        panel_channel: discord.TextChannel,
        ticket_category: discord.CategoryChannel,
        transcript_channel: discord.TextChannel,
        movie_staff_role: discord.Role | None = None,
        notification_role: discord.Role | None = None,
        brand_name: app_commands.Range[str, 2, 80] = "Oxy Movies • 50% Off",
        banner_url: str | None = None,
    ) -> None:
        if not _is_administrator(interaction.user) or interaction.guild is None:
            await _ephemeral(interaction, "Only a server administrator can run setup.")
            return

        resolved_staff_role = movie_staff_role
        if resolved_staff_role is None and self.bot.settings.movie_staff_role_id:
            resolved_staff_role = interaction.guild.get_role(
                self.bot.settings.movie_staff_role_id
            )
        resolved_notification_role = notification_role
        if (
            resolved_notification_role is None
            and self.bot.settings.customer_notification_role_id
        ):
            resolved_notification_role = interaction.guild.get_role(
                self.bot.settings.customer_notification_role_id
            )

        if resolved_staff_role is None:
            await _ephemeral(
                interaction,
                "I could not find the configured **Direct Movies Staff** role. "
                "Choose it manually in `/setup`.",
            )
            return
        if resolved_staff_role.is_default():
            await _ephemeral(interaction, "Choose the private Movie Staff role, not @everyone.")
            return
        if resolved_notification_role is not None and resolved_notification_role.is_default():
            await _ephemeral(interaction, "Choose a customer notification role, not @everyone.")
            return
        if banner_url and not _valid_http_url(banner_url):
            await _ephemeral(interaction, "The banner must be a valid HTTP or HTTPS image URL.")
            return

        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await _ephemeral(
                interaction,
                "Give the movie bot **Manage Channels** permission before running setup.",
            )
            return
        if not panel_channel.permissions_for(bot_member).send_messages:
            await _ephemeral(interaction, "I cannot send messages in the storefront channel.")
            return
        if not transcript_channel.permissions_for(bot_member).attach_files:
            await _ephemeral(
                interaction,
                "I need **Attach Files** permission in the transcript channel.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.db.upsert_guild_settings(
            guild_id=interaction.guild.id,
            brand_name=brand_name.strip(),
            ticket_category_id=ticket_category.id,
            staff_role_id=resolved_staff_role.id,
            notification_role_id=(
                resolved_notification_role.id
                if resolved_notification_role is not None
                else None
            ),
            log_channel_id=transcript_channel.id,
            banner_url=banner_url.strip() if banner_url else None,
        )
        settings = await self.bot.db.get_guild_settings(interaction.guild.id)
        assert settings is not None
        orders_open = await self.bot.db.get_store_open(interaction.guild.id)
        panel_message = await panel_channel.send(
            embed=_panel_embed(settings, orders_open),
            view=MainPanelView(self.bot, orders_open=orders_open),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.db.save_panel(interaction.guild.id, panel_channel.id, panel_message.id)
        notification_text = (
            f" Opening alerts will ping {resolved_notification_role.mention}."
            if resolved_notification_role is not None
            else " No customer notification role was selected."
        )
        await interaction.followup.send(
            (
                f"Movie storefront posted in {panel_channel.mention}. Private tickets will "
                f"open under **{ticket_category.name}** for {resolved_staff_role.mention}."
                f"{notification_text}"
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="panel", description="Refresh or repost the movie storefront")
    @app_commands.guild_only()
    @app_commands.describe(channel="Optional new channel for the movie storefront")
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild is None:
            return
        if not _is_administrator(interaction.user):
            await _ephemeral(interaction, "Only an administrator can manage the panel.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is None and await _refresh_saved_panel(
            self.bot, interaction.guild, settings
        ):
            await interaction.followup.send("Movie storefront refreshed.", ephemeral=True)
            return
        target = channel or (
            interaction.channel
            if isinstance(interaction.channel, discord.TextChannel)
            else None
        )
        if target is None:
            await interaction.followup.send(
                "Choose a text channel for the movie storefront.", ephemeral=True
            )
            return
        orders_open = await self.bot.db.get_store_open(interaction.guild.id)
        message = await target.send(
            embed=_panel_embed(settings, orders_open),
            view=MainPanelView(self.bot, orders_open=orders_open),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.db.save_panel(interaction.guild.id, target.id, message.id)
        await interaction.followup.send(
            f"Movie storefront posted in {target.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_store_status(
        self, interaction: discord.Interaction, *, orders_open: bool
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can open or close orders.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        was_open = await self.bot.db.get_store_open(interaction.guild.id)
        await self.bot.db.set_store_open(interaction.guild.id, orders_open)
        panel_updated = await _refresh_saved_panel(self.bot, interaction.guild, settings)
        status_message = (
            "🟢 Movie orders are now **OPEN**."
            if orders_open
            else "🔴 Movie orders are now **CLOSED**. Existing tickets remain open."
        )
        if not panel_updated:
            status_message += " Run `/panel` to repost the storefront."
        if orders_open and not was_open:
            notice = await _announce_store_open(self.bot, interaction.guild, settings)
            status_message += f"\n{notice}"
        elif orders_open:
            status_message += "\nThe store was already open, so no new ping was sent."
        await interaction.followup.send(status_message, ephemeral=True)

    @store.command(name="open", description="Open movie orders and notify customers once")
    @app_commands.guild_only()
    async def store_open(self, interaction: discord.Interaction) -> None:
        await self._set_store_status(interaction, orders_open=True)

    @store.command(name="close", description="Close new movie orders without pinging")
    @app_commands.guild_only()
    async def store_close(self, interaction: discord.Interaction) -> None:
        await self._set_store_status(interaction, orders_open=False)

    @payments.command(name="set", description="Add or update one of your payment methods")
    @app_commands.guild_only()
    @app_commands.describe(
        method="Payment method name",
        instructions="Customer-facing payment tag, address, or payment link",
    )
    async def payments_set(
        self,
        interaction: discord.Interaction,
        method: app_commands.Range[str, 2, 50],
        instructions: app_commands.Range[str, 2, 1000],
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can configure payments.")
            return
        cleaned_method = method.strip()
        existing = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        existing_names = {item.name.casefold() for item in existing}
        if len(existing) >= 10 and cleaned_method.casefold() not in existing_names:
            await _ephemeral(interaction, "You can configure up to 10 payment methods.")
            return
        await self.bot.db.upsert_payment_method(
            guild_id=interaction.guild_id,
            staff_user_id=interaction.user.id,
            name=cleaned_method,
            instructions=instructions.strip(),
        )
        await _ephemeral(interaction, f"Saved your **{cleaned_method}** instructions.")

    @payments_set.autocomplete("method")
    async def payment_method_autocomplete(
        self, _: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current_folded = current.casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in KNOWN_PAYMENT_METHODS
            if current_folded in name.casefold()
        ][:25]

    @payments.command(name="list", description="Show your saved payment methods")
    @app_commands.guild_only()
    async def payments_list(self, interaction: discord.Interaction) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can view payment settings.")
            return
        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        if not methods:
            await _ephemeral(interaction, "You have no payment methods configured.")
            return
        embed = discord.Embed(title="Your Movie Payment Methods", color=PURPLE)
        for method in methods:
            embed.add_field(
                name=method.name,
                value=method.instructions[:1024],
                inline=False,
            )
        await _ephemeral(interaction, embed=embed)

    @payments.command(name="remove", description="Remove one of your payment methods")
    @app_commands.guild_only()
    @app_commands.describe(method="Exact payment method name")
    async def payments_remove(
        self,
        interaction: discord.Interaction,
        method: app_commands.Range[str, 2, 50],
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can configure payments.")
            return
        removed = await self.bot.db.remove_payment_method(
            interaction.guild_id, interaction.user.id, method.strip()
        )
        await _ephemeral(
            interaction,
            "Payment method removed." if removed else "That payment method was not found.",
        )

    @app_commands.command(name="claim", description="Assign this movie order to yourself")
    @app_commands.guild_only()
    async def claim(self, interaction: discord.Interaction) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can claim orders.")
            return
        if order.assigned_staff_id and order.assigned_staff_id != interaction.user.id:
            await _ephemeral(
                interaction,
                f"This order is already claimed by <@{order.assigned_staff_id}>.",
            )
            return
        if order.assigned_staff_id == interaction.user.id:
            await _ephemeral(interaction, "You already claimed this movie order.")
            return
        await self.bot.db.assign_order(order.id, interaction.user.id)
        await interaction.response.send_message(
            f"🎬 Movie order claimed by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions(
                users=[interaction.user], roles=False, everyone=False, replied_user=False
            ),
        )

    @app_commands.command(name="pay", description="Verify the total and send the 50% invoice")
    @app_commands.guild_only()
    @app_commands.describe(final_total="Optional corrected checkout total after taxes and fees")
    async def pay(
        self,
        interaction: discord.Interaction,
        final_total: str | None = None,
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can send invoices.")
            return
        if order.status in {"closed", "cancelled"}:
            await _ephemeral(interaction, "This movie order is already closed.")
            return
        if order.assigned_staff_id and order.assigned_staff_id != interaction.user.id:
            await _ephemeral(
                interaction,
                f"This order is assigned to <@{order.assigned_staff_id}>.",
            )
            return
        if order.assigned_staff_id is None:
            order = await self.bot.db.assign_order(order.id, interaction.user.id)

        if final_total is not None:
            try:
                corrected_cents = parse_money(final_total)
            except InputError as exc:
                await _ephemeral(interaction, str(exc))
                return
            order = await self.bot.db.update_order_total(
                order.id, corrected_cents, actor_id=interaction.user.id
            )

        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        if not methods:
            await _ephemeral(
                interaction,
                "Configure at least one method with `/payments set` before sending an invoice.",
            )
            return

        await self.bot.db.set_order_status(
            order.id,
            "invoice_sent",
            actor_id=interaction.user.id,
            details={
                "submitted_total_cents": order.submitted_total_cents,
                "customer_price_cents": order.customer_price_cents,
            },
        )
        await interaction.response.send_message(
            content=f"<@{order.customer_id}>",
            embed=_invoice_embed(order),
            view=InvoiceView(self.bot),
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False, replied_user=False
            ),
        )

    @app_commands.command(name="paid", description="Confirm that customer payment was verified")
    @app_commands.guild_only()
    async def paid(self, interaction: discord.Interaction) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can confirm payments.")
            return
        if order.assigned_staff_id not in {None, interaction.user.id} and not _is_administrator(
            interaction.user
        ):
            await _ephemeral(
                interaction,
                f"This order is assigned to <@{order.assigned_staff_id}>.",
            )
            return
        if order.assigned_staff_id is None:
            order = await self.bot.db.assign_order(order.id, interaction.user.id)
        order = await self.bot.db.set_order_status(
            order.id, "paid", actor_id=interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Payment verified for movie order #{order.id:06d}. Movie Staff is processing it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="tickets_sent", description="Mark the movie QR tickets as delivered"
    )
    @app_commands.guild_only()
    @app_commands.describe(details="Delivery or AMC snack pickup details")
    async def tickets_sent(
        self,
        interaction: discord.Interaction,
        details: app_commands.Range[str, 2, 1000],
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can mark tickets delivered.")
            return
        if order.assigned_staff_id not in {None, interaction.user.id}:
            await _ephemeral(
                interaction,
                f"This order is assigned to <@{order.assigned_staff_id}>.",
            )
            return
        if order.assigned_staff_id is None:
            order = await self.bot.db.assign_order(order.id, interaction.user.id)
        order = await self.bot.db.set_order_status(
            order.id,
            "tickets_sent",
            actor_id=interaction.user.id,
            details={"details": details},
        )
        embed = discord.Embed(
            title="🎟️ Your Movie Tickets Are Ready",
            description=details,
            color=SUCCESS,
        )
        embed.set_footer(
            text=(
                "Confirm the movie, theater, date, showtime, and seats. Movie Staff: "
                "run /done after delivery to record the owner's share and start the "
                "one-hour access window."
            )
        )
        await interaction.response.send_message(
            content=f"<@{order.customer_id}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False, replied_user=False
            ),
        )

    async def _finish_successful_order(
        self,
        interaction: discord.Interaction,
        *,
        retention: timedelta,
        legacy_total: str | None = None,
        customer: discord.Member | None = None,
    ) -> None:
        order = (
            await self.bot.db.get_order_by_channel(interaction.channel_id)
            if interaction.channel_id is not None
            else None
        )
        settings = await _configured_settings(self.bot, interaction)
        if (
            settings is None
            or interaction.guild is None
            or not isinstance(interaction.channel, discord.TextChannel)
        ):
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only Movie Staff can complete orders.")
            return
        if order is None:
            if not interaction.channel.name.startswith("movie-"):
                await _ephemeral(interaction, "Use this inside a movie order ticket.")
                return
            if legacy_total is None:
                await _ephemeral(
                    interaction,
                    "This is an older movie ticket. Run `/complete legacy_total:42 "
                    "customer:@Customer` to reconnect it, record 12%, and schedule its close.",
                )
                return
            if customer is None and interaction.channel.topic:
                topic_customer = re.search(
                    r"\bCustomer\s+(\d{15,22})\b", interaction.channel.topic
                )
                if topic_customer:
                    customer = interaction.guild.get_member(int(topic_customer.group(1)))
            if customer is None:
                await _ephemeral(
                    interaction,
                    "I could not identify the customer from this older channel. Run "
                    "`/complete legacy_total:42 customer:@Customer`.",
                )
                return
            if customer.bot:
                await _ephemeral(interaction, "The selected customer cannot be a bot.")
                return
            try:
                total_cents = parse_money(legacy_total)
            except InputError as exc:
                await _ephemeral(interaction, str(exc))
                return
            try:
                order = await self.bot.db.import_legacy_ticket(
                    guild_id=interaction.guild.id,
                    customer_id=customer.id,
                    channel_id=interaction.channel.id,
                    movie_showtime=f"Recovered legacy ticket: {interaction.channel.name}",
                    submitted_total_cents=total_cents,
                    assigned_staff_id=interaction.user.id,
                )
            except ActiveOrderExistsError:
                await _ephemeral(
                    interaction,
                    "That customer already has another active movie order. Close or complete "
                    "that order first, then retry this recovery.",
                )
                return
        if order.assigned_staff_id not in {None, interaction.user.id}:
            await _ephemeral(
                interaction,
                f"This order is assigned to <@{order.assigned_staff_id}>.",
            )
            return
        if order.status in {"closed", "cancelled"}:
            await _ephemeral(interaction, "This movie order is already closed.")
            return
        if order.status == "completed":
            close_text = (
                f" It will close <t:{int(_scheduled_close_datetime(order.scheduled_close_at).timestamp())}:R>."
                if order.scheduled_close_at
                else ""
            )
            await _ephemeral(
                interaction,
                "This movie order is already completed and its 12% share was already "
                f"recorded.{close_text}",
            )
            return
        if order.status not in {"paid", "tickets_sent"}:
            await _ephemeral(
                interaction,
                "Confirm the customer's payment with `/paid` before completing the order.",
            )
            return
        if order.assigned_staff_id is None:
            order = await self.bot.db.assign_order(order.id, interaction.user.id)

        await interaction.response.defer(ephemeral=True, thinking=True)
        requested_close_at = discord.utils.utcnow() + retention
        order, share_recorded, share_cents = await self.bot.db.complete_order_with_owner_share(
            order.id,
            guild_id=interaction.guild.id,
            share_percent=self.bot.settings.owner_share_percent,
            actor_id=interaction.user.id,
            owed_by_staff_id=order.assigned_staff_id or interaction.user.id,
            scheduled_close_at=requested_close_at.isoformat(),
        )
        assert order.scheduled_close_at is not None
        close_at = _scheduled_close_datetime(order.scheduled_close_at)
        close_timestamp = int(close_at.timestamp())
        self.bot.schedule_completed_order(order.id, order.scheduled_close_at)

        closes_immediately = retention <= COMPLETE_TICKET_RETENTION
        window_title = "Closing Now" if closes_immediately else "One-Hour Access Window"
        window_description = (
            "Your order is complete. The final transcript is being saved and this "
            "ticket will close now."
            if closes_immediately
            else (
                "Your order is complete. This ticket will remain available for "
                "**one hour** so you can save your ticket links, QR codes, and pickup "
                "information."
            )
        )

        await interaction.channel.send(
            content=f"<@{order.customer_id}>",
            embed=discord.Embed(
                title=f"✅ Movie Order Completed — {window_title}",
                description=(
                    f"{window_description}\n\n"
                    f"**Automatic close:** <t:{close_timestamp}:F> "
                    f"(<t:{close_timestamp}:R>)"
                ),
                color=SUCCESS,
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        share_status = "recorded" if share_recorded else "was already recorded"
        await interaction.followup.send(
            (
                "Completed. Customer price (right-side total): "
                f"**{format_cents(order.customer_price_cents)}**. "
                f"Your {self.bot.settings.owner_share_percent}% share is "
                f"**{format_cents(share_cents)}** and {share_status}. The ticket will "
                f"close <t:{close_timestamp}:R>. Owed by Movie Staff: "
                f"<@{order.assigned_staff_id or interaction.user.id}>."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="done",
        description="Record 12% and keep the completed ticket open for one hour",
    )
    @app_commands.guild_only()
    async def done(self, interaction: discord.Interaction) -> None:
        await self._finish_successful_order(
            interaction,
            retention=DONE_TICKET_RETENTION,
        )

    @app_commands.command(
        name="complete",
        description="Record the owner's 12% share and close the ticket immediately",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        legacy_total="Older unrecognized ticket only: verified total after taxes and fees",
        customer="Older ticket only: customer if the channel topic does not identify them",
    )
    async def complete(
        self,
        interaction: discord.Interaction,
        legacy_total: str | None = None,
        customer: discord.Member | None = None,
    ) -> None:
        await self._finish_successful_order(
            interaction,
            retention=COMPLETE_TICKET_RETENTION,
            legacy_total=legacy_total,
            customer=customer,
        )

    @app_commands.command(name="close", description="Save the transcript and close this ticket")
    @app_commands.guild_only()
    @app_commands.describe(reason="Why the movie ticket is being closed")
    async def close(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 2, 500] = "Movie order finished",
    ) -> None:
        await _close_ticket(self.bot, interaction, reason=reason)

    @app_commands.command(
        name="earnings",
        description="Admin: view the movie-order owner share currently owed",
    )
    @app_commands.guild_only()
    async def earnings(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not _is_administrator(interaction.user):
            await _ephemeral(
                interaction,
                "Only a server administrator can view owner-share totals.",
            )
            return

        summary = await self.bot.db.get_owner_share_summary(interaction.guild_id)
        staff_summaries = await self.bot.db.get_owner_share_summary_by_staff(
            interaction.guild_id
        )
        embed = discord.Embed(
            title="🎬 Movie Order Owner Share",
            description=(
                f"**{format_cents(summary.owed_cents)}** is currently owed across "
                f"**{summary.owed_order_count}** completed movie order(s)."
            ),
            color=SUCCESS,
        )
        embed.add_field(
            name="Automatic Rate",
            value=(
                f"{self.bot.settings.owner_share_percent}% of the customer's 50%-off "
                "price (the right-side total)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Lifetime Customer Revenue",
            value=(
                f"{format_cents(summary.lifetime_revenue_cents)} across "
                f"{summary.lifetime_order_count} order(s)"
            ),
            inline=True,
        )
        embed.add_field(
            name="Lifetime Owner Profit / Share",
            value=(
                f"{format_cents(summary.lifetime_cents)} across "
                f"{summary.lifetime_order_count} order(s)"
            ),
            inline=True,
        )
        if staff_summaries:
            embed.add_field(
                name="Who Owes It",
                value="\n".join(
                    f"<@{item.staff_user_id}> — **{format_cents(item.owed_cents)} owed** "
                    f"from {format_cents(item.owed_revenue_cents)} in customer revenue "
                    f"({item.owed_order_count} order(s)); "
                    f"{format_cents(item.lifetime_cents)} lifetime owner share"
                    for item in staff_summaries
                )[:1024],
                inline=False,
            )
        embed.set_footer(text="Only /done or /complete records earnings. /close does not.")
        await _ephemeral(interaction, embed=embed)

    @app_commands.command(name="order_info", description="Show the current movie order details")
    @app_commands.guild_only()
    async def order_info(self, interaction: discord.Interaction) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if interaction.user.id != order.customer_id and not _is_staff(
            interaction.user, settings
        ):
            await _ephemeral(interaction, "You cannot view this movie order.")
            return
        embed = _order_summary_embed(order)
        embed.add_field(
            name="Status",
            value=order.status.replace("_", " ").title(),
            inline=True,
        )
        embed.add_field(
            name="Assigned Staff",
            value=(
                f"<@{order.assigned_staff_id}>"
                if order.assigned_staff_id is not None
                else "Not claimed"
            ),
            inline=True,
        )
        await _ephemeral(interaction, embed=embed)


class MovieOrdersBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.closing_channels: set[int] = set()
        self.scheduled_close_tasks: dict[int, asyncio.Task[None]] = {}
        self._scheduled_closures_restored = False
        self._avatar_sync_attempted = False

    def schedule_completed_order(self, order_id: int, scheduled_close_at: str) -> None:
        existing = self.scheduled_close_tasks.get(order_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            _scheduled_close_worker(self, order_id, scheduled_close_at),
            name=f"movie-order-{order_id}-scheduled-close",
        )
        self.scheduled_close_tasks[order_id] = task

        def remove_finished(finished: asyncio.Task[None]) -> None:
            if self.scheduled_close_tasks.get(order_id) is finished:
                self.scheduled_close_tasks.pop(order_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                LOGGER.error(
                    "Scheduled movie close task %s failed",
                    order_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(remove_finished)

    async def _restore_scheduled_closures(self) -> None:
        if self._scheduled_closures_restored:
            return
        pending = await self.db.get_pending_scheduled_closures()
        self._scheduled_closures_restored = True
        for order in pending:
            if order.scheduled_close_at is not None:
                self.schedule_completed_order(order.id, order.scheduled_close_at)
        if pending:
            LOGGER.info("Restored %s scheduled movie ticket close(s)", len(pending))

    async def _sync_brand_avatar(self) -> None:
        if self._avatar_sync_attempted or self.user is None:
            return
        self._avatar_sync_attempted = True

        try:
            avatar_bytes = await asyncio.to_thread(BRAND_AVATAR_PATH.read_bytes)
        except OSError:
            LOGGER.exception("Could not read the bundled Bob's Burgers movie avatar")
            return

        avatar_digest = hashlib.sha256(avatar_bytes).hexdigest()
        applied_digest = await self.db.get_application_state(BRAND_AVATAR_STATE_KEY)
        if applied_digest == avatar_digest:
            LOGGER.info("Bob's Burgers Direct Movies avatar is already applied")
            return

        try:
            await self.user.edit(avatar=avatar_bytes)
        except (discord.HTTPException, ValueError):
            LOGGER.exception("Discord rejected the bundled bot avatar update")
            return

        await self.db.set_application_state(BRAND_AVATAR_STATE_KEY, avatar_digest)
        LOGGER.info("Applied the Bob's Burgers Direct Movies bot avatar")

    async def setup_hook(self) -> None:
        await self.db.initialize()
        await self.add_cog(MovieCommands(self))
        self.add_view(MainPanelView(self))
        self.add_view(TicketControlsView(self))
        self.add_view(InvoiceView(self))
        self.add_view(PaymentSubmittedView(self))
        self.tree.on_error = self.on_tree_error

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info(
                "Synced %s command(s) to development guild %s",
                len(synced),
                self.settings.dev_guild_id,
            )
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %s global command(s)", len(synced))

    async def on_ready(self) -> None:
        if self.user is not None:
            await self._sync_brand_avatar()
            await self._restore_scheduled_closures()
            LOGGER.info(
                "Ready as %s (%s) in %s guild(s)",
                self.user,
                self.user.id,
                len(self.guilds),
            )

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        LOGGER.exception("Application command failed", exc_info=error)
        with contextlib.suppress(discord.HTTPException):
            await _ephemeral(
                interaction,
                "The command could not be completed. Please try again or check Railway logs.",
            )


def run() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    bot = MovieOrdersBot(settings)
    bot.run(settings.token, log_handler=None)
